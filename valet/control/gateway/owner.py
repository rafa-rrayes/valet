"""`valet.owner`: an agent's line to the owner, served in process by the gateway like valet.agents.

- message(text): a note in the owner's inbox (kind `message`, one option: Dismiss).
- ask(question, options?): a question in the owner's inbox (kind `question`), answered with one of the options or in
  the owner's own words. The call returns at once; the answer comes later as a prompt (source `answer`) in the session that
  asked (when the agent had exactly one turn running, that turn's session), else in its main session.
- schedule_wakeup(prompt, at? | in_minutes?), wakeups(), cancel_wakeup(id): prompts the agent sets for its future
  self, fired by the scheduler (control/scheduler.py) into the session that set them, else its main session.

These calls only ever reach the owner or the agent itself, so the server decides them (`decides_calls`): they're
allowed at every permission level, `locked` included, and its own limits keep an agent from flooding the inbox.
Messages and questions don't expire: they outlive the turn, the agent's stop and a restart (hub.DURABLE)."""
import json
import math
import re
from datetime import datetime
from typing import Any

import mcp_types as types

from valet.shared import cron
from valet.shared.owner import say

from ..prompts import DeliveryError, header
from .upstream import Upstream, UpstreamError, say_tools

TEXT_LIMIT = 20_000         # characters of a message or question
TITLE = 120                 # characters of it the inbox shows as the item's title
OPTIONS = 10                # answer options a question may offer
OPTION_LEN = 200
PENDING = 20                # unanswered messages and questions per agent; more is refused
WAKEUPS = 50                # pending wake-ups per agent
MAX_AHEAD = 366 * 24 * 3600     # how far ahead a wake-up may be set
WAKEUP_PROMPT = 8000
# The options carry an ACP kind, as every ask's do, so a client can render any item's options alike: a choice is an
# allow_once, Dismiss a reject_once.
DISMISS = {"optionId": "dismiss", "name": "Dismiss", "kind": "reject_once"}
ANSWER_TEXT = 20_000        # characters of the owner's own words
ADDITIVE = types.ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
READ_ONLY = types.ToolAnnotations(read_only_hint=True, open_world_hint=False)


def _schema(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


def _result(text: str, data: Any = None) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(text=text)], structured_content=data)


def _error(text: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(text=text)], is_error=True)


# The title is plain text (the inbox list, a push, the owner webhook's Slack or Discord line): the first line's
# Markdown marks go, a link keeps its text. The item's `text` keeps them, for the inbox to render.
LINE_MARK = re.compile(r"^(?:#{1,6}\s+|>\s*|[-*+]\s+)")
LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
INLINE_MARKS = re.compile(r"\*\*|~~|`")


def _title(text: str) -> str:
    first = " ".join(text.strip().splitlines()[0].split()) if text.strip() else ""
    first = INLINE_MARKS.sub("", LINK.sub(r"\1", LINE_MARK.sub("", first))).strip()
    return first if len(first) <= TITLE else first[:TITLE - 1] + "…"


class AnswerError(Exception):
    """An answer the item can't take (status: 409 not pending, 422 not a valid answer)."""

    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


class OwnerServer(Upstream):
    name = "valet.owner"
    description = "Message {owner}, ask {owner} questions, and schedule wake-ups (allowed at every permission level)."
    decides_calls = True

    TOOLS = [
        types.Tool(name="message", title="Message {owner}",
                   description="Put a short note in {owner}'s inbox: something worth knowing (a result, a heads-up). "
                               "It doesn't wait, and {owner} doesn't reply to it; to get an answer, use ask.",
                   input_schema=_schema({"text": {"type": "string", "description": "What to tell {owner}"}}, ["text"]),
                   annotations=ADDITIVE),
        types.Tool(name="ask", title="Ask {owner}",
                   description="Ask {owner} a question, in {owner}'s inbox. Returns at once: carry on, or end your turn. The "
                               "answer comes later as a new prompt in this session (or your main session), starting "
                               "with a `Valet:` line that says it's {owner}'s answer. Give `options` when the answer "
                               "is one of a few choices; {owner} can also answer in words.",
                   input_schema=_schema({"question": {"type": "string"},
                                         "options": {"type": "array", "items": {"type": "string"},
                                                     "maxItems": OPTIONS,
                                                     "description": "Choices {owner} can pick with one tap"}},
                                        ["question"]),
                   annotations=ADDITIVE),
        types.Tool(name="schedule_wakeup", title="Schedule a wake-up",
                   description="Have Valet prompt you later with `prompt`: `at` an ISO-8601 time (without an offset "
                               "it's Valet's time zone) or `in_minutes` from now. It comes in this session (or your "
                               "main session), only while you're running.",
                   input_schema=_schema({"prompt": {"type": "string", "description": "What to tell yourself"},
                                         "at": {"type": "string", "description": "e.g. 2026-11-02T09:00"},
                                         "in_minutes": {"type": "number", "exclusiveMinimum": 0}},
                                        ["prompt"]),
                   annotations=ADDITIVE),
        types.Tool(name="wakeups", title="List wake-ups",
                   description="Your wake-ups that haven't fired yet: id, when, and the prompt.",
                   input_schema=_schema({}, []), annotations=READ_ONLY),
        types.Tool(name="cancel_wakeup", title="Cancel a wake-up",
                   description="Cancel one of your pending wake-ups by its id.",
                   input_schema=_schema({"id": {"type": "integer"}}, ["id"]),
                   annotations=ADDITIVE),
    ]

    def __init__(self, control: Any) -> None:
        self.control = control

    @property
    def db(self):
        return self.control.db

    @property
    def tz(self):
        return cron.zone(self.control.cfg.timezone or None)

    async def list_tools(self, fresh: bool = True) -> list[types.Tool]:
        return say_tools(self.TOOLS)

    async def call_tool(self, tool: str, arguments: dict[str, Any] | None, timeout: float,
                        caller: str) -> types.CallToolResult:
        args = arguments or {}
        handler = {"message": self._message, "ask": self._ask, "schedule_wakeup": self._schedule,
                   "wakeups": self._wakeups, "cancel_wakeup": self._cancel}.get(tool)
        if handler is None:
            raise UpstreamError(f"valet.owner has no tool {tool!r}")
        return await handler(caller, args)

    # ---------- the inbox ----------

    def _text(self, args: dict, key: str) -> str | None:
        v = args.get(key)
        return v if isinstance(v, str) and v.strip() and "\x00" not in v else None

    def _too_many(self, agent: str) -> str | None:
        n = self.db.one("SELECT COUNT(*) AS n FROM inbox WHERE agent = ? AND status = 'pending' "
                        "AND kind IN ('message', 'question')", (agent,))["n"]
        if n >= PENDING:
            return say(f"{{Owner}} has {n} messages and questions from you not yet read; wait for {{owner}} to read "
                       "them before sending more.")
        return None

    async def _message(self, agent: str, args: dict) -> types.CallToolResult:
        text = self._text(args, "text")
        if text is None:
            return _error(say("message needs `text`: what to tell {owner}."))
        if len(text) > TEXT_LIMIT:
            return _error(f"That's {len(text)} characters; a message holds at most {TEXT_LIMIT}.")
        if why := self._too_many(agent):
            return _error(why)
        row, _ = await self.control.hub.ask_item("message", agent, _title(text), {"text": text}, [DISMISS])
        return _result(say("Sent: it's in {owner}'s inbox."), {"id": row["id"]})

    async def _ask(self, agent: str, args: dict) -> types.CallToolResult:
        question = self._text(args, "question")
        if question is None:
            return _error("ask needs `question`.")
        if len(question) > TEXT_LIMIT:
            return _error(f"That's {len(question)} characters; a question holds at most {TEXT_LIMIT}.")
        raw = args.get("options") or []
        if not isinstance(raw, list) or not all(isinstance(o, str) and o.strip() for o in raw):
            return _error(say("`options` is a list of short strings, the choices {owner} can pick."))
        names = list(dict.fromkeys(" ".join(o.split()) for o in raw))
        if len(names) > OPTIONS:
            return _error(f"At most {OPTIONS} options.")
        if any(len(n) > OPTION_LEN for n in names):
            return _error(f"An option is at most {OPTION_LEN} characters; put the detail in the question.")
        if why := self._too_many(agent):
            return _error(why)
        options = [{"optionId": f"option-{i}", "name": n, "kind": "allow_once"}
                   for i, n in enumerate(names, 1)] + [DISMISS]
        row, _ = await self.control.hub.ask_item("question", agent, _title(question), {"text": question}, options)
        where = "this session" if row["session_id"] else "your main session"     # where the answer is delivered
        return _result(say(f"Asked: it's in {{owner}}'s inbox. The answer will come as a message in {where} (a new "
                           "prompt starting with a `Valet:` line); don't wait for it, carry on or end your turn."),
                       {"id": row["id"]})

    # ---------- wake-ups ----------

    def _when(self, args: dict) -> tuple[float | None, str | None]:
        """(due_at, None) or (None, the error)."""
        now = self.control.clock.now()
        at, minutes = args.get("at"), args.get("in_minutes")
        if (at is None) == (minutes is None):
            return None, "Give exactly one of `at` (an ISO-8601 time) and `in_minutes`."
        if minutes is not None:
            if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or not math.isfinite(minutes) \
                    or minutes <= 0:
                return None, "`in_minutes` is a number of minutes greater than 0."
            due = now + minutes * 60
        else:
            if not isinstance(at, str):
                return None, "`at` is an ISO-8601 time, like 2026-11-02T09:00 or 2026-11-02T09:00:00+01:00."
            try:
                dt = datetime.fromisoformat(at.strip().replace("Z", "+00:00"))
            except ValueError:
                return None, f"{at!r} isn't an ISO-8601 time (like 2026-11-02T09:00)."
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=self.tz)
            due = dt.timestamp()
            if due <= now:
                return None, f"{at} is in the past (it's {self._iso(now)} now)."
        if due - now > MAX_AHEAD:
            return None, "A wake-up can be at most a year ahead."
        return due, None

    def _iso(self, ts: float) -> str:
        return datetime.fromtimestamp(ts, self.tz).isoformat(timespec="minutes")

    def _wakeup_view(self, w: dict) -> dict:
        return {"id": w["id"], "at": self._iso(w["due_at"]), "prompt": w["prompt"],
                "session": "this session" if w["session_id"] else "main"}

    async def _schedule(self, agent: str, args: dict) -> types.CallToolResult:
        prompt = self._text(args, "prompt")
        if prompt is None:
            return _error("schedule_wakeup needs `prompt`: what to tell yourself when it fires.")
        if len(prompt) > WAKEUP_PROMPT:
            return _error(f"A wake-up's prompt is at most {WAKEUP_PROMPT} characters.")
        due, err = self._when(args)
        if err:
            return _error(err)
        n = self.db.one("SELECT COUNT(*) AS n FROM wakeups WHERE agent = ? AND status = 'pending'", (agent,))["n"]
        if n >= WAKEUPS:
            return _error(f"You already have {n} wake-ups pending; cancel some first (wakeups, cancel_wakeup).")
        turns = self.db.query("SELECT session_id FROM turns WHERE agent = ? AND state = 'running'", (agent,))
        sid = turns[0]["session_id"] if len(turns) == 1 else None
        cur = self.db.execute("INSERT INTO wakeups (agent, session_id, prompt, due_at, created_at) "
                              "VALUES (?, ?, ?, ?, ?)", (agent, sid, prompt, due, self.control.clock.now()))
        await self._changed(agent)
        w = self.db.one("SELECT * FROM wakeups WHERE id = ?", (cur.lastrowid,))
        where = "this session" if sid else "your main session"
        return _result(f"Wake-up {w['id']} set for {self._iso(due)} ({cron.zone_name(self.tz)}); it comes in "
                       f"{where}.", self._wakeup_view(w))

    async def _wakeups(self, agent: str, args: dict) -> types.CallToolResult:
        rows = self.db.query("SELECT * FROM wakeups WHERE agent = ? AND status = 'pending' ORDER BY due_at, id",
                             (agent,))
        data = {"timezone": cron.zone_name(self.tz), "wakeups": [self._wakeup_view(w) for w in rows]}
        return _result(json.dumps(data, indent=1), data)

    async def _cancel(self, agent: str, args: dict) -> types.CallToolResult:
        wid = args.get("id")
        if isinstance(wid, bool) or not isinstance(wid, int):
            return _error("cancel_wakeup needs `id`, a wake-up's number (see wakeups).")
        cur = self.db.execute("UPDATE wakeups SET status = 'cancelled', done_at = ? WHERE id = ? AND agent = ? "
                              "AND status = 'pending'", (self.control.clock.now(), wid, agent))
        if not cur.rowcount:
            return _error(f"You have no pending wake-up {wid}.")
        await self._changed(agent)
        return _result(f"Wake-up {wid} cancelled.", {"id": wid})

    async def _changed(self, agent: str) -> None:
        if (s := getattr(self.control, "scheduler", None)) is not None:
            s.poke()
        await self.control.hub.emit_agent(agent)


# ---------- the owner's answer ----------

async def answer(control: Any, row: dict, option_id: str | None, text: str | None) -> None:
    """POST /api/inbox/{id}/answer for a message or a question. A message takes Dismiss. A question takes one of
    its options, the owner's own words (`text`), or both; Dismiss closes it without telling the agent. The answer is
    recorded, then prompted into the session that asked, else the agent's main session; queued if the agent can't
    take it now (it's never lost). AnswerError if it can't be taken."""
    payload = json.loads(row["payload"] or "{}")
    options = {o.get("optionId"): o for o in payload.get("options") or []}
    words = text.strip() if isinstance(text, str) else ""
    if "\x00" in words or len(words) > ANSWER_TEXT:
        raise AnswerError(f"an answer is at most {ANSWER_TEXT} characters of text")
    if option_id is not None and option_id not in options:
        raise AnswerError("not one of this item's options")
    if row["kind"] == "message":
        if option_id != DISMISS["optionId"] or words:
            raise AnswerError("a message can only be dismissed")
    elif option_id is None and not words:
        raise AnswerError("answer with one of the options, or in your own words (text)")
    elif option_id == DISMISS["optionId"] and words:
        raise AnswerError("dismiss closes the question without an answer; send text on its own to answer")
    hub = control.hub
    if option_id is not None:
        outcome: dict[str, Any] = {"outcome": "selected", "optionId": option_id}
        if words:
            outcome["text"] = words
    else:
        outcome = {"outcome": "text", "text": words}
    if not await hub.record_answer(row, outcome):
        current = control.db.one("SELECT status FROM inbox WHERE id = ?", (row["id"],))
        raise AnswerError(f"this item is already {current['status'] if current else 'gone'}", 409)
    await hub.emit_agent(row["agent"])
    if row["kind"] == "message" or option_id == DISMISS["optionId"]:
        return
    tz = cron.zone(control.cfg.timezone or None)
    asked = datetime.fromtimestamp(row["created_at"], tz).strftime("%Y-%m-%d %H:%M")
    parts = [f"Your question: {payload.get('text') or row['title']}"]
    if option_id is not None:
        parts.append(say(f"{{Owner}}'s choice: {options[option_id].get('name')}"))
    if words:
        parts.append(say(f"{{Owner}}'s words: {words}" if option_id is not None else f"{{Owner}}'s answer: {words}"))
    body = header(say(f"{{Owner}} answered the question you asked at {asked} (valet.owner ask)."), "\n\n".join(parts))
    label = say("{Owner}'s answer")
    sid = row["session_id"]
    try:
        if sid and control.db.one("SELECT 1 FROM sessions WHERE agent = ? AND id = ?", (row["agent"], sid)):
            await control.prompts.deliver(row["agent"], sid, body, "answer", label, title=f"Answer: {row['title']}")
        else:
            await control.prompts.defer_to_main(row["agent"], body, "answer", label, title=f"Answer: {row['title']}")
    except DeliveryError as e:      # its queue is full
        raise AnswerError(f"the answer is recorded, but it couldn't be sent: {e}", e.status) from None
