"""Routes for the always-on parts (docs/API.md, "Triggers" and "Prompt queue"): an agent's triggers and their
runs, webhook keys, schedule previews, wake-ups, a session's prompt queue and the owner's Retry. All under /api and
owner-only, except `router_hooks`: POST /hooks/<agent>/<name>, which webhook senders call with the webhook's key
(webhooks.py)."""
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from valet.shared import cron
from valet.shared.definition import NAME_RE, DefinitionError

from . import auth, views
from .prompts import DeliveryError
from .secrets import SecretError
from .webhooks import BODY_LIMIT

router = APIRouter(prefix="/api", dependencies=[Depends(auth.require_owner)])
router_hooks = APIRouter()
HOOK_NAME = NAME_RE         # webhook names follow agent names' rule


def _control(request: Request):
    return request.app.state.control


def _agent_or_404(c, name: str) -> None:
    if not NAME_RE.match(name) or not c.defs.exists(name):
        raise HTTPException(404, f"no agent named {name}")


# ---------- triggers ----------

def _trigger_view(c, agent: str, t, last: dict | None) -> dict[str, Any]:
    out: dict[str, Any] = {"trigger": t.key, "kind": "schedule" if t.schedule else "webhook", "schedule": t.schedule,
                           "webhook": t.webhook, "prompt": t.prompt, "session": t.session, "last_run": last}
    if t.schedule:
        out["description"] = cron.humanize(t.schedule)
        out["next"] = c.scheduler.upcoming(t.schedule, 3)
    else:
        out["description"] = f"When POST /hooks/{agent}/{t.webhook} is called with its key"
        out["url"] = c.webhooks.url(agent, t.webhook)
        try:
            out["key"] = c.webhooks.ensure(agent, t.webhook)
            out["key_error"] = None
        except SecretError as e:
            out["key"], out["key_error"] = None, str(e)
        out["key_created_at"] = c.webhooks.created(agent, t.webhook)
    return out


def _wakeup_view(c, w: dict) -> dict[str, Any]:
    """A WakeUp (docs/API.md)."""
    return {"id": w["id"], "agent": w["agent"], "session_id": w["session_id"], "prompt": w["prompt"],
            "due_at": w["due_at"], "status": w["status"], "created_at": w["created_at"], "done_at": w["done_at"],
            "run_id": w["run_id"]}


@router.get("/agents/{name}/triggers")
def get_triggers(name: str, request: Request):
    """The agent's triggers as its saved agent.yaml has them, each with what the apps show: a schedule's
    description and next runs, a webhook's URL and key (made the first time it's asked for), the latest run; and
    its pending wake-ups. `error`: agent.yaml doesn't validate (its triggers can't be read)."""
    c = _control(request)
    _agent_or_404(c, name)
    try:
        d, error = c.defs.definition(name), None
    except (DefinitionError, FileNotFoundError) as e:
        d, error = None, str(e)
    triggers = []
    if d is not None:
        c.webhooks.forget(name, [t.webhook for t in d.triggers if t.webhook])
        for t in d.triggers:
            last = c.db.one("SELECT * FROM trigger_runs WHERE agent = ? AND trigger = ? ORDER BY id DESC LIMIT 1",
                            (name, t.key))
            triggers.append(_trigger_view(c, name, t, c.scheduler.run_view(last) if last else None))
    wakeups = c.db.query("SELECT * FROM wakeups WHERE agent = ? AND status = 'pending' ORDER BY due_at, id", (name,))
    return {"agent": name, "timezone": cron.zone_name(c.scheduler.tz), "triggers": triggers,
            "wakeups": [_wakeup_view(c, w) for w in wakeups], "next_trigger": c.scheduler.next_trigger(name, d),
            "error": error}


def _hook_or_404(c, name: str, hook: str):
    _agent_or_404(c, name)
    try:
        d = c.defs.definition(name)
    except (DefinitionError, FileNotFoundError) as e:
        raise HTTPException(422, f"agent.yaml doesn't validate: {e}") from None
    t = d.webhook(hook)
    if t is None:
        raise HTTPException(404, f"{name} has no webhook {hook}")
    return t


@router.post("/agents/{name}/webhooks/{hook}/key")
def rotate_key(name: str, hook: str, request: Request):
    """A new key for the webhook; the old one stops working at once."""
    c = _control(request)
    _hook_or_404(c, name, hook)
    try:
        key = c.webhooks.rotate(name, hook)
    except SecretError as e:
        raise HTTPException(500, str(e)) from None
    return {"webhook": hook, "url": c.webhooks.url(name, hook), "key": key,
            "key_created_at": c.webhooks.created(name, hook)}


@router.get("/agents/{name}/trigger-runs")
def trigger_runs(name: str, request: Request, before: int | None = Query(None, ge=1),
                 limit: int = Query(50, ge=1, le=500), trigger: str | None = None):
    """Newest first; `before`: a run id, for the next page. `trigger`: one trigger's runs (a Trigger's `trigger`)."""
    c = _control(request)
    _agent_or_404(c, name)
    sql, params = "SELECT * FROM trigger_runs WHERE agent = ?", [name]
    if before is not None:
        sql += " AND id < ?"
        params.append(before)
    if trigger:
        sql += " AND trigger = ?"
        params.append(trigger)
    rows = c.db.query(sql + " ORDER BY id DESC LIMIT ?", (*params, limit + 1))
    return {"runs": [c.scheduler.run_view(r) for r in rows[:limit]], "more": len(rows) > limit}


@router.get("/triggers/preview")
def preview(request: Request, schedule: str = Query(..., max_length=500), count: int = Query(3, ge=1, le=20)):
    """What a schedule means before it's saved: 422 with the parser's message if it isn't one."""
    c = _control(request)
    expr = " ".join(schedule.split())
    try:
        parsed = cron.parse(expr)
    except cron.CronError as e:
        raise HTTPException(422, str(e)) from None
    return {"schedule": expr, "description": parsed.describe(), "next": c.scheduler.upcoming(expr, count),
            "timezone": cron.zone_name(c.scheduler.tz)}


# ---------- wake-ups ----------

@router.get("/agents/{name}/wakeups")
def list_wakeups(name: str, request: Request, status: str = Query("pending", pattern="^(pending|all)$")):
    c = _control(request)
    _agent_or_404(c, name)
    if status == "pending":
        rows = c.db.query("SELECT * FROM wakeups WHERE agent = ? AND status = 'pending' ORDER BY due_at, id", (name,))
    else:
        rows = c.db.query("SELECT * FROM wakeups WHERE agent = ? ORDER BY id DESC LIMIT 200", (name,))
    return {"wakeups": [_wakeup_view(c, w) for w in rows]}


@router.delete("/agents/{name}/wakeups/{wid}")
async def cancel_wakeup(name: str, wid: int, request: Request):
    c = _control(request)
    _agent_or_404(c, name)
    w = c.db.one("SELECT * FROM wakeups WHERE id = ? AND agent = ?", (wid, name))
    if w is None:
        raise HTTPException(404, "no such wake-up")
    cur = c.db.execute("UPDATE wakeups SET status = 'cancelled', done_at = ? WHERE id = ? AND status = 'pending'",
                       (c.clock.now(), wid))
    if not cur.rowcount:
        raise HTTPException(409, f"this wake-up is already {w['status']}")
    c.scheduler.poke()
    await c.hub.emit_agent(name)
    return _wakeup_view(c, c.db.one("SELECT * FROM wakeups WHERE id = ?", (wid,)))


# ---------- the prompt queue and Retry ----------

def _session_or_404(c, name: str, sid: str) -> dict:
    s = views.session(c.db, name, sid)
    if not s:
        raise HTTPException(404, "no such session")
    return s


@router.get("/agents/{name}/sessions/{sid}/queue")
def get_queue(name: str, sid: str, request: Request):
    """The prompts waiting in the session, whole, in the order they'll go."""
    c = _control(request)
    _session_or_404(c, name, sid)
    return {"queue": c.prompts.queued(name, sid)}


@router.delete("/agents/{name}/sessions/{sid}/queue/{qid}")
async def remove_queued(name: str, sid: str, qid: int, request: Request):
    """Take a prompt out of the queue before it's sent. 404 if it isn't waiting there (any more)."""
    c = _control(request)
    _session_or_404(c, name, sid)
    if not await c.prompts.remove(name, sid, qid):
        raise HTTPException(404, "no such queued prompt: it may have been sent already")
    return {"ok": True}


def delivery_response(d) -> JSONResponse | dict:
    """A Delivery as the prompt routes answer it: 202 {turn_id} or {queued}, else the runner's error."""
    if d.queue_id is not None:
        return JSONResponse({"queued": d.queue_id, "session_id": d.session_id}, status_code=202)
    if d.error:
        if d.in_doubt:
            raise HTTPException(d.status or 502, d.error + "; the turn goes on if the runner got it")
        raise HTTPException(d.status or 502, d.error)
    return JSONResponse({"turn_id": d.turn_id}, status_code=202)


@router.post("/agents/{name}/sessions/{sid}/turns/{turn_id}/retry", status_code=202)
async def retry(name: str, sid: str, turn_id: str, request: Request):
    """Send an interrupted or failed turn's prompt again, as a new turn (queued if one is running); the new turn's
    `retry_of` names this one. Each turn can be retried once."""
    c = _control(request)
    _session_or_404(c, name, sid)
    try:
        d = await c.prompts.retry(name, sid, turn_id)
    except DeliveryError as e:
        raise HTTPException(e.status, str(e)) from None
    return delivery_response(d)


# ---------- webhooks: the public side ----------

async def _body(request: Request) -> bytes | None:
    """The request body, or None past BODY_LIMIT (read no further)."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > BODY_LIMIT:
        return None
    out = bytearray()
    async for chunk in request.stream():
        out += chunk
        if len(out) > BODY_LIMIT:
            return None
    return bytes(out)


def _not_found() -> JSONResponse:
    return JSONResponse({"detail": "not found"}, status_code=404)


@router_hooks.post("/hooks/{agent}/{name}", include_in_schema=False)
async def hook(agent: str, name: str, request: Request):
    """A webhook call (webhooks.py has the rules). 202 {run_id, outcome: started|queued, session_id, turn_id |
    queue_id} once the prompt is placed; 503 {run_id, outcome: skipped, detail} if the agent can't take it (stopped,
    down); 502 if its runner refused it; 401 without a key, 404 for any key that doesn't open this webhook, 413, 429."""
    c = _control(request)
    w = c.webhooks
    ip = auth.client_ip(request, c.cfg.trust_cf_ip)
    if (wait := w.fails.full(ip)) is not None:
        return JSONResponse({"detail": "too many failed calls from this address; try again later"}, status_code=429,
                            headers={"retry-after": str(int(wait))})
    key, sig = w.credentials(request.headers, dict(request.query_params))
    if key is None and sig is None:
        w.fails.add(ip)
        return JSONResponse({"detail": "this webhook needs its key: Authorization: Bearer <key>, ?key=<key>, or an "
                                       "X-Hub-Signature-256 signature"},
                            status_code=401, headers={"www-authenticate": 'Bearer realm="valet-webhook"'})
    body = await _body(request)
    if body is None:
        return JSONResponse({"detail": f"the body is over {BODY_LIMIT // 1024} KB"}, status_code=413)
    t, d = None, None
    if NAME_RE.match(agent) and HOOK_NAME.match(name) and c.defs.exists(agent):
        try:
            d = c.defs.definition(agent)
            t = d.webhook(name)
        except (DefinitionError, FileNotFoundError):
            t = None
    if not w.verify(agent, name, t is not None, key, sig, body):
        w.fails.add(ip)
        return _not_found()
    s = c.scheduler
    trigger, source, label = t.key, f"webhook:{name}", f"Webhook {name}"
    # The webhook's own limit is checked first, so a refused call doesn't use up a slot of the global one.
    wait = w.hooks.full(f"{agent}/{name}") or w.all.take("*") or w.hooks.take(f"{agent}/{name}")
    if wait is not None:
        run = s.record(agent, trigger, source, label, "skipped", None,
                       error="called too often (more than its limit a minute)")
        return JSONResponse({"run_id": run, "outcome": "skipped", "detail": "too many calls; try again later"},
                            status_code=429, headers={"retry-after": str(int(wait))})
    reason = s.skip_reason(agent)
    if reason is None and c.prompts.runner_of(agent) is None:
        reason = c.prompts.why_not(agent)
    if reason is not None:
        run = s.record(agent, trigger, source, label, "skipped", None, error=reason)
        await c.hub.emit_agent(agent)
        return JSONResponse({"run_id": run, "outcome": "skipped", "detail": reason}, status_code=503)
    text = w.prompt(name, t.prompt, request.headers, body, c.clock.now(), s.tz)
    run = s.record(agent, trigger, source, label, "firing", None)
    row = await s.fire(run, agent, t.session, text, source, label, title=f"{label}: {t.prompt or 'called'}")
    out = {"run_id": row["id"], "outcome": row["outcome"], "session_id": row["session_id"]}
    if row["outcome"] == "queued":
        return JSONResponse({**out, "queue_id": row["queue_id"]}, status_code=202)
    if row["outcome"] == "started":
        return JSONResponse({**out, "turn_id": row["turn_id"]}, status_code=202)
    status = 503 if row["outcome"] == "skipped" else 502
    return JSONResponse({**out, "detail": row["error"]}, status_code=status)
