"""Deleting an agent (DELETE /api/agents/{name}, docs/API.md): it stops, its runner forgets it (agent.forget), its
folder leaves the definitions repo in one commit (the history keeps it), and the control plane forgets everything it
kept under its name (forget_agent), so an agent made again with that name starts clean. The audit logs stay."""
import asyncio
import time
import uuid
from typing import Any

from valet.shared.definition import NAME_RE, OWNER_NAME
from valet.shared.protocol import AgentForget

from . import auth
from .hub import RunnerError

FORGET_TIMEOUT = 120.0      # seconds agent.forget may take: a container stopping, its volumes going
# What the control plane keeps per agent, by its `agent` column (the agents row is by `name`). The audit logs
# (tool_calls, browser_audit, computer_audit) aren't here: they stay.
TABLES = ("sessions", "turns", "events", "inbox", "prompt_queue", "trigger_runs", "trigger_state", "webhook_keys",
          "wakeups", "tool_grants", "browser_grants", "attachments")


class RemovalError(Exception):
    """Refused or failed: str() is shown to the owner, .status is the HTTP status."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


async def delete_agent(c: Any, name: str, volumes: bool = False) -> dict[str, Any]:
    """Delete the agent `name` -> {"volumes": [the volumes deleted], "kept": the folder its runner kept, or None}.

    Stopped first, as Stop stops it (desired stopped, so nothing starts it again; its gateway token revoked), by its
    runner's agent.forget, which also removes what the runner keeps of it: its container, its data dir (a host
    agent's own working folder stays when it has files; a computer.workdir is never touched), its volumes only with
    `volumes`. Then its running turns end and its asks expire, agents/<name>/ is removed in one commit, forget_agent
    drops the rest, and the clients get {"type": "agent_deleted", "agent": name}.

    RemovalError: 422 `owner` (it stands for the owner, never an agent), 404 no such agent, 503 the runner it was
    placed on is registered but not connected (a revoked one is skipped: there's nothing to reach), 501 that runner
    is too old to forget an agent, 409 a delete of it is under way, and the runner's own errors (nothing is deleted
    then; it's stopped). While it runs, the agent can't be started (hub.deleting)."""
    if name == OWNER_NAME:
        raise RemovalError(f"{OWNER_NAME} stands for you in Valet: it isn't an agent's name", 422)
    if not NAME_RE.match(name) or not c.defs.exists(name):
        raise RemovalError(f"no agent named {name}", 404)
    if name in c.hub.deleting:
        raise RemovalError(f"{name} is being deleted already", 409)
    c.hub.deleting.add(name)        # nothing starts it from here on (hub.start_agent, the start route)
    try:
        return await _delete(c, name, volumes)
    finally:
        c.hub.deleting.discard(name)


async def _delete(c: Any, name: str, volumes: bool) -> dict[str, Any]:
    row = c.db.one("SELECT runner FROM agents WHERE name = ?", (name,))
    runner = row["runner"] if row else None
    if runner is not None and runner not in c.hub.connected():
        if c.db.one("SELECT 1 FROM runners WHERE name = ? AND revoked = 0", (runner,)):
            raise RemovalError(f"runner {runner} is not connected: it has to be, to stop {name} and remove its "
                               "files there", 503)
        runner = None
    if runner is not None and "forget" not in c.hub.features(runner):
        raise RemovalError(f"runner {runner} can't delete an agent (forget): update it", 501)

    if row:
        c.db.execute("UPDATE agents SET desired = 'stopped', updated_at = ? WHERE name = ?", (time.time(), name))
    auth.revoke_agent_token(c.db, name)
    out: dict[str, Any] = {"volumes": [], "kept": None}
    if runner is not None:
        try:
            res = await c.hub.call(runner, AgentForget(id=uuid.uuid4().hex, agent=name, volumes=volumes).dump(),
                                   FORGET_TIMEOUT)
        except RunnerError as e:
            await c.hub.emit_agent(name)
            raise RemovalError(str(e), e.status) from None
        out = {"volumes": list(res.get("volumes") or []), "kept": res.get("kept")}
    await c.hub.agent_down(name)        # whatever the runner said (or, unreachable, didn't)
    await asyncio.to_thread(c.defs.delete, name)
    await forget_agent(c, name)
    await c.hub.broadcast({"type": "agent_deleted", "agent": name})
    return out


async def forget_agent(c: Any, name: str) -> None:
    """Everything the control plane keeps under an agent's name goes: its runtime row (its gateway token with it),
    sessions, turns, events, inbox items, queued prompts, trigger runs and state, webhook keys, wake-ups, tool and
    browser grants, attachments and their files, its Shared items and their files; and what's in memory about it (the scheduler's, its activity). The
    one place for it: per-agent state added later gets its line here. Its definition is gone already (so nothing
    starts or schedules it meanwhile), its turns ended and its asks expired (hub.agent_down)."""
    if name == OWNER_NAME:
        raise ValueError(f"{OWNER_NAME} isn't an agent")
    c.hub.activity.clear(name)
    files = [c.attachments.path(r["id"]) for r in c.db.query("SELECT id FROM attachments WHERE agent = ?", (name,))]
    async with c.scheduler.paused():        # no trigger or wake-up of it fires halfway
        c.scheduler.forget(name)
        with c.db.transaction() as db:
            for table in TABLES:
                db.execute(f"DELETE FROM {table} WHERE agent = ?", (name,))
            db.execute("DELETE FROM agents WHERE name = ?", (name,))
    for p in files:
        p.unlink(missing_ok=True)
    if c.shared is not None:        # its Shared space, the items and their files (clients look again on agent_deleted)
        c.shared.forget_agent(name)
