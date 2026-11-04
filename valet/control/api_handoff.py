"""Continuing and handing off conversations (docs/API.md, "Continuing and handing off"):

- continue: a Claude Code session that ran outside Valet (in a terminal, say) goes on in a Claude Code agent
  on the same machine whose working folder is the session's, with its history (session.import);
- hand off: any conversation, a Claude Code transcript the runner can read or a Valet session of any agent, becomes
  the first message of a new session of any agent, condensed (shared/handoff.py).

The Claude Code side is read by a runner (claude.sessions, claude.transcript): the transcripts are on its machine."""
import time
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from valet.shared import handoff
from valet.shared.definition import DefinitionError
from valet.shared.protocol import ClaudeSessions, ClaudeTranscript, SessionImport, SessionNew, SessionPrompt
from valet.shared.transcripts import SESSION_RE

from . import auth, views
from .api import _agent_or_404, _agent_runner, _control, _runner_error, _session_or_404
from .hub import RunnerError

router = APIRouter(prefix="/api", dependencies=[Depends(auth.require_owner)])
IMPORT_TIMEOUT = 300.0      # a load starts Claude Code and replays the whole transcript
TITLE_MAX = 80


def _id() -> str:
    return uuid.uuid4().hex


def _call_error(e: RunnerError) -> HTTPException:
    """The runner's refusals as the owner's errors: no such folder or session 404, already open 409, not the
    kind of agent that can 422; anything else as any runner error."""
    message = str(e)
    for prefix, status in (("no Claude Code", 404), ("already open", 409), ("only a Claude Code agent", 422),
                           ("this harness can't load", 422)):
        if message.startswith(prefix):
            return HTTPException(status, message)
    return _runner_error(e)


def _claude_here(c, name: str) -> None:
    """422 unless the agent is Claude Code at computer level host: the only kind that can continue a Claude Code
    session (its transcripts are on the runner's machine, under the runner's user)."""
    try:
        d, _ = c.defs.load(name)
    except (DefinitionError, FileNotFoundError):
        raise HTTPException(422, f"{name}'s definition doesn't load") from None
    if d.harness != "claude" or d.computer.level != "host":
        raise HTTPException(422, "only a Claude Code agent on this machine (computer level host) can continue a "
                                 "Claude Code session; hand the conversation off instead")


def _holders(c, ids: list[str]) -> dict[str, str]:
    """Session id -> the agent Valet has it under, for the ones it has."""
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    return {r["id"]: r["agent"] for r in c.db.query(f"SELECT id, agent FROM sessions WHERE id IN ({marks})", ids)}


def _runner(c, name: str | None) -> str:
    """The runner named, else the first connected one (by name); 503 if it isn't connected."""
    connected = sorted(c.hub.connected())
    if name is None:
        if not connected:
            raise HTTPException(503, "no runner is connected")
        return connected[0]
    if name not in connected:
        raise HTTPException(503, f"runner {name} is not connected")
    return name


# ---------- continuing a Claude Code session ----------

@router.get("/agents/{name}/claude-sessions")
async def claude_sessions(name: str, request: Request):
    """The Claude Code sessions this agent can continue: the ones that ran in its working folder."""
    c = _control(request)
    _agent_or_404(c, name)
    _claude_here(c, name)
    runner = _agent_runner(c, name)
    try:
        r = await c.hub.call(runner, ClaudeSessions(id=_id(), agent=name).dump())
    except RunnerError as e:
        raise _call_error(e) from None
    sessions = [s for s in r.get("sessions") or [] if isinstance(s, dict) and SESSION_RE.match(str(s.get("id")))]
    have = _holders(c, [s["id"] for s in sessions])
    return {"cwd": r.get("cwd"), "runner": runner, "sessions": [{**s, "imported": have.get(s["id"])} for s in sessions]}


def _drop_session(c, name: str, sid: str) -> None:
    """A failed import: what it stored of the session goes."""
    for table, col in (("events", "session_id"), ("turns", "session_id"), ("sessions", "id")):
        c.db.execute(f"DELETE FROM {table} WHERE agent = ? AND {col} = ?", (name, sid))


@router.post("/agents/{name}/claude-sessions/{sid}/import", status_code=201)
async def import_claude_session(name: str, sid: str, request: Request):
    """The session goes on here: session/load in the agent's harness, its history stored as events (each exchange
    a turn). -> Session."""
    c = _control(request)
    _agent_or_404(c, name)
    _claude_here(c, name)
    if not SESSION_RE.match(sid):
        raise HTTPException(404, "no such Claude Code session")
    if holder := _holders(c, [sid]).get(sid):
        raise HTTPException(409, f"Valet already has this session ({holder})")
    runner = _agent_runner(c, name)
    now = time.time()
    c.db.execute("INSERT INTO sessions (agent, id, created_at, updated_at) VALUES (?, ?, ?, ?)", (name, sid, now, now))
    try:
        r = await c.hub.call(runner, SessionImport(id=_id(), agent=name, session_id=sid).dump(), timeout=IMPORT_TIMEOUT)
    except RunnerError as e:
        if e.in_doubt or e.status == 504:   # it may still be loading: the session stays, its history comes as it does
            raise HTTPException(e.status, f"{e}; the import goes on if the runner got it") from None
        _drop_session(c, name, sid)
        raise _call_error(e) from None
    title = r.get("title") if isinstance(r.get("title"), str) else None
    c.db.execute("UPDATE sessions SET title = COALESCE(title, ?), updated_at = ? WHERE agent = ? AND id = ?",
                 ((title or "").strip()[:TITLE_MAX] or None, time.time(), name, sid))
    await c.hub.emit_session(name, sid)
    return views.session(c.db, name, sid)


# ---------- Claude Code transcripts on a runner's machine ----------

@router.get("/claude/folders")
async def claude_folders(request: Request, runner: str | None = None):
    """The Claude Code project folders on a runner's machine that have sessions, newest first."""
    c = _control(request)
    name = _runner(c, runner)
    try:
        r = await c.hub.call(name, ClaudeSessions(id=_id()).dump())
    except RunnerError as e:
        raise _call_error(e) from None
    return {"runner": name, "folders": r.get("folders") or []}


@router.get("/claude/sessions")
async def claude_folder_sessions(request: Request, folder: str = Query(..., min_length=1, max_length=255),
                                 runner: str | None = None):
    """One project folder's sessions, newest first."""
    c = _control(request)
    name = _runner(c, runner)
    try:
        r = await c.hub.call(name, ClaudeSessions(id=_id(), folder=folder).dump())
    except RunnerError as e:
        raise _call_error(e) from None
    sessions = r.get("sessions") or []
    have = _holders(c, [s["id"] for s in sessions if isinstance(s, dict) and isinstance(s.get("id"), str)])
    return {"runner": name, "folder": folder, "sessions": [{**s, "imported": have.get(s.get("id"))} for s in sessions]}


# ---------- hand-off ----------

class ClaudeSource(BaseModel):
    kind: Literal["claude"]
    folder: str = Field(min_length=1, max_length=255)
    session_id: str
    runner: str | None = None       # default: the first connected runner


class SessionSource(BaseModel):
    kind: Literal["session"]
    agent: str
    session_id: str


class Handoff(BaseModel):
    source: Annotated[ClaudeSource | SessionSource, Field(discriminator="kind")]
    note: str = Field("", max_length=20_000)    # what to do now; it ends the message


async def _conversation(c, source: ClaudeSource | SessionSource) -> dict[str, Any]:
    """-> {"source" (the header's words for it), "title", "entries", "omitted"}."""
    if isinstance(source, ClaudeSource):
        runner = _runner(c, source.runner)
        try:
            r = await c.hub.call(runner, ClaudeTranscript(id=_id(), folder=source.folder, session_id=source.session_id,
                                                          budget=handoff.MAX_BYTES).dump())
        except RunnerError as e:
            raise _call_error(e) from None
        title = str(r.get("title") or "")
        where = f" in {r['cwd']}" if r.get("cwd") else ""
        named = f'"{title}", ' if title else ""
        return {"source": f"a Claude Code session ({named}{source.session_id}) that ran{where} on runner {runner}",
                "title": title or "a Claude Code session", "entries": list(r.get("entries") or []),
                "omitted": int(r.get("omitted") or 0)}
    _agent_or_404(c, source.agent)
    s = _session_or_404(c, source.agent, source.session_id)
    rows = c.db.query("SELECT * FROM events WHERE agent = ? AND session_id = ? ORDER BY id",
                      (source.agent, source.session_id))
    entries = handoff.from_events([views.event_view(r) for r in rows])
    title = s["title"] or "a session"
    return {"source": f'the Valet session "{title}" with agent {source.agent}', "title": title, "entries": entries,
            "omitted": 0}


async def _message(c, body: Handoff) -> dict[str, Any]:
    conv = await _conversation(c, body.source)
    if not conv["entries"]:
        raise HTTPException(422, "that conversation has nothing to hand off")
    out = handoff.compose(conv["source"], conv["entries"], body.note, conv["omitted"])
    return {**out, "title": conv["title"], "source": conv["source"], "bytes": len(out["text"].encode())}


@router.post("/handoff/preview")
async def preview(body: Handoff, request: Request):
    """The message a hand-off would start with: {"text", "bytes", "entries" (kept), "omitted", "title", "source"}."""
    return await _message(_control(request), body)


@router.post("/agents/{name}/handoff", status_code=201)
async def hand_off(name: str, body: Handoff, request: Request):
    """A new session of this agent whose first message is the hand-off (the event marked `handoff`). ->
    {"session": Session, "turn_id"}."""
    c = _control(request)
    _agent_or_404(c, name)
    msg = await _message(c, body)
    runner = _agent_runner(c, name)
    try:
        result = await c.hub.call(runner, SessionNew(id=_id(), agent=name).dump())
    except RunnerError as e:
        raise _runner_error(e) from None
    sid = result.get("session_id")
    if not isinstance(sid, str) or not sid:
        raise HTTPException(502, "the runner didn't return a session id")
    turn_id, now = _id(), time.time()
    title = ("Continued: " + msg["title"])[:TITLE_MAX]
    content = {"type": "text", "text": msg["text"]}
    mark = {"source": msg["source"], "entries": msg["entries"], "omitted": msg["omitted"], "note": body.note.strip()}
    c.db.execute("INSERT OR IGNORE INTO sessions (agent, id, title, state, created_at, updated_at) "
                 "VALUES (?, ?, ?, 'working', ?, ?)", (name, sid, title, now, now))
    c.db.execute("INSERT INTO turns (id, agent, session_id, started_at) VALUES (?, ?, ?, ?)", (turn_id, name, sid, now))
    await c.hub.add_event(name, sid, turn_id, "user_message", {"content": content, "handoff": mark})
    await c.hub.emit_session(name, sid)
    await c.hub.emit_agent(name)
    try:
        await c.hub.call(runner, SessionPrompt(id=_id(), agent=name, session_id=sid, turn_id=turn_id,
                                               prompt=[content]).dump())
    except RunnerError as e:
        if not e.in_doubt and await c.hub.end_turn(turn_id, "failed", None, str(e)):
            await c.hub.emit_agent(name)
        raise _runner_error(e) from None
    return {"session": views.session(c.db, name, sid), "turn_id": turn_id}
