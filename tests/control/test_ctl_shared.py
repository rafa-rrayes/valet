"""The shared space from the control plane's side: the owner's API (notes, uploads raw and multipart within the size
limit, downloads and their headers, edits, replacing a file, deletes, the list's search, filters and paging, what's
unseen) and `shared` frames; valet.shared through the gateway endpoint (decisions per level, inline transfer both
ways, transfer tickets and what ends them, the audit log)."""
import base64
import json
import time

import pytest

from valet.control.gateway import builtin_shared
from valet.control.space import preview
from valet.shared.config import ControlConfig

YAML = "name: {name}\nharness: claude\ncomputer:\n  level: {computer}\npermissions: {level}\ntools:\n  mcp: [valet.shared]\n"
ACCEPT = {"Accept": "application/json, text/event-stream"}
MIB = 1024 * 1024
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
LIST, NOTES, FILES = "/api/agents/scout/shared", "/api/agents/scout/shared/notes", "/api/agents/scout/shared/files"
NONE = {"unseen": 0, "agents": {}, "unassigned": 0}     # the counts with nothing unseen and nothing unassigned


@pytest.fixture
def cfg(tmp_path) -> ControlConfig:
    return ControlConfig(data=tmp_path / "data", definitions=tmp_path / "data" / "definitions", rpc_timeout=5.0,
                         max_file_mb=1)


@pytest.fixture
def client(client):
    """Every test here has scout (not started yet): the owner's notes and uploads go into its space."""
    make_agent(client)
    return client


def make_agent(client, name="scout", level="ask", computer="host"):
    r = client.post("/api/agents", json={"name": name, "agent_yaml": YAML.format(name=name, level=level,
                                                                                 computer=computer)})
    assert r.status_code == 201, r.text


def space(client):
    return client.app.state.control.shared


def upload(client, name, data: bytes, agent="scout", **params):
    return client.post(f"/api/agents/{agent}/shared/files", params={"name": name, **params}, content=data)


# ---------- the owner's API ----------

def test_notes_and_frames(client):
    with client.websocket_connect("/ws/client") as ws:
        assert ws.receive_json()["type"] == "hello"
        r = client.post(NOTES, json={"text": "# Groceries\n\n- milk\n- coffee"})
        assert r.status_code == 201, r.text
        note = r.json()
        assert (note["kind"], note["title"], note["added_by"], note["unseen"]) == ("note", "Groceries", "owner", False)
        assert note["agent"] == "scout"
        assert note["text"] == "# Groceries\n\n- milk\n- coffee" and note["preview"] == "milk"     # past the title
        frame = ws.receive_json()
        assert frame["type"] == "shared" and frame["item"]["id"] == note["id"] and frame["unseen"] == 0
        assert "text" not in frame["item"]

        r = client.patch(f"/api/shared/{note['id']}", json={"title": "Shopping", "text": "- tea"})
        assert r.json()["title"] == "Shopping" and r.json()["text"] == "- tea"
        assert ws.receive_json()["item"]["title"] == "Shopping"
        assert client.patch(f"/api/shared/{note['id']}", json={"text": "  "}).status_code == 422
        assert client.patch(f"/api/shared/{note['id']}", json={}).status_code == 422

        assert client.delete(f"/api/shared/{note['id']}").json() == {"ok": True}
        assert ws.receive_json() == {"type": "shared", "deleted": note["id"], "agent": "scout", **NONE}
    assert client.get(f"/api/shared/{note['id']}").status_code == 404
    assert client.delete(f"/api/shared/{note['id']}").status_code == 404
    assert client.post(NOTES, json={"text": " "}).status_code == 422
    titled = client.post(NOTES, json={"title": "Mine", "text": "body"}).json()
    assert titled["title"] == "Mine"


def test_previews():
    assert preview("# Weekly report\n\n**All green.** Three `things`:\n\n1. one", "Weekly report") == \
        "All green. Three things:"
    assert preview("# Plan\n\n> quoted", "Mine") == "Plan"          # another title: its heading is the preview
    assert preview("## Plan\n1. first", "Plan") == "first" and preview("# Only", "Only") == ""
    assert preview("-5 degrees", "x") == "-5 degrees" and preview("x" * 300)[:3] == "xxx"
    assert len(preview("x" * 300)) == 200


def test_upload_download_replace(client, cfg):
    r = upload(client, "../../etc/report.txt", b"hello\n", text="The report")
    assert r.status_code == 201, r.text
    [item] = r.json()
    assert (item["kind"], item["title"], item["mime"], item["size"]) == ("file", "report.txt", "text/plain", 6)
    assert item["text"] == "The report"
    assert (cfg.data / "shared" / str(item["id"]) / "report.txt").read_bytes() == b"hello\n"

    d = client.get(f"/api/shared/{item['id']}/file")
    assert d.content == b"hello\n" and d.headers["content-type"] == "application/octet-stream"
    assert d.headers["content-disposition"] == "attachment; filename=\"report.txt\"; filename*=UTF-8''report.txt"
    assert "sandbox" in d.headers.get_list("content-security-policy")
    assert d.headers["x-content-type-options"] == "nosniff"
    shown = client.get(f"/api/shared/{item['id']}/file", params={"inline": 1})
    assert shown.headers["content-type"].startswith("text/plain")
    assert shown.headers["content-disposition"].startswith("inline;")

    [page] = upload(client, "page.html", b"<script>alert(1)</script>").json()
    h = client.get(f"/api/shared/{page['id']}/file", params={"inline": 1})
    assert h.headers["content-type"] == "application/octet-stream"       # never served as a page
    assert h.headers["content-disposition"].startswith("attachment;")
    [img] = upload(client, "dot.png", PNG).json()
    assert client.get(f"/api/shared/{img['id']}/file", params={"inline": 1}).headers["content-type"] == "image/png"

    # Replace the bytes: the title follows the file's name when it was the old name.
    r = client.put(f"/api/shared/{item['id']}/file", params={"name": "report-v2.txt"}, content=b"v2")
    assert r.status_code == 200, r.text
    assert (r.json()["title"], r.json()["size"]) == ("report-v2.txt", 2)
    assert client.get(f"/api/shared/{item['id']}/file").content == b"v2"
    assert not (cfg.data / "shared" / str(item["id"]) / "report.txt").exists()
    client.patch(f"/api/shared/{item['id']}", json={"title": "Q3 report.txt"})
    r = client.put(f"/api/shared/{item['id']}/file", content=b"v3")
    assert r.json()["title"] == "Q3 report.txt"                      # renamed by hand: kept
    note = client.post(NOTES, json={"text": "x"}).json()
    assert client.put(f"/api/shared/{note['id']}/file", content=b"x").status_code == 404
    assert client.get(f"/api/shared/{note['id']}/file").status_code == 404
    assert upload(client, "", b"x").status_code == 422


def test_multipart(client):
    files = [("file", ("a.txt", b"aaa", "text/plain")), ("file", ("b.bin", b"\0\1", "application/octet-stream"))]
    r = client.post(FILES, files=files, data={"text": "Two files"})
    assert r.status_code == 201, r.text
    a, b = r.json()
    assert (a["title"], a["size"], b["title"], b["mime"]) == ("a.txt", 3, "b.bin", "application/octet-stream")
    assert a["text"] == b["text"] == "Two files"
    r = client.put(f"/api/shared/{a['id']}/file", files=[("file", ("c.txt", b"cc", "text/plain"))])
    assert r.json()["title"] == "c.txt"
    assert client.post(FILES, files=[], data={"text": "x"}).status_code in (422, 400)


def test_the_size_limit(client, cfg):
    incoming = cfg.data / "shared" / ".incoming"
    r = upload(client, "big.bin", b"x" * (MIB + 1))
    assert r.status_code == 413 and "1 MiB" in r.json()["detail"]

    def chunks():
        for _ in range(3):
            yield b"x" * (MIB // 2)
    r = client.post(FILES, params={"name": "big.bin"}, content=chunks())     # no content-length
    assert r.status_code == 413
    r = client.post(FILES, files=[("file", ("big.bin", b"x" * (2 * MIB), "application/x"))])
    assert r.status_code == 413
    assert client.get(LIST).json()["items"] == []
    assert not any(incoming.iterdir()) if incoming.exists() else True
    assert upload(client, "fits.bin", b"x" * MIB).status_code == 201


def test_list_search_filters_paging(client, connect, rpc):
    for i in range(5):
        client.post(NOTES, json={"title": f"Note {i}", "text": f"body {i}" + (" tea" if i % 2 else "")})
    upload(client, "tea.txt", b"t")
    out = client.get(LIST).json()
    assert [x["title"] for x in out["items"]] == ["tea.txt"] + [f"Note {i}" for i in (4, 3, 2, 1, 0)]
    assert out["more"] is False and out["unseen"] == 0
    assert [x["title"] for x in client.get(LIST, params={"q": "TEA"}).json()["items"]] == [
        "tea.txt", "Note 3", "Note 1"]
    assert [x["kind"] for x in client.get(LIST, params={"kind": "file"}).json()["items"]] == ["file"]
    assert client.get(LIST, params={"kind": "other"}).status_code == 422
    page = client.get(LIST, params={"limit": 2}).json()
    assert page["more"] is True and len(page["items"]) == 2
    rest = client.get(LIST, params={"before": page["items"][-1]["updated_at"]}).json()
    assert [x["title"] for x in rest["items"]] == [f"Note {i}" for i in (3, 2, 1, 0)]
    assert client.get(LIST, params={"q": "100%"}).json()["items"] == []       # LIKE wildcards escaped

    with connect() as runner:
        token = start(client, rpc, runner, level="unrestricted")
        call(client, token, "shared_add_note", {"text": "From scout"})
    assert [x["title"] for x in client.get(LIST, params={"by": "agents"}).json()["items"]] == ["From scout"]
    assert [x["title"] for x in client.get(LIST, params={"by": "scout"}).json()["items"]] == ["From scout"]
    assert len(client.get(LIST, params={"by": "owner"}).json()["items"]) == 6


# ---------- valet.shared through the gateway ----------

def _wait(predicate, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if (v := predicate()):
            return v
        time.sleep(0.02)
    raise AssertionError("condition not reached")


def start(client, rpc, runner, name="scout", level="ask", computer="host") -> str:
    """Create (or rewrite) and start the agent; its gateway token."""
    text = YAML.format(name=name, level=level, computer=computer)
    if client.get(f"/api/agents/{name}").status_code == 404:
        assert client.post("/api/agents", json={"name": name, "agent_yaml": text}).status_code == 201
    else:
        client.put(f"/api/agents/{name}/files/agent.yaml", json={"text": text})
    r, cmd = rpc(runner, client.post, f"/api/agents/{name}/start", expect="agent.start")
    assert r.status_code == 200, r.text
    runner.status(name, "running", capabilities={"mcpCapabilities": {"http": True}})
    _wait(lambda: client.get(f"/api/agents/{name}").json()["state"] == "running")
    return cmd["gateway"]["token"]


def rpc_call(client, token, method, params=None, agent="scout") -> dict:
    r = client.post(f"/mcp/agents/{agent}", json={"jsonrpc": "2.0", "id": 1, "method": method,
                                                  "params": params or {}},
                    headers={**ACCEPT, "Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    if r.headers["content-type"].startswith("application/json"):
        return r.json()["result"]
    return json.loads(next(x[5:] for x in r.text.splitlines() if x.startswith("data:")))["result"]


def call(client, token, tool, args, agent="scout") -> dict:
    return rpc_call(client, token, "tools/call", {"name": f"valet_shared__{tool}", "arguments": args}, agent)


def data(result) -> dict:
    assert not result.get("isError"), result
    return result["structuredContent"]


def error(result) -> str:
    assert result.get("isError"), result
    return result["content"][0]["text"]


def test_listing_per_level(client, connect, rpc):
    with connect() as runner:
        token = start(client, rpc, runner, level="locked")
        names = [t["name"] for t in rpc_call(client, token, "tools/list")["tools"]]
        assert names == ["valet_shared__shared_list", "valet_shared__shared_get"]
        token = start(client, rpc, runner, level="ask")
        names = [t["name"].removeprefix("valet_shared__") for t in rpc_call(client, token, "tools/list")["tools"]]
        assert names == ["shared_list", "shared_get", "shared_add_note", "shared_add_file", "shared_update",
                         "shared_delete"]
    view = client.get("/api/agents/scout/tools", params={"permissions": "trusted"}).json()
    decisions = {t["tool"]: t["decision"] for t in view["servers"][0]["tools"]}
    assert decisions == {"shared_list": "allow", "shared_get": "allow", "shared_add_note": "allow",
                         "shared_add_file": "allow", "shared_update": "ask", "shared_delete": "ask"}


def test_an_add_at_ask_waits_for_alice(client, connect, rpc, pool):
    with connect() as runner:
        token = start(client, rpc, runner, level="ask")
        fut = pool.submit(call, client, token, "shared_add_note", {"title": "Hi", "text": "From scout"})
        [item] = _wait(lambda: client.get("/api/inbox").json())
        assert item["kind"] == "tool" and item["tool"]["name"] == "shared_add_note"
        client.post(f"/api/inbox/{item['id']}/answer", json={"option_id": "allow_once"})
        note = data(fut.result(timeout=10))
        assert note["title"] == "Hi" and note["added_by"] == "scout"
        fut = pool.submit(call, client, token, "shared_delete", {"id": note["id"]})
        [item] = _wait(lambda: client.get("/api/inbox").json())
        client.post(f"/api/inbox/{item['id']}/answer", json={"option_id": "reject_once"})
        assert "The owner rejected this call" in error(fut.result(timeout=10))
    assert client.get(f"/api/shared/{note['id']}").status_code == 200


def test_unseen(client, connect, rpc):
    with connect() as runner:
        token = start(client, rpc, runner, level="unrestricted")
        with client.websocket_connect("/ws/client") as ws:
            ws.receive_json()
            note = data(call(client, token, "shared_add_note", {"text": "# Found it\n\nThe answer is 42."}))
            frame = ws.receive_json()
            assert frame["item"]["unseen"] is True and frame["unseen"] == 1
            assert note["title"] == "Found it" and note["text"] == "# Found it\n\nThe answer is 42."
            assert frame["agents"] == {"scout": 1}
            assert client.get("/api/shared/unseen").json() == {"unseen": 1, "agents": {"scout": 1}, "unassigned": 0}
            assert client.post("/api/shared/seen", json={"ids": [note["id"]]}).json() == NONE
            assert ws.receive_json() == {"type": "shared", "seen": [note["id"]], **NONE}
            # An agent's change makes it new again; the owner's own never does.
            data(call(client, token, "shared_update", {"id": note["id"], "text": "It's 43."}))
            assert ws.receive_json()["unseen"] == 1
            client.patch(f"/api/shared/{note['id']}", json={"text": "the owner says 44."})
            assert ws.receive_json()["unseen"] == 0
            data(call(client, token, "shared_add_note", {"text": "another"}))
            ws.receive_json()
            assert client.post("/api/shared/seen", json={}).json() == NONE
            assert ws.receive_json() == {"type": "shared", "seen": "all", **NONE}


def test_inline_transfer(client, connect, rpc):
    [text] = upload(client, "notes.md", "# Notes\n\nçà\n".encode()).json()
    [img] = upload(client, "dot.png", PNG).json()
    [blob] = upload(client, "data.bin", b"\0\1\2").json()
    with connect() as runner:
        token = start(client, rpc, runner, level="unrestricted")
        listed = data(call(client, token, "shared_list", {}))
        assert [x["title"] for x in listed["items"]] == ["data.bin", "dot.png", "notes.md"]
        assert listed["items"][0]["updated"].endswith("Z") and "unseen" not in listed["items"][0]
        got = data(call(client, token, "shared_get", {"id": text["id"]}))
        assert got["content"] == "# Notes\n\nçà\n" and got["name"] == "notes.md"
        r = call(client, token, "shared_get", {"id": img["id"]})
        assert r["content"][1] == {"type": "image", "data": base64.b64encode(PNG).decode(), "mimeType": "image/png"}
        assert base64.b64decode(data(call(client, token, "shared_get", {"id": blob["id"]}))["content_base64"]) == \
            b"\0\1\2"

        made = data(call(client, token, "shared_add_file", {"name": "out.txt", "content": "result\n",
                                                            "description": "The output"}))
        assert (made["name"], made["size"], made["text"]) == ("out.txt", 7, "The output")
        assert client.get(f"/api/shared/{made['id']}/file").content == b"result\n"
        b64 = data(call(client, token, "shared_add_file", {"name": "x.bin",
                                                           "content_base64": base64.b64encode(b"\xff").decode()}))
        assert client.get(f"/api/shared/{b64['id']}/file").content == b"\xff"
        replaced = data(call(client, token, "shared_update", {"id": made["id"], "content": "v2"}))
        assert replaced["size"] == 2 and replaced["updated_by"] == "scout"

        assert "one way" in error(call(client, token, "shared_add_file", {"name": "a", "content": "x",
                                                                          "upload": True}))
        assert "give the file's content" in error(call(client, token, "shared_add_file", {"name": "a"}))
        assert "valid base64" in error(call(client, token, "shared_add_file", {"name": "a",
                                                                               "content_base64": "!!"}))
        assert "is a note" in error(call(client, token, "shared_update", {
            "id": data(call(client, token, "shared_add_note", {"text": "n"}))["id"], "content": "x"}))
        assert "no shared item 999" in error(call(client, token, "shared_get", {"id": 999}))
        assert "id is a shared item's number" in error(call(client, token, "shared_get", {"id": "x"}))
        assert data(call(client, token, "shared_delete", {"id": blob["id"]}))["deleted"] == blob["id"]


def test_transfer_tickets(client, connect, rpc, monkeypatch):
    big = b"x" * (builtin_shared.INLINE_TEXT + 1)
    [item] = upload(client, "big.txt", big).json()
    with connect() as runner:
        token = start(client, rpc, runner, level="unrestricted")
        got = data(call(client, token, "shared_get", {"id": item["id"]}))
        url = got["download_url"]
        assert "content" not in got and got["expires_in"] == 900 and url.startswith(
            "http://127.0.0.1:8780/mcp/agents/scout/shared/")
        assert got["how"] == f"curl -fsS -o big.txt '{url}'"
        path = url.removeprefix("http://127.0.0.1:8780")
        anon = client.__class__(client.app)         # the agent's shell: no cookie, no token
        d = anon.get(path)
        assert d.status_code == 200 and d.content == big
        assert anon.get(path).status_code == 200           # a download URL works more than once
        assert anon.put(path, content=b"x").status_code == 405
        assert anon.get(path.replace("/scout/", "/other/")).status_code == 404
        assert anon.get(path[:-3] + "AAA").status_code == 404
        small = data(call(client, token, "shared_get", {"id": item["id"], "download": True}))
        assert "download_url" in small

        # Upload: one PUT, and the item appears then.
        up = data(call(client, token, "shared_add_file", {"name": "made.csv", "upload": True,
                                                          "description": "Numbers"}))
        assert up["method"] == "PUT" and up["max_bytes"] == MIB
        upath = up["upload_url"].removeprefix("http://127.0.0.1:8780")
        assert anon.get(upath).status_code == 405
        r = anon.put(upath, content=b"a,b\n1,2\n")
        assert r.status_code == 201, r.text
        made = r.json()
        assert (made["name"], made["size"], made["added_by"], made["text"]) == ("made.csv", 8, "scout", "Numbers")
        assert anon.put(upath, content=b"again").status_code == 404
        listed = client.get(LIST, params={"by": "scout"}).json()["items"]
        assert [x["title"] for x in listed] == ["made.csv"] and listed[0]["unseen"] is True

        # Replace through a ticket; over the limit is refused (and the ticket is spent).
        rep = data(call(client, token, "shared_update", {"id": made["id"], "upload": True}))
        assert anon.put(rep["upload_url"].removeprefix("http://127.0.0.1:8780"),
                        content=b"x" * (MIB + 1)).status_code == 413
        rep = data(call(client, token, "shared_update", {"id": made["id"], "upload": True}))
        r = anon.put(rep["upload_url"].removeprefix("http://127.0.0.1:8780"), content=b"new")
        assert r.status_code == 200 and r.json()["size"] == 3

        # Expired.
        old = data(call(client, token, "shared_get", {"id": item["id"], "download": True}))["download_url"]
        for t in space(client).control.gateway.builtins["valet.shared"].tickets.values():
            t.expires = time.time() - 1
        assert anon.get(old.removeprefix("http://127.0.0.1:8780")).status_code == 404

        # A restart (a new token) ends the tickets from before.
        fresh = data(call(client, token, "shared_get", {"id": item["id"], "download": True}))["download_url"]
        start(client, rpc, runner, level="unrestricted")
        assert anon.get(fresh.removeprefix("http://127.0.0.1:8780")).status_code == 404

    rows = client.get("/api/gateway/calls", params={"agent": "scout", "server": "valet.shared"}).json()["calls"]
    transfers = [(c["tool"], c["outcome"]) for c in rows if c["tool"] in ("download", "upload")]
    assert transfers == [("upload", "ok"), ("upload", "error"), ("upload", "ok"), ("download", "ok"),
                         ("download", "ok")]


def test_no_computer_means_inline_only(client, connect, rpc):
    [item] = upload(client, "big.txt", b"x" * (builtin_shared.INLINE_TEXT + 1)).json()
    with connect() as runner:
        token = start(client, rpc, runner, level="unrestricted", computer="none")
        assert "nowhere to download it to" in error(call(client, token, "shared_get", {"id": item["id"]}))
        assert "nothing to upload from" in error(call(client, token, "shared_add_file", {"name": "a",
                                                                                         "upload": True}))
        assert data(call(client, token, "shared_add_file", {"name": "a.txt", "content": "fine"}))["size"] == 4
