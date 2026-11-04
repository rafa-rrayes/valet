"""The web UI for continuing and handing off conversations (static/handoff.js) and the session log's folded hand-off
message, run by their own JS under node with a stand-in DOM. Skipped without node."""
from tests.web.test_web_render import render
from tests.web.test_web_settings import agent, pytestmark, run  # noqa: F401

SID = "11111111-2222-3333-4444-555555555555"
SESSION = {"id": SID, "title": "Login bug", "cwd": "/srv/app", "updated_at": 1_700_000_000, "size": 2048,
           "imported": None}
HOST = agent(name="coder", level="host")


def route(hash_: str, routes: dict, actions=(), agents=None) -> dict:
    (result,) = run({"view": "route", "hash": hash_, "routes": routes, "actions": list(actions),
                     "agents": agents if agents is not None else [HOST, agent()]})
    return result


def ok(body, status=200):
    return {"status": status, "body": body}


def test_agent_page_offers_continue_only_to_a_host_claude_agent():
    routes = {"GET /api/agents/coder": ok(HOST), "GET /api/agents/coder/sessions": ok([]),
              "GET /api/agents/scout": ok(agent()), "GET /api/agents/scout/sessions": ok([])}
    host = route("#/a/coder", routes)
    assert 'href="#/a/coder/continue"' in host["html"] and 'href="#/a/coder/handoff"' in host["html"]
    boxed = route("#/a/scout", routes)
    assert "#/a/scout/continue" not in boxed["html"] and 'href="#/a/scout/handoff"' in boxed["html"]


def test_continue_lists_and_imports():
    listing = {"cwd": "/srv/app", "runner": "mac", "sessions": [
        SESSION, {**SESSION, "id": "99999999-2222-3333-4444-555555555555", "title": "Older", "imported": "scout"}]}
    routes = {"GET /api/agents/coder/claude-sessions": ok(listing),
              f"POST /api/agents/coder/claude-sessions/{SID}/import": ok({"id": SID}, 201)}
    r = route("#/a/coder/continue", routes)
    assert "Sessions Claude Code ran in /srv/app" in r["text"] and "Login bug" in r["text"] and "2 KB" in r["text"]
    assert "With Scout" in r["text"]            # already in Valet (its display name): a link to it, no button
    r = route("#/a/coder/continue", routes, [{"button": "Continue here"}])
    assert [q["path"] for q in r["requests"] if q["method"] == "POST"] == [f"/api/agents/coder/claude-sessions/{SID}/import"]
    assert r["hash"] == f"#/a/coder/s/{SID}"


def test_continue_explains_what_it_cant_do():
    boxed = route("#/a/scout/continue", {})
    assert "Only a Claude Code agent on this machine can continue" in boxed["text"]
    assert not any(q["path"].endswith("/claude-sessions") for q in boxed["requests"])
    down = route("#/a/coder/continue", {"GET /api/agents/coder/claude-sessions": ok({"detail": "x"}, 503)})
    assert "isn't running: start it" in down["text"]
    failed = route("#/a/coder/continue", {
        "GET /api/agents/coder/claude-sessions": ok({"cwd": "/srv/app", "sessions": [SESSION]}),
        f"POST /api/agents/coder/claude-sessions/{SID}/import": ok({"detail": "Valet already has this session (x)"}, 409)},
        [{"button": "Continue here"}])
    assert "Valet already has this session (x)" in failed["text"] and failed["hash"] == "#/a/coder/continue"


def test_hand_off_a_claude_code_session():
    folder = "-srv-app"
    preview = {"text": "[Valet hand-off] What follows...", "bytes": 3000, "entries": 4, "omitted": 2,
               "title": "Login bug", "source": "a Claude Code session"}
    routes = {"GET /api/claude/folders": ok({"runner": "mac", "folders": [
                  {"folder": folder, "cwd": "/srv/app", "sessions": 3, "updated_at": 1_700_000_000}]}),
              f"GET /api/claude/sessions?folder={folder}": ok({"runner": "mac", "folder": folder, "sessions": [SESSION]}),
              "POST /api/handoff/preview": ok(preview),
              "POST /api/agents/scout/handoff": ok({"session": {"id": "new-1"}, "turn_id": "t"}, 201)}
    r = route("#/a/scout/handoff", routes)
    assert "Claude Code's project folders on mac" in r["text"] and "/srv/app" in r["text"] and "3 sessions" in r["text"]
    picked = [{"click": "folder:" + folder}, {"button": "Use this", "row": "claude:" + SID}]
    r = route("#/a/scout/handoff", routes, picked)
    assert "4 entries, 3 KB; 2 earlier entries didn't fit." in r["text"]
    r = route("#/a/scout/handoff", routes, [*picked, {"input": "note", "value": "Now run the tests."},
                                            {"button": "Start the session"}])
    source = {"kind": "claude", "folder": folder, "session_id": SID, "runner": "mac"}
    posts = [(q["path"], q["body"]) for q in r["requests"] if q["method"] == "POST"]
    assert posts == [("/api/handoff/preview", {"source": source}),
                     ("/api/agents/scout/handoff", {"source": source, "note": "Now run the tests."})]
    assert r["hash"] == "#/a/scout/s/new-1"


def test_hand_off_a_valet_session():
    routes = {"GET /api/agents/coder/sessions": ok([{"id": "s1", "title": "Earlier chat", "updated_at": 1_700_000_000}]),
              "POST /api/handoff/preview": ok({"text": "[Valet hand-off] ...", "bytes": 500, "entries": 2,
                                               "omitted": 0, "title": "Earlier chat", "source": "x"}),
              "POST /api/agents/scout/handoff": ok({"session": {"id": "new-1"}, "turn_id": "t"}, 201)}
    r = route("#/a/scout/handoff", routes, [
        {"click": "from:session"}, {"click": "agent:coder"}, {"button": "Use this", "row": "session:s1"},
        {"input": "note", "value": "carry on"}, {"button": "Start the session"}])
    posts = [(q["path"], q["body"]) for q in r["requests"] if q["method"] == "POST"]
    source = {"kind": "session", "agent": "coder", "session_id": "s1"}
    assert posts == [("/api/handoff/preview", {"source": source}),
                     ("/api/agents/scout/handoff", {"source": source, "note": "carry on"})]
    assert r["hash"] == "#/a/scout/s/new-1"


def test_a_hand_off_message_is_folded():
    text = "[Valet hand-off] What follows is an earlier conversation...\n\nNow run the tests."
    events = [{"id": 1, "kind": "user_message", "turn_id": "t1", "data": {
                  "content": {"type": "text", "text": text},
                  "handoff": {"source": "a Claude Code session", "entries": 4, "omitted": 0, "note": "Now run the tests."}}},
              {"id": 2, "kind": "agent_message", "turn_id": "t1", "data": {"content": {"type": "text", "text": "On it."}}}]
    (r,) = render({"events": events})
    assert '<details class="thought handoff"><summary>Handed off: a Claude Code session</summary>' in r["html"]
    assert r["html"].count("Now run the tests.") == 2      # inside the fold, and the note on its own
    assert "On it." in r["html"]
