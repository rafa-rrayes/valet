"""The real thing: the agent-base image with each harness in a container, driven through the runner's Supervisor.
Opt-in (VALET_TEST_DOCKER=1) and needs `docker build -t valet/agent-base docker/agent-base` first. Prompting a
model needs that harness's key on the runner: CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`) or
ANTHROPIC_API_KEY for Claude, DEEPSEEK_API_KEY for DeepSeek Harness. Without one, the keyless tests still check
the image, ACP startup and DeepSeek Harness's session resume. The permission-level tests run each harness at each
level (DESIGN.md, Permissions) against a real model: keep their prompts tiny, they spend real credit."""
import asyncio
import json
import os
import uuid

import pytest

from tests.test_runner_agent import Sink
from valet.runner.harness import claude, deepseek
from valet.runner.supervisor import Supervisor
from valet.shared.config import PASSTHROUGH_ENV, RunnerConfig
from valet.shared.protocol import AgentStart, AgentStop, PermissionAnswer, SessionNew, SessionPrompt

pytestmark = pytest.mark.docker
NAME = "valet-test-" + uuid.uuid4().hex[:6]
PERSONA = "You are Probe. End every reply with the line: -- Probe"


def has_key(harness) -> bool:
    return any(os.environ.get(k) for k in harness.KEYS)


class Agent:
    """One container agent behind a Supervisor; start/stop can repeat, the volumes stay until close()."""

    def __init__(self, tmp_path, harness: str, permissions: str = "ask") -> None:
        cfg = RunnerConfig(control_url="ws://127.0.0.1:1/ws/runner", token="vrun_test", data=tmp_path,
                           image=os.environ.get("VALET_AGENT_IMAGE", "valet/agent-base"),
                           passthrough={k: os.environ[k] for k in PASSTHROUGH_ENV if os.environ.get(k)})
        self.definition = {"name": NAME, "harness": harness, "computer": {"level": "container"},
                           "permissions": permissions}
        self.secrets: dict[str, str] = {}
        self.sink = Sink()
        self.sup = Supervisor(cfg, self.sink)
        self._ids = iter(range(1, 10_000))

    def id(self) -> str:
        return str(next(self._ids))

    async def start(self) -> dict:
        before = len(self.sink.of("agent.status"))
        await self.sup.handle(AgentStart(id=self.id(), agent=NAME, definition=self.definition,
                                         files={"persona.md": PERSONA}, secrets=self.secrets))
        async with asyncio.timeout(120):
            while True:
                done = [m for m in self.sink.of("agent.status")[before:] if m["state"] in ("running", "failed")]
                if done:
                    return done[0]
                await asyncio.sleep(0.1)

    async def stop(self) -> None:
        await self.sup.handle(AgentStop(id=self.id(), agent=NAME))

    async def new_session(self) -> str:
        return (await self.sup.handle(SessionNew(id=self.id(), agent=NAME)))["session_id"]

    async def turn(self, sid: str, text: str, timeout: float = 180, answer: str | None = None) -> tuple[dict, str]:
        """Prompt and wait for turn.end -> (turn.end, the reply's text). `answer` ("allow_once", "reject_once")
        answers every permission request of the turn with the option of that kind."""
        tid = self.last_turn = "t" + uuid.uuid4().hex[:6]
        await self.sup.handle(SessionPrompt(id=self.id(), agent=NAME, session_id=sid, turn_id=tid,
                                            prompt=[{"type": "text", "text": text}]))
        answered = set()
        async with asyncio.timeout(timeout):
            while not (ends := self.sink.of("turn.end", turn_id=tid)):
                for req in self.sink.of("permission.request", turn_id=tid):
                    if answer and req["request_id"] not in answered:
                        answered.add(req["request_id"])
                        option = next(o["optionId"] for o in req["options"] if o["kind"] == answer)
                        await self.sup.handle(PermissionAnswer(id=self.id(), agent=NAME, request_id=req["request_id"],
                                                               outcome={"outcome": "selected", "optionId": option}))
                await asyncio.sleep(0.2)
        reply = "".join(m["update"]["content"].get("text", "") for m in self.sink.of("session.update", turn_id=tid)
                        if m["update"]["sessionUpdate"] == "agent_message_chunk" and not m["replay"])
        return ends[0], reply

    def asked(self, turn_id: str) -> int:
        """How many permission requests that turn made."""
        return len(self.sink.of("permission.request", turn_id=turn_id))

    def tools(self, turn_id: str) -> set[str]:
        """The tools that turn called: Claude's tool name, else the title (DeepSeek Harness titles a call with it)."""
        calls = [m["update"] for m in self.sink.of("session.update", turn_id=turn_id)
                 if m["update"]["sessionUpdate"] == "tool_call"]
        return {((u.get("_meta") or {}).get("claudeCode") or {}).get("toolName") or u.get("title") for u in calls}

    async def sh(self, script: str) -> int:
        """Run a shell script in the agent's container, as its user -> exit code."""
        p = await asyncio.create_subprocess_exec("docker", "exec", f"valet-agent-{NAME}", "sh", "-c", script,
                                                 stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        return await p.wait()

    async def exists(self, path: str) -> bool:
        return await self.sh(f"test -e {path}") == 0

    async def close(self) -> None:
        await self.stop()
        await self.sup.shutdown()
        # Volumes are kept by design; a test cleans up its own.
        for vol in (f"valet-agent-{NAME}-home", f"valet-agent-{NAME}-work"):
            p = await asyncio.create_subprocess_exec("docker", "volume", "rm", "-f", vol,
                                                     stdout=asyncio.subprocess.DEVNULL,
                                                     stderr=asyncio.subprocess.DEVNULL)
            await p.wait()


@pytest.fixture
async def make_agent(tmp_path):
    made = []

    async def make(harness: str, permissions: str = "ask") -> Agent:
        a = Agent(tmp_path, harness, permissions)
        made.append(a)
        status = await a.start()
        assert status["state"] == "running", status["error"]
        return a

    yield make
    for a in made:
        await a.close()


async def test_claude_initializes(make_agent):
    a = await make_agent("claude")
    assert a.sink.of("agent.status")[-1]["capabilities"] is not None
    assert await a.new_session()


async def test_secret_is_in_the_container_env_and_not_on_its_command_line(tmp_path):
    """agent.start's secrets reach the harness's environment inside the container, through the docker CLI's env."""
    value = "probe-" + uuid.uuid4().hex
    a = Agent(tmp_path, "claude")
    a.definition["secrets"] = ["PROBE_TOKEN"]
    a.secrets = {"PROBE_TOKEN": value}
    try:
        status = await a.start()
        assert status["state"] == "running", status["error"]
        assert value not in " ".join(a.sup._agents[NAME].launch.args)
        probe = await asyncio.create_subprocess_exec("docker", "exec", f"valet-agent-{NAME}", "sh", "-c",
                                                     'printf %s "$PROBE_TOKEN"; tr "\\0" " " < /proc/1/cmdline',
                                                     stdout=asyncio.subprocess.PIPE)
        out = (await probe.communicate())[0].decode()
        assert out.startswith(value) and value not in out[len(value):]      # in the env; not in PID 1's argv
    finally:
        await a.close()


async def test_deepseek_initializes_and_resumes_across_restarts(make_agent):
    """dsh advertises resume (no load); a session made by one container is resumed by the next, from the home
    volume. Without a key the resumed prompt fails at the model call, which is past the resume."""
    a = await make_agent("deepseek")
    status = a.sink.of("agent.status")[-1]
    assert status["agent_info"]["name"] == "deepseek-harness-acp"
    assert status["capabilities"]["loadSession"] is False
    assert "resume" in status["capabilities"]["sessionCapabilities"]
    sid = await a.new_session()
    await a.stop()
    assert (await a.start())["state"] == "running"
    if has_key(deepseek):
        end, reply = await a.turn(sid, "Reply with exactly the word: pong")
        assert end["stop_reason"] == "end_turn", end
    else:
        end, _ = await a.turn(sid, "hi", timeout=60)
        assert "no API key" in (end["error"] or ""), end


@pytest.mark.skipif(not has_key(claude), reason="needs CLAUDE_CODE_OAUTH_TOKEN (claude setup-token) or ANTHROPIC_API_KEY")
async def test_claude_answers(make_agent):
    a = await make_agent("claude")
    end, reply = await a.turn(await a.new_session(), "Reply with exactly the word: pong")
    assert end["stop_reason"] == "end_turn", end
    assert "pong" in reply.lower()


@pytest.mark.skipif(not has_key(deepseek), reason="needs DEEPSEEK_API_KEY")
async def test_deepseek_answers_in_persona_and_remembers_after_restart(make_agent):
    a = await make_agent("deepseek")
    sid = await a.new_session()
    end, reply = await a.turn(sid, "Remember the word 'tangerine'. Then tell me your name, in one word.")
    assert end["stop_reason"] == "end_turn", end
    assert "Probe" in reply                                     # persona.md reached it, through AGENTS.md
    await a.stop()
    await a.start()
    end, reply = await a.turn(sid, "Which word did I ask you to remember? Reply with just that word.")
    assert end["stop_reason"] == "end_turn", end
    assert "tangerine" in reply.lower()


# ---------- permission levels ----------

NEEDS = {"claude": "needs CLAUDE_CODE_OAUTH_TOKEN (claude setup-token) or ANTHROPIC_API_KEY",
         "deepseek": "needs DEEPSEEK_API_KEY"}
HARNESSES = [pytest.param(h, marks=pytest.mark.skipif(not has_key(m), reason=NEEDS[h]))
             for h, m in (("claude", claude), ("deepseek", deepseek))]
WRITERS = {"Bash", "Write", "Edit", "NotebookEdit"}     # Claude's tools that run or change things
# What a trusted agent could leave behind for a later, stricter level: allow rules and a hook in each settings file
# it can write. The hook would touch /work/hook-ran on every prompt.
AGENT_WRITTEN = {"claude": json.dumps({
    "permissions": {"allow": ["Bash", "Bash(*)", "Write", "Edit"]},
    "hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "touch /work/hook-ran"}]}]}})}


def touch(path: str) -> str:
    return f"Run this exact shell command: touch {path}"


@pytest.mark.parametrize("harness", HARNESSES)
@pytest.mark.parametrize("level, runs, asks", [("locked", False, False), ("ask", False, True),
                                               ("unrestricted", True, False)], ids=["locked", "ask", "unrestricted"])
async def test_permission_level(make_agent, harness, level, runs, asks):
    """In a container, asked to run a command: locked can't (no request, nothing runs), ask asks (rejected here:
    nothing runs), unrestricted just runs it. Every request is rejected, so a wrong ask can't run anything.
    Where a level holds back, reading still works and never asks, even outside the workspace (where Claude Code
    would ask on its own). (trusted: the resume test below.)"""
    a = await make_agent(harness, level)
    sid = await a.new_session()             # Claude's `tools` for locked are accepted by session/new
    end, _ = await a.turn(sid, touch("/work/level.txt"), answer="reject_once")
    assert end["stop_reason"] == "end_turn", end
    assert bool(a.asked(a.last_turn)) is asks
    assert await a.exists("/work/level.txt") is runs
    if harness == "claude" and level == "locked":
        assert not WRITERS & a.tools(a.last_turn)       # it has none: nothing was even attempted
    if runs:
        return
    word = "kumquat-" + uuid.uuid4().hex[:4]
    assert await a.sh(f"echo {word} > /tmp/note.txt") == 0
    end, reply = await a.turn(sid, "Use your file-reading tool (not a shell) to read /tmp/note.txt, and reply with "
                                   "its contents.", answer="reject_once")
    assert end["stop_reason"] == "end_turn", end
    assert a.asked(a.last_turn) == 0
    assert word in reply


@pytest.mark.parametrize("harness", HARNESSES)
async def test_trusted_runs_freely_and_a_restart_at_ask_asks_again_in_the_same_session(make_agent, harness):
    """trusted in a container runs a command without asking. Restarted at ask, the same (resumed) session asks
    again: the level is the definition's, not whatever the session's history last had, nor allow rules or hooks
    the agent wrote into its own settings while it was trusted (planted here, AGENT_WRITTEN)."""
    a = await make_agent(harness, "trusted")
    sid = await a.new_session()
    end, _ = await a.turn(sid, touch("/work/trusted.txt"), answer="reject_once")
    assert end["stop_reason"] == "end_turn", end
    assert a.asked(a.last_turn) == 0
    assert await a.exists("/work/trusted.txt")
    if settings := AGENT_WRITTEN.get(harness):
        assert await a.sh(f"mkdir -p ~/.claude /work/.claude && echo '{settings}' > ~/.claude/settings.json && "
                          f"echo '{settings}' > /work/.claude/settings.local.json") == 0
    await a.stop()
    a.definition["permissions"] = "ask"
    assert (await a.start())["state"] == "running"
    end, _ = await a.turn(sid, touch("/work/asked.txt"), answer="reject_once")
    assert end["stop_reason"] == "end_turn", end
    assert a.asked(a.last_turn) >= 1
    assert not await a.exists("/work/asked.txt")
    assert not await a.exists("/work/hook-ran")
