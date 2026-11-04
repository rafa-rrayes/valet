"""Prompts into sessions. Every prompt an agent gets goes through here: the owner's, a schedule's or a webhook's,
another agent's message, the owner's answer to a question, the agent's own wake-up, and the owner's Retry.

- A prompt for a session starts a turn at once if the session is idle and nothing is queued ahead of it; otherwise
  it goes to the end of that session's queue (table prompt_queue). The queue drains one turn at a time: when a
  turn ends, and when the agent comes up again. Queued prompts wait while the agent is stopped.
- Taking the head of a queue, the turn row and its `user_message` event happen in one transaction, and only then is
  the prompt sent. So after a crash a prompt is either still queued or a turn: one the runner got (it goes on), or
  one the runner's next hello doesn't list, which becomes `interrupted` and is sent again only if the owner asks
  (Retry). Never lost, never sent twice.
- Each agent has a main session, where triggers, other agents and answers go (unless a trigger says
  `session: new`). It's made on first use (session/new) and kept in agents.main_session. If the harness can no
  longer resume it (its home was wiped, or the agent moved to another harness), the next prompt makes a new one,
  and the prompts that were going to the old one move there, the one that failed included (it never reached the
  model) unless the owner typed it.
- The owner's prompt may carry attachments (control/attachments.py), placed before it gets here: the turn records
  one `user_message` event per block (the text, then each attachment), and a queued prompt keeps their ids
  (prompt_queue.attachments), as the turn does (turns.attachments, for Retry).
- Prompts that don't come from the owner typing start with one line, `Valet: ...`, saying where they came from
  (header()); the turn records the source (owner, schedule, webhook:<name>, agent:<name>, wakeup, answer) and a
  short label for the apps, and its `user_message` event carries both."""
import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from valet.shared.events import Event
from valet.shared.owner import say
from valet.shared.protocol import SessionNew, SessionPrompt

from . import views
from .hub import RunnerError

log = logging.getLogger(__name__)

MAIN = "main"               # deliver(): the agent's main session
NEW = "new"                 # deliver(): a new session
LOST = "cannot resume session"      # how the runner says the harness doesn't have a session any more
# How the runner ends an agent's running turns when the agent itself goes down (stopped, its harness exited, its
# network proxy gone), just before it reports the agent's state: the queue then waits for the agent to be up again.
DOWN = ("agent stopped", "agent exited", "its network proxy stopped")
TITLE = 80                  # characters of the first prompt a session takes as its title
QUEUE_LIMIT = 100           # prompts queued per session; more is refused (something is looping)


class DeliveryError(Exception):
    """A prompt that couldn't be placed at all: no such session (404), the agent can't take it (503), the queue is
    full (429), or the runner failed making a session (502/504). str() is shown as is."""

    def __init__(self, message: str, status: int = 503) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class Delivery:
    """Where a prompt went. turn_id: it started (error: the runner refused it, so that turn ended failed; in_doubt:
    the link dropped after sending, the runner's next hello settles it). queue_id: it waits in the queue."""
    session_id: str
    turn_id: str | None = None
    queue_id: int | None = None
    error: str | None = None
    status: int | None = None       # the HTTP status for `error`
    in_doubt: bool = False

    @property
    def outcome(self) -> str:
        if self.queue_id is not None:
            return "queued"
        return "failed" if self.error and not self.in_doubt else "started"


def header(line: str, body: str) -> str:
    """A prompt from anyone but the owner typing: one `Valet: <line>` line, a blank line, then the body."""
    return f"Valet: {line}\n\n{body}" if body else f"Valet: {line}"


def source_label(source: str) -> str:
    """A label for a source no caller named."""
    kind, _, name = source.partition(":")
    return {"owner": "You", "schedule": "Schedule", "wakeup": "Wake-up", "answer": say("{Owner}'s answer"),
            "webhook": f"Webhook {name}", "agent": f"From {name}"}.get(kind, source)


class Prompts:
    def __init__(self, control: Any) -> None:
        self.control = control
        self._main_locks: dict[str, asyncio.Lock] = {}

    @property
    def db(self):
        return self.control.db

    @property
    def hub(self):
        return self.control.hub

    def now(self) -> float:
        return self.control.clock.now()

    # ---------- where an agent is ----------

    def runner_of(self, agent: str) -> str | None:
        """The connected runner the agent runs on, if it's running and the owner wants it running."""
        row = self.db.one("SELECT runner, state, desired FROM agents WHERE name = ?", (agent,))
        if not row or row["desired"] != "running" or row["state"] != "running":
            return None
        return row["runner"] if row["runner"] in self.hub.connected() else None

    def why_not(self, agent: str) -> str:
        """Why the agent can't take a prompt now, for the owner and the trigger runs."""
        row = self.db.one("SELECT runner, state, desired, error FROM agents WHERE name = ?", (agent,))
        if not row or row["desired"] != "running":
            return f"{agent} is stopped"
        if row["state"] == "running" and row["runner"] not in self.hub.connected():
            return f"{agent}'s runner {row['runner']} is offline"
        if row["state"] == "starting":
            return f"{agent} is starting"
        if row["state"] == "failed":
            return f"{agent} failed" + (f": {row['error'].splitlines()[0]}" if row["error"] else "")
        if row["runner"] and row["runner"] not in self.hub.connected():
            return f"{agent}'s runner {row['runner']} is offline"
        return f"{agent} isn't running"

    # ---------- sessions ----------

    async def new_session(self, agent: str) -> str:
        """ACP session/new on the agent's runner; the session row; a `session` frame. DeliveryError if the agent
        can't make one now."""
        runner = self.runner_of(agent)
        if runner is None:
            raise DeliveryError(self.why_not(agent), 503)
        try:
            result = await self.hub.call(runner, SessionNew(id=uuid.uuid4().hex, agent=agent).dump())
        except RunnerError as e:
            raise DeliveryError(f"couldn't make a session: {e}", e.status) from None
        sid = result.get("session_id")
        if not isinstance(sid, str) or not sid:
            raise DeliveryError("the runner didn't return a session id", 502)
        now = self.now()
        self.db.execute("INSERT OR IGNORE INTO sessions (agent, id, created_at, updated_at) VALUES (?, ?, ?, ?)",
                        (agent, sid, now, now))
        await self.hub.emit_session(agent, sid)
        return sid

    def main_of(self, agent: str) -> str | None:
        row = self.db.one("SELECT a.main_session AS sid FROM agents a JOIN sessions s ON s.agent = a.name "
                          "AND s.id = a.main_session WHERE a.name = ?", (agent,))
        return row["sid"] if row else None

    async def main_session(self, agent: str) -> str:
        """The agent's main session, made now if it has none (one at a time per agent). DeliveryError if it has none
        and can't make one."""
        if sid := self.main_of(agent):
            return sid
        lock = self._main_locks.setdefault(agent, asyncio.Lock())
        async with lock:
            if sid := self.main_of(agent):
                return sid
            sid = await self.new_session(agent)
            self.db.execute("UPDATE agents SET main_session = ?, updated_at = ? WHERE name = ?",
                            (sid, self.now(), agent))
            # Prompts waiting for a main session (defer_to_main) go in this one, whoever made it.
            self.db.execute("UPDATE prompt_queue SET session_id = ? WHERE agent = ? AND session_id IS NULL",
                            (sid, agent))
            log.info("agent %s: main session %s", agent, sid)
        await self.hub.emit_session(agent, sid)
        await self.hub.emit_agent(agent)
        return sid

    # ---------- placing a prompt ----------

    async def deliver(self, agent: str, target: str, text: str, source: str, label: str | None = None,
                      *, title: str | None = None, defer: bool = True) -> Delivery:
        """A prompt for `target`: a session id, MAIN or NEW. DeliveryError if there's no session to put it in.
        With `defer` (everyone but the owner typing), a prompt for a session the agent can't take now waits in the
        queue instead of starting a turn that would fail at once."""
        if target == NEW:
            sid = await self.new_session(agent)
        elif target == MAIN:
            sid = await self.main_session(agent)
        else:
            sid = target
        return await self.submit(agent, sid, text, source, label, title=title, defer=defer)

    async def defer_to_main(self, agent: str, text: str, source: str, label: str | None = None,
                            *, title: str | None = None) -> Delivery | None:
        """For a prompt that must not be lost while the agent is down (The owner's answer): into its main session, queued
        if the agent can't take it now; with no main session yet and none to be made now, queued for the one it
        gets when it next runs. None if queued that way."""
        sid = self.main_of(agent)
        if sid is None and self.runner_of(agent) is not None:
            try:
                sid = await self.main_session(agent)
            except DeliveryError as e:
                log.warning("agent %s: no main session for now (%s); the prompt waits for one", agent, e)
        if sid is None:
            self._enqueue(agent, None, text, source, label)
            await self.hub.emit_agent(agent)
            return None
        return await self.submit(agent, sid, text, source, label, title=title, defer=True)

    async def submit(self, agent: str, sid: str, text: str, source: str = "owner", label: str | None = None,
                     *, retry_of: str | None = None, title: str | None = None, defer: bool = False,
                     attachments: list[dict] | None = None) -> Delivery:
        """Start a turn in `sid` now, or queue the prompt (see the module docstring). A turn started now is sent at
        once; if the runner refuses it, it ends failed and Delivery.error says why (the caller decides what to tell).
        DeliveryError: no such session (404), its queue is full (429). `attachments`: rows Attachments.place()
        readied, which this prompt now owns (Attachments.claim)."""
        label = label or source_label(source)
        rows = attachments or []
        up = self.runner_of(agent) is not None
        with self.db.transaction():
            s = self.db.one("SELECT state FROM sessions WHERE agent = ? AND id = ?", (agent, sid))
            if s is None:
                raise DeliveryError("no such session", 404)
            ahead = self.db.one("SELECT COUNT(*) AS n FROM prompt_queue WHERE agent = ? AND session_id = ?",
                                (agent, sid))["n"]
            if s["state"] == "idle" and not ahead and (up or not defer):
                turn_id, events = self._begin(agent, sid, text, source, label, retry_of, title, rows)
                qid = None
            elif ahead >= QUEUE_LIMIT:
                raise DeliveryError(f"{QUEUE_LIMIT} prompts are already waiting in this session", 429)
            else:
                qid = self._enqueue(agent, sid, text, source, label, retry_of, attachments=rows)
        if qid is not None:
            await self.hub.emit_session(agent, sid)
            await self.hub.emit_agent(agent)
            if up and s["state"] == "idle":
                self.hub.spawn(self.pump(agent, sid))     # the agent was down when these queued: catch up
            return Delivery(sid, queue_id=qid)
        await self._started(agent, sid, events)
        return await self._send(agent, sid, turn_id, text, rows)

    def _enqueue(self, agent: str, sid: str | None, text: str, source: str, label: str | None,
                 retry_of: str | None = None, pos: float | None = None, attachments: list[dict] | None = None) -> int:
        ids = [r["id"] for r in attachments or []]
        cur = self.db.execute(
            "INSERT INTO prompt_queue (agent, session_id, pos, text, source, label, retry_of, created_at, attachments) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (agent, sid, self.now() if pos is None else pos, text, source, label or source_label(source), retry_of,
             self.now(), json.dumps(ids) if ids else None))
        if attachments:
            self.control.attachments.claim(attachments, sid)
        return cur.lastrowid

    def _attachments_of(self, agent: str, row: dict) -> list[dict]:
        """A queue row's or a turn's attachments (those still here)."""
        ids = json.loads(row["attachments"]) if row.get("attachments") else []
        return self.control.attachments.rows(agent, ids) if ids else []

    def _begin(self, agent: str, sid: str, text: str, source: str, label: str, retry_of: str | None,
               title: str | None, attachments: list[dict] | None = None) -> tuple[str, list[Event]]:
        """Inside a transaction: the turn row, the session working (titled by its first prompt), its user_message
        events (the text, then one per attachment). Returns (turn_id, the events to broadcast once committed)."""
        rows = attachments or []
        turn_id, now = uuid.uuid4().hex, self.now()
        ids = [r["id"] for r in rows]
        self.db.execute("INSERT INTO turns (id, agent, session_id, started_at, source, source_label, prompt, retry_of, "
                        "attachments) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (turn_id, agent, sid, now, source, label, text, retry_of, json.dumps(ids) if ids else None))
        first = (title if title is not None else text).strip() or ", ".join(r["name"] for r in rows)
        self.db.execute("UPDATE sessions SET state = 'working', updated_at = ?, title = COALESCE(title, ?) "
                        "WHERE agent = ? AND id = ?", (now, first[:TITLE] or None, agent, sid))
        blocks = ([{"type": "text", "text": text}] if text or not rows else []) + \
            self.control.attachments.stored_blocks(agent, rows)
        events = []
        for content in blocks:
            data: dict[str, Any] = {"content": content, "source": source, "label": label}
            if retry_of:
                data["retry_of"] = retry_of
            cur = self.db.execute("INSERT INTO events (agent, session_id, turn_id, kind, data, ts) VALUES "
                                  "(?, ?, ?, ?, ?, ?)", (agent, sid, turn_id, "user_message", json.dumps(data), now))
            events.append(Event(id=cur.lastrowid, agent=agent, session_id=sid, turn_id=turn_id, ts=now,
                                kind="user_message", data=data))
        if rows:
            self.control.attachments.claim(rows, sid)
        return turn_id, events

    async def _started(self, agent: str, sid: str, events: list[Event]) -> None:
        for event in events:
            await self.hub.broadcast({"type": "event", "event": event})
        await self.hub.emit_session(agent, sid)
        await self.hub.emit_agent(agent)

    async def _send(self, agent: str, sid: str, turn_id: str, text: str,
                    attachments: list[dict] | None = None) -> Delivery:
        """session.prompt for a turn already recorded. A refusal ends the turn failed (Delivery.error)."""
        runner = self.runner_of(agent)
        status, error = 503, None
        if runner is None:
            error = self.why_not(agent)
        else:
            try:
                prompt = [{"type": "text", "text": text}] if text or not attachments else []
                if attachments:
                    prompt += await self.control.attachments.prompt_blocks(agent, attachments)
                await self.hub.call(runner, SessionPrompt(id=uuid.uuid4().hex, agent=agent, session_id=sid,
                                                          turn_id=turn_id, prompt=prompt).dump())
                return Delivery(sid, turn_id=turn_id)
            except RunnerError as e:
                if e.in_doubt:
                    # The link dropped after sending: the runner may be running it. Its next hello settles the
                    # turn (listed: it goes on; not listed: interrupted).
                    return Delivery(sid, turn_id=turn_id, error=str(e), status=e.status, in_doubt=True)
                status, error = e.status, str(e)
        if await self.hub.end_turn(turn_id, "failed", None, error):
            await self.hub.emit_agent(agent)
        return Delivery(sid, turn_id=turn_id, error=error, status=status)

    # ---------- draining ----------

    async def pump(self, agent: str, sid: str) -> None:
        """Send the head of `sid`'s queue if the session is idle and the agent can take it now."""
        if self.runner_of(agent) is None:
            return
        with self.db.transaction():
            s = self.db.one("SELECT state FROM sessions WHERE agent = ? AND id = ?", (agent, sid))
            head = self.db.one("SELECT * FROM prompt_queue WHERE agent = ? AND session_id = ? ORDER BY pos, id LIMIT 1",
                               (agent, sid))
            if s is None or s["state"] != "idle" or head is None:
                return
            self.db.execute("DELETE FROM prompt_queue WHERE id = ?", (head["id"],))
            rows = self._attachments_of(agent, head)
            turn_id, events = self._begin(agent, sid, head["text"], head["source"], head["label"], head["retry_of"],
                                          None, rows)
        await self._started(agent, sid, events)
        await self._send(agent, sid, turn_id, head["text"], rows)

    async def pump_agent(self, agent: str) -> None:
        """The agent came up: prompts waiting for a main session get one, then each session with a queue drains."""
        if self.runner_of(agent) is None:
            return
        if self.db.one("SELECT 1 FROM prompt_queue WHERE agent = ? AND session_id IS NULL LIMIT 1", (agent,)):
            try:
                main = await self.main_session(agent)
            except DeliveryError as e:
                log.warning("agent %s: prompts wait for a main session: %s", agent, e)
            else:
                self.db.execute("UPDATE prompt_queue SET session_id = ? WHERE agent = ? AND session_id IS NULL",
                                (main, agent))
                await self.hub.emit_session(agent, main)
        for row in self.db.query("SELECT DISTINCT session_id FROM prompt_queue "
                                 "WHERE agent = ? AND session_id IS NOT NULL", (agent,)):
            await self.pump(agent, row["session_id"])

    async def after_turn(self, turn_id: str) -> None:
        """A turn ended (Hub.end_turn): a main session the harness lost is replaced; then the next prompt goes."""
        turn = self.db.one("SELECT * FROM turns WHERE id = ?", (turn_id,))
        if not turn:
            return
        agent, sid = turn["agent"], turn["session_id"]
        if turn["state"] == "failed" and (turn["error"] or "").startswith(LOST) and self._main_lost(turn):
            await self.hub.emit_session(agent, sid)
            await self.hub.emit_agent(agent)
            await self.pump_agent(agent)
            return
        if turn["state"] == "failed" and turn["error"] in DOWN:
            return          # the agent is going down: pump_agent drains the queue when it's running again
        await self.pump(agent, sid)

    def _main_lost(self, turn: dict) -> bool:
        """The harness can't resume the agent's main session: forget it, and move what was going to it (the prompt
        that failed, unless the owner typed it, then the queue) to the next main session. False if `turn` wasn't in
        the main session."""
        agent, sid = turn["agent"], turn["session_id"]
        with self.db.transaction():
            cur = self.db.execute("UPDATE agents SET main_session = NULL WHERE name = ? AND main_session = ?",
                                  (agent, sid))
            if not cur.rowcount:
                return False
            first = self.db.one("SELECT MIN(pos) AS pos FROM prompt_queue WHERE agent = ? AND session_id = ?",
                                (agent, sid))["pos"]
            self.db.execute("UPDATE prompt_queue SET session_id = NULL WHERE agent = ? AND session_id = ?",
                            (agent, sid))
            if turn["source"] != "owner" and turn["prompt"]:
                pos = min(first if first is not None else turn["started_at"], turn["started_at"]) - 1
                self._enqueue(agent, None, turn["prompt"], turn["source"], turn["source_label"], pos=pos)
        log.warning("agent %s: the harness can't resume its main session %s; the next prompt makes a new one",
                    agent, sid)
        return True

    # ---------- the queue, the owner's way ----------

    def queued(self, agent: str, sid: str) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT * FROM prompt_queue WHERE agent = ? AND session_id = ? ORDER BY pos, id",
                             (agent, sid))
        return [views.queued_view(r, None, views.queued_attachments(self.db, r)) for r in rows]

    async def remove(self, agent: str, sid: str, queue_id: int) -> bool:
        """A queued prompt the owner took back; its attachments go with it."""
        row = self.db.one("SELECT * FROM prompt_queue WHERE id = ? AND agent = ? AND session_id = ?",
                          (queue_id, agent, sid))
        cur = self.db.execute("DELETE FROM prompt_queue WHERE id = ? AND agent = ? AND session_id = ?",
                              (queue_id, agent, sid))
        if not cur.rowcount:
            return False
        self.control.attachments.discard(self._attachments_of(agent, row))
        await self.hub.emit_session(agent, sid)
        await self.hub.emit_agent(agent)
        return True

    def prompt_of(self, turn: dict) -> str:
        """The text a turn was sent: turns.prompt, or (a turn from before it was recorded) its user_message events."""
        if turn.get("prompt"):
            return turn["prompt"]
        rows = self.db.query("SELECT data FROM events WHERE turn_id = ? AND kind = 'user_message' ORDER BY id",
                             (turn["id"],))
        return "".join((json.loads(r["data"]).get("content") or {}).get("text", "") for r in rows)

    async def retry(self, agent: str, sid: str, turn_id: str) -> Delivery:
        """The owner's Retry of an interrupted or failed turn: its prompt again, as a new turn (or queued), with the
        same source. DeliveryError: no such turn (404), not one to retry or already retried (409), the agent can't
        take it (503)."""
        turn = self.db.one("SELECT * FROM turns WHERE id = ? AND agent = ? AND session_id = ?", (turn_id, agent, sid))
        if turn is None:
            raise DeliveryError("no such turn", 404)
        if turn["state"] not in ("interrupted", "failed"):
            raise DeliveryError(f"only an interrupted or failed turn can be retried; this one is {turn['state']}", 409)
        if (self.db.one("SELECT 1 FROM turns WHERE retry_of = ?", (turn_id,))
                or self.db.one("SELECT 1 FROM prompt_queue WHERE retry_of = ?", (turn_id,))):
            raise DeliveryError("this turn was already retried", 409)
        text = self.prompt_of(turn)
        rows = self._attachments_of(agent, turn)
        if not text.strip() and not rows:
            raise DeliveryError("this turn has no prompt to send again", 409)
        if self.runner_of(agent) is None:
            raise DeliveryError(self.why_not(agent) + ": start it, then retry", 503)
        return await self.submit(agent, sid, text, turn["source"] or "owner", turn["source_label"], retry_of=turn_id,
                                 attachments=rows)
