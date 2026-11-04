"""Database rows -> the JSON shapes of docs/API.md (Runner, Agent, AgentDetail, Session, QueuedPrompt, InboxItem,
Event, Secret, SkillInfo, Skill, AgentSkills). One place, so the REST routes and the client WebSocket can't drift
apart."""
import hashlib
import json
import time
from typing import Any

from valet.shared import skills
from valet.shared.definition import AgentDefinition, DefinitionError
from valet.shared.events import Event

from .db import Database
from .gateway.registry import RegistryError, references


def loads(text: str | None) -> Any:
    return json.loads(text) if text else None


def ensure_agent(db: Database, name: str) -> dict:
    """The agent's runtime row, created on first touch."""
    db.execute("INSERT OR IGNORE INTO agents (name, updated_at) VALUES (?, ?)", (name, time.time()))
    return db.one("SELECT * FROM agents WHERE name = ?", (name,))


# How the apps show an agent; nothing on the runner reads them, so changing them needs no restart.
COSMETIC = {"display_name", "emoji"}
# What the control plane reads at every call, not at a start: the gateway's tool rules and the browser's site rules
# (an "always" answer writes one). A new rule applies at the next call, so it needs no restart, and an agent without
# rules fingerprints as it did before they existed.
LIVE = {"tools": {"rules"}, "browser": {"rules"}}


def _running_part(d: AgentDefinition) -> dict[str, Any]:
    return d.model_dump(mode="json", exclude={**{k: True for k in COSMETIC}, **LIVE})


def fingerprint(d: AgentDefinition, files: dict[str, str], stamps: dict[str, float | None] | None = None,
                gateway: str | None = None) -> str:
    """What an agent runs with: changes when its definition (COSMETIC fields and LIVE rules aside) or files do (its
    persona.md, its skills' files, and the owner profile and authority preset when its directives list them:
    defs.load's files), one of its secrets or vault entries is set again or deleted (`stamps`:
    SecretStore.definition_stamps, names and updated_at, never values), or the
    MCP gateway's endpoint it was handed moves (`gateway`: the URL when it has gateway servers, never its token), so a
    running agent can say it needs a restart to pick up a saved change. Without secrets or a gateway it's what it
    was before either existed, and for directives [persona.md] what it was before M2a. (The gateway reads the definition,
    mcp.yaml and their credentials at every call: those need no restart, and tools.rules and browser.rules (LIVE)
    are left out. The generated environment and memory layers aren't in `files`: the environment follows from the
    definition, plus tool listings that a harness only reads at a session's start, and memory changes all the time
    while the agent reads it fresh through valet.memory.)"""
    what: dict[str, Any] = {"definition": _running_part(d), "files": files}
    if what["definition"]["computer"].get("workdir") is None:     # unset, it's what it was before the field existed
        del what["definition"]["computer"]["workdir"]
    if stamps:
        what["secrets"] = stamps
    if gateway:
        what["gateway"] = gateway
    text = json.dumps(what, sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def runner_view(row: dict, connected: list[str], harnesses: list[str] | None = None) -> dict:
    return {"name": row["name"], "connected": row["name"] in connected, "last_seen": row["last_seen"],
            "version": row["version"], "harnesses": harnesses or []}


def runners(control) -> list[dict]:
    connected = control.hub.connected()
    rows = control.db.query("SELECT * FROM runners WHERE revoked = 0 ORDER BY name")
    return [runner_view(r, connected, control.hub.harnesses(r["name"])) for r in rows]


def agent_view(control, name: str) -> dict:
    db = control.db
    row = db.one("SELECT * FROM agents WHERE name = ?", (name,)) or {
        "runner": None, "desired": "stopped", "state": "stopped", "error": None, "capabilities": None,
        "started": None}
    d, files, err, stamps = None, {}, None, {}
    try:
        d, files = control.defs.load(name)
        stamps = control.secrets.definition_stamps(d)
    except (DefinitionError, FileNotFoundError) as e:
        err = str(e) if isinstance(e, DefinitionError) else "agent.yaml is missing"
    status = row["state"]
    if status == "running":
        # message and question items (valet.owner) wait on no call: the agent carries on, so it isn't waiting.
        if db.one("SELECT 1 FROM inbox WHERE agent = ? AND status = 'pending' AND kind NOT IN ('message', 'question') "
                  "LIMIT 1", (name,)):
            status = "waiting"
        elif db.one("SELECT 1 FROM sessions WHERE agent = ? AND state = 'working' LIMIT 1", (name,)):
            status = "working"
        else:
            status = "idle"
    restart = bool(d and row["state"] in ("starting", "running") and row["started"]
                   and row["started"] != fingerprint(d, files, stamps,
                                                     control.gateway.url(name) if d.gateway_servers() else None))
    return {
        "name": name,
        "display_name": d.display_name if d else None,
        "emoji": d.emoji if d else None,
        "harness": d.harness if d else None,
        "model": d.model if d else None,
        "level": d.computer.level if d else None,
        "definition_error": err,
        "runner": row["runner"],
        "connected": row["runner"] in control.hub.connected() if row["runner"] else False,
        "desired": row["desired"],
        "state": row["state"],
        "status": status,
        "error": row["error"],
        "capabilities": loads(row["capabilities"]),
        "permissions": d.permissions if d else None,
        "restart_needed": restart,
        "missing_secrets": [n for n, at in stamps.items() if at is None],
        **always_on(control, name, d, status, row),
    }


def always_on(control, name: str, d: AgentDefinition | None, status: str, row: dict) -> dict:
    """The Agent shape's always-on fields: what it's doing while it works, how many prompts wait in its sessions'
    queues, its next schedule or wake-up, and its main session."""
    db = control.db
    hub = getattr(control, "hub", None)
    scheduler = getattr(control, "scheduler", None)
    activity = hub.activity.get(name) if hub is not None and status in ("working", "waiting") else None
    queued = db.one("SELECT COUNT(*) AS n FROM prompt_queue WHERE agent = ?", (name,))["n"]
    return {"activity": activity, "queued": queued,
            "next_trigger": scheduler.next_trigger(name, d) if scheduler is not None else None,
            "main_session": row.get("main_session")}


def agents(control) -> list[dict]:
    return [agent_view(control, n) for n in control.defs.names()]


def secrets(control) -> list[dict]:
    """GET /api/secrets: every secret that's set, and every name a valid definition lists or a valid mcp.yaml
    references (`${secret:NAME}`) that isn't, by name, with the agents that list it and the MCP servers whose
    entries reference it. Never a value."""
    used: dict[str, list[str]] = {}
    for agent in control.defs.names():
        try:
            d, _ = control.defs.load(agent)
        except (DefinitionError, FileNotFoundError):
            continue
        for n in d.secret_names():
            used.setdefault(n, []).append(agent)
    servers: dict[str, list[str]] = {}
    try:
        registry = control.gateway.registry()
    except RegistryError:
        registry = None
    for server, spec in sorted(registry.servers.items()) if registry else ():
        for n in dict.fromkeys(n for scheme, n in references(spec) if scheme == "secret"):
            servers.setdefault(n, []).append(server)
    stored = {r["name"]: r for r in control.secrets.listing()}
    return [{"name": n, "set": n in stored, "updated_at": stored[n]["updated_at"] if n in stored else None,
             "used_by": used.get(n, []), "servers": servers.get(n, [])}
            for n in sorted(stored.keys() | used.keys() | servers.keys())]


def directive_users(control, entry: str) -> list[str]:
    """Agents whose (valid) definition lists `entry` in its directives (`owner`, `authority/full`...), by name."""
    out = []
    for agent in control.defs.names():
        try:
            d, _ = control.defs.load(agent)
        except (DefinitionError, FileNotFoundError):
            continue
        if entry in d.directives:
            out.append(agent)
    return out


def owner(control) -> dict:
    """GET /api/owner."""
    return {"text": control.defs.owner_text(), "commit": control.defs.head(),
            "used_by": directive_users(control, "owner")}


def preset(control, name: str) -> dict:
    """An AuthorityPreset. FileNotFoundError if there's none by that name."""
    return {"name": name, "text": control.defs.preset_text(name),
            "used_by": directive_users(control, "authority/" + name)}


def secret(control, name: str) -> dict | None:
    return next((s for s in secrets(control) if s["name"] == name), None)


def agent_detail(control, name: str) -> dict:
    view = agent_view(control, name)
    try:
        d, _ = control.defs.load(name)
        definition = d.model_dump(mode="json")
    except (DefinitionError, FileNotFoundError):
        definition = None
    return {**view, "files": control.defs.raw(name), "definition": definition, "commit": control.defs.head()}


QUEUE_CLIP = 300        # characters of a queued prompt in the Session shape (GET .../queue has them whole)


def session_view(db: Database, row: dict) -> dict:
    turn = db.one("SELECT id FROM turns WHERE agent = ? AND session_id = ? AND state = 'running' "
                  "ORDER BY started_at DESC LIMIT 1", (row["agent"], row["id"]))
    main = db.one("SELECT 1 FROM agents WHERE name = ? AND main_session = ?", (row["agent"], row["id"]))
    queue = db.query("SELECT * FROM prompt_queue WHERE agent = ? AND session_id = ? ORDER BY pos, id",
                     (row["agent"], row["id"]))
    return {"agent": row["agent"], "id": row["id"], "title": row["title"], "state": row["state"],
            "turn_id": turn["id"] if turn else None, "created_at": row["created_at"], "updated_at": row["updated_at"],
            "main": bool(main), "queued": len(queue), "queue": [queued_view(q, QUEUE_CLIP, queued_attachments(db, q))
                                                        for q in queue]}


def queued_attachments(db: Database, row: dict) -> list[dict]:
    """A queued prompt's attachments (control/attachments.py), as it lists them: {id, name, mime, image, url}."""
    ids = loads(row.get("attachments")) or []
    rows = [db.one("SELECT id, agent, name, mime, kind FROM attachments WHERE id = ?", (i,)) for i in ids]
    return [{"id": r["id"], "name": r["name"], "mime": r["mime"], "image": r["kind"] == "image",
             "url": f"/api/agents/{r['agent']}/attachments/{r['id']}"} for r in rows if r]


def queued_view(row: dict, clip: int | None = None, attachments: list[dict] | None = None) -> dict:
    """A QueuedPrompt: one waiting in a session's queue."""
    text = row["text"]
    if clip is not None and len(text) > clip:
        text = text[:clip] + "…"
    return {"id": row["id"], "session_id": row["session_id"], "text": text, "source": row["source"],
            "label": row["label"], "retry_of": row["retry_of"], "created_at": row["created_at"],
            "attachments": attachments or []}


def session(db: Database, agent: str, sid: str) -> dict | None:
    row = db.one("SELECT * FROM sessions WHERE agent = ? AND id = ?", (agent, sid))
    return session_view(db, row) if row else None


def inbox_view(row: dict) -> dict:
    payload = loads(row["payload"]) or {}
    return {"id": row["id"], "kind": row["kind"], "agent": row["agent"], "session_id": row["session_id"],
            "turn_id": row["turn_id"], "status": row["status"], "title": row["title"],
            "tool_call": payload.get("tool_call") or {}, "options": payload.get("options") or [],
            "tool": payload.get("tool"),    # kind "tool": {server, name, title, description, arguments, annotations}
            "browser": payload.get("browser"),  # kind "browser": {profile, tab, url, host, kind, summary, detail, shot}
            "text": payload.get("text"),    # kinds "message", "question" (valet.owner): what the agent says or asks
            "answer": loads(row["answer"]), "created_at": row["created_at"], "answered_at": row["answered_at"]}


def tool_call_view(row: dict) -> dict:
    """A tool_calls (audit) row as the API shows it."""
    return {**row, "arguments": loads(row["arguments"]) if _is_json(row["arguments"]) else row["arguments"]}


def _is_json(text: str | None) -> bool:
    try:
        json.loads(text or "")
        return True
    except ValueError:
        return False


def inbox_item(db: Database, item_id: str) -> dict | None:
    row = db.one("SELECT * FROM inbox WHERE id = ?", (item_id,))
    return inbox_view(row) if row else None


def pending_inbox(db: Database) -> list[dict]:
    rows = db.query("SELECT * FROM inbox WHERE status = 'pending' ORDER BY created_at DESC, rowid DESC")
    return [inbox_view(r) for r in rows]


def event_view(row: dict) -> Event:
    return Event(id=row["id"], agent=row["agent"], session_id=row["session_id"], turn_id=row["turn_id"],
                 ts=row["ts"], kind=row["kind"], data=loads(row["data"]) or {})


# ---------- skills ----------

def skill_users(control) -> dict[str, list[str]]:
    """Shared skill name -> the agents (valid definitions) whose tools.skills picks it, by name or "*", and that
    don't have their own skill of that name (theirs wins)."""
    library = control.defs.skill_names()
    out: dict[str, list[str]] = {}
    for agent in control.defs.names():
        try:
            d = control.defs.definition(agent)
        except (DefinitionError, FileNotFoundError):
            continue
        own = set(control.defs.skill_names(agent))
        chosen = library if skills.ALL in d.tools.skills else d.tools.skills
        for n in dict.fromkeys(chosen):
            if n != skills.ALL and n not in own:
                out.setdefault(n, []).append(agent)
    return out


def read_skill(control, name: str, agent: str | None = None) -> tuple[dict[str, str] | None, str | None]:
    """(files, error): its files as on disk, or None if they can't be read; why it isn't valid, or None.
    FileNotFoundError if there's no such skill."""
    try:
        files = control.defs.skill(name, agent)
    except skills.SkillError as e:
        return None, str(e)
    try:
        skills.check(name, files)
    except skills.SkillError as e:
        return files, str(e)
    return files, None


def _parts(files: dict[str, str] | None) -> tuple[dict, str]:
    try:
        return skills.split((files or {})[skills.SKILL_FILE])
    except (KeyError, skills.SkillError):
        return {}, ""


def skill_info(name: str, files: dict[str, str] | None, error: str | None, used_by: list[str]) -> dict:
    """SkillInfo: what a list shows."""
    meta, _ = _parts(files)
    desc = meta.get("description")
    return {"name": name, "description": desc if isinstance(desc, str) else None,
            "files": sorted(files or {}, key=lambda p: (p != skills.SKILL_FILE, p)),
            "size": sum(len(t.encode(errors="replace")) for t in (files or {}).values()),
            "error": error, "used_by": used_by}


def skills_list(control) -> list[dict]:
    """GET /api/skills: the shared library, by name."""
    users = skill_users(control)
    return [skill_info(n, *read_skill(control, n), users.get(n, [])) for n in control.defs.skill_names()]


def skill_detail(control, name: str, agent: str | None = None) -> dict:
    """Skill: SkillInfo + SKILL.md's instructions and other frontmatter, every file's text, and the repo's HEAD.
    FileNotFoundError if there's no such skill."""
    files, error = read_skill(control, name, agent)
    used_by = [agent] if agent else skill_users(control).get(name, [])
    meta, body = _parts(files)
    extra = {k: v for k, v in meta.items() if k not in ("name", "description")}
    return {**skill_info(name, files, error, used_by), "agent": agent, "instructions": body, "extra": extra,
            "content": files or {}, "commit": control.defs.head()}


def agent_skills(control, name: str) -> dict:
    """AgentSkills: the agent's own skills, and what it gets (its saved definition): every skill, its own or
    shared; names it picks that the library doesn't have; skills left out, and why. DefinitionError if its
    agent.yaml doesn't validate."""
    d = control.defs.definition(name)
    sel = control.defs.skills_of(d)
    library = set(control.defs.skill_names())
    own = [{**skill_info(n, *read_skill(control, n, name), [name]), "replaces_shared": n in library}
           for n in control.defs.skill_names(name)]
    return {"agent": name, "all": skills.ALL in d.tools.skills, "own": own,
            "skills": [{"name": n, "from": sel.sources[n]} for n in sorted(sel.skills)],
            "missing": sel.missing, "problems": sel.problems}
