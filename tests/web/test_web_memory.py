"""The web UI's memory (memory.js): Settings › Memory (the switch over `memory` and its layer, the files, a new file)
and a memory file's screen (Markdown, editing, deleting, history and older versions). Run by their own JS under node
with a stand-in DOM and a fake API (tests/web/settings.js). Skipped without node."""
import copy

from tests.web.test_web_settings import CATALOG, DEFINITION, agent, detail, pytestmark, run  # noqa: F401

FILES = [{"name": "notes", "title": "Notes", "size": 2048, "updated_at": 1_700_000_000},
         {"name": "people", "title": "People", "size": 120, "updated_at": 1_700_000_100}]
LIMITS = {"file": 65536, "total": 1048576, "files": 200, "layer": 32768}
MEMORY = {"agent": "scout", "enabled": True, "layer": True, "files": FILES, "size": 2168, "limits": LIMITS}
SERVERS = {"servers": [
    {"name": "valet.agents", "kind": "builtin", "builtin": True, "description": "Valet's agents.", "comes_with": None,
     "agents": [], "connected": True},
    {"name": "valet.memory", "kind": "builtin", "builtin": True, "description": "Your memory.", "comes_with": "memory",
     "agents": ["scout"], "connected": True}], "error": None}


def definition(**over) -> dict:
    d = copy.deepcopy(DEFINITION)
    d.update(over)
    return d


def settings(actions=(), d=None, routes=None) -> dict:
    r = {"GET /api/agents/scout": {"status": 200, "body": detail(d)},
         "GET /api/catalog": {"status": 200, "body": CATALOG},
         "GET /api/agents/scout/history": {"status": 200, "body": []},
         "GET /api/agents/scout/memory": {"status": 200, "body": MEMORY},
         "GET /api/gateway/servers": {"status": 200, "body": SERVERS},
         "PUT /api/agents/scout/definition": {"status": 200, "body": detail(d)}}
    r.update(routes or {})
    (result,) = run({"view": "settings", "name": "scout", "routes": r, "actions": list(actions), "agents": [agent()]})
    return result


def screen(hash_="#/a/scout/memory/notes", actions=(), routes=None) -> dict:
    r = {"GET /api/agents/scout/memory/notes": {"status": 200, "body": {
        "name": "notes", "commit": None, "file": FILES[0], "text": "# Notes\n\nAlice likes **tea**.\n"}}}
    r.update(routes or {})
    (result,) = run({"view": "route", "hash": hash_, "routes": r, "actions": list(actions), "agents": [agent()]})
    return result


def sent(r, method, path):
    return [q["body"] for q in r["requests"] if q["method"] == method and q["path"] == path]


# ---------- Settings › Memory ----------

def test_the_section_lists_the_files():
    r = settings()
    assert r["controls"]["memory"]["checked"] is True
    assert "Memory" in r["text"] and "2 files, 2.1 KiB of 1 MiB" in r["text"]
    row = r["rows"]["memory:notes"]
    assert 'href="#/a/scout/memory/notes"' in row and "Notes · 2.0 KiB · updated" in row
    assert "memory:people" in r["rows"]
    assert r["text"].index("What it knows") < r["text"].index("2 files") < r["text"].index("Brain")
    # valet.memory comes with Memory: Tools doesn't offer it.
    assert "mcp:valet.memory" not in r["controls"] and "mcp:valet.agents" in r["controls"]


def test_switching_memory_off_and_on_saves_both():
    r = settings([{"check": "memory", "checked": False}, {"button": "Save"}])
    [body] = sent(r, "PUT", "/api/agents/scout/definition")
    assert body["definition"]["memory"] is False and "memory" not in body["definition"]["directives"]
    r = settings([{"check": "memory", "checked": False}])
    assert "Off: no memory tools" in r["rows"]["memory"] and "Unsaved changes" in r["seen"]

    off = definition(memory=False, directives=["owner", "persona.md", "environment"])
    r = settings([{"check": "memory", "checked": True}, {"button": "Save"}], d=off)
    [body] = sent(r, "PUT", "/api/agents/scout/definition")
    assert body["definition"]["memory"] is True
    assert body["definition"]["directives"] == ["owner", "persona.md", "environment", "memory"]


def test_on_without_its_layer_offers_to_tell_it():
    r = settings([{"button": "Tell it", "within": "memory"}, {"button": "Save"}],
                 d=definition(directives=["persona.md"]))
    assert sent(r, "PUT", "/api/agents/scout/definition")[0]["definition"]["directives"] == ["persona.md", "memory"]


def test_a_new_file():
    r = settings([{"button": "New file"}, {"input": "memory-new", "value": "Project Notes.md"}, {"button": "Create"}])
    assert r["hash"] == "#/a/scout/memory/project-notes"
    r = settings([{"button": "New file"}, {"input": "memory-new", "value": "notes"}, {"button": "Create"}])
    assert r["errors"]["memory-new"] == "There's already a file named notes." and r["hash"] == ""
    r = settings([{"button": "New file"}, {"input": "memory-new", "value": "!!"}, {"button": "Create"}])
    assert "Lowercase letters and digits" in r["errors"]["memory-new"]


def test_frames_keep_the_list_current():
    new = {"name": "plans", "title": "Plans", "size": 10, "updated_at": 1_700_000_200}
    r = settings([{"frame": {"type": "memory", "agent": "scout", "name": "plans", "file": new}},
                  {"frame": {"type": "memory", "agent": "scout", "name": "people", "file": None}},
                  {"frame": {"type": "memory", "agent": "other", "name": "notes", "file": None}}])
    assert "memory:plans" in r["rows"] and "memory:people" not in r["rows"] and "memory:notes" in r["rows"]
    assert "2 files, 2.0 KiB" in r["text"]


def test_the_section_when_memory_cant_load():
    r = settings(routes={"GET /api/agents/scout/memory": {"status": 500, "body": {"detail": "disk on fire"}}})
    assert "disk on fire" in r["text"] and r["controls"]["memory"]["checked"] is True


# ---------- a memory file ----------

def test_a_file_shows_as_markdown():
    r = screen()
    assert r["rows"]["memory-text"].startswith('<div class="card md mem-md" data-row="memory-text"><h1>Notes</h1>')
    assert "<strong>tea</strong>" in r["rows"]["memory-text"]
    assert "notes.md" in r["text"] and "Notes · 2.0 KiB · updated" in r["text"]
    assert 'href="#/a/scout/settings"' in r["html"]


def test_edit_and_save():
    put = {"status": 200, "body": {"file": {**FILES[0], "size": 9}}}
    r = screen(actions=[{"button": "Edit"}, {"input": "memory-text", "value": "# Notes\n\nCoffee."},
                        {"button": "Save"}], routes={"PUT /api/agents/scout/memory/notes": put})
    assert sent(r, "PUT", "/api/agents/scout/memory/notes") == [{"text": "# Notes\n\nCoffee."}]
    assert "memory-text" not in r["controls"]           # back to reading it
    r = screen(actions=[{"button": "Edit"}, {"input": "memory-text", "value": "  "}, {"button": "Save"}])
    assert "Write something first" in r["errors"]["memory"]
    r = screen(actions=[{"button": "Edit"}, {"input": "memory-text", "value": "x"}, {"button": "Save"}],
               routes={"PUT /api/agents/scout/memory/notes": {"status": 422, "body": {"detail": "too big"}}})
    assert r["errors"]["memory"] == "too big" and r["controls"]["memory-text"]["value"] == "x"


def test_a_new_file_opens_the_editor():
    r = screen("#/a/scout/memory/plans", actions=[{"input": "memory-text", "value": "# Plans"}, {"button": "Create"}],
               routes={"GET /api/agents/scout/memory/plans": {"status": 404, "body": {"detail": "no"}},
                       "PUT /api/agents/scout/memory/plans": {"status": 200, "body": {"file": FILES[0]}}})
    assert sent(r, "PUT", "/api/agents/scout/memory/plans") == [{"text": "# Plans"}]
    assert "A new memory file" not in r["text"]
    bad = screen("#/a/scout/memory/Bad_Name")
    assert "isn't a memory file's name" in bad["text"] and not bad["requests"]


def test_delete_asks_then_goes_back():
    r = screen(actions=[{"button": "Delete"}, {"button": "Delete", "within": "confirm"}],
               routes={"DELETE /api/agents/scout/memory/notes": {"status": 200, "body": {"ok": True}}})
    assert sent(r, "DELETE", "/api/agents/scout/memory/notes") == [None]
    assert r["hash"] == "#/a/scout/settings"


def test_history_and_an_older_version():
    hist = {"name": "notes", "history": [
        {"commit": "c" * 40, "message": "Memory: scout wrote notes.md", "at": 1_700_000_100},
        {"commit": "b" * 40, "message": "Memory: owner wrote scout's notes.md", "at": 1_700_000_000}]}
    r = screen(actions=[{"open": "mem-history"}, {"button": "View", "row": "mem-commit:" + "b" * 40},
                        {"button": "Restore this version"}],
               routes={"GET /api/agents/scout/memory/notes/history": {"status": 200, "body": hist},
                       f"GET /api/agents/scout/memory/notes?at={'b' * 40}": {"status": 200, "body": {
                           "name": "notes", "commit": "b" * 40, "file": None, "text": "# Old notes\n"}}})
    assert r["controls"]["memory-text"]["value"] == "# Old notes\n"
    assert "Restored into the editor: Save to keep it." in r["text"]


def test_unsaved_edits_ask_before_leaving():
    r = screen(actions=[{"button": "Edit"}, {"input": "memory-text", "value": "changed"}, {"go": "#/"}])
    assert "You have unsaved changes to this file." in r["seen"] and r["hash"] == "#/a/scout/memory/notes"
    r = screen(actions=[{"button": "Edit"}, {"input": "memory-text", "value": "changed"}, {"go": "#/"},
                        {"button": "Discard & leave"}])
    assert r["hash"] == "#/"


def test_a_change_elsewhere():
    frame = {"type": "memory", "agent": "scout", "name": "notes", "file": FILES[0]}
    r = screen(actions=[{"frame": frame}])
    assert len([q for q in r["requests"] if q["path"] == "/api/agents/scout/memory/notes"]) == 2   # reloaded
    r = screen(actions=[{"button": "Edit"}, {"frame": frame}])
    assert "changed elsewhere since you started editing" in r["text"]
    r = screen(actions=[{"frame": {**frame, "file": None}}])
    assert "It was just deleted." in r["text"]
