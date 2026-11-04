"""The live part of the control plane: connected runners (commands out, replies and notifications in) and
connected owner clients (fan-out). Runner notifications are applied to the database here: events stored,
inbox items created/expired, agent/session/turn state updated, and every change broadcast to clients."""
import asyncio
import contextlib
import json
import logging
import time
import uuid
from typing import Any

from fastapi import WebSocket

from valet import __version__
from valet.shared.definition import NAME_RE, DefinitionError
from valet.shared.events import Event, normalize_update
from valet.shared.protocol import (
    CLOSE_BAD_PROTOCOL, CLOSE_REPLACED, CLOSE_UNAUTHORIZED, PROTOCOL_VERSION, AgentStart, AgentState, AgentStatus,
    AgentStop, GatewayAccess, Hello, PermissionClosed, PermissionRequest, Reply, RunnerMessage, SessionUpdate,
    TerminalClosed, TerminalOutput, TurnEnd, Welcome, parse_runner_message,
)

from . import auth, directives, views
from .activity import Activity
from .auth import token_hash
from .secrets import SecretError

log = logging.getLogger(__name__)

HELLO_TIMEOUT = 10.0        # seconds a new runner socket has to send its hello
TOKEN_CHECK = 5.0           # how often a connected runner's token is rechecked (revoke/re-key from the CLI)
CLIENT_QUEUE = 1000         # frames buffered per client before it's dropped (it reconnects and refetches)
# Inbox kinds that no call waits on (valet.owner's message and question): they outlive the turn that made them, the
# agent's stop and a control-plane restart, and they don't make the agent "waiting on you".
DURABLE = ("message", "question")
_NOT_DURABLE = "kind NOT IN ('message', 'question')"


class RunnerError(Exception):
    """A runner replied ok=false, isn't connected, or didn't reply within cfg.rpc_timeout. str() is shown to the
    owner (API 502/503/504 per docs/API.md); .status is that HTTP status. in_doubt: the command was sent and the
    link dropped before its reply, so the runner may have carried it out (its next hello tells)."""

    def __init__(self, message: str, status: int = 502, in_doubt: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.in_doubt = in_doubt


class _Link:
    """One runner socket: sends are serialized, replies are matched to waiting calls by id."""

    def __init__(self, ws: WebSocket, token: str) -> None:
        self.id = uuid.uuid4().hex          # this connection's: what an agent's terminals hang off (computer.py)
        self.ws = ws
        self.token = token                  # sha256 of the bearer token it connected with
        self.pending: dict[str, asyncio.Future] = {}
        self.lock = asyncio.Lock()
        self.seq = 0                        # the last notification seq applied from this runner's boot
        self.harnesses: list[str] = []      # from its hello: the harnesses it can run
        self.features: list[str] = []       # from its hello: what it can do beyond v1's commands (FEATURES)

    async def send(self, message: dict[str, Any]) -> None:
        async with self.lock:
            await self.ws.send_text(json.dumps(message))

    def fail_pending(self) -> None:
        for fut in self.pending.values():
            if not fut.done():
                fut.set_exception(RunnerError("the runner disconnected", 503, in_doubt=True))


class Hub:
    def __init__(self, control: Any) -> None:
        """control: the app's Control object (cfg, db, defs)."""
        self.control = control
        self._runners: dict[str, _Link] = {}
        self._clients: dict[WebSocket, asyncio.Queue] = {}
        self._tasks: set[asyncio.Task] = set()
        self._asks: dict[str, asyncio.Future] = {}     # gateway inbox item id -> the waiting call's answer
        self.activity = Activity(self.emit_agent)       # what each working agent is doing (Agent.activity)
        self.deleting: set[str] = set()     # agents being deleted (removal.py): none of them starts meanwhile

    @property
    def db(self):
        return self.control.db

    def _spawn(self, coro) -> None:
        """Background work started from the runner's read loop, which must never wait on a reply itself."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    spawn = _spawn          # for the other always-on parts (prompts.py): work that outlives the request that began it

    # ---- runners ----
    async def serve_runner(self, ws: WebSocket, runner: str) -> None:
        """Own one authenticated runner socket until it closes: expect hello, send welcome, apply the
        notifications the runner resends, reconcile once caught up with the hello (see protocol.Hello), then read
        frames. Replaces (closes 4409) an older socket of the same runner."""
        try:
            hello = parse_runner_message(json.loads(await asyncio.wait_for(ws.receive_text(), HELLO_TIMEOUT)))
        except Exception:
            hello = None
        if not isinstance(hello, Hello) or hello.protocol != PROTOCOL_VERSION:
            log.warning("runner %s: no valid hello, closing", runner)
            with contextlib.suppress(Exception):
                await ws.close(CLOSE_BAD_PROTOCOL)
            return

        link = _Link(ws, token_hash(_bearer_token(ws)))
        link.harnesses = list(hello.harnesses)
        link.features = list(hello.features)
        seen = self.db.one("SELECT boot, seq FROM runner_seq WHERE runner = ?", (runner,))
        link.seq = seen["seq"] if seen and seen["boot"] == hello.boot else 0
        self.db.execute("INSERT INTO runner_seq (runner, boot, seq) VALUES (?, ?, ?) ON CONFLICT (runner) "
                        "DO UPDATE SET boot = excluded.boot, seq = excluded.seq", (runner, hello.boot, link.seq))
        # Welcome first: no command may reach the runner before it.
        await link.send(Welcome(runner=runner, version=__version__, last_seq=link.seq).dump())
        old = self._runners.get(runner)
        self._runners[runner] = link
        if old:
            old.fail_pending()
            with contextlib.suppress(Exception):
                await old.ws.close(CLOSE_REPLACED)
        log.info("runner %s connected (version %s)", runner, hello.version)
        self.db.execute("UPDATE runners SET last_seen = ?, version = ? WHERE name = ?",
                        (time.time(), hello.version, runner))
        synced = hello.seq <= link.seq
        if synced:
            await self._reconcile(runner, hello)
        await self._emit_runner(runner)

        watcher = asyncio.create_task(self._watch_token(runner, link))
        try:
            while True:
                text = await ws.receive_text()
                try:
                    msg = parse_runner_message(json.loads(text))
                except Exception:
                    log.warning("runner %s: ignoring a frame that isn't a runner message", runner)
                    continue
                if isinstance(msg, Hello):
                    log.warning("runner %s: ignoring a second hello", runner)
                    continue
                if isinstance(msg, Reply):
                    fut = link.pending.get(msg.id)
                    if fut and not fut.done():
                        fut.set_result(msg)
                        # Let the caller record what the reply confirms (an answered permission) before the
                        # notifications the runner sent after it.
                        await asyncio.sleep(0)
                    continue
                if isinstance(msg, TerminalOutput | TerminalClosed):
                    # Unnumbered, never resent: straight to the owner's terminal (it never waits on the owner).
                    self.control.computer.frame(runner, msg)
                    continue
                if msg.seq is not None:
                    if msg.seq <= link.seq:
                        continue        # resent, and already applied
                    if msg.seq > link.seq + 1:
                        log.warning("runner %s: %d notifications lost", runner, msg.seq - link.seq - 1)
                    link.seq = msg.seq
                    self.db.execute("UPDATE runner_seq SET seq = ? WHERE runner = ?", (msg.seq, runner))
                try:
                    await self.on_runner_message(runner, msg)
                except Exception:
                    log.exception("runner %s: failed to apply %s", runner, msg.type)
                if not synced and (msg.seq or 0) >= hello.seq:
                    synced = True       # caught up with the hello's snapshot
                    await self._reconcile(runner, hello)
                    await self._emit_runner(runner)
        except Exception:
            pass        # disconnect (WebSocketDisconnect, or a receive after close)
        finally:
            watcher.cancel()
            link.fail_pending()
            self.control.computer.link_gone(link.id)        # its terminals ended with it
            if self._runners.get(runner) is link:
                del self._runners[runner]
                log.info("runner %s disconnected", runner)
                self.db.execute("UPDATE runners SET last_seen = ? WHERE name = ?", (time.time(), runner))
                await self._emit_runner(runner)

    async def _watch_token(self, runner: str, link: _Link) -> None:
        """Revoking or re-keying happens in the CLI, another process: notice it through the database."""
        while True:
            await asyncio.sleep(TOKEN_CHECK)
            ok = self.db.one("SELECT 1 FROM runners WHERE name = ? AND token_hash = ? AND revoked = 0",
                             (runner, link.token))
            if not ok:
                log.info("runner %s: token revoked, disconnecting", runner)
                with contextlib.suppress(Exception):
                    await link.ws.close(CLOSE_UNAUTHORIZED)
                return

    def connected(self) -> list[str]:
        return sorted(self._runners)

    def harnesses(self, runner: str) -> list[str]:
        """What a connected runner said it can run ([] if it isn't connected)."""
        link = self._runners.get(runner)
        return list(link.harnesses) if link else []

    def features(self, runner: str) -> list[str]:
        """What else a connected runner said it can do (protocol.FEATURES; [] if it isn't connected)."""
        link = self._runners.get(runner)
        return list(link.features) if link else []

    def link_id(self, runner: str) -> str | None:
        """The id of a connected runner's current connection."""
        link = self._runners.get(runner)
        return link.id if link else None

    async def tell(self, runner: str, message: dict[str, Any]) -> bool:
        """Send a one-way message (no id, no reply: terminal.input, resize, ack). False if it can't go."""
        link = self._runners.get(runner)
        if link is None:
            return False
        try:
            await link.send(message)
        except Exception:
            return False
        return True

    async def call(self, runner: str, command: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        """Send a command (an `id` is added if missing) and await its Reply's result, up to `timeout` seconds
        (default VALET_RPC_TIMEOUT). Raises RunnerError."""
        link = self._runners.get(runner)
        if link is None:
            raise RunnerError(f"runner {runner} is not connected", 503)
        command = {**command, "id": command.get("id") or uuid.uuid4().hex}
        fut = asyncio.get_running_loop().create_future()
        link.pending[command["id"]] = fut
        try:
            try:
                await link.send(command)
            except Exception:
                raise RunnerError("the runner disconnected", 503) from None
            try:
                reply: Reply = await asyncio.wait_for(fut, timeout or self.control.cfg.rpc_timeout)
            except TimeoutError:
                raise RunnerError(f"runner {runner} didn't answer in time", 504) from None
        finally:
            link.pending.pop(command["id"], None)
        if not reply.ok:
            error = reply.error or "runner error"
            raise RunnerError(error, 503 if error == "not running" else 502)
        return reply.result or {}

    async def on_runner_message(self, runner: str, msg: RunnerMessage) -> None:
        """Apply one notification. A runner speaks only for agents placed on it (the control plane places them,
        in start_agent or a reconcile claim): anything about another agent is dropped."""
        if isinstance(msg, AgentStatus):
            await self._apply_state(runner, msg)
            return
        if not self._placed(runner, msg.agent):
            log.warning("runner %s sent %s for agent %s, which isn't placed on it; ignored", runner, msg.type,
                        msg.agent)
            return
        if isinstance(msg, SessionUpdate):
            await self._apply_update(msg)
        elif isinstance(msg, PermissionRequest):
            await self._apply_permission(runner, msg)
        elif isinstance(msg, PermissionClosed):
            row = self.db.one("SELECT * FROM inbox WHERE id = ? AND agent = ? AND runner = ?",
                              (msg.request_id, msg.agent, runner))
            if not row:
                return
            if msg.reason == "answered" and msg.outcome is not None:
                changed = await self.record_answer(row, msg.outcome)
            else:
                changed = await self.expire_item(row)
            if changed:
                await self.emit_agent(row["agent"])
        elif isinstance(msg, TurnEnd):
            if not self.db.one("SELECT 1 FROM turns WHERE id = ? AND agent = ?", (msg.turn_id, msg.agent)):
                return
            state = "cancelled" if msg.stop_reason == "cancelled" else "failed" if msg.error else "done"
            if await self.end_turn(msg.turn_id, state, msg.stop_reason, msg.error):
                await self.emit_agent(msg.agent)

    def _placed(self, runner: str, agent: str) -> bool:
        row = self.db.one("SELECT runner FROM agents WHERE name = ?", (agent,))
        return bool(row and row["runner"] == runner)

    async def _apply_state(self, runner: str, st: AgentState, broadcast: bool = True) -> None:
        if not self._placed(runner, st.agent):
            log.warning("runner %s reported agent %s, which isn't placed on it; ignored", runner, st.agent)
            return
        self.db.execute(
            "UPDATE agents SET runner = ?, state = ?, error = ?, capabilities = ?, agent_info = ?, updated_at = ? "
            "WHERE name = ?",
            (runner, st.state, st.error, _dumps(st.capabilities), _dumps(st.agent_info), time.time(), st.agent))
        if st.state in ("stopped", "failed"):
            await self.agent_down(st.agent)
        elif st.state == "running" and getattr(self.control, "prompts", None) is not None:
            self._spawn(self.control.prompts.pump_agent(st.agent))     # prompts that queued while it was down
        if broadcast:
            await self.emit_agent(st.agent)

    async def agent_down(self, agent: str) -> None:
        """The harness process is gone: nothing of it can still be waiting or running (requests first: they
        belong inside their turn in the event stream)."""
        for row in self.db.query(f"SELECT * FROM inbox WHERE agent = ? AND status = 'pending' AND {_NOT_DURABLE}",
                                 (agent,)):
            await self.expire_item(row)
        for t in self.db.query("SELECT id FROM turns WHERE agent = ? AND state = 'running'", (agent,)):
            await self.end_turn(t["id"], "interrupted", None, "interrupted")

    async def _apply_update(self, msg: SessionUpdate) -> None:
        if msg.history:
            # An imported session's history (session.import), user messages too, each exchange a turn of its own
            # (the runner's turn.end closes it); only for a session the import made.
            if not msg.turn_id or not self.db.one("SELECT 1 FROM sessions WHERE agent = ? AND id = ?",
                                                  (msg.agent, msg.session_id)):
                return
            self.db.execute("INSERT OR IGNORE INTO turns (id, agent, session_id, started_at) VALUES (?, ?, ?, ?)",
                            (msg.turn_id, msg.agent, msg.session_id, time.time()))
        elif msg.replay or msg.update.get("sessionUpdate") == "user_message_chunk":
            return      # replays are already stored; the owner's prompt was recorded when it was sent
        kind, data = normalize_update(msg.update)
        await self.add_event(msg.agent, msg.session_id, msg.turn_id, kind, data)
        if msg.turn_id:
            self.activity.note(msg.agent, kind, data)
        title = data.get("title") if kind == "session_info" else None
        if isinstance(title, str) and title.strip():
            # The harness's own title replaces the first-prompt fallback. It resends it unchanged every turn.
            title = title.strip()[:200]
            cur = self.db.execute("UPDATE sessions SET title = ? WHERE agent = ? AND id = ? AND title IS NOT ?",
                                  (title, msg.agent, msg.session_id, title))
            if cur.rowcount:
                await self.emit_session(msg.agent, msg.session_id)

    async def _apply_permission(self, runner: str, msg: PermissionRequest) -> None:
        title = str(msg.tool_call.get("title") or "Permission request")
        cur = self.db.execute(
            "INSERT OR IGNORE INTO inbox (id, kind, agent, session_id, turn_id, runner, title, payload, created_at) "
            "VALUES (?, 'permission', ?, ?, ?, ?, ?, ?, ?)",
            (msg.request_id, msg.agent, msg.session_id, msg.turn_id, runner, title,
             json.dumps({"tool_call": msg.tool_call, "options": msg.options}), time.time()))
        if not cur.rowcount:
            return      # known already (a runner re-listing it in its hello): no second notification either
        await self.emit_inbox(msg.request_id)
        await self.add_event(msg.agent, msg.session_id, msg.turn_id, "permission_request",
                             {"requestId": msg.request_id, "toolCall": msg.tool_call, "options": msg.options})
        await self.emit_agent(msg.agent)
        self.item_created(msg.request_id)

    async def _reconcile(self, runner: str, hello: Hello) -> None:
        """Bring the database in line with what the runner says it has (PROTOCOL.md, handshake step 3)."""
        listed = {a.agent: a for a in hello.agents}
        for st in hello.agents:
            await self._apply_state(runner, st, broadcast=False)
        for row in self.db.query("SELECT name FROM agents WHERE runner = ? AND state IN ('starting', 'running')",
                                 (runner,)):
            if row["name"] not in listed:       # the runner restarted: that process is gone
                await self._apply_state(runner, AgentState(agent=row["name"], state="stopped"), broadcast=False)
        for req in hello.pending_permissions:   # a request the control plane never heard of (lost while away)
            if self._placed(runner, req.agent):
                await self._apply_permission(runner, req)
        live_requests = {req.request_id for req in hello.pending_permissions}
        for row in self.db.query("SELECT * FROM inbox WHERE runner = ? AND status = 'pending'", (runner,)):
            if row["id"] not in live_requests:
                await self.expire_item(row)
        live_turns = {t.turn_id for t in hello.turns}
        for t in self.db.query("SELECT t.id FROM turns t JOIN agents a ON a.name = t.agent "
                               "WHERE a.runner = ? AND t.state = 'running'", (runner,)):
            if t["id"] not in live_turns:
                await self.end_turn(t["id"], "interrupted", None, "interrupted")
        for row in self.db.query("SELECT name, runner, desired FROM agents"):
            st = listed.get(row["name"])
            up = bool(st and st.state in ("starting", "running"))
            if row["desired"] == "stopped":
                if up and row["runner"] == runner:      # stopped by the owner while this runner was away
                    self._spawn(self._stop_quietly(row["name"], runner))
            elif not up and row["runner"] == runner:
                self._spawn(self._start_quietly(row["name"], runner))
            elif not up and row["runner"] is None and self._wants(row["name"], runner):
                # Claim it now, atomically: another runner's reconcile mustn't start it too.
                cur = self.db.execute("UPDATE agents SET runner = ? WHERE name = ? AND runner IS NULL",
                                      (runner, row["name"]))
                if cur.rowcount:
                    self._spawn(self._start_quietly(row["name"], runner))

    def _wants(self, agent: str, runner: str) -> bool:
        """An unplaced agent the owner wants running may start here unless its definition names another runner."""
        try:
            d, _ = self.control.defs.load(agent)
        except (DefinitionError, FileNotFoundError):
            return False
        return d.runner in (None, runner)

    async def start_agent(self, agent: str, runner: str) -> None:
        """Place `agent` on `runner` and send agent.start with its directive layers (the repo's, and the environment
        and memory generated now), the values of its secrets, and a new gateway token if it has gateway servers
        (gateway_servers(): tools.mcp, and valet.memory with memory; the old one stops working; without any it's
        revoked). Raises DefinitionError (an authority preset or a browser profile it lists doesn't exist: nothing is sent or changed),
        FileNotFoundError, SecretError (one isn't set: likewise), RunnerError (a start the runner surely didn't take
        puts the old token back: a process still running there keeps working). The fingerprint it records leaves the
        generated layers out: what the environment says follows from the definition, the gateway's tool lists change
        without a restart, and the agent reads fresh memory through valet.memory."""
        d, files = self.control.defs.load(agent)
        directives.check(self.control.defs.root, d)
        self.control.browser.check(d)       # a browser profile it lists must exist
        values, stamps = self.control.secrets.resolve(d)
        sent = await directives.start_files(self.control, d, files)    # + the generated environment and memory
        views.ensure_agent(self.db, agent)
        self.db.execute("UPDATE agents SET runner = ?, updated_at = ? WHERE name = ?", (runner, time.time(), agent))
        old_token = self.db.one("SELECT token_hash FROM agents WHERE name = ?", (agent,))["token_hash"]
        gateway = None
        if d.gateway_servers():
            cfg = self.control.cfg
            gateway = GatewayAccess(url=self.control.gateway.url(agent), token=auth.issue_agent_token(self.db, agent),
                                    timeout=cfg.gateway_ask_timeout + cfg.gateway_call_timeout)
        else:
            auth.revoke_agent_token(self.db, agent)
        if agent in self.deleting:      # checked last, after the awaits above: its agent.forget may be on its way
            auth.revoke_agent_token(self.db, agent)
            raise DefinitionError(f"{agent} is being deleted")
        try:
            await self.call(runner, AgentStart(id=uuid.uuid4().hex, agent=agent, definition=d.model_dump(mode="json"),
                                               files=sent, secrets=values, gateway=gateway).dump())
        except RunnerError as e:
            log.warning("starting %s on %s failed: %s", agent, runner, e)
            if not e.in_doubt and e.status != 504:      # never sent, or refused: nothing new runs with the token
                self.db.execute("UPDATE agents SET token_hash = ? WHERE name = ?", (old_token, agent))
            raise
        # Recorded once the runner has it: a start that never got there keeps the old fingerprint, so a saved
        # change still says restart_needed. (In doubt, the runner's next hello starts it again and records it then.)
        self.db.execute("UPDATE agents SET started = ? WHERE name = ?",
                        (views.fingerprint(d, files, stamps, gateway.url if gateway else None), agent))

    async def _start_quietly(self, agent: str, runner: str) -> None:
        try:
            await self.start_agent(agent, runner)
        except (DefinitionError, FileNotFoundError, SecretError) as e:
            error = "agent.yaml is missing" if isinstance(e, FileNotFoundError) else str(e)
            self.db.execute("UPDATE agents SET state = 'failed', error = ?, updated_at = ? WHERE name = ?",
                            (error, time.time(), agent))
            await self.emit_agent(agent)
        except RunnerError:
            pass        # logged by start_agent; the runner's next hello retries

    async def _stop_quietly(self, agent: str, runner: str) -> None:
        try:
            await self.call(runner, AgentStop(id=uuid.uuid4().hex, agent=agent).dump())
        except RunnerError as e:
            log.warning("stopping %s on %s failed: %s", agent, runner, e)

    # ---- state changes shared with the API ----
    async def add_event(self, agent: str, session_id: str, turn_id: str | None, kind: str,
                        data: dict[str, Any]) -> Event:
        ts = time.time()
        cur = self.db.execute("INSERT INTO events (agent, session_id, turn_id, kind, data, ts) VALUES (?, ?, ?, ?, ?, ?)",
                              (agent, session_id, turn_id, kind, json.dumps(data), ts))
        event = Event(id=cur.lastrowid, agent=agent, session_id=session_id, turn_id=turn_id, ts=ts, kind=kind,
                      data=data)
        await self.broadcast({"type": "event", "event": event})
        return event

    async def end_turn(self, turn_id: str, state: str, stop_reason: str | None, error: str | None) -> bool:
        """Finish a running turn once: turn row, session idle, turn_end event. False if it wasn't running.
        Its still-pending requests expire first, whatever order the runner reports them in, so turn_end is
        always the turn's last event."""
        turn = self.db.one("SELECT * FROM turns WHERE id = ?", (turn_id,))
        if not turn or turn["state"] != "running":
            return False
        for row in self.db.query(f"SELECT * FROM inbox WHERE turn_id = ? AND status = 'pending' AND {_NOT_DURABLE}",
                                 (turn_id,)):
            await self.expire_item(row)
        cur = self.db.execute("UPDATE turns SET state = ?, stop_reason = ?, error = ?, ended_at = ? "
                              "WHERE id = ? AND state = 'running'", (state, stop_reason, error, time.time(), turn_id))
        if not cur.rowcount:
            return False
        self.db.execute("UPDATE sessions SET state = 'idle', updated_at = ? WHERE agent = ? AND id = ?",
                        (time.time(), turn["agent"], turn["session_id"]))
        await self.add_event(turn["agent"], turn["session_id"], turn_id, "turn_end",
                             {"stopReason": stop_reason, "error": error})
        if not self.db.one("SELECT 1 FROM turns WHERE agent = ? AND state = 'running' LIMIT 1", (turn["agent"],)):
            self.activity.clear(turn["agent"])
        await self.emit_session(turn["agent"], turn["session_id"])
        if getattr(self.control, "prompts", None) is not None:
            self._spawn(self.control.prompts.after_turn(turn_id))      # the session's next queued prompt
        return True

    async def record_answer(self, row: dict, outcome: dict[str, Any]) -> bool:
        """The harness (or the waiting gateway call) got `outcome` for a pending inbox item. False if it wasn't
        pending (already recorded)."""
        cur = self.db.execute("UPDATE inbox SET status = 'answered', answer = ?, answered_at = ? "
                              "WHERE id = ? AND status = 'pending'", (json.dumps(outcome), time.time(), row["id"]))
        if not cur.rowcount:
            return False
        self._resolve(row["id"], outcome)
        await self.emit_inbox(row["id"])
        if row["session_id"]:
            await self.add_event(row["agent"], row["session_id"], row["turn_id"], "permission_answer",
                                 {"requestId": row["id"], "outcome": outcome, "status": "answered"})
        return True

    async def expire_item(self, row: dict) -> bool:
        """A pending inbox item can no longer be answered (a gateway call waiting on it gets None). False if it
        wasn't pending."""
        cur = self.db.execute("UPDATE inbox SET status = 'expired', answered_at = ? WHERE id = ? AND status = 'pending'",
                              (time.time(), row["id"]))
        if not cur.rowcount:
            return False
        self._resolve(row["id"], None)
        await self.emit_inbox(row["id"])
        if row["session_id"]:
            await self.add_event(row["agent"], row["session_id"], row["turn_id"], "permission_answer",
                                 {"requestId": row["id"], "outcome": None, "status": "expired"})
        return True

    # ---- the gateway's asks ----
    async def ask_tool(self, agent: str, title: str, tool: dict[str, Any],
                       options: list[dict[str, Any]]) -> tuple[dict, asyncio.Future]:
        """An inbox item (kind `tool`, no runner) for a gateway call waiting on the owner -> (row, future of the
        ACP-style outcome, or None once it expires). Shown in the agent's conversation too when exactly one of its
        turns is running (the call can't say which session made it)."""
        item_id = uuid.uuid4().hex
        turns = self.db.query("SELECT id, session_id FROM turns WHERE agent = ? AND state = 'running'", (agent,))
        sid, tid = (turns[0]["session_id"], turns[0]["id"]) if len(turns) == 1 else (None, None)
        tool_call = {"toolCallId": f"gateway-{item_id}", "title": title, "kind": "other",
                     "rawInput": tool.get("arguments") or {}}
        self.db.execute(
            "INSERT INTO inbox (id, kind, agent, session_id, turn_id, runner, title, payload, created_at) "
            "VALUES (?, 'tool', ?, ?, ?, NULL, ?, ?, ?)",
            (item_id, agent, sid, tid, title, json.dumps({"tool_call": tool_call, "options": options, "tool": tool}),
             time.time()))
        fut = asyncio.get_running_loop().create_future()
        self._asks[item_id] = fut
        await self.emit_inbox(item_id)
        if sid:
            await self.add_event(agent, sid, tid, "permission_request", {"requestId": item_id, "kind": "tool",
                                 "toolCall": tool_call, "options": options, "tool": tool})
        await self.emit_agent(agent)
        self.item_created(item_id)
        return self.db.one("SELECT * FROM inbox WHERE id = ?", (item_id,)), fut

    def item_created(self, item_id: str) -> None:
        """Every new pending inbox item, whatever made it, comes through here once, right after it's stored and
        broadcast: the owner gets a push and the webhook is called (notify.py), in the background, so the caller
        never waits on Apple or the webhook. A creation site calls it only for a row it really inserted."""
        item = views.inbox_item(self.db, item_id)
        notifier = getattr(self.control, "notifier", None)
        if item and item["status"] == "pending" and notifier is not None:
            self._spawn(notifier.item_created(item))

    async def ask_item(self, kind: str, agent: str, title: str, extra: dict[str, Any],
                       options: list[dict[str, Any]], raw_input: Any = None) -> tuple[dict, asyncio.Future]:
        """ask_tool for any other kind of item the control plane holds (no runner): `browser` (valet.browser's
        asks), `message` and `question` (valet.owner's). `extra`'s fields go into the payload, and so into
        InboxItem (`browser` for a browser ask, `text` for a message or a question); `raw_input` is the tool call's
        rawInput, if any -> (row, future of the answer: the outcome record_answer gets, or None once it expires).
        Shown in the agent's conversation too when exactly one of its turns is running. Kinds in DURABLE wait on no
        call: they outlive it, their future never resolves, and the answer is read from the database. Every new
        item goes through item_created (push, the webhook)."""
        item_id = uuid.uuid4().hex
        turns = self.db.query("SELECT id, session_id FROM turns WHERE agent = ? AND state = 'running'", (agent,))
        sid, tid = (turns[0]["session_id"], turns[0]["id"]) if len(turns) == 1 else (None, None)
        tool_call = {"toolCallId": f"{kind}-{item_id}", "title": title, "kind": "other",
                     **({"rawInput": raw_input} if raw_input is not None else {})}
        self.db.execute(
            "INSERT INTO inbox (id, kind, agent, session_id, turn_id, runner, title, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)",
            (item_id, kind, agent, sid, tid, title, json.dumps({**extra, "tool_call": tool_call, "options": options}),
             time.time()))
        fut = asyncio.get_running_loop().create_future()
        if kind not in DURABLE:
            self._asks[item_id] = fut
        await self.emit_inbox(item_id)
        if sid:
            await self.add_event(agent, sid, tid, "permission_request",
                                 {**extra, "requestId": item_id, "toolCall": tool_call, "options": options,
                                  "kind": kind})
        await self.emit_agent(agent)
        self.item_created(item_id)
        return self.db.one("SELECT * FROM inbox WHERE id = ?", (item_id,)), fut

    def waiting(self, item_id: str) -> bool:
        """A gateway call is still waiting on this item (False after a control-plane restart)."""
        fut = self._asks.get(item_id)
        return fut is not None and not fut.done()

    def _resolve(self, item_id: str, outcome: dict[str, Any] | None) -> None:
        fut = self._asks.pop(item_id, None)
        if fut is not None and not fut.done():
            fut.set_result(outcome)

    async def expire_gateway_asks(self) -> None:
        """At startup: gateway calls don't survive a restart, so their pending items can't be answered (the
        DURABLE kinds wait on no call, and stay)."""
        for row in self.db.query(f"SELECT * FROM inbox WHERE runner IS NULL AND status = 'pending' AND {_NOT_DURABLE}"):
            await self.expire_item(row)

    async def emit_agent(self, name: str) -> None:
        """The agent changed (its definition, state or sessions): tell the clients, and the scheduler, which looks
        at its triggers again. Nothing goes to the clients for a name without a definition (a deleted agent: a late
        frame mustn't bring it back to their lists)."""
        if (scheduler := getattr(self.control, "scheduler", None)) is not None:
            scheduler.poke()
        if not NAME_RE.match(name) or not self.control.defs.exists(name):
            return
        await self.broadcast({"type": "agent", "agent": views.agent_view(self.control, name)})

    async def emit_session(self, agent: str, sid: str) -> None:
        s = views.session(self.db, agent, sid)
        if s:
            await self.broadcast({"type": "session", "session": s})

    async def emit_inbox(self, item_id: str) -> None:
        item = views.inbox_item(self.db, item_id)
        if item:
            await self.broadcast({"type": "inbox", "item": item})

    async def _emit_runner(self, name: str) -> None:
        row = self.db.one("SELECT * FROM runners WHERE name = ?", (name,))
        if row:
            view = views.runner_view(row, self.connected(), self.harnesses(name))
            await self.broadcast({"type": "runner", "runner": view})
        for a in self.db.query("SELECT name FROM agents WHERE runner = ?", (name,)):
            if self.control.defs.exists(a["name"]):
                await self.emit_agent(a["name"])

    # ---- clients ----
    def client_hello(self) -> dict[str, Any]:
        last = self.db.one("SELECT MAX(id) AS id FROM events")
        return {"type": "hello", "agents": views.agents(self.control), "runners": views.runners(self.control),
                "inbox": views.pending_inbox(self.db), "last_event_id": (last and last["id"]) or 0}

    async def serve_client(self, ws: WebSocket, token: str) -> None:
        """Own one authenticated owner socket (`token`: the session it came with): send a `hello` snapshot, then
        forward broadcasts until it closes."""
        queue: asyncio.Queue = asyncio.Queue(CLIENT_QUEUE)
        queue.put_nowait(self.client_hello())     # snapshot and registration with no await between: no gap
        self._clients[ws] = queue
        sender = asyncio.create_task(self._send_to_client(ws, queue))
        watcher = asyncio.create_task(self._watch_owner(ws, token))
        try:
            while True:
                if (await ws.receive())["type"] == "websocket.disconnect":
                    break
        except Exception:
            pass
        finally:
            self._clients.pop(ws, None)
            sender.cancel()
            watcher.cancel()

    async def _watch_owner(self, ws: WebSocket, token: str) -> None:
        """A new owner password (set in the CLI, another process) or the session's age signs the socket out: notice
        it through the database, as _watch_token does a runner's."""
        while True:
            await asyncio.sleep(TOKEN_CHECK)
            if not auth.is_owner_token(self.db, token):
                log.info("client socket: the owner's session ended, disconnecting")
                with contextlib.suppress(Exception):
                    await ws.close(CLOSE_UNAUTHORIZED)
                return

    async def _send_to_client(self, ws: WebSocket, queue: asyncio.Queue) -> None:
        try:
            while (message := await queue.get()) is not None:
                await ws.send_text(json.dumps(message))
        except Exception:
            pass
        with contextlib.suppress(Exception):
            await ws.close()

    async def broadcast(self, message: dict[str, Any]) -> None:
        """Send to every client socket; drop sockets that fail. Queued per client, so a slow phone can't stall a
        runner's stream; one that falls CLIENT_QUEUE frames behind is closed and catches up on reconnect."""
        for ws, queue in list(self._clients.items()):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                self._drop_client(ws)

    def _drop_client(self, ws: WebSocket) -> None:
        queue = self._clients.pop(ws, None)
        if queue is None:
            return
        while not queue.empty():
            queue.get_nowait()
        queue.put_nowait(None)      # the sender closes the socket

    async def close(self) -> None:
        """Shutdown: close every socket."""
        for link in list(self._runners.values()):
            link.fail_pending()
            with contextlib.suppress(Exception):
                await link.ws.close(1001)
        for ws in list(self._clients):
            self._drop_client(ws)
        for task in list(self._tasks):
            task.cancel()


def _bearer_token(ws: WebSocket) -> str:
    return (ws.headers.get("authorization") or "").strip().removeprefix("Bearer ").strip()


def _dumps(value: Any) -> str | None:
    return json.dumps(value) if value is not None else None
