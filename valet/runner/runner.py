"""The link to the control plane: connect to cfg.control_url with `Authorization: Bearer <token>`, send hello,
then dispatch commands to the Supervisor and send its notifications up. Reconnects with exponential backoff
(1s doubling to cfg.reconnect_max); agents keep running while disconnected.

Notifications are numbered (seq) and the recent ones kept (BACKLOG): after a reconnect the control plane says
the last one it applied and the runner resends the rest, so output produced while offline (or lost in flight
with the old socket) still reaches the event store. Terminal frames are the exception: unnumbered, sent only while
connected, never kept (notify), and a terminal lives no longer than the connection it was opened on."""
import asyncio
import collections
import contextlib
import json
import logging
import uuid
from typing import Any

from pydantic import ValidationError
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

from valet import __version__
from valet.shared.config import RunnerConfig
from valet.shared.protocol import (
    CLOSE_BAD_PROTOCOL, CLOSE_UNAUTHORIZED, FEATURES, ONE_WAY, Hello, Reply, Welcome, parse_control_message,
)

from .harness import runnable
from .supervisor import CommandError, Supervisor

log = logging.getLogger(__name__)

MAX_FRAME = 64 * 1024 * 1024
BACKLOG = 10_000                    # notifications kept for resending ...
BACKLOG_BYTES = 64 * 1024 * 1024    # ... and their total size (the newest is always kept)


class Runner:
    def __init__(self, cfg: RunnerConfig) -> None:
        self.cfg = cfg
        self.supervisor = Supervisor(cfg, self._send, self.notify)
        self.connected = asyncio.Event()      # set while a connection is up (tests wait on it)
        self._ws: ClientConnection | None = None
        self._conn: asyncio.Queue[str] | None = None    # the current connection's outbox (replies)
        self._queue: asyncio.Queue[str] | None = None   # the same, once welcomed (notifications), else None
        self._boot = uuid.uuid4().hex
        self._seq = 0
        self._log: collections.deque[tuple[int, str]] = collections.deque()   # (seq, frame), oldest first
        self._log_bytes = 0
        self._closed = asyncio.Event()
        self._tasks: set[asyncio.Task] = set()

    async def run(self) -> None:
        """Connect-and-serve loop; returns only after close(). A close code of 4401 (bad token) or 4400 (protocol
        mismatch) is fatal: raise RuntimeError instead of retrying."""
        backoff = 1.0
        while not self._closed.is_set():
            welcomed = False
            try:
                async with connect(self.cfg.control_url, max_size=MAX_FRAME,
                                   additional_headers={"Authorization": f"Bearer {self.cfg.token}"}) as ws:
                    self._ws = ws
                    welcomed = await self._serve(ws)
            except InvalidStatus as e:
                if e.response.status_code in (401, 403):
                    raise RuntimeError(f"control plane refused the runner token (HTTP {e.response.status_code})")
                log.warning("control plane rejected the connection: %s", e)
            except ConnectionClosed:
                pass
            except (OSError, InvalidHandshake, TimeoutError) as e:
                log.warning("can't reach the control plane at %s: %s", self.cfg.control_url, e)
            finally:
                code = self._ws.close_code if self._ws is not None else None
                self._ws = None
            if code == CLOSE_UNAUTHORIZED:
                raise RuntimeError("control plane refused the runner token (4401): check VALET_RUNNER_TOKEN")
            if code == CLOSE_BAD_PROTOCOL:
                raise RuntimeError("control plane rejected the runner protocol (4400): versions differ")
            if self._closed.is_set():
                break
            if welcomed:
                backoff = 1.0
            log.info("reconnecting in %.0fs", backoff)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._closed.wait(), backoff)
            backoff = min(backoff * 2, self.cfg.reconnect_max)

    async def close(self) -> None:
        """Stop every agent (Supervisor.shutdown) and the socket."""
        if self._closed.is_set():
            return
        self._closed.set()
        await self.supervisor.shutdown()
        queue = self._queue
        if queue is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(queue.join(), 5.0)
        if self._ws is not None:
            await self._ws.close()
        for t in list(self._tasks):
            t.cancel()

    async def _serve(self, ws: ClientConnection) -> bool:
        """One connection: hello, then commands until it closes. Returns whether the control plane welcomed us."""
        queue: asyncio.Queue[str] = asyncio.Queue()
        hello = Hello(version=__version__, boot=self._boot, seq=self._seq, agents=self.supervisor.agents(),
                      turns=self.supervisor.active_turns(), pending_permissions=self.supervisor.pending_requests(),
                      harnesses=runnable(self.cfg), features=list(FEATURES))
        self._conn = queue
        sender = asyncio.create_task(self._sender(ws, queue))
        welcomed = False
        try:
            await ws.send(json.dumps(hello.dump()))
            async for raw in ws:
                data = None
                try:
                    data = json.loads(raw)
                    msg = parse_control_message(data)
                except (ValueError, ValidationError) as e:
                    log.warning("bad frame from the control plane: %s", _reason(e))
                    if isinstance(data, dict) and isinstance(data.get("id"), str):
                        queue.put_nowait(json.dumps(Reply(id=data["id"], ok=False, error="bad command").dump()))
                    continue
                if isinstance(msg, ONE_WAY):
                    self.supervisor.one_way(msg)      # in order, at once: keystrokes mustn't overtake each other
                    continue
                if isinstance(msg, Welcome):
                    welcomed = True
                    self._resend(queue, msg.last_seq)
                    self._queue = queue       # same synchronous stretch as the resend: nothing slips between
                    self.connected.set()
                    log.info("connected to the control plane as runner %r", msg.runner)
                    continue
                task = asyncio.create_task(self._dispatch(msg, queue))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
        finally:
            if self._conn is queue:
                self._conn = self._queue = None
            self.connected.clear()
            sender.cancel()
            # Its terminals belonged to this connection: nobody can see them now.
            self.supervisor.close_terminals("the connection to the control plane dropped")
        return welcomed

    async def _dispatch(self, msg: Any, queue: asyncio.Queue) -> None:
        """Each command runs as its own task: a slow agent.stop mustn't hold up a permission.answer."""
        try:
            reply = Reply(id=msg.id, ok=True, result=await self.supervisor.handle(msg))
        except CommandError as e:
            reply = Reply(id=msg.id, ok=False, error=str(e))
        except Exception as e:
            log.exception("command %s failed", msg.type)
            reply = Reply(id=msg.id, ok=False, error=f"runner error: {e}")
        if self._conn is queue:               # a reply for a connection that's gone means nothing to the next one
            queue.put_nowait(json.dumps(reply.dump()))

    async def _send(self, msg: dict[str, Any]) -> None:
        """Number, keep and (if welcomed) queue one notification."""
        self._seq += 1
        frame = json.dumps({**msg, "seq": self._seq})
        self._log.append((self._seq, frame))
        self._log_bytes += len(frame)
        while len(self._log) > 1 and (len(self._log) > BACKLOG or self._log_bytes > BACKLOG_BYTES):
            self._log_bytes -= len(self._log.popleft()[1])
        if self._queue is not None:
            self._queue.put_nowait(frame)

    def notify(self, msg: dict[str, Any]) -> None:
        """Send one unnumbered message (terminal output) now, in line with everything else, if welcomed; otherwise
        it's dropped. Never kept or resent."""
        if self._queue is not None:
            self._queue.put_nowait(json.dumps(msg))

    def _resend(self, queue: asyncio.Queue, last_seq: int) -> None:
        """Queue every kept notification after last_seq (the control plane's), oldest first."""
        if self._log and self._log[0][0] > last_seq + 1 and last_seq < self._seq:
            log.warning("%d notifications to the control plane were lost while offline (backlog full)",
                        self._log[0][0] - last_seq - 1)
        for seq, frame in self._log:
            if seq > last_seq:
                queue.put_nowait(frame)

    async def _sender(self, ws: ClientConnection, queue: asyncio.Queue) -> None:
        """The only writer after the hello, so frames leave in the order they were queued."""
        while True:
            msg = await queue.get()
            try:
                await ws.send(msg)
            except ConnectionClosed:
                return
            finally:
                queue.task_done()


def _reason(e: ValueError) -> str:
    """Why a frame didn't parse, without the frame's values (pydantic's str() quotes them: an agent.start's secrets)."""
    if isinstance(e, ValidationError):
        return "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors(include_input=False))
    return str(e)
