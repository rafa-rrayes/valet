"""The web UI's agent computer: the terminal screen (terminal.js: xterm loaded on demand, the socket's frames, typing,
the key bar, tabs, closing), the files screen (files.js: listing, a file's preview, uploads that ask before replacing,
new folders, volumes and deleting them), and attachments (attach.js: picking and pasting, chips, sending, the image
and file blocks in the log). Run by their own JS under node with a stand-in DOM, fake sockets and a fake xterm
(tests/web/computer.js). Skipped without node."""
import json
import subprocess

from tests.web.test_web_settings import HERE, STATIC, agent, pytestmark  # noqa: F401

FILES = "/api/agents/scout/computer/files"
ROOTS = [{"path": "/work", "label": "Workspace", "writable": True},
         {"path": "/home/agent", "label": "Home", "writable": True},
         {"path": "/work/notes", "label": "Shared folder /srv/notes", "writable": False}]
LISTING = {"roots": ROOTS, "path": "/work", "type": "dir", "size": 96, "mtime": 1_700_000_000, "truncated": False,
           "entries": [{"name": "src", "type": "dir", "link": False, "size": 64, "mtime": 1_700_000_000},
                       {"name": "escape", "type": "other", "link": True, "size": 4, "mtime": 1_700_000_000},
                       {"name": "notes.md", "type": "file", "link": False, "size": 2048, "mtime": 1_700_000_000}]}


def run(*jobs: dict) -> list[dict]:
    out = subprocess.run(["node", str(HERE / "computer.js"), str(STATIC)], input=json.dumps(jobs),
                         capture_output=True, text=True, timeout=60, check=False)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def page(hash_: str, actions=(), routes=None, agents=None, **job) -> dict:
    (r,) = run({"hash": hash_, "routes": routes or {}, "actions": list(actions),
                "agents": agents if agents is not None else [agent()], **job})
    return r


def sent(r, method, path=None):
    return [(q["path"], q["body"]) for q in r["requests"] if q["method"] == method
            and (path is None or q["path"].startswith(path))]


# ---------- the terminal ----------

def test_terminal_opens_types_and_shows_output():
    r = page("#/a/scout/terminal", [
        {"socket": 0, "recv": json.dumps({"type": "ready", "terminal": "t1", "agent": "scout"})},
        {"socket": 0, "recv": {"bytes": "agent@box:/work$ "}},
        {"type": "ls\r"}, {"button": "Ctrl"}, {"type": "c"}, {"button": "Esc"}, {"button": "↑"}])
    [ws] = r["sockets"]
    assert ws["url"] == "wss://valet.test/ws/terminal/scout?cols=100&rows=30"     # its size, fitted
    assert ws["sent"] == [{"bytes": "ls\r"}, {"bytes": "\x03"}, {"bytes": "\x1b"}, {"bytes": "\x1b[A"}]
    [t] = r["terms"]
    assert t["writes"] == "agent@box:/work$ " and t["theme"]["background"] == "#ffffff"
    assert "A shell in its container, as its user, in /work." in r["text"]
    assert 'class="tab on"' in r["rows"]["term-tab:1"] and "Shell 1" in r["rows"]["term-tab:1"]
    assert r["rows"]["term-status"].endswith("></div>")         # nothing to say while it's open


def test_terminal_tabs_exit_and_closing():
    r = page("#/a/scout/terminal", [
        {"socket": 0, "recv": json.dumps({"type": "ready", "terminal": "t1", "agent": "scout"})},
        {"button": "New terminal"},
        {"socket": 1, "recv": json.dumps({"type": "ready", "terminal": "t2", "agent": "scout"})},
        {"socket": 1, "recv": json.dumps({"type": "exit", "code": 3, "reason": None})},
        {"socket": 0, "recv": json.dumps({"type": "exit", "code": None, "reason": "the agent stopped"})},
        {"button": "Shell 1"}])
    assert len(r["sockets"]) == 2
    assert 'class="tab on ended"' in r["rows"]["term-tab:1"] and 'class="tab ended"' in r["rows"]["term-tab:2"]
    assert "Closed: the agent stopped." in r["rows"]["term-status"]
    # Closing a tab closes its socket and drops its xterm; leaving the screen closes the rest.
    r = page("#/a/scout/terminal", [
        {"socket": 0, "recv": json.dumps({"type": "ready", "terminal": "t1", "agent": "scout"})},
        {"button": "New terminal"}, {"button": "×", "row": "term-close:1"}, {"go": "#/a/scout"}])
    assert [s["closed"] for s in r["sockets"]] == [1000, 1000]
    assert [t["disposed"] for t in r["terms"]] == [True, True]


def test_terminal_refusals():
    r = page("#/a/scout/terminal", [{"socket": 0, "recv": json.dumps({"type": "error", "message": "scout is not "
                                                                      "running"})}])
    assert "scout is not running" in r["rows"]["term-status"]
    r = page("#/a/scout/terminal", [{"socket": 0, "close": 4401}])
    assert "Sign in again." in r["rows"]["term-status"]
    # Not running: no shell, a way to start it.
    r = page("#/a/scout/terminal", agents=[agent(state="stopped", desired="stopped", status="stopped")])
    assert r["sockets"] == [] and "It isn't running: start it to open a terminal." in r["text"]
    r = page("#/a/scout/terminal", agents=[agent(level="none")])
    assert r["sockets"] == [] and "no terminal" in r["text"]
    # Started meanwhile: a shell opens.
    r = page("#/a/scout/terminal", [{"frame": {"type": "agent", "agent": agent()}}],
             agents=[agent(state="stopped", desired="stopped", status="stopped")])
    assert len(r["sockets"]) == 1


def test_xterm_loads_once_and_only_here():
    r = page("#/a/scout/terminal", xterm=False)
    assert r["head"] == ["/static/vendor/xterm/xterm.css", "/static/vendor/xterm/xterm.js",
                         "/static/vendor/xterm/addon-fit.js"]
    assert r["sockets"] == [] and "Connecting…" in r["rows"]["term-status"]
    actions = "EXTRA.actions.flatMap((fn) => fn(S.agents.get('scout'))).filter(Boolean).map((e) => e.textContent)"
    r = page("#/inbox", xterm=False, eval=actions, routes={"GET /api/inbox": {"status": 200, "body": []}})
    assert r["head"] == [] and r["value"][-2:] == ["Terminal", "Files"]
    r = page("#/inbox", agents=[agent(level="none")], eval=actions)
    assert "Terminal" not in r["value"] and "Files" not in r["value"]


# ---------- files ----------

def files_routes(**over):
    r = {f"GET {FILES}": {"status": 200, "body": LISTING},
         "GET /api/agents/scout/computer/volumes": {"status": 200, "body": {
             "runner": "mac", "running": True, "volumes": [{"name": "valet-agent-scout-home", "mount": "/home/agent"},
                                                           {"name": "valet-agent-scout-work", "mount": "/work"}]}}}
    r.update(over)
    return r


def test_a_folder():
    r = page("#/a/scout/files", routes=files_routes())
    assert r["rows"]["file:src"].startswith('<a class="fentry row" href="#/a/scout/files/work/src"')
    assert "src/" in r["rows"]["file:src"] and "Folder" in r["rows"]["file:src"]
    assert r["rows"]["file:escape"].startswith('<div class="fentry row other"')
    assert "2 KB" in r["rows"]["file:notes.md"]
    assert "WorkspaceHomeShared folder /srv/notes" in r["text"]           # the roots
    assert "valet-agent-scout-home" in r["rows"]["volumes"] and "Stop it first." in r["rows"]["volumes"]
    r = page("#/a/scout/files/work/notes", routes=files_routes(**{f"GET {FILES}?path=%2Fwork%2Fnotes": {
        "status": 200, "body": {**LISTING, "path": "/work/notes", "entries": []}}}))
    assert "Read-only" in r["text"] and "Nothing here yet." in r["text"]
    assert ">Workspace<" not in r["html"].split('class="crumbs"')[1].split("</nav>")[0]   # the deepest root


def test_a_file():
    text = {"path": "/work/notes.md", "size": 2048, "text": "# Notes\n", "binary": False, "truncated": False}
    r = page("#/a/scout/files/work/notes.md", routes=files_routes(**{
        f"GET {FILES}?path=%2Fwork%2Fnotes.md": {"status": 200, "body": {**LISTING, "path": "/work/notes.md",
                                                                       "type": "file", "entries": []}},
        f"GET {FILES}/text?path=%2Fwork%2Fnotes.md": {"status": 200, "body": text}}))
    assert r["rows"]["file-text"] == '<pre class="files-text" data-row="file-text"># Notes\n</pre>'
    assert f'href="{FILES}/download?path=%2Fwork%2Fnotes.md" download="notes.md"' in r["html"]
    r = page("#/a/scout/files/work/pic.png", routes=files_routes(**{
        f"GET {FILES}?path=%2Fwork%2Fpic.png": {"status": 200, "body": {**LISTING, "path": "/work/pic.png",
                                                                      "type": "file", "entries": []}}}))
    assert f'src="{FILES}/download?inline=1&amp;path=%2Fwork%2Fpic.png"' in r["html"]


def test_uploads_ask_before_replacing():
    up = f"POST {FILES}/upload?dir=%2Fwork&name="
    r = page("#/a/scout/files", [{"files": "files-upload", "names": ["new.txt", "notes.md"]},
                                 {"button": "Keep both"}],
             routes=files_routes(**{up + "new.txt&exists=fail": {"status": 201, "body": {}},
                                    up + "notes.md&exists=rename": {"status": 201, "body": {}}}))
    assert sent(r, "POST") == [(f"{FILES}/upload?dir=%2Fwork&name=new.txt&exists=fail", {"file": "new.txt"}),
                               (f"{FILES}/upload?dir=%2Fwork&name=notes.md&exists=rename", {"file": "notes.md"})]
    r = page("#/a/scout/files", [{"files": "files-upload", "names": ["notes.md"]}, {"button": "Cancel"}],
             routes=files_routes())
    assert sent(r, "POST") == []
    r = page("#/a/scout/files", [{"files": "files-upload", "names": ["big.bin"]}],
             routes=files_routes(**{up + "big.bin&exists=fail": {"status": 413, "body": {
                 "detail": "over the 100 MiB limit (VALET_MAX_FILE_MB)"}}}))
    assert r["errors"]["files"] == "big.bin: over the 100 MiB limit (VALET_MAX_FILE_MB)"


def test_new_folder():
    r = page("#/a/scout/files", [{"button": "New folder"}, {"input": "files-new", "value": "drafts"},
                                 {"button": "Create"}],
             routes=files_routes(**{f"POST {FILES}/folder": {"status": 201, "body": {"path": "/work/drafts"}}}))
    assert sent(r, "POST") == [(f"{FILES}/folder", {"path": "/work/drafts"})]
    assert r["hash"] == "#/a/scout/files/work/drafts"


def test_not_running_and_volumes():
    stopped = agent(state="stopped", desired="stopped", status="stopped")
    vols = {"runner": "mac", "running": False, "volumes": [
        {"name": "valet-agent-scout-home", "mount": "/home/agent"}, {"name": "valet-agent-scout-work",
                                                                     "mount": "/work"}]}
    routes = {"GET /api/agents/scout/computer/volumes": [{"status": 200, "body": vols},
                                                         {"status": 200, "body": {**vols, "volumes": []}}],
              "DELETE /api/agents/scout/computer/volumes": {"status": 200, "body": {
                  "removed": ["valet-agent-scout-home", "valet-agent-scout-work"]}}}
    r = page("#/a/scout/files", [{"button": "Delete volumes"}], routes=dict(routes), agents=[stopped])
    assert "start it to look" in r["text"] and not sent(r, "GET", FILES)
    assert ("Delete valet-agent-scout-home (/home/agent) and valet-agent-scout-work (/work)? Everything in its home "
            "and workspace goes, for good.") in r["seen"]
    assert sent(r, "DELETE") == []
    r = page("#/a/scout/files", [{"button": "Delete volumes"}, {"button": "Delete for good"}], routes=dict(routes),
             agents=[stopped])
    assert sent(r, "DELETE") == [("/api/agents/scout/computer/volumes", None)]
    assert "Deleted valet-agent-scout-home and valet-agent-scout-work." in r["rows"]["volumes"]
    assert "It has none on its runner." in r["rows"]["volumes"]
    # A host agent has none to show.
    r = page("#/a/scout/files", routes=files_routes(), agents=[agent(level="host")])
    assert "Volumes" not in r["seen"] and not sent(r, "GET", "/api/agents/scout/computer/volumes")


# ---------- attachments ----------

SESSION = {"agent": "scout", "id": "S1", "title": "t", "state": "idle", "turn_id": None}


def session_routes(**over):
    r = {"GET /api/agents/scout/sessions/S1": {"status": 200, "body": SESSION},
         "GET /api/agents/scout/sessions/S1/events?after=0&limit=500": {"status": 200, "body": {"events": [],
                                                                                                "more": False}},
         "POST /api/agents/scout/attachments?name=pic.png": {"status": 201, "body": {
             "id": "a1", "url": "/api/agents/scout/attachments/a1", "image": True}},
         "POST /api/agents/scout/attachments?name=notes.txt": {"status": 201, "body": {
             "id": "a2", "url": "/api/agents/scout/attachments/a2", "image": False}},
         "POST /api/agents/scout/sessions/S1/prompt": {"status": 202, "body": {"turn_id": "T"}}}
    r.update(over)
    return r


def test_attach_then_send():
    r = page("#/a/scout/s/S1", [{"files": "attach-files", "names": ["pic.png", "notes.txt"],
                                 "types": ["image/png", "text/plain"]},
                                {"submit": True}],
             routes=session_routes())
    posts = sent(r, "POST")
    assert posts[:2] == [("/api/agents/scout/attachments?name=pic.png", {"file": "pic.png"}),
                         ("/api/agents/scout/attachments?name=notes.txt", {"file": "notes.txt"})]
    assert posts[2] == ("/api/agents/scout/sessions/S1/prompt", {"text": "", "attachments": ["a1", "a2"]})
    assert "attachment:0" not in r["rows"]                       # sent: the chips went
    # Before sending: chips, a thumbnail for the image; taking one off deletes it.
    r = page("#/a/scout/s/S1", [{"paste": ["pic.png", "notes.txt"], "types": ["image/png", "text/plain"]},
                                {"button": "×", "row": "attachment:1"}], routes=session_routes())
    assert '<img src="blob:x"' in r["rows"]["attachment:0"] and "attachment:1" not in r["rows"]
    assert sent(r, "DELETE") == [("/api/agents/scout/attachments/a2", None)]


def test_a_failed_upload_holds_the_message():
    r = page("#/a/scout/s/S1", [{"files": "attach-files", "names": ["pic.png"]}, {"submit": True}],
             routes=session_routes(**{"POST /api/agents/scout/attachments?name=pic.png": {
                 "status": 413, "body": {"detail": "over the 100 MiB limit (VALET_MAX_FILE_MB)"}}}))
    assert "over the 100 MiB limit" in r["rows"]["attachment:0"] and "failed" in r["rows"]["attachment:0"]
    assert not [p for p, _ in sent(r, "POST") if p.endswith("/prompt")]
    # The agent couldn't take a file: nothing was sent, the attachments stay.
    r = page("#/a/scout/s/S1", [{"files": "attach-files", "names": ["notes.txt"]}, {"submit": True}],
             routes=session_routes(**{"POST /api/agents/scout/sessions/S1/prompt": {
                 "status": 503, "body": {"detail": "scout is not running", "sent": False}}}))
    assert "attachment:0" in r["rows"] and "scout is not running" in r["seen"]


def test_image_and_file_blocks_in_the_log():
    events = [
        {"id": 1, "agent": "scout", "session_id": "S1", "turn_id": "T", "ts": 0, "kind": "user_message",
         "data": {"content": {"type": "text", "text": "look"}}},
        {"id": 2, "agent": "scout", "session_id": "S1", "turn_id": "T", "ts": 0, "kind": "user_message",
         "data": {"content": {"type": "image", "mimeType": "image/png", "uri": "/api/agents/scout/attachments/a1",
                              "name": "pic.png", "size": 70}}},
        {"id": 3, "agent": "scout", "session_id": "S1", "turn_id": "T", "ts": 0, "kind": "user_message",
         "data": {"content": {"type": "resource_link", "uri": "file:///work/attachments/notes.txt",
                              "name": "notes.txt", "mimeType": "text/plain", "size": 10}}},
        {"id": 4, "agent": "scout", "session_id": "S1", "turn_id": "T", "ts": 0, "kind": "agent_message",
         "data": {"content": {"type": "text", "text": "a red dot"}}}]
    r = page("#/a/scout/s/S1", routes=session_routes(**{
        "GET /api/agents/scout/sessions/S1/events?after=0&limit=500": {"status": 200, "body": {
            "events": events, "more": False}}}))
    assert r["rows"]["att-image"] == ('<a class="att-image" href="/api/agents/scout/attachments/a1" target="_blank" '
                                      'rel="noopener" data-row="att-image"><img src="/api/agents/scout/attachments/a1"'
                                      ' alt="pic.png" loading="lazy"></img></a>')
    assert r["rows"]["att-file"].startswith('<a class="att-file row" href="#/a/scout/files/work/attachments/notes.txt"')
    assert '<div class="msg user">look</div><div class="blocks user">' in r["html"]
    assert "[image]" not in r["text"] and "a red dot" in r["text"]


def test_a_message_with_attachments_while_it_works():
    """Sent while a turn runs, it's queued like any other: the chips go, the queue lists it with its attachments."""
    working = {**SESSION, "state": "working", "queue": [
        {"id": 7, "session_id": "S1", "text": "look", "source": "owner", "label": "You", "retry_of": None,
         "created_at": 0, "attachments": [{"id": "a1", "name": "pic.png", "mime": "image/png", "image": True,
                                           "url": "/api/agents/scout/attachments/a1"}]},
        {"id": 8, "session_id": "S1", "text": "", "source": "owner", "label": "You", "retry_of": None,
         "created_at": 0, "attachments": [{"id": "a2", "name": "notes.txt", "mime": "text/plain", "image": False,
                                           "url": "/api/agents/scout/attachments/a2"}]}]}
    r = page("#/a/scout/s/S1", [{"files": "attach-files", "names": ["pic.png"], "types": ["image/png"]},
                                {"submit": True}],
             routes=session_routes(**{
                 "GET /api/agents/scout/sessions/S1": {"status": 200, "body": working},
                 "POST /api/agents/scout/sessions/S1/prompt": {"status": 202, "body": {"queued": 7,
                                                                                         "session_id": "S1"}}}))
    assert sent(r, "POST")[-1] == ("/api/agents/scout/sessions/S1/prompt", {"text": "", "attachments": ["a1"]})
    assert "attachment:0" not in r["rows"] and "Queued" in r["seen"]
    assert "look + pic.png" in r["rows"]["queued-7"] and "notes.txt" in r["rows"]["queued-8"]
