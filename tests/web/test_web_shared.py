"""The web UI's shared space (shared.js): an agent's Shared section on its page (the list, search, filters, paging, a
new note, uploads, what's new), "N new in Shared" under an agent, the unassigned items (#/shared, and the agents
list's card for them while there are any), and an item's screen (a note's Markdown, a file's preview and download,
editing, replacing, deleting, moving an unassigned one to an agent). Run by their own JS under node with a stand-in
DOM and a fake API (tests/web/settings.js). Skipped without node."""
from tests.web.test_web_settings import agent, detail, pytestmark, run  # noqa: F401

NOTE = {"id": 3, "agent": "scout", "kind": "note", "title": "Done", "mime": None, "size": None, "added_by": "scout",
        "updated_by": "scout", "created_at": 1_700_000_300, "updated_at": 1_700_000_300, "unseen": True,
        "preview": "# Done"}
FILE = {"id": 2, "agent": "scout", "kind": "file", "title": "report.md", "mime": "text/markdown", "size": 2048,
        "added_by": "owner", "updated_by": "scout", "created_at": 1_700_000_100, "updated_at": 1_700_000_200,
        "unseen": True, "preview": "Q3 numbers"}
IMAGE = {"id": 1, "agent": "scout", "kind": "file", "title": "dot.png", "mime": "image/png", "size": 70,
         "added_by": "owner", "updated_by": "owner", "created_at": 1_700_000_000, "updated_at": 1_700_000_000,
         "unseen": False, "preview": ""}
OLD = {**NOTE, "id": 9, "agent": None, "title": "From before", "added_by": "claude", "updated_by": "claude",
       "unseen": False}
LIST = {"items": [NOTE, FILE, IMAGE], "more": False, "unseen": 2}
BASE = "/api/agents/scout/shared"
COUNTS = {"unseen": 2, "agents": {"scout": 2}, "unassigned": 0}
AGENTS = [agent(), agent(name="coder", display_name="Coder")]


def page(hash_="#/a/scout", actions=(), routes=None, list_=LIST, eval_=None) -> dict:
    r = {"GET /api/agents/scout": {"status": 200, "body": detail()},
         "GET /api/agents/scout/sessions": {"status": 200, "body": []},
         f"GET {BASE}?limit=20": {"status": 200, "body": list_},
         "POST /api/shared/seen": {"status": 200, "body": {**COUNTS, "unseen": 0, "agents": {}}}}
    r.update(routes or {})
    job = {"view": "route", "hash": hash_, "routes": r, "actions": list(actions), "agents": AGENTS}
    if eval_:
        job["eval"] = eval_
    (result,) = run(job)
    return result


def sent(r, method, path):
    return [q["body"] for q in r["requests"] if q["method"] == method and q["path"] == path]


def shared_rows(r):
    return [k for k in r["rows"] if k.startswith("shared:")]


# ---------- an agent's Shared section ----------

def test_the_section_on_the_agents_page():
    r = page()
    assert shared_rows(r) == ["shared:3", "shared:2", "shared:1"]
    note, file = r["rows"]["shared:3"], r["rows"]["shared:2"]
    assert 'href="#/shared/3"' in note and 'class="dot on"' in note and "Scout · " in note
    assert "File" in file and "Added by You, changed by Scout · " in file and "2 KB" in file
    assert 'class="dot"' in r["rows"]["shared:1"]
    assert "Notes and files between you and Scout only" in r["seen"]
    # What's new on screen is seen now; its dot stays while the screen is up.
    assert sent(r, "POST", "/api/shared/seen") == [{"ids": [3, 2]}]
    assert "AnyoneYouScout" in r["text"]                 # who added it: you or this agent, never another
    r = page(list_={"items": [], "more": False, "unseen": 0})
    assert "Nothing shared yet. Leave a note or a file here for Scout" in r["text"]
    assert not sent(r, "POST", "/api/shared/seen")


def test_search_filters_and_paging():
    more = {"items": [NOTE], "more": True, "unseen": 0}
    r = page(actions=[{"change": "shared-kind", "value": "file"}, {"change": "shared-by", "value": "scout"},
                      {"button": "Older"}],
             routes={f"GET {BASE}?kind=file&limit=20": {"status": 200, "body": more},
                     f"GET {BASE}?by=scout&kind=file&limit=20": {"status": 200, "body": more},
                     f"GET {BASE}?by=scout&kind=file&limit=20&before=1700000300":
                         {"status": 200, "body": {"items": [IMAGE], "more": False, "unseen": 0}}})
    paths = [q["path"] for q in r["requests"] if q["method"] == "GET" and q["path"].startswith(BASE)]
    assert paths[-3:] == [f"{BASE}?kind=file&limit=20", f"{BASE}?by=scout&kind=file&limit=20",
                          f"{BASE}?by=scout&kind=file&limit=20&before=1700000300"]
    assert shared_rows(r) == ["shared:3", "shared:1"]
    assert "Older" not in r["seen"]


def test_a_new_note_and_an_upload_go_into_the_agents_space():
    r = page(actions=[{"button": "New note"}, {"input": "note-title", "value": "Groceries"},
                      {"input": "note-text", "value": "- milk"}, {"button": "Add note"}],
             routes={f"POST {BASE}/notes": {"status": 201, "body": NOTE}})
    assert sent(r, "POST", f"{BASE}/notes") == [{"title": "Groceries", "text": "- milk"}]
    assert "note-text" not in r["controls"]
    r = page(actions=[{"button": "New note"}, {"button": "Add note"}])
    assert r["errors"]["shared-form"] == "Write the note first."

    r = page(actions=[{"button": "Upload"}, {"files": "upload-files", "names": ["a.txt", "b.pdf"]},
                      {"input": "upload-text", "value": "Two files"}, {"button": "Upload", "within": "shared-new"}],
             routes={f"POST {BASE}/files": {"status": 201, "body": [FILE]}})
    assert sent(r, "POST", f"{BASE}/files") == [{"form": [["file", "a.txt"], ["file", "b.pdf"],
                                                          ["text", "Two files"]]}]
    r = page(actions=[{"button": "Upload"}, {"files": "upload-files", "names": ["big.bin"]},
                      {"button": "Upload", "within": "shared-new"}],
             routes={f"POST {BASE}/files": {"status": 413, "body": {"detail": "over the 100 MiB limit"}}})
    assert r["errors"]["shared-form"] == "over the 100 MiB limit"


def test_frames_keep_only_this_agents_items():
    new = {**NOTE, "id": 4, "title": "Fresh", "updated_at": 1_700_000_400}
    coders = {**NOTE, "id": 5, "agent": "coder", "title": "Coder's"}
    moved = {**IMAGE, "agent": "coder"}
    r = page(actions=[{"frame": {"type": "shared", "item": new, **COUNTS}},
                      {"frame": {"type": "shared", "item": coders, **COUNTS}},
                      {"frame": {"type": "shared", "item": moved, **COUNTS}},
                      {"frame": {"type": "shared", "deleted": 2, "agent": "scout", **COUNTS}}])
    assert shared_rows(r) == ["shared:4", "shared:3"]
    assert sent(r, "POST", "/api/shared/seen") == [{"ids": [3, 2]}, {"ids": [4]}]


# ---------- what's new, under each agent ----------

def test_new_items_show_under_their_agent():
    r = page("#/", actions=[{"frame": {"type": "hello", "agents": AGENTS, "runners": [], "inbox": []}}],
             routes={"GET /api/shared/unseen": {"status": 200, "body": COUNTS}})
    assert ("GET", "/api/shared/unseen") in [(q["method"], q["path"]) for q in r["requests"]]
    assert ">2 new in Shared<" in r["rows"]["shared-new:scout"] and 'href="#/a/scout"' in r["rows"]["shared-new:scout"]
    assert "2 new in Shared" in r["seen"] and "0 new in Shared" not in r["seen"]      # none for coder
    # Frames move the counts, wherever you are; no header tab any more.
    r = page("#/", actions=[{"frame": {"type": "shared", "item": NOTE, "unseen": 1, "agents": {"coder": 1},
                                       "unassigned": 0}}],
             eval_="[SHARED.agents, EXTRA.tabs.some((t) => t.key === 'shared')]")
    assert ">1 new in Shared<" in r["rows"]["shared-new:coder"] and "2 new" not in r["seen"]
    assert r["seen"].count("new in Shared") == 1
    assert r["value"] == [{"coder": 1}, False]


def test_a_deleted_agents_counts_go_with_it():
    # Its items went with it and no shared frame says so: the counts are fetched again.
    after = {"unseen": 0, "agents": {}, "unassigned": 0}
    r = page("#/", actions=[{"frame": {"type": "hello", "agents": AGENTS, "runners": [], "inbox": []}},
                            {"frame": {"type": "agent_deleted", "agent": "coder"}}],
             routes={"GET /api/shared/unseen": {"status": 200, "body": after}}, eval_="SHARED.agents")
    assert [q["path"] for q in r["requests"] if q["path"] == "/api/shared/unseen"] == ["/api/shared/unseen"] * 2
    assert r["value"] == {} and "new in Shared" not in r["seen"]


# ---------- the unassigned ----------

def test_the_unassigned_card_and_list():
    r = page("#/")
    assert "Shared items with no agent" not in r["seen"]
    r = page("#/", actions=[{"frame": {"type": "shared", "seen": "all", "unseen": 0, "agents": {},
                                       "unassigned": 2}}])
    assert 'href="#/shared"' in r["rows"]["shared-unassigned"]
    assert "Shared items with no agent2 from when Shared was one list" in r["seen"]

    r = page("#/shared", routes={"GET /api/shared/unassigned?limit=20": {"status": 200, "body": {
        "items": [OLD], "more": False, "unseen": 0}}})
    assert shared_rows(r) == ["shared:9"] and "Shared items with no agent" in r["text"]
    assert "New note" not in r["text"] and "shared-by" not in r["controls"]      # nothing new goes there
    r = page("#/shared", routes={"GET /api/shared/unassigned?limit=20": {"status": 200, "body": {
        "items": [], "more": False, "unseen": 0}}})
    assert "every shared item is with its agent" in r["text"]


def test_moving_an_unassigned_item_to_an_agent():
    r = page("#/shared/9", actions=[{"change": "shared-move", "value": "coder"}, {"button": "Move"}],
             routes={"GET /api/shared/9": {"status": 200, "body": {**OLD, "text": "x"}},
                     "POST /api/shared/9/move": {"status": 200, "body": {**OLD, "agent": "coder", "text": "x"}}})
    assert sent(r, "POST", "/api/shared/9/move") == [{"agent": "coder"}]
    assert "‹ Coder" in r["text"] and "Move it to an agent" not in r["text"]
    r = page("#/shared/9", routes={"GET /api/shared/9": {"status": 200, "body": {**OLD, "text": "x"}}})
    assert "‹ Shared items with no agent" in r["text"] and "Move it to an agent" in r["text"]
    r = page("#/shared/3", routes={"GET /api/shared/3": {"status": 200, "body": {**NOTE, "text": "x"}}})
    assert "Move it to an agent" not in r["text"] and "‹ Scout" in r["text"]


# ---------- one item ----------

def test_a_note():
    r = page("#/shared/3", routes={"GET /api/shared/3": {"status": 200, "body": {**NOTE, "text": "# Done\n\n**All** green."}}})
    assert "<strong>All</strong>" in r["rows"]["shared-text"]
    assert sent(r, "POST", "/api/shared/seen") == [{"ids": [3]}]
    assert "Note · Scout · " in r["text"]

    r = page("#/shared/3", actions=[{"button": "Edit"}, {"input": "shared-text", "value": "Still green."},
                                    {"button": "Save"}],
             routes={"GET /api/shared/3": {"status": 200, "body": {**NOTE, "text": "# Done"}},
                     "PATCH /api/shared/3": {"status": 200, "body": {**NOTE, "text": "Still green."}}})
    assert sent(r, "PATCH", "/api/shared/3") == [{"text": "Still green."}]

    r = page("#/shared/3", actions=[{"button": "Delete"}, {"button": "Delete", "within": "confirm"}],
             routes={"GET /api/shared/3": {"status": 200, "body": {**NOTE, "text": "x"}},
                     "DELETE /api/shared/3": {"status": 200, "body": {"ok": True}}})
    assert sent(r, "DELETE", "/api/shared/3") == [None] and r["hash"] == "#/a/scout"     # back to its agent


def test_files():
    r = page("#/shared/2", routes={"GET /api/shared/2": {"status": 200, "body": {**FILE, "text": "Q3 numbers"}},
                                   "GET /api/shared/2/file?inline=1": {"status": 200, "body": "# Q3\n\n| a | b |"}})
    assert r["rows"]["shared-preview"].startswith('<div class="md" data-row="shared-preview"><h1>Q3</h1>')
    assert 'href="/api/shared/2/file" download="report.md"' in r["html"]
    assert "text/markdown · 2 KB · Added by You, changed by Scout" in r["text"]
    img = page("#/shared/1", routes={"GET /api/shared/1": {"status": 200, "body": {**IMAGE, "text": ""}}})
    assert '<img class="shared-img" src="/api/shared/1/file?inline=1&amp;v=1700000000" alt="dot.png">' in img["html"]
    assert not sent(img, "POST", "/api/shared/seen")

    r = page("#/shared/1", actions=[{"button": "Replace file"}, {"files": "replace-file", "names": ["dot2.png"]},
                                    {"button": "Replace"}],
             routes={"GET /api/shared/1": {"status": 200, "body": {**IMAGE, "text": ""}},
                     "PUT /api/shared/1/file": {"status": 200, "body": {**IMAGE, "title": "dot2.png", "text": ""}}})
    assert sent(r, "PUT", "/api/shared/1/file") == [{"form": [["file", "dot2.png"]]}]
    assert "dot2.png" in r["text"]


def test_an_item_thats_gone():
    r = page("#/shared/9", routes={"GET /api/shared/9": {"status": 404, "body": {"detail": "no"}}})
    assert "This item was deleted." in r["text"]
    r = page("#/shared/3", actions=[{"frame": {"type": "shared", "deleted": 3, "agent": "scout", **COUNTS}}],
             routes={"GET /api/shared/3": {"status": 200, "body": {**NOTE, "text": "x"}}})
    assert "This item was deleted." in r["text"]
