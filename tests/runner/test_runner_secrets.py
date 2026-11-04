"""Secrets on the runner: agent.start's values become env vars of the harness process (host: its env; container: the
docker CLI's env, with only `-e NAME` on argv) and appear nowhere else: not on argv, in the config dir, in a
notification, in the hello, in a log line about a bad frame, or in a repr."""
import asyncio
import json
import stat
import sys

import pytest
from pydantic import ValidationError

from runner_helpers import FAKE, fake_cmd, host_agent, runner_cfg
from test_runner_link import control, make_runner, started  # noqa: F401  (control is a fixture)
from valet.runner.agent import AgentProcess
from valet.runner.harness import Compiled
from valet.runner.launch import LaunchError, plan
from valet.runner.runner import _reason
from valet.runner.supervisor import Supervisor
from valet.shared.protocol import AgentStart, parse_control_message

VALUE = "ghp_TEST_not-a-real-token_0123456789"
SECRETS = {"GITHUB_TOKEN": VALUE, "NPM_TOKEN": "npm_" + VALUE}


def text(t: str) -> list[dict]:
    return [{"type": "text", "text": t}]


def without_replies(sent) -> str:
    """Everything sent except the agent's own messages (an agent may echo its env; Valet must not)."""
    return json.dumps([m for m in sent if m["type"] != "session.update"])


# ---------- launch plans ----------
def test_plan_puts_secrets_in_the_env_and_only_names_on_argv(tmp_path):
    cfg = runner_cfg(tmp_path)
    compiled = Compiled(argv=["claude-agent-acp"], env={"CLAUDE_AGENT_ACP_PROTOCOL": "v1"})
    p = plan(host_agent(computer={"level": "container"}), compiled, cfg, SECRETS)
    assert p.env == {"CLAUDE_AGENT_ACP_PROTOCOL": "v1", **SECRETS}
    i = p.args.index("valet/agent-base")
    assert p.args[i - 6:i] == ["-e", "CLAUDE_AGENT_ACP_PROTOCOL", "-e", "GITHUB_TOKEN", "-e", "NPM_TOKEN"]
    assert VALUE not in " ".join(p.args) and VALUE not in repr(p)
    assert compiled.env == {"CLAUDE_AGENT_ACP_PROTOCOL": "v1"}

    host = plan(host_agent(), compiled, cfg, SECRETS)
    assert host.env == {"CLAUDE_AGENT_ACP_PROTOCOL": "v1", **SECRETS}
    assert VALUE not in " ".join([host.command, *host.args])
    assert plan(host_agent(), compiled, cfg).env == compiled.env


def test_plan_refuses_a_name_it_cannot_give(tmp_path):
    cfg = runner_cfg(tmp_path)
    harness_sets_it = Compiled(argv=["x"], env={"OPENAI_API_KEY": "the harness's own key"})
    with pytest.raises(LaunchError) as e:
        plan(host_agent(), harness_sets_it, cfg, {"OPENAI_API_KEY": VALUE})
    assert str(e.value) == "secret OPENAI_API_KEY: the harness sets OPENAI_API_KEY itself"
    with pytest.raises(LaunchError, match="secret HOME: reserved"):
        plan(host_agent(), Compiled(argv=["x"]), cfg, {"HOME": VALUE})
    with pytest.raises(LaunchError, match="secret lower: an env var name"):
        plan(host_agent(), Compiled(argv=["x"]), cfg, {"lower": VALUE})


# ---------- processes ----------
async def test_host_harness_gets_them_in_its_env(tmp_path, sent):
    """DeepSeek's adapter writes config files, so the config dir is checked too."""
    cfg = runner_cfg(tmp_path, harness_commands={"deepseek": fake_cmd()})
    p = AgentProcess(host_agent(harness="deepseek", secrets=list(SECRETS)), {"persona.md": "Hi."}, cfg, sent,
                     secrets=SECRETS)
    await p.start()
    assert p.state == "running", p.error
    sid = (await p.new_session())["session_id"]
    await p.prompt(sid, "t1", text("env GITHUB_TOKEN NPM_TOKEN OTHER_TOKEN"))
    await sent.wait_for("turn.end", turn_id="t1")
    assert json.loads(sent.texts(sid)[0]) == {**SECRETS, "OTHER_TOKEN": None}

    assert VALUE not in without_replies(sent)
    assert VALUE not in p.agent_state().model_dump_json()
    files = [f for f in (tmp_path / "runner").rglob("*") if f.is_file()]
    assert any(f.name == "dsh.patch.yml" for f in files)
    assert not [f for f in files if VALUE.encode() in f.read_bytes()]
    await p.stop()


async def test_container_values_travel_in_the_docker_cli_env(tmp_path, sent):
    """A stand-in `docker` logs its argv and the secret it was given, then runs the fake agent, which inherits the
    CLI's env the way `-e NAME` hands it to the container."""
    log = tmp_path / "docker.log"
    script = tmp_path / "docker"
    script.write_text(f"""#!/bin/sh
echo "$@ SECRET=$GITHUB_TOKEN" >> {log}
if [ "$1" = run ]; then exec {sys.executable} {FAKE}; fi
exit 1
""")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    cfg = runner_cfg(tmp_path, docker=str(script), harness_commands={})
    p = AgentProcess(host_agent(computer={"level": "container"}, secrets=["GITHUB_TOKEN"]), {}, cfg, sent,
                     secrets={"GITHUB_TOKEN": VALUE})
    await p.start()
    assert p.state == "running", p.error
    sid = (await p.new_session())["session_id"]
    await p.prompt(sid, "t1", text("env GITHUB_TOKEN"))
    await sent.wait_for("turn.end", turn_id="t1")
    assert json.loads(sent.texts(sid)[0]) == {"GITHUB_TOKEN": VALUE}
    await p.stop()
    rm, run, stop = log.read_text().splitlines()
    assert "-e CLAUDE_AGENT_ACP_PROTOCOL -e GITHUB_TOKEN valet/agent-base claude-agent-acp" in run
    assert run.endswith(" SECRET=" + VALUE) and VALUE not in run[:-len(VALUE)]
    assert rm == "rm -f valet-agent-scout SECRET=" and stop == "stop valet-agent-scout SECRET="
    assert VALUE not in without_replies(sent)


async def test_supervisor_hands_them_over_and_a_bad_name_fails_the_start(tmp_path, sent):
    sup = Supervisor(runner_cfg(tmp_path), sent)
    d = host_agent(secrets=["GITHUB_TOKEN"]).model_dump(mode="json")

    def cmd(type_, **fields):
        return parse_control_message({"type": type_, "id": "c-" + type_, **fields})
    try:
        await sup.handle(cmd("agent.start", agent="scout", definition=d, secrets={"GITHUB_TOKEN": VALUE}))
        await sent.wait_for("agent.status", state="running")
        sid = (await sup.handle(cmd("session.new", agent="scout")))["session_id"]
        await sup.handle(cmd("session.prompt", agent="scout", session_id=sid, turn_id="t1",
                             prompt=text("env GITHUB_TOKEN")))
        await sent.wait_for("turn.end", turn_id="t1")
        assert json.loads(sent.texts(sid)[0]) == {"GITHUB_TOKEN": VALUE}
        assert VALUE not in json.dumps([a.model_dump() for a in sup.agents()])

        await sup.handle(cmd("agent.start", agent="scout", definition=d, secrets={"HOME": VALUE}))
        failed = await sent.wait_for("agent.status", state="failed")
        assert failed["error"] == "secret HOME: reserved: Valet, a harness or the computer sets it"
        assert VALUE not in without_replies(sent)
    finally:
        await sup.shutdown()


# ---------- the link ----------
async def test_hello_and_notifications_never_carry_them(control, tmp_path):  # noqa: F811
    runner = make_runner(control, tmp_path)
    task = await started(runner)
    try:
        d = host_agent(secrets=["GITHUB_TOKEN"]).model_dump(mode="json")
        r = await control.call("agent.start", agent="scout", definition=d, secrets={"GITHUB_TOKEN": VALUE})
        assert r["ok"] is True
        await control.got.wait_for("agent.status", state="running")
        runner.connected.clear()
        await control.ws.close(1012, "restarting")
        async with asyncio.timeout(10):
            await runner.connected.wait()
        assert [(a["agent"], a["state"]) for a in control.hellos[-1]["agents"]] == [("scout", "running")]
        assert VALUE not in json.dumps(control.hellos) and VALUE not in json.dumps(control.got)
    finally:
        await runner.close()
        await asyncio.wait_for(task, 10)


def test_agent_start_keeps_them_out_of_repr_and_bad_frame_logs():
    start = AgentStart(id="x", agent="scout", definition={}, secrets={"GITHUB_TOKEN": VALUE})
    assert start.dump()["secrets"] == {"GITHUB_TOKEN": VALUE}
    assert VALUE not in repr(start)
    assert AgentStart(id="x", agent="scout", definition={}).secrets == {}
    with pytest.raises(ValidationError) as e:
        parse_control_message({"type": "agent.start", "id": "x", "agent": "scout", "definition": {},
                               "secrets": {"GITHUB_TOKEN": [VALUE]}})
    assert VALUE in str(e.value)            # pydantic quotes the input...
    reason = _reason(e.value)               # ... the runner's log line doesn't
    assert VALUE not in reason and "secrets.GITHUB_TOKEN" in reason
