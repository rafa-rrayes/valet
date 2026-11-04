"""The MCP gateway (DESIGN.md, "Tools: the MCP gateway"): every MCP server an agent gets is reached through one
streamable-HTTP endpoint, `/mcp/agents/<name>`, with that agent's own bearer token (`vagt_...`, issued when it
starts). The gateway

- exposes the tools of the agent's gateway servers (AgentDefinition.gateway_servers(): those `tools.mcp` lists,
  and valet.memory with `memory: true`), namespaced `<server>__<tool>` (dots in a server name become `_`:
  `valet.agents` -> `valet_agents__list_agents`), minus the ones it would always deny;
- decides each call with policy.decide_tool (allow / ask / deny): the owner's rule for the tool (`tools.rules`),
  else its permission level and the tool's annotations, an ask turned into an allow by a live hour grant; unless the
  server decides its own calls (Upstream.decides_calls: valet.browser,
  valet.memory, valet.owner), where only a rule's deny
  applies. An ask becomes an inbox item of kind `tool` that the owner answers like any other, and the call waits for
  it (VALET_GATEWAY_ASK_TIMEOUT). The answer's scope (once, an hour, always) is applied here (scopes.py);
- holds the servers' credentials (registry.py: `${secret:NAME}` from Valet's secrets, `${env:NAME}` from its own
  environment, resolved only when it connects to a server, and scrubbed from the errors it reports) and records
  every call in the audit log (table tool_calls);
- serves valet.shared's file transfers, /mcp/agents/<name>/shared/<ticket> (builtin_shared.py).

The definition (its rules included), the registry and the grants are read at every request, so a saved change
applies to the next call; a running harness lists tools once per session, so it sees added tools in its next one.

Stateless streamable HTTP (no MCP session ids): any request stands alone, so a control-plane restart costs a
running agent nothing but the calls in flight."""
import asyncio
import contextlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any

import anyio
import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from valet import __version__
from valet.shared.definition import AgentDefinition, DefinitionError
from valet.shared.owner import say

from .. import auth
from . import scopes
from .builtin import PLANNED, builtins
from .policy import Decision, Source, decide, decide_tool, rule_for
from ..secrets import SecretError
from .registry import Registry, RegistryError, ServerSpec, fingerprint, load, public_view, references, resolved
from .upstream import Connection, Upstream, UpstreamError

log = logging.getLogger(__name__)

SERVER_NAME = "valet"           # the MCP server name harnesses see: Claude calls these tools mcp__valet__...
SCOPE_KEY = "valet.agent"       # the authenticated agent, put in the ASGI scope for the MCP handlers
PATH_RE = re.compile(r"^/mcp/agents/([a-z0-9][a-z0-9-]{0,30})/?$")
# A shared-space transfer (builtin_shared.py): the ticket in the path is the credential, not the agent's token.
TRANSFER_RE = re.compile(r"^/mcp/agents/([a-z0-9][a-z0-9-]{0,30})/shared/([A-Za-z0-9_-]{32,64})$")
SHARED = "valet.shared"
ARGS_LIMIT = 2000               # characters of a call's arguments kept in the audit log
ERROR_LIMIT = 500
ASK_OPTIONS = scopes.OPTIONS    # allow_once, allow_hour, allow_always, reject_once (the cross-track contract)
INSTRUCTIONS = ("Valet's gateway: the tools {owner} gave this agent. Valet enforces {owner}'s rules here: a call that "
                "needs {owner}'s approval waits for it, and a refused one returns an error saying so.")


def prefix(server: str) -> str:
    return server.replace(".", "_")


@dataclass
class Exposed:
    """One tool as an agent sees it."""
    name: str                   # <prefix(server)>__<tool>
    server: str
    tool: types.Tool            # as the server listed it
    decision: Decision          # what a call does now
    source: Source = "level"    # why: the owner's rule, a live hour grant, or the level and annotations
    level_decision: Decision | None = None  # what the level and annotations alone say

    @property
    def annotations(self) -> dict[str, Any] | None:
        a = self.tool.annotations
        return a.model_dump(by_alias=True, exclude_none=True, mode="json") if a else None

    def public(self) -> types.Tool:
        return self.tool.model_copy(update={"name": self.name, "meta": None, "execution": None})


def _text_result(text: str, error: bool = True) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(text=text)], is_error=error)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


class Gateway:
    def __init__(self, control: Any) -> None:
        """control: the app's Control (cfg, db, defs, hub)."""
        self.control = control
        # How ${scheme:NAME} references in mcp.yaml resolve, when the gateway connects to a server.
        self.lookups = {"env": os.environ.get, "secret": self._secret}
        self.builtins: dict[str, Upstream] = builtins(control)
        self._conns: dict[str, Connection] = {}
        self._closing: set[asyncio.Task] = set()
        self._listing: set[asyncio.Task] = set()     # listings tools(timeout=...) stopped waiting for
        self._lock = asyncio.Lock()
        self.server = Server(SERVER_NAME, version=__version__, instructions=say(INSTRUCTIONS),
                             on_list_tools=self._on_list_tools, on_call_tool=self._on_call_tool)
        self.manager = StreamableHTTPSessionManager(app=self.server, stateless=True)

    @property
    def cfg(self):
        return self.control.cfg

    @property
    def db(self):
        return self.control.db

    @contextlib.asynccontextmanager
    async def run(self):
        """For the app's lifespan: the MCP session manager's task group, and closing upstreams at the end. Calls
        left from before a restart are settled first: their asks expire and their audit rows say interrupted."""
        await self.control.hub.expire_gateway_asks()
        self.db.execute("UPDATE tool_calls SET outcome = 'interrupted', ended_at = ? WHERE outcome = 'pending'",
                        (time.time(),))
        async with self.manager.run():
            try:
                yield
            finally:
                for conn in list(self._conns.values()):
                    await conn.close()
                self._conns.clear()
                if self._closing:
                    await asyncio.gather(*self._closing, return_exceptions=True)

    def url(self, agent: str) -> str:
        """The endpoint as agents reach it (VALET_GATEWAY_URL, default the public URL)."""
        return f"{self.cfg.gateway_url}/mcp/agents/{agent}"

    # ---------- HTTP ----------

    async def asgi(self, scope, receive, send) -> None:
        """/mcp/agents/<name>: the agent's own token, for its own path, while it's starting or running.
        /mcp/agents/<name>/shared/<ticket>: a shared-space transfer, the ticket its own credential."""
        if t := TRANSFER_RE.match(scope["path"]):
            return await self.builtins[SHARED].transfer(scope, receive, send, t.group(1), t.group(2))
        m = PATH_RE.match(scope["path"])
        if not m:
            return await _json(send, 404, {"detail": "not found"})
        name = m.group(1)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        agent = auth.agent_for_token(self.db, headers.get("authorization"))
        if agent is None:
            return await _json(send, 401, {"detail": "a valid agent token is required"},
                               [(b"www-authenticate", b'Bearer realm="valet"')])
        if agent != name:
            log.warning("gateway: agent %s's token used for %s's endpoint; refused", agent, name)
            return await _json(send, 403, {"detail": "this token is for another agent"})
        row = self.db.one("SELECT state FROM agents WHERE name = ?", (name,))
        if not row or row["state"] not in ("starting", "running") or not self.control.defs.exists(name):
            return await _json(send, 403, {"detail": f"{name} isn't running"})
        await self.manager.handle_request({**scope, SCOPE_KEY: name}, receive, send)

    # ---------- registry and upstreams ----------

    def registry(self) -> Registry:
        return load(self.control.defs.root)

    def _secret(self, name: str) -> str | None:
        """`${secret:NAME}`: the value from Valet's secrets store, decrypted now (None if it isn't set)."""
        try:
            return self.control.secrets.get(name)
        except SecretError as e:
            raise RegistryError(str(e)) from None

    def _refs(self, spec: ServerSpec) -> dict[str, Any]:
        """What the entry's references stand for now, for its fingerprint, without decrypting anything: env values,
        and secrets by updated_at (set again or deleted -> a new connection, or none)."""
        refs = references(spec)
        env = sorted({n for scheme, n in refs if scheme == "env"})
        secrets = sorted({n for scheme, n in refs if scheme == "secret"})
        return {"env": {n: self.lookups["env"](n) for n in env},
                "secret": self.control.secrets.stamps(secrets) if secrets else {}}

    async def upstream(self, name: str) -> Upstream:
        """The server called `name`: Valet's own, or a registry entry (connected on first use, reconnected when
        its entry or what its references stand for change; credentials are resolved, secrets decrypted, only
        then). UpstreamError if there's none or it can't be configured; a connection it had is then closed."""
        if name in self.builtins:
            return self.builtins[name]
        if name in PLANNED:
            raise UpstreamError(f"{name} isn't available yet (it arrives in {PLANNED[name]})")
        async with self._lock:
            old = self._conns.get(name)
            try:
                spec = self.registry().servers.get(name)
                if spec is None:
                    raise UpstreamError(f"no server named {name} in mcp.yaml")
                fp = fingerprint(spec, self._refs(spec))
                if old is not None and old.fingerprint == fp:
                    return old
                used: list[str] = []
                spec = resolved(spec, self.lookups, used)
            except (RegistryError, UpstreamError) as e:
                if old is not None:     # it can't be used any more (its entry, a secret it needs, is gone)
                    self._close_later(self._conns.pop(name))
                raise e if isinstance(e, UpstreamError) else UpstreamError(f"{name}: {e}") from None
            conn = self._conns[name] = Connection(name, spec, fp, self.cfg.data / "mcp" / name, redact=used)
        if old is not None:             # its entry or credentials changed: the old one closes in the background
            self._close_later(old)
        return conn

    def _close_later(self, conn: Connection) -> None:
        task = asyncio.create_task(conn.close())
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    def servers(self) -> dict[str, Any]:
        """GET /api/gateway/servers: every server an agent can be given (Valet's own, then the registry's), which
        agents' definitions list it, and whether the gateway holds a connection to it now. `error`: mcp.yaml
        doesn't validate (its servers are then unavailable)."""
        out = [{"name": s.name, "kind": "builtin", "builtin": True, "description": say(getattr(s, "description", None) or "") or None,
                "comes_with": getattr(s, "comes_with", None)} for s in self.builtins.values()]
        error = None
        try:
            reg = self.registry()
        except RegistryError as e:
            reg, error = Registry(), str(e)
        out += [{**public_view(n, spec), "comes_with": None} for n, spec in sorted(reg.servers.items())]
        users: dict[str, list[str]] = {}
        for agent in self.control.defs.names():
            with contextlib.suppress(DefinitionError, FileNotFoundError):
                for s in self.control.defs.definition(agent).gateway_servers():
                    users.setdefault(s, []).append(agent)
        for s in out:
            s["agents"] = users.get(s["name"], [])
            conn = self._conns.get(s["name"])
            s["connected"] = True if s["builtin"] else bool(conn and conn.connected)
        return {"servers": out, "error": error}

    # ---------- an agent's tools ----------

    def definition(self, agent: str) -> AgentDefinition:
        return self.control.defs.load(agent)[0]

    async def _server_tools(self, d: AgentDefinition, server: str, fresh: bool = True,
                            grants: bool = True) -> list[Exposed]:
        """The server's tools as `d` gets them, each decided (decide_tool) with its rules and, unless `grants` is
        False, the agent's live hour grants. A server that decides its own calls (valet.browser) gets `allow` for
        each, source `server`: it's listed, and the call goes through to the server, which applies its own rules;
        only the owner's deny rule for the tool stops it here."""
        up = await self.upstream(server)
        tools = await up.list_tools(fresh=fresh)
        rules = d.tools.rules.get(server)
        granted = scopes.granted(self.db, d.name, server) if grants else set()
        out = []
        for t in tools:
            ann = t.annotations.model_dump(by_alias=True, exclude_none=True, mode="json") if t.annotations else None
            if up.decides_calls:
                denied = rule_for(rules, t.name) == "deny"
                decision, source = ("deny", "rule") if denied else ("allow", "server")
            else:
                decision, source = decide_tool(d.permissions, ann, rules, t.name, t.name in granted)
            out.append(Exposed(f"{prefix(server)}__{t.name}", server, t, decision, source, decide(d.permissions, ann)))
        return out

    def own_rules(self, server: str) -> bool:
        """Whether the server decides its own calls (Upstream.decides_calls)."""
        up = self.builtins.get(server)
        return bool(up and up.decides_calls)

    async def tools(self, d: AgentDefinition, timeout: float | None = None,
                    grants: bool = True) -> list[dict[str, Any]]:
        """Per gateway server of the definition, in order: {server, ok, error, tools: [Exposed]} (denied ones included:
        callers filter). Servers are listed concurrently; one that fails doesn't hide the others. `timeout`: seconds
        to wait for them; a server that hasn't answered by then comes back not ok, while its listing carries on in
        the background (warming the connection and its cache). `grants`: False decides without hour grants (the
        environment layer: they expire)."""
        names = d.gateway_servers()
        if timeout is None:
            results = await asyncio.gather(*(self._server_tools(d, s, grants=grants) for s in names),
                                           return_exceptions=True)
        else:
            tasks = [asyncio.ensure_future(self._server_tools(d, s, grants=grants)) for s in names]
            try:
                if tasks:
                    await asyncio.wait(tasks, timeout=timeout)
            finally:
                for t in tasks:
                    if not t.done():
                        self._listing.add(t)
                        t.add_done_callback(self._listing.discard)
                        t.add_done_callback(lambda t: t.cancelled() or t.exception())  # retrieved, never raised
            results = [(t.exception() or t.result()) if t.done()
                       else UpstreamError(f"{server}: no answer within {timeout:.0f}s")
                       for server, t in zip(names, tasks)]
        out = []
        for server, r in zip(names, results):
            if isinstance(r, BaseException):
                if not isinstance(r, UpstreamError):
                    log.warning("gateway: listing %s failed", server, exc_info=r)
                out.append({"server": server, "ok": False, "error": str(r) or type(r).__name__, "tools": []})
            else:
                out.append({"server": server, "ok": True, "error": None, "tools": r})
        return out

    async def agent_tools(self, agent: str, permissions: str | None = None) -> dict[str, Any]:
        """GET /api/agents/{name}/tools: what the agent sees through the gateway, and what each call would do (and
        why: its rule, a live hour grant, or the level), at its saved permission level or at `permissions` (a
        preview: nothing is saved)."""
        d = self.definition(agent)
        if permissions is not None:
            d = d.model_copy(update={"permissions": permissions})
        servers = []
        for s in await self.tools(d):
            servers.append({"server": s["server"], "ok": s["ok"], "error": s["error"],
                            "own_rules": self.own_rules(s["server"]), "tools": [
                {"name": e.name, "tool": e.tool.name, "title": e.tool.title or (e.tool.annotations and
                                                                               e.tool.annotations.title),
                 "description": e.tool.description, "annotations": e.annotations, "decision": e.decision,
                 "source": e.source, "level_decision": e.level_decision}
                for e in s["tools"]]})
        return {"agent": agent, "permissions": d.permissions, "url": self.url(agent), "servers": servers}

    # ---------- MCP handlers ----------

    async def _on_list_tools(self, ctx, params) -> types.ListToolsResult:
        agent = ctx.request.scope[SCOPE_KEY]
        try:
            d = self.definition(agent)
        except (DefinitionError, FileNotFoundError):
            return types.ListToolsResult(tools=[])
        tools = [e.public() for s in await self.tools(d) for e in s["tools"] if e.decision != "deny"]
        return types.ListToolsResult(tools=tools)

    async def _on_call_tool(self, ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        return await self.call(ctx.request.scope[SCOPE_KEY], params.name, params.arguments)

    # ---------- a call ----------

    async def _find(self, d: AgentDefinition, name: str) -> Exposed | None:
        """The agent's tool called `name`: its server is the one whose prefix it starts with (the longest)."""
        candidates = sorted((s for s in d.gateway_servers() if name.startswith(prefix(s) + "__")),
                            key=len, reverse=True)
        for server in candidates:
            for fresh in (False, True):         # a tool the cached listing doesn't have yet: list again
                found = next((e for e in await self._server_tools(d, server, fresh) if e.name == name), None)
                if found is not None:
                    return found
        return None

    async def call(self, agent: str, name: str, arguments: dict[str, Any] | None) -> types.CallToolResult:
        """Decide, maybe ask, forward, audit. Every refusal comes back as an error result the model can read."""
        started = time.time()
        try:
            d = self.definition(agent)
        except (DefinitionError, FileNotFoundError) as e:
            return _text_result(f"Valet can't read this agent's definition: {e}")
        try:
            e = await self._find(d, name)
        except UpstreamError as err:
            self._audit(agent, name.partition("__")[0], name, arguments, None, "failed", started, str(err))
            return _text_result(str(err))
        if e is None:
            self._audit(agent, "", name, arguments, None, "unknown", started, "no such tool")
            return _text_result(f"There's no tool called {name} for this agent.")

        call_id = self._audit_start(agent, e.server, e.tool.name, arguments, e.decision, started)
        outcome, error = "failed", None
        try:
            if e.decision == "deny":
                outcome = "denied"
                if e.source == "rule":
                    return _text_result(say(f"Valet doesn't allow {e.server} {e.tool.name} for this agent: {{Owner}}'s "
                                            "rule for it says no."))
                return _text_result(f"Valet doesn't allow {e.server} {e.tool.name} for this agent: its permission "
                                    f"level is {d.permissions}, which only lets read-only tools run.")
            if e.decision == "ask":
                choice = await self._ask(agent, e, arguments)
                if choice is None:
                    outcome = "expired"
                    return _text_result(say(f"{{Owner}} didn't answer in time, so {e.tool.name} was not run. Try "
                                            "again later if it's still needed."))
                if choice not in scopes.ALLOWING:
                    outcome = "rejected"
                    return _text_result(say(f"{{Owner}} rejected this call: {e.tool.name} was not run."))
                await self._apply_scope(agent, e, choice)
            up = await self.upstream(e.server)
            result = await up.call_tool(e.tool.name, arguments, self.cfg.gateway_call_timeout, agent)
            outcome = "error" if result.is_error else "ok"
            return result.model_copy(update={"meta": None})
        except UpstreamError as err:
            outcome, error = "failed", str(err)
            return _text_result(str(err))
        except Exception as err:
            outcome, error = "failed", str(err) or type(err).__name__
            log.exception("gateway: %s calling %s %s", agent, e.server, e.tool.name)
            return _text_result(f"Valet's gateway failed on this call: {error}")
        except BaseException:
            outcome = "cancelled"       # the agent gave up on the call (cancelled, disconnected)
            raise
        finally:
            self._audit_end(call_id, outcome, error)

    async def _ask(self, agent: str, e: Exposed, arguments: dict[str, Any] | None) -> str | None:
        """An inbox item for the owner; the chosen optionId, or None if it expired (timeout, or the agent or its
        turn went away). The item never outlives the wait."""
        hub = self.control.hub
        title = f"{e.server}: {e.tool.title or (e.tool.annotations and e.tool.annotations.title) or e.tool.name}"
        tool = {"server": e.server, "name": e.tool.name, "title": e.tool.title, "description": e.tool.description,
                "arguments": arguments or {}, "annotations": e.annotations}
        row, fut = await hub.ask_tool(agent, title, tool, scopes.options(e.tool.name))
        try:
            with anyio.move_on_after(self.cfg.gateway_ask_timeout):
                outcome = await fut
                return outcome.get("optionId") if outcome and outcome.get("outcome") == "selected" else None
            return None
        finally:
            with anyio.CancelScope(shield=True):
                if await hub.expire_item(row):      # nobody answered: the agent stops showing as waiting
                    await hub.emit_agent(agent)

    async def _apply_scope(self, agent: str, e: Exposed, choice: str) -> None:
        """What an allowing answer covers beyond this call: a grant for an hour, or a rule in the agent's
        definition. A scope that can't be recorded is logged; the call the owner allowed runs anyway. Either way an
        agent frame follows, for a settings page open on it to look again."""
        try:
            if choice == scopes.ALLOW_HOUR:
                scopes.grant(self.db, agent, e.server, e.tool.name)
                await self.control.hub.emit_agent(agent)
            elif choice == scopes.ALLOW_ALWAYS:
                if await asyncio.to_thread(scopes.always_allow, self.control.defs, agent, e.server, e.tool.name):
                    await self.control.hub.emit_agent(agent)
        except Exception:
            log.warning("gateway: couldn't keep %s for %s %s %s", choice, agent, e.server, e.tool.name,
                        exc_info=True)

    # ---------- audit ----------

    def _audit_start(self, agent: str, server: str, tool: str, arguments: Any, decision: str | None,
                     started: float) -> int:
        args = _clip(json.dumps(arguments if arguments is not None else {}, ensure_ascii=False), ARGS_LIMIT)
        cur = self.db.execute("INSERT INTO tool_calls (agent, server, tool, arguments, decision, outcome, started_at) "
                              "VALUES (?, ?, ?, ?, ?, 'pending', ?)", (agent, server, tool, args, decision, started))
        return cur.lastrowid

    def _audit_end(self, call_id: int, outcome: str, error: str | None) -> None:
        now = time.time()
        self.db.execute("UPDATE tool_calls SET outcome = ?, error = ?, ended_at = ?, duration = ? - started_at "
                        "WHERE id = ?", (outcome, _clip(error, ERROR_LIMIT) if error else None, now, now, call_id))

    def _audit(self, agent: str, server: str, tool: str, arguments: Any, decision: str | None, outcome: str,
               started: float, error: str | None) -> None:
        self._audit_end(self._audit_start(agent, server, tool, arguments, decision, started), outcome, error)


async def _json(send, status: int, body: dict, headers: list[tuple[bytes, bytes]] = ()) -> None:
    data = json.dumps(body).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(data)).encode()),
                            *headers]})
    await send({"type": "http.response.body", "body": data})
