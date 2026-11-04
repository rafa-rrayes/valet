"""The runner's side of continuing and handing off Claude Code sessions: listing the transcripts under the runner
user's ~/.claude/projects (a temporary HOME here), reading one for a hand-off, and session.import in a host Claude
Code agent: session/load with the history sent up as it replays."""
import asyncio
import uuid

import pytest
from runner_helpers import Sent, host_agent, runner_cfg
from valet.runner import claude_sessions
from valet.runner.agent import AgentProcess
from valet.runner.supervisor import CommandError, Supervisor
from valet.shared.protocol import parse_control_message

from tests.claude_transcripts import Chain, simple


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    return h


@pytest.fixture
def project(tmp_path):
    p = tmp_path / "project"
    p.mkdir()
    return str(p)


# ---------- listing and reading ----------

def test_sessions_of_a_folder_newest_first(home, project):
    old = simple(project, ("first task", "done"))
    old.write(home, mtime=1_000)
    new = simple(project, ("second task", "done"))
    new.line("ai-title", aiTitle="The second")
    new.write(home, mtime=2_000)
    side = Chain(project)
    side.user("a subagent's work", isSidechain=True)
    side.write(home)
    (home / ".claude" / "projects" / claude_sessions.transcripts.folder_name(project) / "notes.jsonl").write_text("{}\n")
    elsewhere = simple("/srv/other", ("other place", "ok"))
    elsewhere.write(home)
    listed = claude_sessions.sessions_for(project)
    assert [(s["id"], s["title"], s["cwd"]) for s in listed] == [
        (new.session_id, "The second", project), (old.session_id, "first task", project)]
    assert listed[0]["size"] > 0 and listed[0]["updated_at"] == 2_000
    assert claude_sessions.sessions_for(str(home / "nothing-here")) == []


def test_a_cwd_seen_through_a_symlink(home, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    s = simple(str(real), ("in the real folder", "ok"))
    s.write(home)
    assert [x["id"] for x in claude_sessions.sessions_for(str(link))] == [s.session_id]
    assert claude_sessions.find(str(link), s.session_id).name == f"{s.session_id}.jsonl"


def test_a_long_folder_found_by_its_prefix(home, tmp_path):
    cwd = str(tmp_path / ("deep-" * 50))
    s = simple(cwd, ("deep down", "ok"))
    name = claude_sessions.transcripts.folder_name(cwd)
    s.write(home, folder=name[:200] + "-otherhash")          # the CLI's hash may not be the SDK's
    assert [x["id"] for x in claude_sessions.sessions_for(cwd)] == [s.session_id]


def test_folders_and_reading(home, project):
    a = simple(project, ("fix it", "Fixed."))
    a.tool("Bash", {"command": "pytest"})
    a.write(home, mtime=5_000)
    simple("/srv/b", ("b", "ok")).write(home, mtime=1_000)
    (home / ".claude" / "projects" / "empty").mkdir()
    folders = claude_sessions.folders()
    assert [(f["cwd"], f["sessions"]) for f in folders] == [(project, 1), ("/srv/b", 1)]
    got = claude_sessions.read(folders[0]["folder"], a.session_id)
    assert got["title"] == "fix it" and got["cwd"] == project
    assert got["entries"] == [{"role": "user", "text": "fix it"}, {"role": "assistant", "text": "Fixed."},
                              {"role": "tool", "text": "Bash: pytest"}]


@pytest.mark.parametrize("folder", ["..", "../x", "a/b", "", ".claude", "-x/../../etc"])
def test_nothing_outside_the_projects_dir(home, folder):
    with pytest.raises(claude_sessions.NotFound):
        claude_sessions.sessions_in(folder)
    with pytest.raises(claude_sessions.NotFound):
        claude_sessions.read(folder, str(uuid.uuid4()))


def test_a_session_is_an_id(home, project):
    s = simple(project, ("x", "y"))
    s.write(home)
    folder = claude_sessions.transcripts.folder_name(project)
    for bad in ("../../x", s.session_id + "/..", "abc"):
        with pytest.raises(claude_sessions.NotFound):
            claude_sessions.read(folder, bad)
        with pytest.raises(claude_sessions.NotFound):
            claude_sessions.find(project, bad)


async def test_supervisor_commands(home, project, tmp_path):
    s = simple(project, ("fix it", "Fixed."), ("and test it", "Tested."))
    s.write(home)
    sup = Supervisor(runner_cfg(tmp_path), Sent())
    folder = claude_sessions.transcripts.folder_name(project)
    r = await sup.handle(parse_control_message({"type": "claude.sessions", "id": "1"}))
    assert [f["folder"] for f in r["folders"]] == [folder]
    r = await sup.handle(parse_control_message({"type": "claude.sessions", "id": "2", "folder": folder}))
    assert [x["id"] for x in r["sessions"]] == [s.session_id]
    r = await sup.handle(parse_control_message({"type": "claude.transcript", "id": "3", "folder": folder,
                                                "session_id": s.session_id, "budget": 30}))
    assert r["entries"] == [{"role": "assistant", "text": "Tested."}] and r["omitted"] == 3   # the newest that fit
    with pytest.raises(CommandError, match="no Claude Code project folder"):
        await sup.handle(parse_control_message({"type": "claude.sessions", "id": "4", "folder": "nope"}))
    with pytest.raises(CommandError, match="not running"):
        await sup.handle(parse_control_message({"type": "claude.sessions", "id": "5", "agent": "scout"}))


# ---------- session.import ----------

def agent_here(project: str, **kw):
    return host_agent(computer={"level": "host", "workdir": project}, **kw)


async def started(tmp_path, project, *flags, **kw) -> tuple[AgentProcess, Sent]:
    sent = Sent()
    p = AgentProcess(agent_here(project, **kw), {}, runner_cfg(tmp_path, *flags), sent)
    await p.start()
    assert p.state == "running", p.error
    return p, sent


async def test_import_sends_the_history_then_replies(home, project, tmp_path):
    s = simple(project, ("fix the bug", "Looking."), ("and add a test", "Added."))
    s.tool("Bash", {"command": "pytest -q"})
    s.assistant("All green.")
    s.line("ai-title", aiTitle="Bug and test")
    s.write(home)
    p, sent = await started(tmp_path, project)
    try:
        listed = await p.claude_sessions()
        assert listed["cwd"] == project and [x["id"] for x in listed["sessions"]] == [s.session_id]
        r = await p.import_session(s.session_id)
        assert r == {"session_id": s.session_id, "modes": None, "title": "Bug and test"}
        history = [m for m in sent if m.get("session_id") == s.session_id]
        assert all(m["type"] in ("session.update", "turn.end") for m in history)
        updates = [m for m in history if m["type"] == "session.update"]
        assert all(m["history"] and m["replay"] for m in updates)
        turns = list(dict.fromkeys(m["turn_id"] for m in updates))
        assert len(turns) == 2 and all(turns)                       # one made-up turn per exchange
        assert [m["turn_id"] for m in history if m["type"] == "turn.end"] == turns   # each closed, in order
        assert [m["stop_reason"] for m in history if m["type"] == "turn.end"] == ["end_turn", "end_turn"]
        kinds = [(m["turn_id"] == turns[1], m["update"]["sessionUpdate"]) for m in updates]
        assert kinds == [(False, "user_message_chunk"), (False, "agent_message_chunk"), (True, "user_message_chunk"),
                         (True, "agent_message_chunk"), (True, "tool_call"), (True, "agent_message_chunk")]
        # The session is open now: a prompt runs in it without loading it again, as a live turn.
        before = len(sent)
        await p.prompt(s.session_id, "t1", [{"type": "text", "text": "hello"}])
        await sent.wait_for("turn.end", turn_id="t1")
        after = [m for m in sent[before:] if m["type"] == "session.update"]
        assert after and not any(m["replay"] or m["history"] for m in after)
        assert sent.texts(s.session_id)[-1] == "hello"
    finally:
        await p.stop()


async def test_what_cant_be_imported(home, project, tmp_path):
    s = simple(project, ("x", "y"))
    s.write(home)
    p, sent = await started(tmp_path, project)
    try:
        with pytest.raises(CommandError, match="no Claude Code session"):
            await p.import_session(str(uuid.uuid4()))
        sid = (await p.new_session())["session_id"]
        with pytest.raises(CommandError, match="already open"):
            await p.import_session(sid)
        await p.import_session(s.session_id)
        with pytest.raises(CommandError, match="already open"):
            await p.import_session(s.session_id)
    finally:
        await p.stop()
    # Not Claude Code on this machine.
    fake = runner_cfg(tmp_path).harness_commands["claude"]
    q = AgentProcess(agent_here(project, harness="deepseek"), {}, runner_cfg(tmp_path, harness_commands={
        "deepseek": fake}), Sent())
    await q.start()
    try:
        with pytest.raises(CommandError, match="only a Claude Code agent on this machine"):
            await q.import_session(s.session_id)
        with pytest.raises(CommandError, match="only a Claude Code agent on this machine"):
            await q.claude_sessions()
    finally:
        await q.stop()
    # A harness that can't load sessions.
    q, _ = await started(tmp_path, project, "--caps", "resume")
    try:
        with pytest.raises(CommandError, match="can't load sessions"):
            await q.import_session(s.session_id)
    finally:
        await q.stop()


async def test_a_failed_load_leaves_nothing_open(home, project, tmp_path):
    s = simple(project, ("x", "y"))
    s.write(home)
    p, sent = await started(tmp_path, project, "--load-fails")
    try:
        with pytest.raises(CommandError, match="session/load failed"):
            await p.import_session(s.session_id)
        assert s.session_id not in p._sessions and not p._importing and not p._loading
    finally:
        await p.stop()


async def test_a_prompt_while_importing_is_busy(home, project, tmp_path):
    s = simple(project, ("x", "y"))
    s.write(home)
    p, sent = await started(tmp_path, project, "--slow-load")
    try:
        task = asyncio.create_task(p.import_session(s.session_id))
        await asyncio.sleep(0.3)
        with pytest.raises(CommandError, match="busy"):
            await p.prompt(s.session_id, "t1", [{"type": "text", "text": "hi"}])
        assert (await task)["session_id"] == s.session_id
    finally:
        await p.stop()
