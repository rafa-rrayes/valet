"""What the owner's answer to a gateway ask covers:

- `allow_once` ("Allow once"): this call;
- `allow_hour` ("Allow for an hour"): this call, and a grant (table tool_grants) that turns the tool's asks into
  allows for an hour;
- `allow_always` ("Always allow"): this call, and a rule `tools.rules.<server>.<tool>: allow` written into the
  agent's agent.yaml, as a commit, where the owner sees it (Settings › Tools) and can take it back;
- `reject_once` ("Reject"): not this call.

The answer itself travels the generic way (POST /api/inbox/{id}/answer -> Hub.record_answer -> the waiting call's
future gets the optionId); the gateway applies the scope here once it has it. Grants and rules apply at the next
call: neither needs a restart."""
import threading
import time
from typing import Any

from valet.shared.definition import AGENT_FILE, TOOL_RE, AgentDefinition, to_yaml

HOUR = 3600.0
ALLOW_ONCE, ALLOW_HOUR, ALLOW_ALWAYS, REJECT_ONCE = "allow_once", "allow_hour", "allow_always", "reject_once"
OPTIONS = [{"optionId": ALLOW_ONCE, "name": "Allow once", "kind": "allow_once"},
           {"optionId": ALLOW_HOUR, "name": "Allow for an hour", "kind": "allow_once"},
           {"optionId": ALLOW_ALWAYS, "name": "Always allow", "kind": "allow_always"},
           {"optionId": REJECT_ONCE, "name": "Reject", "kind": "reject_once"}]
ALLOWING = {ALLOW_ONCE, ALLOW_HOUR, ALLOW_ALWAYS}

_always = threading.Lock()      # one rule write at a time (the browser's "always" too): each reads agent.yaml, adds its
                                # rule, writes it back


def options(tool: str) -> list[dict[str, Any]]:
    """The options of an ask for `tool` (the server's own name for it), in order. "Always allow" only when the name
    can be a key of tools.rules (MCP's tool-name grammar); a server is free to break it."""
    return [dict(o) for o in OPTIONS if o["optionId"] != ALLOW_ALWAYS or TOOL_RE.match(tool)]


# ---------- hour grants ----------

def grant_view(row: dict) -> dict[str, Any]:
    """A tool_grants row as GET /api/gateway/grants shows it (ToolGrant in docs/API.md)."""
    return {"id": row["id"], "agent": row["agent"], "server": row["server"], "tool": row["tool"],
            "expires_at": row["expires_at"], "created_at": row["created_at"]}


def grant(db, agent: str, server: str, tool: str, seconds: float = HOUR) -> dict[str, Any]:
    """Let `agent` call `server`'s `tool` without asking for `seconds` from now (a live grant for it is renewed)."""
    now = time.time()
    db.execute("INSERT INTO tool_grants (agent, server, tool, expires_at, created_at) VALUES (?, ?, ?, ?, ?) "
               "ON CONFLICT (agent, server, tool) DO UPDATE SET expires_at = excluded.expires_at, "
               "created_at = excluded.created_at", (agent, server, tool, now + seconds, now))
    return grant_view(db.one("SELECT * FROM tool_grants WHERE agent = ? AND server = ? AND tool = ?",
                             (agent, server, tool)))


def live(db, agent: str | None = None) -> list[dict[str, Any]]:
    """Grants that haven't expired (of one agent, or all), soonest to expire first. Expired rows are removed."""
    now = time.time()
    db.execute("DELETE FROM tool_grants WHERE expires_at <= ?", (now,))
    sql, params = "SELECT * FROM tool_grants WHERE expires_at > ?", [now]
    if agent is not None:
        sql += " AND agent = ?"
        params.append(agent)
    return [grant_view(r) for r in db.query(sql + " ORDER BY expires_at, id", tuple(params))]


def granted(db, agent: str, server: str) -> set[str]:
    """The tools of `server` that `agent` holds a live grant for."""
    rows = db.query("SELECT tool FROM tool_grants WHERE agent = ? AND server = ? AND expires_at > ?",
                    (agent, server, time.time()))
    return {r["tool"] for r in rows}


def revoke(db, grant_id: int) -> dict[str, Any] | None:
    """Remove a grant now; the grant as it was, or None if there's no live one by that id."""
    row = db.one("SELECT * FROM tool_grants WHERE id = ? AND expires_at > ?", (grant_id, time.time()))
    db.execute("DELETE FROM tool_grants WHERE id = ?", (grant_id,))
    return grant_view(row) if row else None


# ---------- "always": a rule in the definition ----------

def always_allow(defs, agent: str, server: str, tool: str) -> str | None:
    """Write `tools.rules.<server>.<tool>: allow` into the agent's agent.yaml (to_yaml, as a settings save does) in
    one commit, "Inbox: always allow <server> <tool> for <agent>". Returns the commit, or None if the rule was
    already there. DefinitionError / FileNotFoundError / ValueError (pydantic) if agent.yaml can't take it: a file
    that doesn't validate is left alone. Blocking (git): run it in a thread."""
    with _always:
        d = defs.definition(agent)
        rules = {s: dict(t) for s, t in d.tools.rules.items()}
        if rules.get(server, {}).get(tool) == "allow":
            return None
        rules.setdefault(server, {})[tool] = "allow"
        data = d.model_dump(mode="json")
        data["tools"]["rules"] = rules
        new = AgentDefinition.model_validate(data)
        return defs.write(agent, {AGENT_FILE: to_yaml(new)}, f"Inbox: always allow {server} {tool} for {agent}")
