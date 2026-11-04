"""The agent's computer through the whole stack (fake harness, level host): the terminal's socket, the files API,
volumes, the audit, a runner too old for them; and attachments in a conversation."""
import asyncio
import dataclasses
import json
import os
import struct
import zlib

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from tests.e2e.stack import eventually, kinds, texts

NAME = "m4c-box"


def png(width: int = 2, height: int = 2) -> bytes:
    """A small valid PNG."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    raw = b"".join(b"\0" + b"\xff\x00\x00" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def workdir(stack, name: str = NAME):
    return stack.rcfg.data / "agents" / name / "work"


def term_url(stack, name: str = NAME, **query) -> str:
    q = "&".join(f"{k}={v}" for k, v in {"token": stack.owner_token, **query}.items())
    return f"ws://127.0.0.1:{stack.ccfg.port}/ws/terminal/{name}?{q}"


class Term:
    """An owner's terminal socket: text frames kept apart from the output."""

    def __init__(self, ws) -> None:
        self.ws = ws
        self.out = b""
        self.texts: list[dict] = []

    async def until(self, pred, what: str, timeout: float = 10.0) -> None:
        async def loop():
            while not pred():
                m = await self.ws.recv()
                if isinstance(m, bytes):
                    self.out += m
                else:
                    self.texts.append(json.loads(m))
        try:
            await asyncio.wait_for(loop(), timeout)
        except TimeoutError:
            raise AssertionError(f"timed out waiting for {what}: {self.out[-300:]!r} {self.texts}") from None

    def text(self, kind: str) -> dict | None:
        return next((t for t in self.texts if t["type"] == kind), None)


async def audit(stack, name: str = NAME) -> list[dict]:
    r = await stack.http.get(f"/api/agents/{name}/computer/audit")
    assert r.status_code == 200, r.text
    return r.json()["entries"]


# ---------- the terminal ----------

async def test_terminal_type_resize_close(stack):
    await stack.ready_agent(NAME)
    async with connect(term_url(stack, cols=100, rows=30)) as ws:
        t = Term(ws)
        await t.until(lambda: t.text("ready"), "ready")
        assert t.text("ready")["agent"] == NAME
        await ws.send(b"echo hi-$((1+2)); pwd; stty size\n")
        await t.until(lambda: b"30 100" in t.out, "the size")
        assert b"hi-3" in t.out and os.path.realpath(workdir(stack)).encode() in t.out
        await ws.send(json.dumps({"type": "resize", "cols": 70, "rows": 12}))
        await ws.send(json.dumps({"type": "input", "data": "stty size\n"}))
        await t.until(lambda: b"12 70" in t.out, "the new size")
    entries = await eventually(lambda: _closed(stack), what="terminal_close in the audit")
    assert [e["action"] for e in entries[:2]] == ["terminal_close", "terminal_open"]
    assert "the owner closed it" in entries[0]["note"] and entries[1]["note"] == "100x30"
    assert entries[0]["target"] == entries[1]["target"] == t.text("ready")["terminal"]


async def _closed(stack):
    entries = await audit(stack)
    return entries if entries and entries[0]["action"] == "terminal_close" else None


async def test_terminal_exit_and_agent_stop(stack):
    await stack.ready_agent(NAME)
    async with connect(term_url(stack)) as ws:
        t = Term(ws)
        await t.until(lambda: t.text("ready"), "ready")
        await ws.send(b"exit 3\n")
        await t.until(lambda: t.text("exit"), "exit")
        assert t.text("exit") == {"type": "exit", "code": 3, "reason": None}
        with pytest.raises(ConnectionClosed):
            await asyncio.wait_for(ws.recv(), 5)
    async with connect(term_url(stack)) as ws:
        t = Term(ws)
        await t.until(lambda: t.text("ready"), "ready")
        assert (await stack.http.post(f"/api/agents/{NAME}/stop")).status_code == 200
        await t.until(lambda: t.text("exit"), "exit")
        assert t.text("exit")["reason"] == "the agent stopped"
    # Not running: refused with a reason.
    async with connect(term_url(stack)) as ws:
        msg = json.loads(await asyncio.wait_for(ws.recv(), 5))
        assert msg == {"type": "error", "message": f"{NAME} is not running"}
    notes = [e["note"] for e in await audit(stack) if e["action"] == "terminal_close"]
    assert any("the shell exited (3)" in n for n in notes) and any("the agent stopped" in n for n in notes)


async def test_terminal_socket_auth(stack):
    await stack.create_agent(NAME)
    base = f"ws://127.0.0.1:{stack.ccfg.port}/ws/terminal/{NAME}"
    for url, headers, code in [(base, {}, 4401),
                               (term_url(stack), {"Origin": "https://evil.example"}, 4403),
                               (term_url(stack, name="nobody"), {}, 4404)]:
        async with connect(url, additional_headers=headers) as ws:
            with pytest.raises(ConnectionClosed) as e:
                await asyncio.wait_for(ws.recv(), 5)
            assert e.value.rcvd.code == code


async def test_the_runner_dropping_ends_the_terminal(stack):
    await stack.ready_agent(NAME)
    async with connect(term_url(stack)) as ws:
        t = Term(ws)
        await t.until(lambda: t.text("ready"), "ready")
        await stack.runner._ws.close()          # the link drops; the runner reconnects, its agents keep running
        await t.until(lambda: t.text("exit"), "exit")
        assert t.text("exit")["reason"] == "the runner disconnected"
    await eventually(stack.runner_connected, what="runner reconnected")
    assert (await stack.agent(NAME))["state"] == "running"
    async with connect(term_url(stack)) as ws:     # a new one works
        t = Term(ws)
        await t.until(lambda: t.text("ready"), "ready")


# ---------- files ----------

async def test_files_list_view_download_upload(stack):
    await stack.ready_agent(NAME)
    work = workdir(stack)
    (work / "src").mkdir()
    (work / "src" / "main.py").write_text("print('héllo')\n")
    (work / "pic.png").write_bytes(png())
    base = f"/api/agents/{NAME}/computer/files"
    r = await stack.http.get(base)
    assert r.status_code == 200, r.text
    out = r.json()
    root = os.path.abspath(work)
    assert out["roots"] == [{"path": root, "label": "Working folder", "writable": True}] and out["path"] == root
    assert out["entries"][0]["name"] == "src" and out["entries"][0]["type"] == "dir"      # folders first
    assert {"pic.png", "src"} <= {e["name"] for e in out["entries"]}
    r = await stack.http.get(base + "/text", params={"path": f"{root}/src/main.py"})
    assert r.json() == {"path": f"{root}/src/main.py", "size": 16, "text": "print('héllo')\n", "binary": False,
                        "truncated": False}
    assert (await stack.http.get(base + "/text", params={"path": f"{root}/pic.png"})).json()["binary"] is True
    r = await stack.http.get(base + "/download", params={"path": f"{root}/src/main.py"})
    assert r.status_code == 200 and r.content == "print('héllo')\n".encode()
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers["content-disposition"].startswith('attachment; filename="main.py"')
    assert "sandbox" in r.headers.get_list("content-security-policy")
    r = await stack.http.get(base + "/download", params={"path": f"{root}/pic.png", "inline": 1})
    assert r.headers["content-type"] == "image/png" and r.content == png()

    # Upload: bigger than a chunk, then the name taken.
    big = os.urandom(5 * 1024 * 1024 + 123)
    r = await stack.http.post(base + "/upload", params={"dir": f"{root}/src", "name": "big.bin"}, content=big)
    assert r.status_code == 201, r.text
    assert r.json() == {"path": f"{root}/src/big.bin", "size": len(big)}
    assert (work / "src" / "big.bin").read_bytes() == big
    r = await stack.http.get(base + "/download", params={"path": f"{root}/src/big.bin"})
    assert r.content == big and r.headers["content-length"] == str(len(big))
    r = await stack.http.post(base + "/upload", params={"dir": f"{root}/src", "name": "main.py"}, content=b"x")
    assert r.status_code == 409 and r.json()["detail"] == "exists"
    r = await stack.http.post(base + "/upload", params={"dir": f"{root}/src", "name": "main.py",
                                                        "exists": "rename"}, content=b"x")
    assert r.json()["path"] == f"{root}/src/main-2.py"
    r = await stack.http.post(base + "/upload", params={"dir": f"{root}/src", "name": "main.py",
                                                        "exists": "replace"}, content=b"new")
    assert r.status_code == 201 and (work / "src" / "main.py").read_bytes() == b"new"
    r = await stack.http.post(base + "/folder", json={"path": f"{root}/a/b"})
    assert r.status_code == 201 and (work / "a" / "b").is_dir()
    assert not [p for p in os.listdir(work / "src") if p.startswith(".valet-upload-")]

    actions = [(e["action"], e["outcome"]) for e in await audit(stack)]
    assert ("file_upload", "ok") in actions and ("file_upload", "failed") in actions
    assert ("file_download", "ok") in actions and ("file_view", "ok") in actions and ("folder_create", "ok") in actions


async def test_files_confinement_and_errors(stack, tmp_path):
    await stack.ready_agent(NAME)
    work = workdir(stack)
    root = os.path.abspath(work)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("nope")
    os.symlink(outside, work / "escape")
    base = f"/api/agents/{NAME}/computer/files"
    for path, status, text in [(f"{root}/../outside", 422, ". or .."), ("relative", 422, "absolute"),
                               (str(outside), 422, "outside its folders"),
                               (f"{root}/escape", 422, "leads outside"),
                               (f"{root}/missing", 404, "no such file or folder")]:
        r = await stack.http.get(base, params={"path": path})
        assert r.status_code == status and text in r.json()["detail"], (path, r.text)
    r = await stack.http.get(base + "/download", params={"path": f"{root}/escape/secret.txt"})
    assert r.status_code == 422
    r = await stack.http.post(base + "/upload", params={"dir": f"{root}/escape", "name": "x"}, content=b"x")
    assert r.status_code == 422 and sorted(os.listdir(outside)) == ["secret.txt"]
    r = await stack.http.post(base + "/upload", params={"dir": root, "name": "a/b"}, content=b"x")
    assert r.status_code == 422
    assert (await stack.http.get("/api/agents/nobody/computer/files")).status_code == 404
    await stack.http.post(f"/api/agents/{NAME}/stop")
    await stack.wait_state(NAME, "stopped")
    r = await stack.http.get(base)
    assert r.status_code == 503 and r.json()["detail"] == f"{NAME} is not running"


async def test_upload_limit(make_stack):
    stack = await make_stack(runner=False)
    stack.ccfg = dataclasses.replace(stack.ccfg, max_file_mb=1)
    await stack.control.stop()
    stack.control.cfg = stack.ccfg
    await stack.control.start()
    await stack.start_runner()
    await stack.ready_agent(NAME)
    root = os.path.abspath(workdir(stack))
    base = f"/api/agents/{NAME}/computer/files/upload"
    r = await stack.http.post(base, params={"dir": root, "name": "x.bin"}, content=b"x" * (1024 * 1024 + 1))
    assert r.status_code == 413
    # Streamed without a length: stopped as it goes, and the partial file goes.

    async def body():
        for _ in range(3):
            yield b"y" * (700 * 1024)
    r = await stack.http.post(base, params={"dir": root, "name": "y.bin"}, content=body())
    assert r.status_code == 413
    await eventually(lambda: not [p for p in os.listdir(root) if p.startswith(".valet-upload-")],
                     what="the partial upload gone")
    assert not (workdir(stack) / "y.bin").exists()


# ---------- volumes ----------

async def test_volumes_of_a_host_agent(stack):
    await stack.ready_agent(NAME)
    r = await stack.http.get(f"/api/agents/{NAME}/computer/volumes")
    assert r.status_code == 200 and r.json() == {"runner": "testrunner", "running": True, "volumes": []}
    r = await stack.http.delete(f"/api/agents/{NAME}/computer/volumes")
    assert r.status_code == 409 and r.json()["detail"] == f"{NAME} is running: stop it first"
    await stack.http.post(f"/api/agents/{NAME}/stop")
    await stack.wait_state(NAME, "stopped")
    r = await stack.http.delete(f"/api/agents/{NAME}/computer/volumes")
    assert r.status_code == 200 and r.json() == {"removed": []}
    [entry] = [e for e in await audit(stack) if e["action"] == "volumes_delete"]
    assert entry["outcome"] == "ok" and entry["note"] == "it had none"
    # Never ran: nothing anywhere.
    await stack.create_agent("m4c-new")
    r = await stack.http.get("/api/agents/m4c-new/computer/volumes")
    assert r.json() == {"runner": None, "running": False, "volumes": []}


async def test_a_runner_without_the_features(make_stack, monkeypatch):
    monkeypatch.setattr("valet.runner.runner.FEATURES", ())
    stack = await make_stack()
    await stack.ready_agent(NAME)
    for method, url in [("GET", "files"), ("GET", "volumes")]:
        r = await stack.http.request(method, f"/api/agents/{NAME}/computer/{url}")
        assert r.status_code == 501 and "update it" in r.json()["detail"], r.text
    async with connect(term_url(stack)) as ws:
        msg = json.loads(await asyncio.wait_for(ws.recv(), 5))
        assert msg["type"] == "error" and "update it" in msg["message"]


# ---------- attachments ----------

async def attach(stack, name: str, data: bytes, agent: str = NAME) -> dict:
    r = await stack.http.post(f"/api/agents/{agent}/attachments", params={"name": name}, content=data)
    assert r.status_code == 201, r.text
    return r.json()


async def test_an_image_reaches_the_harness(stack):
    await stack.ready_agent(NAME)
    sid = (await stack.new_session(NAME))["id"]
    a = await attach(stack, "red dot.png", png())
    assert a["image"] is True and a["mime"] == "image/png" and a["sent"] is False and a["size"] == len(png())
    r = await stack.http.post(f"/api/agents/{NAME}/sessions/{sid}/prompt",
                              json={"text": "blocks", "attachments": [a["id"]]})
    assert r.status_code == 202, r.text
    evs = await stack.turn(NAME, sid, r.json()["turn_id"])
    got = json.loads(texts(evs))
    assert got == [{"type": "text", "text": "blocks"}, {"type": "image", "mimeType": "image/png",
                                                        "bytes": len(png())}]
    users = [e["data"]["content"] for e in evs if e["kind"] == "user_message"]
    assert users == [{"type": "text", "text": "blocks"},
                     {"type": "image", "mimeType": "image/png", "uri": a["url"], "name": "red dot.png",
                      "size": len(png())}]
    r = await stack.http.get(a["url"])
    assert r.status_code == 200 and r.content == png() and r.headers["content-type"] == "image/png"
    assert "sandbox" in r.headers.get_list("content-security-policy")
    # Sent: part of the conversation; it can't be sent again or taken off.
    r = await stack.http.post(f"/api/agents/{NAME}/sessions/{sid}/prompt",
                              json={"text": "again", "attachments": [a["id"]]})
    assert r.status_code == 409
    assert (await stack.http.delete(a["url"])).status_code == 409


async def test_a_file_goes_into_the_workspace(stack):
    await stack.ready_agent(NAME)
    sid = (await stack.new_session(NAME))["id"]
    root = os.path.abspath(workdir(stack))
    one = await attach(stack, "notes.txt", b"some notes")
    two = await attach(stack, "notes.txt", b"other notes")
    assert one["image"] is False and one["mime"] == "text/plain"
    r = await stack.http.post(f"/api/agents/{NAME}/sessions/{sid}/prompt",
                              json={"text": "blocks", "attachments": [one["id"], two["id"]]})
    assert r.status_code == 202, r.text
    evs = await stack.turn(NAME, sid, r.json()["turn_id"])
    got = json.loads(texts(evs))
    assert got == [{"type": "text", "text": "blocks"},
                   {"type": "text", "text": f"[Attached file: {root}/attachments/notes.txt (text/plain, 10 bytes)]"},
                   {"type": "text", "text": f"[Attached file: {root}/attachments/notes-2.txt (text/plain, 11 bytes)]"}]
    assert (workdir(stack) / "attachments" / "notes.txt").read_bytes() == b"some notes"
    assert (workdir(stack) / "attachments" / "notes-2.txt").read_bytes() == b"other notes"
    users = [e["data"]["content"] for e in evs if e["kind"] == "user_message"]
    assert users[1] == {"type": "resource_link", "uri": f"file://{root}/attachments/notes.txt", "name": "notes.txt",
                        "mimeType": "text/plain", "size": 10}
    # The control plane's copy went once it was placed.
    r = await stack.http.get(one["url"])
    assert r.status_code == 404 and "attachments/notes.txt" in r.json()["detail"]
    assert [e["action"] for e in await audit(stack)].count("attachment") == 2
    s = await stack.session(NAME, sid)
    assert s["title"] == "blocks"


async def test_attachments_alone_and_taken_off(stack):
    await stack.ready_agent(NAME)
    sid = (await stack.new_session(NAME))["id"]
    gone = await attach(stack, "drop.txt", b"x")
    assert (await stack.http.delete(gone["url"])).json() == {"ok": True}
    assert (await stack.http.get(gone["url"])).status_code == 404
    a = await attach(stack, "pic.png", png())
    r = await stack.http.post(f"/api/agents/{NAME}/sessions/{sid}/prompt", json={"attachments": [a["id"]]})
    assert r.status_code == 202, r.text
    evs = await stack.turn(NAME, sid, r.json()["turn_id"])
    assert kinds(evs)[0] == "user_message" and evs[0]["data"]["content"]["type"] == "image"
    assert (await stack.session(NAME, sid))["title"] == "pic.png"
    r = await stack.http.post(f"/api/agents/{NAME}/sessions/{sid}/prompt", json={"attachments": ["nope"]})
    assert r.status_code == 404
    await stack.create_agent("m4c-other")         # another agent's attachment isn't this one's
    b = await attach(stack, "x.png", png(), agent="m4c-other")
    r = await stack.http.post(f"/api/agents/{NAME}/sessions/{sid}/prompt", json={"attachments": [b["id"]]})
    assert r.status_code == 404


async def test_attachments_wait_in_the_queue(stack):
    """A prompt with attachments that arrives mid-turn queues like any other: its file is placed at once, its image
    waits here, and both go with it when its turn comes. Taking a queued one back deletes its attachments."""
    await stack.ready_agent(NAME)
    sid = (await stack.new_session(NAME))["id"]
    base = f"/api/agents/{NAME}/sessions/{sid}"
    r = await stack.http.post(base + "/prompt", json={"text": "slow"})
    assert r.status_code == 202 and "turn_id" in r.json(), r.text
    img, doc = await attach(stack, "red.png", png()), await attach(stack, "q.txt", b"queued")
    r = await stack.http.post(base + "/prompt", json={"text": "blocks", "attachments": [img["id"], doc["id"]]})
    assert r.status_code == 202 and r.json()["queued"], r.text
    assert (workdir(stack) / "attachments" / "q.txt").read_bytes() == b"queued"
    queue = (await stack.session(NAME, sid))["queue"]
    assert queue[0]["text"] == "blocks" and queue[0]["attachments"] == [
        {"id": img["id"], "name": "red.png", "mime": "image/png", "image": True, "url": img["url"]},
        {"id": doc["id"], "name": "q.txt", "mime": "text/plain", "image": False, "url": doc["url"]}]
    assert (await stack.http.delete(img["url"])).status_code == 409       # the queued prompt's now
    gone = await attach(stack, "gone.png", png())
    r = await stack.http.post(base + "/prompt", json={"attachments": [gone["id"]]})
    qid = r.json()["queued"]
    assert (await stack.http.delete(f"{base}/queue/{qid}")).status_code == 200
    assert (await stack.http.get(gone["url"])).status_code == 404
    assert (await stack.http.post(base + "/cancel")).status_code == 200     # the slow turn ends: the queue goes on

    async def queued_turn():
        evs = await stack.events(NAME, sid)
        return next((e["turn_id"] for e in evs if e["kind"] == "user_message"
                     and e["data"]["content"] == {"type": "text", "text": "blocks"}), None)
    turn = await eventually(queued_turn, what="the queued prompt's turn")
    evs = await stack.turn(NAME, sid, turn)
    root = os.path.abspath(workdir(stack))
    assert json.loads(texts(evs)) == [
        {"type": "text", "text": "blocks"}, {"type": "image", "mimeType": "image/png", "bytes": len(png())},
        {"type": "text", "text": f"[Attached file: {root}/attachments/q.txt (text/plain, 6 bytes)]"}]
    users = [e["data"]["content"]["type"] for e in evs if e["kind"] == "user_message"]
    assert users == ["text", "image", "resource_link"]
    assert (await stack.session(NAME, sid))["queue"] == []


async def test_retry_sends_the_attachments_again(stack):
    await stack.ready_agent(NAME)
    sid = (await stack.new_session(NAME))["id"]
    base = f"/api/agents/{NAME}/sessions/{sid}"
    img = await attach(stack, "red.png", png())
    r = await stack.http.post(base + "/prompt", json={"text": "fail", "attachments": [img["id"]]})
    first = r.json()["turn_id"]
    evs = await stack.turn(NAME, sid, first)
    assert next(e for e in evs if e["kind"] == "turn_end")["data"]["error"]
    r = await stack.http.post(f"{base}/turns/{first}/retry")
    assert r.status_code == 202, r.text
    evs = await stack.turn(NAME, sid, r.json()["turn_id"])
    users = [e["data"] for e in evs if e["kind"] == "user_message"]
    assert [u["content"]["type"] for u in users] == ["text", "image"]
    assert users[1]["content"]["uri"] == img["url"] and users[1]["retry_of"] == first


async def test_an_image_for_a_harness_without_images_is_a_file(make_stack):
    stack = await make_stack(runner=False)
    stack.rcfg.harness_commands["claude"].append("--no-image")
    await stack.start_runner()
    await stack.ready_agent(NAME)
    sid = (await stack.new_session(NAME))["id"]
    a = await attach(stack, "pic.png", png())
    r = await stack.http.post(f"/api/agents/{NAME}/sessions/{sid}/prompt",
                              json={"text": "blocks", "attachments": [a["id"]]})
    evs = await stack.turn(NAME, sid, r.json()["turn_id"])
    root = os.path.abspath(workdir(stack))
    assert json.loads(texts(evs))[1] == {
        "type": "text", "text": f"[Attached file: {root}/attachments/pic.png (image/png, {len(png())} bytes)]"}
    assert (workdir(stack) / "attachments" / "pic.png").read_bytes() == png()


async def test_attachment_limit(make_stack):
    stack = await make_stack(runner=False)
    stack.ccfg = dataclasses.replace(stack.ccfg, max_file_mb=1)
    await stack.control.stop()
    stack.control.cfg = stack.ccfg
    await stack.control.start()
    await stack.create_agent(NAME)
    r = await stack.http.post(f"/api/agents/{NAME}/attachments", params={"name": "big.bin"},
                              content=b"x" * (1024 * 1024 + 1))
    assert r.status_code == 413
    assert not list((stack.ccfg.data / "attachments" / ".incoming").glob("*"))
