"""The owner in an agent's computer (docs/API.md, "The agent's computer"): terminals, files, volumes, and their audit.
The work happens on the agent's runner (protocol features terminal, files, volumes, docs/PROTOCOL.md); a runner that
doesn't list a feature gets none of its commands (501: update it).

Terminals: each owner socket on /ws/terminal/<agent> is one shell (terminal.open). The runner's terminal.output and
terminal.closed frames come through the hub's read loop, which must never wait on an owner, so frame() only queues
them; the terminal's pump sends them on and acknowledges (terminal.ack) what it has sent, so a slow owner holds the
runner's PTY (its window, protocol.TERMINAL_WINDOW), not the control plane's memory. A terminal lives as long as its
socket, the shell, the agent and the runner's connection all do. What it shows is never logged or kept.

Files: listings, a text view, downloads streamed chunk by chunk (files.read), uploads streamed chunk by chunk
(files.write, within VALET_MAX_FILE_MB), new folders. Volumes: listed and deleted on the runner the agent was placed
on. Every terminal opened and closed, file downloaded, viewed or uploaded, and volume deleted is in the audit
(`computer_audit`)."""
import asyncio
import base64
import contextlib
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import WebSocket

from valet.shared.protocol import (
    FILE_CHUNK, FilesCancel, FilesList, FilesMkdir, FilesRead, FilesWrite, TerminalAck, TerminalClose,
    TerminalClosed, TerminalInput, TerminalOpen, TerminalOutput, TerminalResize, VolumesList, VolumesRemove,
)

from .hub import RunnerError

log = logging.getLogger(__name__)

INPUT_CHUNK = 64 * 1024         # bytes of the owner's input per terminal.input
TEXT_MAX = 1024 * 1024          # bytes of a file the text view shows
AUDIT_PAGE = 100
AUDIT_PAGE_MAX = 500
CLOSE_TIMEOUT = 10.0            # seconds terminal.close may take when the owner leaves
UP = ("starting", "running")


class ComputerError(Exception):
    """Refused or failed: str() is shown to the owner, .status is the HTTP status."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


def _from_runner(e: RunnerError) -> ComputerError:
    """A runner's answer as the owner's status: its refusals about paths and state are the owner's to fix."""
    msg = str(e)
    if e.status != 502:
        return ComputerError(msg, e.status)            # not running (503), disconnected (503), timeout (504)
    if "no such file or folder" in msg:
        return ComputerError(msg, 404)
    if msg == "exists" or msg.startswith(("it has no computer", "it's running", "its container still exists")):
        return ComputerError(msg, 409)
    if msg.startswith(("can't reach its files", "its computer didn't answer", "can't ask Docker", "Docker didn't")):
        return ComputerError(msg, 502)
    return ComputerError(msg, 422)


def _id() -> str:
    return uuid.uuid4().hex


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


class _Terminal:
    def __init__(self, terminal: str, agent: str, runner: str, link: str | None) -> None:
        self.id = terminal
        self.agent = agent
        self.runner = runner
        self.link = link                    # the runner connection it lives on
        self.queue: asyncio.Queue[bytes | TerminalClosed] = asyncio.Queue()
        self.opened = time.time()


class Computer:
    def __init__(self, control: Any) -> None:
        self.control = control
        self.terminals: dict[str, _Terminal] = {}
        self._tasks: set[asyncio.Task] = set()

    @property
    def db(self):
        return self.control.db

    @property
    def hub(self):
        return self.control.hub

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ---------- where an agent's computer is ----------

    def _runner(self, agent: str, feature: str) -> str:
        """The connected runner a running agent is on, if it has `feature`."""
        row = self.db.one("SELECT runner, state FROM agents WHERE name = ?", (agent,))
        if not row or row["state"] != "running" or not row["runner"]:
            raise ComputerError(f"{agent} is not running", 503)
        return self._connected(row["runner"], feature)

    def _connected(self, runner: str, feature: str) -> str:
        if runner not in self.hub.connected():
            raise ComputerError(f"runner {runner} is not connected", 503)
        if feature not in self.hub.features(runner):
            raise ComputerError(f"runner {runner} can't do that ({feature}): update it", 501)
        return runner

    async def _call(self, runner: str, command: Any, timeout: float | None = None) -> dict[str, Any]:
        try:
            return await self.hub.call(runner, command.dump(), timeout)
        except RunnerError as e:
            raise _from_runner(e) from None

    # ---------- audit ----------

    def audit(self, agent: str, action: str, target: str | None, outcome: str, note: str | None = None) -> None:
        try:
            self.db.execute("INSERT INTO computer_audit (ts, agent, action, target, outcome, note) "
                            "VALUES (?, ?, ?, ?, ?, ?)", (time.time(), agent, action, target, outcome, note))
        except Exception:
            log.exception("couldn't audit %s %s of %s", action, target, agent)

    def audit_list(self, agent: str, before: int | None = None, limit: int = AUDIT_PAGE) -> dict[str, Any]:
        """Newest first: {"entries": [AuditEntry], "more": bool}."""
        rows = self.db.query(
            "SELECT * FROM computer_audit WHERE agent = ? AND (? IS NULL OR id < ?) ORDER BY id DESC LIMIT ?",
            (agent, before, before, limit + 1))
        return {"entries": [{k: r[k] for k in ("id", "ts", "action", "target", "outcome", "note")}
                            for r in rows[:limit]], "more": len(rows) > limit}

    # ---------- terminals ----------

    def frame(self, runner: str, msg: TerminalOutput | TerminalClosed) -> None:
        """From the hub's read loop: never waits."""
        t = self.terminals.get(msg.terminal)
        if t is None or t.runner != runner:
            return
        if isinstance(msg, TerminalOutput):
            try:
                t.queue.put_nowait(base64.b64decode(msg.data, validate=True))
            except ValueError:
                log.warning("terminal %s: output that isn't base64, dropped", t.id)
        else:
            t.queue.put_nowait(msg)

    def link_gone(self, link: str) -> None:
        """A runner connection closed: the terminals on it ended with it."""
        for t in list(self.terminals.values()):
            if t.link == link:
                t.queue.put_nowait(TerminalClosed(terminal=t.id, reason="the runner disconnected"))

    async def serve_terminal(self, ws: WebSocket, agent: str, cols: int, rows: int) -> None:
        """One shell for an accepted, authenticated owner socket, until either side ends it. Text frames to the owner:
        {"type": "ready", "terminal", "agent"}, {"type": "exit", "code", "reason"}, {"type": "error", "message"};
        binary frames: the terminal's output. From the owner: binary frames (or {"type": "input", "data": str}) are
        keystrokes, {"type": "resize", "cols", "rows"} its size."""
        try:
            runner = self._runner(agent, "terminal")
        except ComputerError as e:
            await _refuse(ws, str(e))
            return
        t = _Terminal(_id(), agent, runner, self.hub.link_id(runner))
        self.terminals[t.id] = t        # before terminal.open: its first output may come before the reply
        try:
            await self._call(runner, TerminalOpen(id=_id(), agent=agent, terminal=t.id, cols=cols, rows=rows))
        except ComputerError as e:
            self.terminals.pop(t.id, None)
            self.audit(agent, "terminal_open", t.id, "failed", str(e))
            await _refuse(ws, str(e))
            return
        self.audit(agent, "terminal_open", t.id, "ok", f"{cols}x{rows}")
        how = "the owner closed it"
        pump = asyncio.create_task(self._pump(ws, t))
        owner = asyncio.create_task(self._from_owner(ws, t))
        try:
            await ws.send_text(json.dumps({"type": "ready", "terminal": t.id, "agent": agent}))
            await asyncio.wait({pump, owner}, return_when=asyncio.FIRST_COMPLETED)
            if pump.done() and not pump.cancelled() and isinstance(pump.result(), TerminalClosed):
                end = pump.result()
                how = end.reason or f"the shell exited ({end.code})"
                with contextlib.suppress(Exception):
                    await ws.close(1000)
        except Exception:
            pass
        finally:
            pump.cancel()
            owner.cancel()
            self.terminals.pop(t.id, None)
            if how == "the owner closed it":
                self._spawn(self._close_quietly(t))
            self.audit(agent, "terminal_close", t.id, "ok", f"open {round(time.time() - t.opened)} s; {how}")

    async def _pump(self, ws: WebSocket, t: _Terminal) -> TerminalClosed | None:
        """The runner's frames to the owner, acknowledged once sent. -> the end, if the terminal ended; None if the
        owner's socket failed."""
        while True:
            item = await t.queue.get()
            if isinstance(item, TerminalClosed):
                with contextlib.suppress(Exception):
                    await ws.send_text(json.dumps({"type": "exit", "code": item.code, "reason": item.reason}))
                return item
            try:
                await ws.send_bytes(item)
            except Exception:
                return None
            await self.hub.tell(t.runner, TerminalAck(terminal=t.id, bytes=len(item)).dump())

    async def _from_owner(self, ws: WebSocket, t: _Terminal) -> None:
        """The owner's keystrokes and sizes to the runner, in order, until the socket closes."""
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                return
            data = message.get("bytes")
            if data is None and message.get("text") is not None:
                try:
                    frame = json.loads(message["text"])
                except ValueError:
                    continue
                if not isinstance(frame, dict):
                    continue
                if frame.get("type") == "resize":
                    cols, rows = frame.get("cols"), frame.get("rows")
                    if all(isinstance(x, int) and 2 <= x <= 1000 for x in (cols, rows)):
                        await self.hub.tell(t.runner, TerminalResize(terminal=t.id, cols=cols, rows=rows).dump())
                    continue
                if frame.get("type") == "input" and isinstance(frame.get("data"), str):
                    data = frame["data"].encode()
            for i in range(0, len(data or b""), INPUT_CHUNK):
                await self.hub.tell(t.runner, TerminalInput(terminal=t.id, data=_b64(data[i:i + INPUT_CHUNK])).dump())

    async def _close_quietly(self, t: _Terminal) -> None:
        with contextlib.suppress(RunnerError):
            await self.hub.call(t.runner, TerminalClose(id=_id(), terminal=t.id).dump(), CLOSE_TIMEOUT)

    # ---------- files ----------

    async def list(self, agent: str, path: str | None) -> dict[str, Any]:
        runner = self._runner(agent, "files")
        return await self._call(runner, FilesList(id=_id(), agent=agent, path=path))

    async def text(self, agent: str, path: str) -> dict[str, Any]:
        """The start of a file as text: {"path", "size", "text" (null when binary), "binary", "truncated"}."""
        runner = self._runner(agent, "files")
        r = await self._call(runner, FilesRead(id=_id(), agent=agent, path=path, offset=0, length=TEXT_MAX))
        data = base64.b64decode(r.get("data") or "")
        text = None if b"\0" in data else _utf8(data, cut=not r.get("eof"))
        self.audit(agent, "file_view", path, "ok", f"{len(data)} bytes")
        return {"path": path, "size": r.get("size", len(data)), "text": text, "binary": text is None,
                "truncated": not r.get("eof")}

    async def download(self, agent: str, path: str) -> tuple[int, AsyncIterator[bytes]]:
        """(the file's size, its bytes as they come): the first chunk is read before this returns, so a refusal is
        an error here rather than a broken download."""
        runner = self._runner(agent, "files")
        first = await self._call(runner, FilesRead(id=_id(), agent=agent, path=path, offset=0, length=FILE_CHUNK))
        size = int(first.get("size") or 0)

        async def chunks() -> AsyncIterator[bytes]:
            data = base64.b64decode(first.get("data") or "")[:size]
            sent, eof = 0, bool(first.get("eof"))
            try:
                while True:
                    if data:
                        yield data
                        sent += len(data)
                    if eof or sent >= size or not data:
                        break
                    r = await self._call(runner, FilesRead(id=_id(), agent=agent, path=path, offset=sent,
                                                           length=min(FILE_CHUNK, size - sent)))
                    data, eof = base64.b64decode(r.get("data") or ""), bool(r.get("eof"))
            except BaseException as e:
                self.audit(agent, "file_download", path, "failed",
                           f"{sent} of {size} bytes; {e or 'the download stopped'}")
                raise
            self.audit(agent, "file_download", path, "ok" if sent == size else "failed", f"{sent} of {size} bytes")
            if sent < size:
                raise ComputerError("the file got shorter while it was downloading", 502)
        return size, chunks()

    async def upload(self, agent: str, folder: str, name: str, exists: str, body: AsyncIterator[bytes],
                     limit: int, action: str = "file_upload") -> dict[str, Any]:
        """The body into `folder`/`name`, FILE_CHUNK at a time; over `limit` bytes: 413, and the partial file goes.
        -> {"path", "size"}. Audited as `action` (an attachment's is "attachment")."""
        runner = self._runner(agent, "files")
        upload, buf, offset = _id(), bytearray(), 0
        target = f"{folder.rstrip('/')}/{name}"

        def chunk(data: bytes, final: bool) -> FilesWrite:
            return FilesWrite(id=_id(), agent=agent, upload=upload, dir=folder, name=name, offset=offset,
                              data=_b64(data), final=final, exists=exists)
        try:
            async for piece in body:
                buf += piece
                if offset + len(buf) > limit:
                    raise ComputerError(f"over the {limit // 1024 // 1024} MiB limit (VALET_MAX_FILE_MB)", 413)
                while len(buf) >= FILE_CHUNK:
                    data = bytes(buf[:FILE_CHUNK])
                    del buf[:FILE_CHUNK]
                    await self._call(runner, chunk(data, False))
                    offset += len(data)
            out = await self._call(runner, chunk(bytes(buf), True))
        except BaseException as e:
            self._spawn(self._cancel_quietly(runner, agent, upload, folder))
            self.audit(agent, action, target, "failed", str(e) or "the upload stopped")
            raise
        self.audit(agent, action, out.get("path") or target, "ok", f"{out.get('size', offset)} bytes")
        return out

    async def _cancel_quietly(self, runner: str, agent: str, upload: str, folder: str) -> None:
        with contextlib.suppress(RunnerError):
            await self.hub.call(runner, FilesCancel(id=_id(), agent=agent, upload=upload, dir=folder).dump())

    async def mkdir(self, agent: str, path: str, audit: bool = True) -> dict[str, Any]:
        runner = self._runner(agent, "files")
        out = await self._call(runner, FilesMkdir(id=_id(), agent=agent, path=path))
        if audit:
            self.audit(agent, "folder_create", out.get("path") or path, "ok")
        return out

    # ---------- volumes ----------

    def _placed(self, agent: str) -> tuple[str | None, str]:
        row = self.db.one("SELECT runner, state FROM agents WHERE name = ?", (agent,))
        return (row["runner"], row["state"]) if row else (None, "stopped")

    async def volumes(self, agent: str) -> dict[str, Any]:
        """{"runner", "running", "volumes": [{"name", "mount"}]}: on the runner it was placed on (none if it never
        ran anywhere)."""
        runner, state = self._placed(agent)
        if runner is None:
            return {"runner": None, "running": False, "volumes": []}
        self._connected(runner, "volumes")
        out = await self._call(runner, VolumesList(id=_id(), agent=agent))
        return {"runner": runner, "running": state in UP, "volumes": out.get("volumes") or []}

    async def remove_volumes(self, agent: str) -> dict[str, Any]:
        runner, state = self._placed(agent)
        if state in UP:
            raise ComputerError(f"{agent} is running: stop it first", 409)
        if runner is None:
            return {"removed": []}
        self._connected(runner, "volumes")
        try:
            out = await self._call(runner, VolumesRemove(id=_id(), agent=agent), timeout=120)
        except ComputerError as e:
            self.audit(agent, "volumes_delete", None, "failed", str(e))
            raise
        removed = out.get("removed") or []
        self.audit(agent, "volumes_delete", ", ".join(removed) or None, "ok",
                   None if removed else "it had none")
        return {"removed": removed}

    async def close(self) -> None:
        for t in self._tasks:
            t.cancel()


def _utf8(data: bytes, cut: bool) -> str | None:
    """UTF-8 text, or None. A read that stopped early may end inside a character: those last bytes are dropped."""
    for drop in range(4 if cut else 1):
        try:
            return data[:len(data) - drop].decode()
        except UnicodeDecodeError:
            continue
    return None


async def _refuse(ws: WebSocket, message: str) -> None:
    with contextlib.suppress(Exception):
        await ws.send_text(json.dumps({"type": "error", "message": message}))
        await ws.close(1000)
