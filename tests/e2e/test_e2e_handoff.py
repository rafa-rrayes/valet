"""Continuing and handing off conversations end to end: the real control plane and runner, the fake ACP agent as a
host Claude Code agent, and fake Claude Code transcripts under a temporary HOME (the runner's, and the agent's)."""
import pytest

from tests.claude_transcripts import simple
from tests.e2e.stack import eventually, kinds, texts
from valet.shared.transcripts import folder_name

AGENT = """\
name: {name}
harness: claude
computer:
  level: {level}
{workdir}permissions: ask
directives: [persona.md]
"""


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


async def make(stack, name: str, level: str = "host", workdir: str | None = None, start: bool = True) -> None:
    yaml = AGENT.format(name=name, level=level, workdir=f"  workdir: {workdir}\n" if workdir else "")
    r = await stack.http.post("/api/agents", json={"name": name, "agent_yaml": yaml})
    assert r.status_code == 201, r.text
    if start:
        await stack.start_agent(name)


def transcript(home, project):
    s = simple(project, ("fix the login bug", "Found it in auth.py."), ("now add a test", "Added test_login."))
    s.line("ai-title", aiTitle="Login bug")
    s.write(home)
    return s


async def test_continue_a_claude_code_session(stack, home, project):
    s = transcript(home, project)
    await make(stack, "coder", workdir=project)
    feed = await stack.feed()
    r = await stack.http.get("/api/agents/coder/claude-sessions")
    assert r.status_code == 200, r.text
    listed = r.json()
    assert listed["cwd"] == project and listed["runner"] == "testrunner"
    [item] = listed["sessions"]
    assert (item["id"], item["title"], item["imported"]) == (s.session_id, "Login bug", None)

    r = await stack.http.post(f"/api/agents/coder/claude-sessions/{s.session_id}/import")
    assert r.status_code == 201, r.text
    session = r.json()
    assert (session["id"], session["title"], session["state"], session["turn_id"]) == (
        s.session_id, "Login bug", "idle", None)
    await feed.wait(lambda f: f["type"] == "session" and f["session"]["id"] == s.session_id, what="session frame")

    evs = await stack.events("coder", s.session_id)
    assert kinds(evs) == ["user_message", "agent_message", "turn_end", "user_message", "agent_message",
                          "session_info", "turn_end"]       # the harness's title comes with the history
    first, second = evs[0]["turn_id"], evs[3]["turn_id"]
    assert first and second and first != second and {e["turn_id"] for e in evs} == {first, second}
    assert texts(evs, "user_message") == "fix the login bugnow add a test"
    assert texts(evs) == "Found it in auth.py.Added test_login."
    assert [e["data"]["stopReason"] for e in evs if e["kind"] == "turn_end"] == ["end_turn", "end_turn"]

    # Listed as Valet's now, and not imported twice (into any agent).
    r = await stack.http.get("/api/agents/coder/claude-sessions")
    assert r.json()["sessions"][0]["imported"] == "coder"
    r = await stack.http.post(f"/api/agents/coder/claude-sessions/{s.session_id}/import")
    assert r.status_code == 409 and "already has this session (coder)" in r.json()["detail"]

    # It goes on: a prompt runs in it like in any session.
    turn = await stack.prompt("coder", s.session_id, "hello again")
    assert texts(await stack.turn("coder", s.session_id, turn)) == "echo: hello again"


async def test_what_cant_be_continued(stack, home, project):
    s = transcript(home, project)
    await make(stack, "boxed", level="container", start=False)
    r = await stack.http.get("/api/agents/boxed/claude-sessions")
    assert r.status_code == 422 and "only a Claude Code agent on this machine" in r.json()["detail"]
    r = await stack.http.post(f"/api/agents/boxed/claude-sessions/{s.session_id}/import")
    assert r.status_code == 422

    await make(stack, "idle", workdir=project, start=False)
    r = await stack.http.get("/api/agents/idle/claude-sessions")
    assert r.status_code == 503

    await make(stack, "coder", workdir=project)
    r = await stack.http.post("/api/agents/coder/claude-sessions/00000000-0000-0000-0000-000000000000/import")
    assert r.status_code == 404 and "no Claude Code session" in r.json()["detail"]
    r = await stack.http.post("/api/agents/coder/claude-sessions/not-an-id/import")
    assert r.status_code == 404
    # Nothing was left behind by the refused import.
    r = await stack.http.get("/api/agents/coder/sessions")
    assert r.json() == []

    # An agent working elsewhere doesn't see the project's sessions.
    await make(stack, "other")
    r = await stack.http.get("/api/agents/other/claude-sessions")
    assert r.status_code == 200 and r.json()["sessions"] == []


async def test_hand_off_a_claude_code_session(stack, home, project):
    s = transcript(home, project)
    await make(stack, "helper", level="host")

    r = await stack.http.get("/api/claude/folders")
    assert r.status_code == 200, r.text
    assert [(f["folder"], f["cwd"], f["sessions"]) for f in r.json()["folders"]] == [(folder_name(project), project, 1)]
    r = await stack.http.get("/api/claude/sessions", params={"folder": folder_name(project)})
    assert [x["id"] for x in r.json()["sessions"]] == [s.session_id]
    assert (await stack.http.get("/api/claude/sessions", params={"folder": "../etc"})).status_code == 404
    assert (await stack.http.get("/api/claude/folders", params={"runner": "nope"})).status_code == 503

    source = {"kind": "claude", "folder": folder_name(project), "session_id": s.session_id}
    r = await stack.http.post("/api/handoff/preview", json={"source": source})
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["entries"] == 4 and p["omitted"] == 0 and p["title"] == "Login bug"
    assert p["text"].startswith('[Valet hand-off] What follows is an earlier conversation, a Claude Code session '
                                f'("Login bug", {s.session_id}) that ran in {project} on runner testrunner')
    assert "[User]\nfix the login bug\n\n[Assistant]\nFound it in auth.py." in p["text"]
    assert p["bytes"] == len(p["text"].encode())

    r = await stack.http.post("/api/agents/helper/handoff", json={"source": source, "note": "Now run the tests."})
    assert r.status_code == 201, r.text
    sid, turn = r.json()["session"]["id"], r.json()["turn_id"]
    assert r.json()["session"]["title"] == "Continued: Login bug"
    evs = await stack.turn("helper", sid, turn)
    first = evs[0]
    assert first["kind"] == "user_message"
    assert first["data"]["content"]["text"].endswith("--- End of the earlier conversation. ---\n\nNow run the tests.")
    assert first["data"]["handoff"] == {"source": p["source"], "entries": 4, "omitted": 0, "note": "Now run the tests."}
    assert next(e for e in evs if e["kind"] == "turn_end")["data"]["error"] is None


async def test_hand_off_a_valet_session(stack, home):
    await make(stack, "first")
    await make(stack, "second")
    sid = (await stack.new_session("first"))["id"]
    await stack.turn("first", sid, await stack.prompt("first", sid, "remember the number 42"))
    source = {"kind": "session", "agent": "first", "session_id": sid}
    p = (await stack.http.post("/api/handoff/preview", json={"source": source})).json()
    assert 'the Valet session "remember the number 42" with agent first' in p["text"]
    assert "[User]\nremember the number 42\n\n[Assistant]\necho: remember the number 42" in p["text"]
    r = await stack.http.post("/api/agents/second/handoff", json={"source": source})
    assert r.status_code == 201, r.text
    evs = await stack.turn("second", r.json()["session"]["id"], r.json()["turn_id"])
    assert "remember the number 42" in evs[0]["data"]["content"]["text"]

    # Nothing to hand off, an unknown session, a stopped agent.
    empty = (await stack.new_session("first"))["id"]
    r = await stack.http.post("/api/handoff/preview", json={"source": {**source, "session_id": empty}})
    assert r.status_code == 422
    r = await stack.http.post("/api/handoff/preview", json={"source": {**source, "session_id": "nope"}})
    assert r.status_code == 404
    await stack.http.post("/api/agents/second/stop")

    async def stopped():
        return (await stack.agent("second"))["state"] == "stopped"
    await eventually(stopped, what="second stopped")
    r = await stack.http.post("/api/agents/second/handoff", json={"source": source})
    assert r.status_code == 503
