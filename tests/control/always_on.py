"""Helpers for the always-on tests (schedules, webhooks, queues, valet.owner): an app on a ManualClock in UTC, an
agent started on the test's fake runner, and waiting for what background tasks do."""
import contextlib
import time
from datetime import UTC, datetime

from tests.control.conftest import FakeRunner
from valet.shared.definition import AGENT_FILE, agent_dir

HOST_YAML = """\
name: {name}
harness: claude
model: m1
computer:
  level: host
permissions: {level}
tools:
  mcp: [valet.owner, valet.agents]
"""


def ts(*args: int) -> float:
    """A UTC wall-clock time as unix seconds: ts(2026, 11, 2, 8) is 08:00 on 2 November 2026."""
    return datetime(*args, tzinfo=UTC).timestamp()


def agent_yaml(name: str = "scout", triggers: str = "", level: str = "ask") -> str:
    return HOST_YAML.format(name=name, level=level) + (f"triggers:\n{triggers}" if triggers else "")


def wait(predicate, timeout: float = 5.0, what: str = "condition"):
    """Poll until predicate() is truthy; return its value."""
    end = time.time() + timeout
    while time.time() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def control(client):
    return client.app.state.control


def start(client, rpc, runner, name: str = "scout", triggers: str = "", level: str = "ask", create: bool = True):
    """Create the agent (unless create=False), start it on `runner` and report it running; the agent.start command."""
    if create:
        r = client.post("/api/agents", json={"name": name, "agent_yaml": agent_yaml(name, triggers, level)})
        assert r.status_code == 201, r.text
    r, cmd = rpc(runner, client.post, f"/api/agents/{name}/start", expect="agent.start")
    assert r.status_code == 200, r.text
    runner.status(name, "starting")
    runner.status(name, "running", capabilities={"loadSession": True}, agent_info={"name": "fake"})
    wait(lambda: client.get(f"/api/agents/{name}").json()["state"] == "running", what=f"{name} running")
    return cmd


def session(client, rpc, runner, sid: str = "S1", name: str = "scout") -> dict:
    r, _ = rpc(runner, client.post, f"/api/agents/{name}/sessions", expect="session.new",
               result={"session_id": sid, "modes": None})
    assert r.status_code == 201, r.text
    return r.json()


def answer_new(runner, sid: str) -> dict:
    """The control plane makes a session (a main one, or a trigger's new one): answer its session.new."""
    cmd = runner.recv("session.new")
    runner.reply(cmd, {"session_id": sid, "modes": None})
    return cmd


def answer_prompt(runner, error: str | None = None) -> dict:
    """Take the next session.prompt and accept it (or refuse it with `error`)."""
    cmd = runner.recv("session.prompt")
    runner.reply(cmd, {}, error)
    return cmd


def end_turn(runner, cmd: dict, stop_reason: str | None = "end_turn", error: str | None = None) -> None:
    runner.send(type="turn.end", agent=cmd["agent"], session_id=cmd["session_id"], turn_id=cmd["turn_id"],
                stop_reason=stop_reason, error=error)


def text_of(cmd: dict) -> str:
    return cmd["prompt"][0]["text"]


def runs(client, name: str = "scout") -> list[dict]:
    return client.get(f"/api/agents/{name}/trigger-runs").json()["runs"]


def idle(client, sid: str, name: str = "scout") -> dict | None:
    s = client.get(f"/api/agents/{name}/sessions/{sid}").json()
    return s if s["state"] == "idle" else None


@contextlib.contextmanager
def fake_runner(client, token: str, **hello):
    """A runner socket on `client` (what the `connect` fixture does, for a client a test makes itself)."""
    with client.websocket_connect("/ws/runner", headers={"Authorization": f"Bearer {token}"}) as ws:
        runner = FakeRunner(ws)
        runner.hello(**hello)
        yield runner


def hand_edit(client, text: str, name: str = "scout") -> None:
    """Write agent.yaml on disk, as a hand edit would (the API refuses one that doesn't validate)."""
    (agent_dir(control(client).defs.root, name) / AGENT_FILE).write_text(text)
