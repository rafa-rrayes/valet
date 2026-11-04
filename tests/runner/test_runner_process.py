"""AgentProcess against the scripted fake ACP agent, run as a host-level process."""
import json
import stat
import sys

import pytest

from runner_helpers import FAKE, Sent, fake_cmd, host_agent, runner_cfg
from valet.runner.agent import AgentProcess, write_config
from valet.runner.supervisor import CommandError

TEXT = [{"type": "text", "text": "hi there"}]


def text(t: str) -> list[dict]:
    return [{"type": "text", "text": t}]


@pytest.fixture
async def proc(tmp_path, sent):
    p = AgentProcess(host_agent(), {"persona.md": "Be terse."}, runner_cfg(tmp_path), sent)
    await p.start()
    yield p
    await p.stop()


async def test_start_reports_capabilities(proc, sent, tmp_path):
    assert [m["state"] for m in sent.of("agent.status")] == ["starting", "running"]
    running = sent.of("agent.status")[-1]
    assert running["capabilities"]["loadSession"] is True
    assert running["agent_info"] == {"name": "fake-agent", "version": "0"}
    assert (tmp_path / "runner/agents/scout/work").is_dir()


async def test_session_and_streaming_turn(proc, sent):
    s = await proc.new_session()
    sid = s["session_id"]
    assert sid.startswith("fake-") and s["modes"] is None
    await proc.prompt(sid, "t1", TEXT)
    end = await sent.wait_for("turn.end", turn_id="t1")
    assert (end["stop_reason"], end["error"]) == ("end_turn", None)
    updates = sent.of("session.update", session_id=sid)
    assert [u["update"]["sessionUpdate"] for u in updates] == [
        "agent_thought_chunk", "agent_message_chunk", "agent_message_chunk"]
    assert all(u["turn_id"] == "t1" and u["replay"] is False for u in updates)
    assert sent.texts(sid) == ["echo: ", "hi there"]
    # turn.end comes after every update of its turn
    assert sent.index(end) > sent.index(updates[-1])
    assert proc.active_turns() == []


async def test_persona_and_cwd_reach_session_meta(proc, sent, tmp_path):
    sid = (await proc.new_session())["session_id"]
    await proc.prompt(sid, "t1", text("meta"))
    await sent.wait_for("turn.end", turn_id="t1")
    meta = json.loads(sent.texts(sid)[0])
    assert meta["cwd"] == str(tmp_path / "runner/agents/scout/work")
    assert meta["systemPrompt"] == {"append": "Be terse."}


async def test_busy_and_bad_prompt(proc, sent):
    sid = (await proc.new_session())["session_id"]
    await proc.prompt(sid, "t1", text("slow"))
    with pytest.raises(CommandError, match="busy"):
        await proc.prompt(sid, "t2", TEXT)
    assert [t.turn_id for t in proc.active_turns()] == ["t1"]
    await proc.cancel(sid)
    end = await sent.wait_for("turn.end", turn_id="t1")
    assert end["stop_reason"] == "cancelled"
    with pytest.raises(CommandError, match="invalid prompt"):
        await proc.prompt(sid, "t3", [{"type": "nonsense"}])


@pytest.mark.parametrize("option,status,said", [("allow", "completed", "allowed"), ("reject", "failed", "rejected")])
async def test_permission_round_trip(proc, sent, option, status, said):
    sid = (await proc.new_session())["session_id"]
    await proc.prompt(sid, "t1", text("permission please"))
    req = await sent.wait_for("permission.request")
    assert req["turn_id"] == "t1" and req["session_id"] == sid
    assert req["tool_call"]["title"] == "Write hello.txt"
    assert [o["optionId"] for o in req["options"]] == ["allow", "allow-always", "reject"]
    assert proc.pending_permissions() == [req["request_id"]]
    with pytest.raises(CommandError, match="unknown request"):
        await proc.answer("nope", {"outcome": "selected", "optionId": "allow"})
    with pytest.raises(CommandError, match="invalid outcome"):
        await proc.answer(req["request_id"], {"outcome": "maybe"})
    await proc.answer(req["request_id"], {"outcome": "selected", "optionId": option})
    end = await sent.wait_for("turn.end", turn_id="t1")
    assert end["stop_reason"] == "end_turn"
    tool_updates = [m["update"] for m in sent.of("session.update") if m["update"]["sessionUpdate"] == "tool_call_update"]
    assert tool_updates[-1]["status"] == status
    assert sent.texts(sid)[-1] == said
    assert proc.pending_permissions() == []
    with pytest.raises(CommandError, match="unknown request"):
        await proc.answer(req["request_id"], {"outcome": "selected", "optionId": option})


async def test_cancel_closes_pending_permission(proc, sent):
    sid = (await proc.new_session())["session_id"]
    await proc.prompt(sid, "t1", text("permission"))
    req = await sent.wait_for("permission.request")
    await proc.cancel(sid)
    closed = await sent.wait_for("permission.closed")
    assert (closed["request_id"], closed["reason"]) == (req["request_id"], "cancelled")
    end = await sent.wait_for("turn.end", turn_id="t1")
    assert end["stop_reason"] == "cancelled"
    assert sent.texts(sid)[-1] == "cancelled"


async def test_harness_error_ends_turn(proc, sent):
    sid = (await proc.new_session())["session_id"]
    await proc.prompt(sid, "t1", text("fail"))
    end = await sent.wait_for("turn.end", turn_id="t1")
    assert end["stop_reason"] is None and "fake failure" in end["error"]
    assert proc.state == "running"


async def test_crash_mid_turn(proc, sent):
    sid = (await proc.new_session())["session_id"]
    await proc.prompt(sid, "t0", text("permission"))
    req = await sent.wait_for("permission.request")
    other = (await proc.new_session())["session_id"]
    await proc.prompt(other, "t1", text("crash"))
    failed = await sent.wait_for("agent.status", state="failed")
    assert "exited with code 3" in failed["error"]
    assert "crashing on purpose" in failed["error"]
    ends = {m["turn_id"]: m for m in sent.of("turn.end")}
    assert ends["t0"]["error"] == ends["t1"]["error"] == "agent exited"
    assert len(sent.of("turn.end")) == 2
    closed = sent.of("permission.closed")
    assert [(c["request_id"], c["reason"]) for c in closed] == [(req["request_id"], "exited")]
    assert sent.index(failed) > max(sent.index(m) for m in [*ends.values(), *closed])
    with pytest.raises(CommandError, match="not running"):
        await proc.new_session()


async def test_restart_loads_unknown_session(tmp_path, sent):
    cfg = runner_cfg(tmp_path)
    first = AgentProcess(host_agent(), {}, cfg, sent)
    await first.start()
    sid = (await first.new_session())["session_id"]
    await first.stop()
    assert sent.of("agent.status")[-1]["state"] == "stopped"

    second = AgentProcess(host_agent(), {}, cfg, sent)
    await second.start()
    try:
        await second.prompt(sid, "t2", TEXT)
        assert [t.turn_id for t in second.active_turns()] == ["t2"]    # listed in a hello while it loads
        end = await sent.wait_for("turn.end", turn_id="t2")
        replays = [m for m in sent.of("session.update", session_id=sid) if m["replay"]]
        assert [m["update"]["sessionUpdate"] for m in replays] == ["user_message_chunk", "agent_message_chunk"]
        assert all(m["turn_id"] is None for m in replays)
        assert end["stop_reason"] == "end_turn"
        assert sent.texts(sid) == ["echo: ", "hi there"]
    finally:
        await second.stop()


async def test_cancel_while_loading(tmp_path, sent):
    """A cancel that arrives while the turn is still loading its session ends the turn without prompting."""
    cfg = runner_cfg(tmp_path, "--slow-load")
    first = AgentProcess(host_agent(), {}, cfg, sent)
    await first.start()
    sid = (await first.new_session())["session_id"]
    await first.stop()

    second = AgentProcess(host_agent(), {}, cfg, sent)
    await second.start()
    try:
        await second.prompt(sid, "t2", text("slow"))
        assert sid in second._loading
        await second.cancel(sid)
        end = await sent.wait_for("turn.end", turn_id="t2")
        assert (end["stop_reason"], end["error"]) == ("cancelled", None)
        assert sent.texts(sid) == []          # never prompted: no "working"
        assert second.active_turns() == []
    finally:
        await second.stop()


async def test_resume_when_only_resume_is_advertised(tmp_path, sent):
    p = AgentProcess(host_agent(), {}, runner_cfg(tmp_path, "--caps", "resume"), sent)
    await p.start()
    try:
        await p.prompt("old-session", "t1", text("meta"))
        await sent.wait_for("turn.end", turn_id="t1")
        assert json.loads(sent.texts("old-session")[0])["resumed"] is True
    finally:
        await p.stop()


async def test_cannot_resume(tmp_path, sent):
    p = AgentProcess(host_agent(), {}, runner_cfg(tmp_path, "--caps", "none"), sent)
    await p.start()
    try:
        with pytest.raises(CommandError, match="^cannot resume session$"):
            await p.prompt("old-session", "t1", TEXT)
        assert p.active_turns() == [] and sent.of("turn.end") == []
    finally:
        await p.stop()


async def test_stop_ends_turns_and_requests(tmp_path, sent):
    p = AgentProcess(host_agent(), {}, runner_cfg(tmp_path), sent)
    await p.start()
    sid = (await p.new_session())["session_id"]
    await p.prompt(sid, "t1", text("permission"))
    req = await sent.wait_for("permission.request")
    await p.stop()
    assert sent.of("turn.end", turn_id="t1")[0]["error"] == "agent stopped"
    assert sent.of("permission.closed")[0] == {"type": "permission.closed", "agent": "scout", "session_id": sid,
                                               "request_id": req["request_id"], "reason": "exited",
                                               "outcome": None}
    assert sent[-1]["type"] == "agent.status" and sent[-1]["state"] == "stopped"
    assert len(sent.of("turn.end")) == 1


async def test_start_failures(tmp_path, sent):
    cfg = runner_cfg(tmp_path, "--exit-early")
    p = AgentProcess(host_agent(), {}, cfg, sent)
    await p.start()
    assert p.state == "failed"
    assert "exited with code 7" in p.error and "refusing to start" in p.error

    missing = AgentProcess(host_agent("ghost"), {}, runner_cfg(tmp_path, harness_commands={"claude": ["/nonexistent/acp"]}),
                           Sent())
    await missing.start()
    assert missing.state == "failed" and missing.error.startswith("cannot run /nonexistent/acp")

    # A plain ACP agent Valet has no adapter for: nobody knows where its model is, so `gateway` can't work.
    later = Sent()
    plain = runner_cfg(tmp_path, harness_commands={"mine": fake_cmd()})
    m2 = AgentProcess(host_agent(harness="mine", computer={"level": "container", "network": "gateway"}), {}, plain,
                      later)
    await m2.start()
    assert [m["state"] for m in later.of("agent.status")] == ["starting", "failed"]
    assert "Valet doesn't know where this harness's model is" in later.of("agent.status")[-1]["error"]


async def test_container_launch_uses_docker_cli(tmp_path, sent):
    """A stand-in `docker` that logs its argv and env, then runs the fake agent: checks rm -f before start,
    -e passthrough through the CLI's environment, and docker stop after."""
    log = tmp_path / "docker.log"
    script = tmp_path / "docker"
    script.write_text(f"""#!/bin/sh
echo "$@ KEY=$ANTHROPIC_API_KEY" >> {log}
if [ "$1" = run ]; then exec {sys.executable} {FAKE}; fi
exit 1
""")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    cfg = runner_cfg(tmp_path, docker=str(script), passthrough={"ANTHROPIC_API_KEY": "sk-test"}, harness_commands={})
    p = AgentProcess(host_agent(computer={"level": "container"}), {}, cfg, sent)
    await p.start()
    assert p.state == "running", p.error
    sid = (await p.new_session())["session_id"]
    await p.prompt(sid, "t1", text("meta"))
    await sent.wait_for("turn.end", turn_id="t1")
    assert json.loads(sent.texts(sid)[0])["cwd"] == "/work"
    await p.stop()
    lines = log.read_text().splitlines()
    assert lines[0] == "rm -f valet-agent-scout KEY="
    assert lines[1].startswith("run -i --rm --init --name valet-agent-scout ")
    assert f"-v {tmp_path}/runner/agents/scout/config:/valet:ro -w /work" in lines[1]
    assert "-e ANTHROPIC_API_KEY -e CLAUDE_AGENT_ACP_PROTOCOL valet/agent-base claude-agent-acp" in lines[1]
    assert lines[1].endswith("KEY=sk-test") and "sk-test" not in lines[1][:-len("KEY=sk-test")]
    assert lines[2] == "stop valet-agent-scout KEY="


async def test_deepseek_config_dir_is_written_and_replaced(tmp_path, sent):
    """The compiled files land in the config dir before the harness starts, readable by any uid, and a restart
    replaces the whole dir: a file the definition no longer produces is gone."""
    cfg = runner_cfg(tmp_path, "--caps", "resume", harness_commands={"deepseek": fake_cmd("--caps", "resume")})
    config = tmp_path / "runner/agents/scout/config"
    p = AgentProcess(host_agent(harness="deepseek", model="deepseek-v4-pro"), {"persona.md": "You are Scout."},
                     cfg, sent)
    await p.start()
    assert p.state == "running", p.error
    assert p.capabilities["sessionCapabilities"] == {"resume": {}}
    assert (config / "agents/AGENTS.md").read_text() == "You are Scout.\n"
    assert 'model: "deepseek-v4-pro"' in (config / "dsh.patch.yml").read_text()
    assert p.compiled.env["DSH_AGENTS_HOME"] == str(config / "agents")
    assert stat.S_IMODE((config / "agents/AGENTS.md").stat().st_mode) == 0o644
    assert stat.S_IMODE((config / "agents").stat().st_mode) == 0o755
    await p.stop()

    p = AgentProcess(host_agent(harness="deepseek", directives=[]), {"persona.md": "You are Scout."}, cfg, sent)
    await p.start()
    assert p.state == "running", p.error
    assert sorted(f.name for f in config.rglob("*")) == ["dsh.patch.yml", "dsh_approval_gate.mjs"]   # no AGENTS.md
    assert "model:" not in (config / "dsh.patch.yml").read_text()
    await p.stop()


async def test_skills_land_in_the_config_dir_and_reach_the_harness(tmp_path, sent):
    """agent.start's skills/<name>/<path> files: Claude's plugin in the config dir, named in session/new's _meta;
    a restart without them leaves none behind."""
    skill = "---\nname: code-word\ndescription: Use when asked for the code word.\n---\n\nSay PELICAN.\n"
    config = tmp_path / "runner/agents/scout/config"
    files = {"persona.md": "Be terse.", "skills/code-word/SKILL.md": skill, "skills/code-word/refs/w.md": "PELICAN"}
    p = AgentProcess(host_agent(), files, runner_cfg(tmp_path), sent)
    await p.start()
    assert p.state == "running", p.error
    assert (config / "claude-plugin/skills/code-word/SKILL.md").read_text() == skill
    assert (config / "claude-plugin/skills/code-word/refs/w.md").read_text() == "PELICAN"
    assert json.loads((config / "claude-plugin/.claude-plugin/plugin.json").read_text())["name"] == "valet"
    assert stat.S_IMODE((config / "claude-plugin/skills/code-word/refs").stat().st_mode) == 0o755
    sid = (await p.new_session())["session_id"]
    await p.prompt(sid, "t1", text("meta"))
    await sent.wait_for("turn.end", turn_id="t1")
    options = json.loads(sent.texts(sid)[0])["claudeCode"]["options"]
    assert options["plugins"] == [{"type": "local", "path": str(config / "claude-plugin"), "skipMcpDiscovery": True}]
    assert options["skills"] == ["valet:code-word"]
    await p.stop()

    p = AgentProcess(host_agent(), {"persona.md": "Be terse."}, runner_cfg(tmp_path), sent)
    await p.start()
    assert not (config / "claude-plugin").exists()
    await p.stop()


async def test_unwritable_config_dir_fails_the_agent(tmp_path, sent):
    (tmp_path / "runner/agents").mkdir(parents=True)
    (tmp_path / "runner/agents/scout").write_text("a file where the agent's dir should be")
    p = AgentProcess(host_agent(), {}, runner_cfg(tmp_path), sent)
    await p.start()
    assert p.state == "failed" and p.error.startswith("cannot prepare the agent's files:")


def test_write_config_refuses_paths_outside(tmp_path):
    with pytest.raises(ValueError, match="outside the config dir"):
        write_config(tmp_path / "config", {"../escape.txt": "x"})
    assert not (tmp_path / "escape.txt").exists()


# ---------- permission level -> session mode (Claude's compiled.mode, set with session/set_mode) ----------

MODES = "default,acceptEdits,bypassPermissions"


def modes_set(sent: Sent, sid: str) -> list[str] | None:
    """What session/set_mode calls the fake recorded for the session (from a `meta` prompt's answer)."""
    return json.loads(sent.texts(sid)[0]).get("modes_set")


@pytest.mark.parametrize("permissions,mode", [("unrestricted", "bypassPermissions"), ("trusted", "acceptEdits")])
async def test_new_session_is_put_in_the_levels_mode(tmp_path, sent, permissions, mode):
    """Right after session/new, and the reply reports the mode the session is in now. (A host agent: trusted is
    acceptEdits there.)"""
    p = AgentProcess(host_agent(permissions=permissions), {}, runner_cfg(tmp_path, "--modes", MODES), sent)
    await p.start()
    try:
        s = await p.new_session()
        assert s["modes"]["currentModeId"] == mode
        assert [m["id"] for m in s["modes"]["availableModes"]] == MODES.split(",")
        await p.prompt(s["session_id"], "t1", text("meta"))
        await sent.wait_for("turn.end", turn_id="t1")
        assert modes_set(sent, s["session_id"]) == [mode]
    finally:
        await p.stop()


async def test_session_already_in_the_mode_is_left_alone(tmp_path, sent):
    p = AgentProcess(host_agent(permissions="ask"), {}, runner_cfg(tmp_path, "--modes", MODES), sent)
    await p.start()
    try:
        s = await p.new_session()
        assert s["modes"]["currentModeId"] == "default"
        await p.prompt(s["session_id"], "t1", text("meta"))
        await sent.wait_for("turn.end", turn_id="t1")
        assert modes_set(sent, s["session_id"]) is None
    finally:
        await p.stop()


async def test_a_strict_mode_the_harness_lacks_stops_the_session(tmp_path, sent):
    """locked and ask need Claude's `default`: a harness whose modes lack it may be in a freer one, so the session
    doesn't run (a free mode that's missing only asks more: see the next test)."""
    p = AgentProcess(host_agent(permissions="ask"), {}, runner_cfg(tmp_path, "--modes", "acceptEdits,plan"), sent)
    await p.start()
    try:
        with pytest.raises(CommandError, match="can't switch to the 'default' mode its permission level needs"):
            await p.new_session()
    finally:
        await p.stop()


async def test_harness_without_the_mode_keeps_its_own_and_says_so(tmp_path, sent, caplog):
    """Claude offers bypassPermissions only to a non-root user: the session stays in the harness's mode (it asks
    more, never less), and the runner logs it. The same for a harness with no modes at all."""
    p = AgentProcess(host_agent(permissions="unrestricted"), {}, runner_cfg(tmp_path, "--modes", "default,plan"),
                     sent)
    await p.start()
    try:
        s = await p.new_session()
        assert s["modes"]["currentModeId"] == "default"
        assert "the harness has no mode 'bypassPermissions'; session " in caplog.text
        assert f"{s['session_id']} stays in 'default'" in caplog.text
        await p.prompt(s["session_id"], "t1", text("meta"))
        await sent.wait_for("turn.end", turn_id="t1")
        assert modes_set(sent, s["session_id"]) is None
    finally:
        await p.stop()

    caplog.clear()
    p = AgentProcess(host_agent(permissions="unrestricted"), {}, runner_cfg(tmp_path), sent)
    await p.start()
    try:
        s = await p.new_session()
        assert s["modes"] is None
        assert f"no mode 'bypassPermissions'; session {s['session_id']} stays in its own mode" in caplog.text
    finally:
        await p.stop()


async def test_set_mode_failure_fails_the_new_session(tmp_path, sent):
    """The session might be in a freer mode than the level allows: it isn't handed out."""
    cfg = runner_cfg(tmp_path, "--modes", MODES, "--set-mode-fails")
    p = AgentProcess(host_agent(permissions="unrestricted"), {}, cfg, sent)
    await p.start()
    try:
        with pytest.raises(CommandError, match="^session/new failed: .*fake set_mode failure"):
            await p.new_session()
        assert p._sessions == set() and p.state == "running"
    finally:
        await p.stop()


async def test_loaded_session_is_put_back_in_the_levels_mode(tmp_path, sent):
    """A session continued by a new harness process carries on in whatever mode it was in: the runner sets the
    level's mode again after session/load, before the prompt."""
    cfg = runner_cfg(tmp_path, "--modes", MODES)
    first = AgentProcess(host_agent(permissions="unrestricted"), {}, cfg, sent)
    await first.start()
    sid = (await first.new_session())["session_id"]
    await first.stop()

    second = AgentProcess(host_agent(permissions="unrestricted"), {}, cfg, sent)
    await second.start()
    try:
        await second.prompt(sid, "t2", text("meta"))
        end = await sent.wait_for("turn.end", turn_id="t2")
        assert end["error"] is None
        assert modes_set(sent, sid) == ["bypassPermissions"]       # the meta of the load, in the new process
    finally:
        await second.stop()


async def test_resumed_session_is_put_in_the_levels_mode(tmp_path, sent):
    p = AgentProcess(host_agent(permissions="unrestricted"), {}, runner_cfg(tmp_path, "--caps", "resume", "--modes",
                                                                             MODES), sent)
    await p.start()
    try:
        await p.prompt("old-session", "t1", text("meta"))
        await sent.wait_for("turn.end", turn_id="t1")
        meta = json.loads(sent.texts("old-session")[0])
        assert meta["resumed"] is True and meta["modes_set"] == ["bypassPermissions"]
    finally:
        await p.stop()


async def test_set_mode_failure_on_resume_fails_the_turn_before_prompting(tmp_path, sent):
    cfg = runner_cfg(tmp_path, "--caps", "resume", "--modes", MODES, "--set-mode-fails")
    p = AgentProcess(host_agent(permissions="unrestricted"), {}, cfg, sent)
    await p.start()
    try:
        await p.prompt("old-session", "t1", text("meta"))
        end = await sent.wait_for("turn.end", turn_id="t1")
        assert end["stop_reason"] is None
        assert end["error"].startswith("cannot resume session: ") and "fake set_mode failure" in end["error"]
        assert sent.texts("old-session") == []         # never prompted
        assert "old-session" not in p._sessions        # the next prompt tries again
    finally:
        await p.stop()
