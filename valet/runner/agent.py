"""One agent's harness process and its ACP connection (acp.spawn_agent_process / acp.ClientSideConnection).
Owns the agent's sessions on this process, the running turn per session, and pending permission requests.

Every message this agent sends goes through one outbox drained by one task, so the control plane sees them in
the order the harness produced them (docs/PROTOCOL.md, Ordering)."""
import asyncio
import collections
import contextlib
import json
import logging
import shutil
import uuid
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import acp
from acp.schema import ClientCapabilities, HttpHeader, HttpMcpServer, Implementation, PromptRequest
from pydantic import ValidationError

from valet import __version__
from valet.shared.config import RunnerConfig
from valet.shared.definition import AgentDefinition
from valet.shared.protocol import (
    ActiveTurn, AgentState, AgentStatus, GatewayAccess, PermissionClosed, PermissionRequest, SessionUpdate, TurnEnd,
)

from . import claude_sessions
from .acp_client import ValetClient, acp_json
from .egress import EgressError, Proxy, docker_env
from .harness import Compiled, HarnessError, compile_agent
from .launch import Launch, LaunchError, config_dir, container_name, plan
from .supervisor import CommandError, Send

log = logging.getLogger(__name__)

STREAM_LIMIT = 50 * 1024 * 1024   # one ACP line can carry a large tool output
INIT_TIMEOUT = 60.0               # spawn to initialize response (a cold container start included)
STDERR_LINES = 20                 # kept for the failure message
DOCKER_TIMEOUT = 30.0
CANCELLED = {"outcome": "cancelled"}
GATEWAY_SERVER = "valet"          # the MCP server name the harness sees for the gateway (Claude: mcp__valet__...)


def write_config(root: Path, files: dict[str, str]) -> None:
    """Replace the agent's config dir with `files` (relative path -> text): a file the definition no longer
    produces is gone. World-readable, so the container's user can read it whatever its uid; nothing secret goes
    here (credentials travel as env)."""
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    root = root.resolve()
    for rel, text in files.items():
        path = (root / rel).resolve()
        if root not in path.parents:
            raise ValueError(f"config file {rel!r} is outside the config dir")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    for p in [root, *root.rglob("*")]:
        p.chmod(0o755 if p.is_dir() else 0o644)


def describe(e: BaseException) -> str:
    """acp.RequestError keeps the useful part in .data; str() alone is just "Internal error"."""
    data = getattr(e, "data", None)
    return f"{e}: {json.dumps(data)}" if data else (str(e) or type(e).__name__)


class AgentProcess:
    def __init__(self, definition: AgentDefinition, files: dict[str, str], cfg: RunnerConfig, send: Send,
                 *, secrets: dict[str, str] | None = None, gateway: GatewayAccess | None = None) -> None:
        self.definition = definition
        self.name = definition.name
        self.files = files
        self._secrets = dict(secrets or {})       # agent.start's: env vars of the harness, nowhere else
        self.cfg = cfg
        self.gateway = gateway            # the MCP gateway endpoint and this agent's token, if it has MCP servers
        self._send = send
        self.state = "stopped"
        self.error: str | None = None
        self.capabilities: dict[str, Any] | None = None
        self.agent_info: dict[str, Any] | None = None
        self.compiled: Compiled | None = None
        self.launch: Launch | None = None
        self._conn: Any = None
        self._stack: AsyncExitStack | None = None
        self._stopping = False
        self._watcher: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stderr: collections.deque[str] = collections.deque(maxlen=STDERR_LINES)
        self._sessions: set[str] = set()                  # sessions this process created or loaded
        self._loading: set[str] = set()                   # session/load or resume in flight: updates are replays
        self._cancelled: set[str] = set()                 # cancelled while loading: the turn ends before prompting
        self._turns: dict[str, str] = {}                  # session_id -> accepted turn_id (loading or prompting)
        # session.import in flight: session_id -> (its history's current made-up turn id, last update a user message)
        self._importing: dict[str, tuple[str | None, bool]] = {}
        self._turn_tasks: set[asyncio.Task] = set()
        self._pending: dict[str, tuple[PermissionRequest, asyncio.Future]] = {}   # request_id -> (request, answer)
        self._out: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._sender: asyncio.Task | None = None
        self._egress: Proxy | None = None                 # its network proxy, when its network isn't open

    @property
    def container(self) -> bool:
        """Runs in a container (computer level container or none)."""
        return self.definition.computer.level != "host"

    # ---------- state for the hello ----------

    def agent_state(self) -> AgentState:
        return AgentState(agent=self.name, state=self.state, error=self.error,
                          capabilities=self.capabilities, agent_info=self.agent_info)

    def active_turns(self) -> list[ActiveTurn]:
        return [ActiveTurn(agent=self.name, session_id=s, turn_id=t) for s, t in self._turns.items()]

    def pending_permissions(self) -> list[str]:
        return list(self._pending)

    def pending_requests(self) -> list[PermissionRequest]:
        return [req for req, _ in self._pending.values()]

    # ---------- lifecycle ----------

    async def start(self) -> None:
        """compile -> plan -> (container: `docker rm -f` a stale container of the same name) -> write the config
        dir (and, on the host, create its own work dir; a computer.workdir must exist) -> (a restricted network: its proxy, egress.Proxy) -> spawn ->
        ACP initialize (protocol_version=acp.PROTOCOL_VERSION, ClientCapabilities() with fs and terminal off,
        client_info name "valet"). Sends agent.status starting, then running (with capabilities) or failed."""
        await self._start()
        await self._flush()

    async def _start(self) -> None:
        self._stopping = False
        self.capabilities = self.agent_info = None
        self._stderr.clear()
        self._set_state("starting")
        try:
            config_local, config_inside = config_dir(self.definition, self.cfg)
            self.compiled = compile_agent(self.definition, self.files, self.cfg, config_inside, self.gateway)
            self.launch = launch = plan(self.definition, self.compiled, self.cfg, self._secrets,
                                        gateway=self.gateway.url if self.gateway else None)
        except (HarnessError, LaunchError) as e:
            self._set_state("failed", str(e))
            return

        env = dict(launch.env)
        if self.container:
            env.update(docker_env())
            await self._docker("rm", "-f", container_name(self.name))
        if launch.cwd and self.definition.computer.workdir and not Path(launch.cwd).is_dir():
            self._set_state("failed", f"its working folder {launch.cwd} doesn't exist on this machine")
            return
        try:
            if launch.cwd:
                Path(launch.cwd).mkdir(parents=True, exist_ok=True)
            write_config(config_local, self.compiled.files)
        except (OSError, ValueError) as e:
            self._set_state("failed", f"cannot prepare the agent's files: {e}")
            return
        if launch.egress is not None:
            self._egress = Proxy(self.name, launch.egress, self.cfg.docker, self._on_egress_exit)
            try:
                await self._egress.start()
            except EgressError as e:
                self._egress = None
                self._set_state("failed", str(e))
                return

        self._stack = AsyncExitStack()
        try:
            conn, proc = await self._stack.enter_async_context(acp.spawn_agent_process(
                ValetClient(self), launch.command, *launch.args, env=env, cwd=launch.cwd,
                transport_kwargs={"limit": STREAM_LIMIT}))
        except OSError as e:
            self._stack = None
            await self._stop_egress()
            self._set_state("failed", f"cannot run {launch.command}: {e}")
            return
        self._conn = conn
        self._stderr_task = asyncio.create_task(self._drain_stderr(proc))
        log.info("agent %s: spawned %s (pid %s)", self.name, launch.command, proc.pid)

        try:
            init = await asyncio.wait_for(conn.initialize(
                protocol_version=acp.PROTOCOL_VERSION, client_capabilities=ClientCapabilities(),
                client_info=Implementation(name="valet", version=__version__)), INIT_TIMEOUT)
        except Exception as e:
            reason = "initialize timed out" if isinstance(e, TimeoutError) else f"initialize failed: {describe(e)}"
            self._stopping = True
            await self._close()
            self._set_state("failed", self._exit_error(reason, proc.returncode))
            return
        self.capabilities = acp_json(init.agent_capabilities) if init.agent_capabilities else {}
        self.agent_info = acp_json(init.agent_info) if init.agent_info else None
        self._watcher = asyncio.create_task(self._watch(proc))
        self._set_state("running")

    async def stop(self) -> None:
        """Close pending permission requests (reason "exited"), close ACP, end the process. Sends agent.status stopped."""
        self._stopping = True
        self._abandon("agent stopped")
        await self._close()
        if self._watcher and self._watcher is not asyncio.current_task():
            self._watcher.cancel()
        self._set_state("stopped")
        await self._flush()

    async def _watch(self, proc: asyncio.subprocess.Process) -> None:
        """The harness exiting on its own: every running turn and pending request ends, the agent is failed."""
        code = await proc.wait()
        if self._stopping:
            return
        self._stopping = True
        log.warning("agent %s: harness exited with code %s", self.name, code)
        self._abandon("agent exited")
        await self._close()
        self._set_state("failed", self._exit_error(f"exited with code {code}", code))
        await self._flush()

    def _on_egress_exit(self, code: int | None) -> None:
        """The network proxy stopped on its own: the agent can't reach anything, so it stops too, failed."""
        asyncio.get_running_loop().create_task(self._lost_network(code))

    async def _lost_network(self, code: int | None) -> None:
        if self._stopping:
            return
        self._stopping = True
        self._abandon("its network proxy stopped")
        await self._close()
        if self._watcher and self._watcher is not asyncio.current_task():
            self._watcher.cancel()
        self._set_state("failed", f"its network proxy exited with code {code}")
        await self._flush()

    async def _stop_egress(self) -> None:
        egress, self._egress = self._egress, None
        if egress is not None:
            await egress.stop()

    def _abandon(self, turn_error: str) -> None:
        for sid, tid in list(self._turns.items()):
            self._end_turn(sid, tid, None, turn_error)
        for rid in list(self._pending):
            self._close_request(rid, "exited")

    async def _close(self) -> None:
        stack, self._stack, self._conn = self._stack, None, None
        for t in list(self._turn_tasks):
            t.cancel()
        if stack is not None:
            try:
                await stack.aclose()      # closes stdin, waits, then terminates/kills the process
            except Exception:
                log.exception("agent %s: error closing the harness", self.name)
        if self.container:
            await self._docker("stop", container_name(self.name))
        await self._stop_egress()
        if self._stderr_task is not None:
            await asyncio.wait({self._stderr_task}, timeout=1.0)
            self._stderr_task.cancel()

    def _exit_error(self, reason: str, code: int | None) -> str:
        if code is not None and "exited with code" not in reason:
            reason += f" (exited with code {code})"
        tail = "\n".join(self._stderr)
        return f"{reason}\n{tail}" if tail else reason

    async def _drain_stderr(self, proc: asyncio.subprocess.Process) -> None:
        """stderr is a pipe: unread, a chatty harness would block on it."""
        if proc.stderr is None:
            return
        try:
            while line := await proc.stderr.readline():
                text = line.decode(errors="replace").rstrip()
                self._stderr.append(text)
                log.info("agent %s stderr: %s", self.name, text)
        except (ValueError, OSError) as e:
            log.warning("agent %s: stopped reading stderr: %s", self.name, e)

    async def _docker(self, *args: str) -> None:
        """Best-effort docker CLI call (rm -f a stale container, stop one); failures are expected and ignored."""
        try:
            proc = await asyncio.create_subprocess_exec(
                self.cfg.docker, *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL, env={**acp.default_environment(), **docker_env()})
        except OSError as e:
            log.warning("agent %s: cannot run %s: %s", self.name, self.cfg.docker, e)
            return
        try:
            await asyncio.wait_for(proc.wait(), DOCKER_TIMEOUT)
        except TimeoutError:
            proc.kill()
            await proc.wait()

    # ---------- commands ----------

    def _running(self) -> Any:
        if self.state != "running" or self._conn is None:
            raise CommandError("not running")
        return self._conn

    def mcp_servers(self) -> list[HttpMcpServer]:
        """What every session/new, load and resume gets: the gateway as an `http` MCP server named "valet" with the
        agent's bearer token, when it has one and the harness advertised mcpCapabilities.http at initialize. None when
        the adapter gave the harness the gateway in its own config (DeepSeek Harness: Compiled.gateway_in_config)."""
        if self.gateway is None or (self.compiled is not None and self.compiled.gateway_in_config):
            return []
        if not ((self.capabilities or {}).get("mcpCapabilities") or {}).get("http"):
            log.warning("agent %s: the harness doesn't take HTTP MCP servers; it gets no gateway tools", self.name)
            return []
        return [HttpMcpServer(type="http", name=GATEWAY_SERVER, url=self.gateway.url,
                              headers=[HttpHeader(name="Authorization", value=f"Bearer {self.gateway.token}")])]

    async def new_session(self) -> dict[str, Any]:
        """ACP session/new(cwd=launch.session_cwd, mcp_servers=mcp_servers(), **compiled.session_meta) ->
        {"session_id": ..., "modes": ... | None}."""
        conn = self._running()
        try:
            s = await conn.new_session(cwd=self.launch.session_cwd, mcp_servers=self.mcp_servers(),
                                       **self.compiled.session_meta)
            modes = await self._set_mode(conn, s.session_id, s.modes)
        except (acp.RequestError, ConnectionError) as e:
            raise CommandError(f"session/new failed: {describe(e)}") from None
        self._sessions.add(s.session_id)
        return {"session_id": s.session_id, "modes": acp_json(modes) if modes else None}

    async def _set_mode(self, conn: Any, session_id: str, modes: Any) -> Any:
        """Put the session in compiled.mode (the permission level's) unless it's there already; return the modes as
        they are now. A harness with no modes at all has nothing to switch (logged). One whose modes lack that
        mode: with compiled.mode_strict (locked, ask) the session doesn't run (CommandError), since it might be in
        a freer mode from before; otherwise (a free mode, e.g. bypassPermissions, which Claude offers only to a
        non-root user) it keeps its own, which asks more, and that's logged. A failed session/set_mode raises:
        same reason."""
        want = self.compiled.mode
        if not want or (modes is not None and modes.current_mode_id == want):
            return modes
        if modes is None or want not in {m.id for m in modes.available_modes}:
            current = repr(modes.current_mode_id) if modes else "its own mode"
            if modes is not None and self.compiled.mode_strict:
                raise CommandError(f"the harness can't switch to the {want!r} mode its permission level needs "
                                   f"(session {session_id} is in {current})")
            log.warning("agent %s: the harness has no mode %r; session %s stays in %s", self.name, want, session_id,
                        current)
            return modes
        await conn.set_session_mode(session_id=session_id, mode_id=want)
        return modes.model_copy(update={"current_mode_id": want})

    async def prompt(self, session_id: str, turn_id: str, prompt: list[dict[str, Any]]) -> None:
        """Validate, register the turn and run it as a background task (loading the session first if this process
        doesn't know it); return at once. From here the turn is in the hello and ends with exactly one turn.end,
        so a slow session/load can't outlast the control plane's RPC timeout or slip out of a reconnect.
        Raises CommandError("busy"), ("cannot resume session")."""
        conn = self._running()
        if session_id in self._turns or session_id in self._importing:
            raise CommandError("busy")
        try:
            blocks = PromptRequest(session_id=session_id, prompt=prompt).prompt
        except ValidationError as e:
            raise CommandError(f"invalid prompt: {e.errors()[0]['msg']}") from None
        if session_id not in self._sessions and not self._can_resume():
            raise CommandError("cannot resume session")
        self._turns[session_id] = turn_id
        task = asyncio.create_task(self._run_turn(conn, session_id, turn_id, blocks))
        self._turn_tasks.add(task)
        task.add_done_callback(self._turn_tasks.discard)
        await asyncio.sleep(0)   # the turn task queues its session/prompt now, so a cancel can't overtake it

    # ---------- Claude Code sessions from outside Valet ----------

    def _claude_here(self) -> None:
        d = self.definition
        if d.harness != "claude" or d.computer.level != "host":
            raise CommandError("only a Claude Code agent on this machine (computer level host) can continue a "
                               "Claude Code session")

    async def claude_sessions(self) -> dict[str, Any]:
        """claude.sessions with `agent`: the sessions Claude Code ran in this agent's working folder."""
        self._claude_here()
        self._running()
        cwd = self.launch.session_cwd
        return {"cwd": cwd, "sessions": await asyncio.to_thread(claude_sessions.sessions_for, cwd)}

    async def import_session(self, session_id: str) -> dict[str, Any]:
        """session.import: session/load of a session of this agent's working folder that this process doesn't have,
        its history sent up as it replays (on_update -> _history), then its mode set as after session/new.
        -> {"session_id", "modes", "title" (as claude.sessions lists it)}."""
        self._claude_here()
        conn = self._running()
        if not (self.capabilities or {}).get("loadSession"):
            raise CommandError("this harness can't load sessions")
        if session_id in self._sessions or session_id in self._loading or session_id in self._turns:
            raise CommandError("already open")
        cwd = self.launch.session_cwd
        try:
            path = await asyncio.to_thread(claude_sessions.find, cwd, session_id)
        except claude_sessions.NotFound as e:
            raise CommandError(str(e)) from None
        info = await asyncio.to_thread(claude_sessions.session_info, path) or {}
        self._importing[session_id] = (None, False)
        self._loading.add(session_id)
        try:
            r = await conn.load_session(cwd=cwd, session_id=session_id, mcp_servers=self.mcp_servers(),
                                        **self.compiled.session_meta)
            modes = await self._set_mode(conn, session_id, r.modes if r else None)
        except (acp.RequestError, ConnectionError) as e:
            raise CommandError(f"session/load failed: {describe(e)}") from None
        finally:
            self._loading.discard(session_id)
            turn, _ = self._importing.pop(session_id)
            if turn is not None:
                self._emit(TurnEnd(agent=self.name, session_id=session_id, turn_id=turn, stop_reason="end_turn").dump())
            await self._flush()     # the history is up before the reply
        self._sessions.add(session_id)
        return {"session_id": session_id, "modes": acp_json(modes) if modes else None, "title": info.get("title")}

    def _history(self, session_id: str, update: dict[str, Any]) -> None:
        """One update of an imported session's history: a new made-up turn at each user message that follows
        something else (the one before it ends), history and replay set."""
        turn, after_user = self._importing[session_id]
        user = update.get("sessionUpdate") == "user_message_chunk"
        if turn is None or (user and not after_user):
            if turn is not None:
                self._emit(TurnEnd(agent=self.name, session_id=session_id, turn_id=turn, stop_reason="end_turn").dump())
            turn = uuid.uuid4().hex
        self._importing[session_id] = (turn, user)
        self._emit(SessionUpdate(agent=self.name, session_id=session_id, turn_id=turn, replay=True, history=True,
                                 update=update).dump())

    def _can_resume(self) -> bool:
        caps = self.capabilities or {}
        return bool(caps.get("loadSession")) or "resume" in (caps.get("sessionCapabilities") or {})

    async def _resume(self, conn: Any, session_id: str) -> None:
        """A session this process doesn't know (the harness restarted): load it if the harness can (its
        replayed history goes up with replay=True), else resume it."""
        meta = self.compiled.session_meta
        cwd = self.launch.session_cwd
        self._loading.add(session_id)
        try:
            servers = self.mcp_servers()
            if (self.capabilities or {}).get("loadSession"):
                r = await conn.load_session(cwd=cwd, session_id=session_id, mcp_servers=servers, **meta)
            else:
                r = await conn.resume_session(session_id=session_id, cwd=cwd, mcp_servers=servers, **meta)
            await self._set_mode(conn, session_id, r.modes if r else None)
        except (acp.RequestError, ConnectionError) as e:
            raise CommandError(f"cannot resume session: {describe(e)}") from None
        finally:
            self._loading.discard(session_id)
        self._sessions.add(session_id)

    async def _run_turn(self, conn: Any, session_id: str, turn_id: str, blocks: list[Any]) -> None:
        stop_reason = error = None
        try:
            if session_id not in self._sessions:
                await self._resume(conn, session_id)
            if session_id in self._cancelled:       # the owner cancelled while it loaded: never prompt
                stop_reason = "cancelled"
            else:
                r = await conn.prompt(session_id=session_id, prompt=blocks)
                stop_reason = r.stop_reason
        except CommandError as e:
            error = str(e)
        except acp.RequestError as e:
            error = describe(e)
        except ConnectionError:
            error = "agent exited"
        except Exception as e:
            log.exception("agent %s: turn %s failed", self.name, turn_id)
            error = describe(e)
        finally:
            self._cancelled.discard(session_id)
        self._end_turn(session_id, turn_id, stop_reason, error)

    def _end_turn(self, session_id: str, turn_id: str, stop_reason: str | None, error: str | None) -> None:
        """Exactly one turn.end per accepted prompt, whoever notices the end first (prompt returning, the process
        exiting, a stop)."""
        if self._turns.get(session_id) != turn_id:
            return
        del self._turns[session_id]
        self._emit(TurnEnd(agent=self.name, session_id=session_id, turn_id=turn_id,
                           stop_reason=stop_reason, error=error).dump())

    async def cancel(self, session_id: str) -> None:
        """ACP session/cancel, and answer this session's pending permission requests {"outcome": "cancelled"}
        (sending permission.closed reason "cancelled"), as the ACP spec requires. A turn still loading its
        session is marked instead: it ends "cancelled" without prompting."""
        conn = self._running()
        if session_id in self._loading:
            self._cancelled.add(session_id)
            return
        try:
            await conn.cancel(session_id=session_id)
        except ConnectionError:
            raise CommandError("not running") from None
        for rid, (req, _) in list(self._pending.items()):
            if req.session_id == session_id:
                self._close_request(rid, "cancelled")

    async def answer(self, request_id: str, outcome: dict[str, Any]) -> None:
        """Resolve a pending session/request_permission. Raises CommandError("unknown request")."""
        entry = self._pending.get(request_id)
        if entry is None or entry[1].done():
            raise CommandError("unknown request")
        try:
            acp.RequestPermissionResponse.model_validate({"outcome": outcome})
        except ValidationError:
            raise CommandError("invalid outcome") from None
        del self._pending[request_id]
        entry[1].set_result(outcome)
        # Also as a notification: if the reply is lost with the link, the resent notification still records it.
        self._emit(PermissionClosed(agent=self.name, session_id=entry[0].session_id, request_id=request_id,
                                    reason="answered", outcome=outcome).dump())

    def _close_request(self, request_id: str, reason: str) -> None:
        req, fut = self._pending.pop(request_id)
        if not fut.done():
            fut.set_result(CANCELLED)
        self._emit(PermissionClosed(agent=self.name, session_id=req.session_id, request_id=request_id,
                                    reason=reason).dump())

    # ---------- from the harness (ValetClient) ----------

    def on_update(self, session_id: str, update: dict[str, Any]) -> None:
        if session_id in self._importing:
            self._history(session_id, update)
            return
        replay = session_id in self._loading
        self._emit(SessionUpdate(agent=self.name, session_id=session_id, replay=replay, update=update,
                                 turn_id=None if replay else self._turns.get(session_id)).dump())

    async def on_permission(self, session_id: str, tool_call: dict[str, Any],
                            options: list[dict[str, Any]]) -> dict[str, Any]:
        """Hold the harness's request open until the owner answers (or the turn is cancelled / the process
        ends). The request id doubles as the inbox item id."""
        request_id = uuid.uuid4().hex
        fut = asyncio.get_running_loop().create_future()
        req = PermissionRequest(agent=self.name, session_id=session_id, turn_id=self._turns.get(session_id),
                                request_id=request_id, tool_call=tool_call, options=options)
        self._pending[request_id] = (req, fut)
        self._emit(req.dump())
        try:
            return await fut
        finally:
            self._pending.pop(request_id, None)

    # ---------- outbox ----------

    def _set_state(self, state: str, error: str | None = None) -> None:
        self.state, self.error = state, error
        if state == "failed":
            log.warning("agent %s failed: %s", self.name, error)
        self._emit(AgentStatus(**self.agent_state().model_dump()).dump())

    def _emit(self, msg: dict[str, Any]) -> None:
        """Synchronous on purpose: callers must not await before their message is in line."""
        self._out.put_nowait(msg)
        if self._sender is None or self._sender.done():
            self._sender = asyncio.get_running_loop().create_task(self._drain_out())

    async def _drain_out(self) -> None:
        while True:
            msg = await self._out.get()
            try:
                await self._send(msg)
            except Exception:
                log.exception("agent %s: couldn't send %s", self.name, msg.get("type"))
            finally:
                self._out.task_done()

    async def _flush(self, timeout: float = 5.0) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._out.join(), timeout)

    async def close_outbox(self) -> None:
        """Send what's queued, then end the sender task (the runner is shutting down)."""
        await self._flush()
        if self._sender is not None:
            self._sender.cancel()

