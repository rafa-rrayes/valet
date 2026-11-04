"""Agents on this runner: one AgentProcess per agent name. Turns control-plane commands into calls on them and
reports their state for the hello. Also the owner's way into an agent's computer: its terminals (terminal.py), its
files (files.py) and its Docker volumes; and what's left of a deleted agent (agent.forget)."""
import asyncio
import logging
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import acp
from pydantic import ValidationError

from valet.shared.config import RunnerConfig
from valet.shared.definition import NAME_RE, AgentDefinition
from valet.shared import handoff
from valet.shared.protocol import (
    ActiveTurn, AgentForget, AgentStart, AgentState, AgentStatus, AgentStop, ClaudeSessions, ClaudeTranscript,
    FilesCancel, FilesList, FilesMkdir, FilesRead, FilesWrite, GatewayAccess, PermissionAnswer, PermissionRequest, SessionCancel,
    SessionImport, SessionNew, SessionPrompt, TerminalAck, TerminalClose, TerminalInput, TerminalOpen,
    TerminalResize, VolumesList, VolumesRemove,
)

Send = Callable[[dict[str, Any]], Awaitable[None]]   # sends one runner->control message (protocol model .dump())
Notify = Callable[[dict[str, Any]], None]            # sends one unnumbered message now, if connected (or drops it)

log = logging.getLogger(__name__)


class CommandError(Exception):
    """Becomes Reply(ok=False, error=str(e))."""


from . import agent as _agent  # noqa: E402  (agent imports CommandError and Send from here)
from . import claude_sessions  # noqa: E402
from . import files as _files  # noqa: E402
from . import terminal as _terminal  # noqa: E402
from .egress import docker as _docker, docker_env  # noqa: E402
from .launch import container_name  # noqa: E402

VOLUMES = (("home", "/home/agent"), ("work", "/work"))     # valet-agent-<name>-<suffix>, mounted at


class Supervisor:
    def __init__(self, cfg: RunnerConfig, send: Send, notify: Notify | None = None) -> None:
        self.cfg = cfg
        self._send = send
        self._notify = notify if notify is not None else (lambda msg: None)
        self._agents: dict[str, _agent.AgentProcess] = {}
        self._locks: dict[str, asyncio.Lock] = {}     # start/stop of one agent never interleave
        self._tasks: set[asyncio.Task] = set()
        self.terminals: dict[str, _terminal.Terminal] = {}
        self.uploads = _files.Uploads()

    async def handle(self, msg: Any) -> dict[str, Any]:
        """Execute one control->runner command (a parsed protocol model) and return the Reply result.
        Raises CommandError for expected failures (busy, not running, unknown request...)."""
        match msg:
            case AgentStart():
                definition = self._definition(msg)
                self._spawn(self._start(definition, msg.files, msg.secrets, msg.gateway))
                return {}
            case AgentStop():
                await self._stop(msg.agent)
                return {}
            case AgentForget():
                return await self._forget(msg.agent, msg.volumes)
            case SessionNew():
                return await self._get(msg.agent).new_session()
            case SessionPrompt():
                await self._get(msg.agent).prompt(msg.session_id, msg.turn_id, msg.prompt)
                return {}
            case SessionCancel():
                await self._get(msg.agent).cancel(msg.session_id)
                return {}
            case PermissionAnswer():
                proc = self._agents.get(msg.agent)
                if proc is None:
                    raise CommandError("unknown request")
                await proc.answer(msg.request_id, msg.outcome)
                return {}
            case SessionImport():
                return await self._get(msg.agent).import_session(msg.session_id)
            case ClaudeSessions():
                if msg.agent is not None:
                    return await self._get(msg.agent).claude_sessions()
                try:
                    if msg.folder is not None:
                        return {"folder": msg.folder,
                                "sessions": await asyncio.to_thread(claude_sessions.sessions_in, msg.folder)}
                    return {"folders": await asyncio.to_thread(claude_sessions.folders)}
                except claude_sessions.NotFound as e:
                    raise CommandError(str(e)) from None
            case ClaudeTranscript():
                try:
                    t = await asyncio.to_thread(claude_sessions.read, msg.folder, msg.session_id)
                except claude_sessions.NotFound as e:
                    raise CommandError(str(e)) from None
                entries, omitted = handoff.newest(t["entries"], msg.budget)
                return {"title": t["title"], "cwd": t["cwd"], "entries": entries, "omitted": omitted}
            case TerminalOpen():
                await self._open_terminal(msg)
                return {}
            case TerminalClose():
                t = self.terminals.get(msg.terminal)
                if t is not None:
                    await t.close("closed")
                return {}
            case FilesList() | FilesRead() | FilesWrite() | FilesCancel() | FilesMkdir():
                return await self._files(msg)
            case VolumesList():
                return {"volumes": await self._volumes(msg.agent)}
            case VolumesRemove():
                return await self._remove_volumes(msg.agent)
        raise CommandError(f"unknown command {getattr(msg, 'type', type(msg).__name__)!r}")

    def one_way(self, msg: Any) -> None:
        """terminal.input / resize / ack, in the order they came; one for a terminal that's gone is dropped."""
        t = self.terminals.get(msg.terminal)
        if t is None:
            return
        match msg:
            case TerminalInput():
                try:
                    t.input(_files.unb64(msg.data))
                except _files.FilesError:
                    log.warning("terminal %s: input that isn't base64, dropped", msg.terminal)
            case TerminalResize():
                t.resize(msg.cols, msg.rows)
            case TerminalAck():
                t.ack(msg.bytes)

    def close_terminals(self, reason: str, agent: str | None = None) -> list[asyncio.Task]:
        """Close every terminal (of `agent`): the link dropped, the agent stopped. -> the tasks ending them."""
        return [t.close(reason) for t in list(self.terminals.values()) if agent is None or t.agent == agent]

    # ---------- the agent's computer ----------

    def _computer(self, name: str) -> "_agent.AgentProcess":
        """A running agent that has a computer of its own (container or host)."""
        proc = self._get(name)
        if proc.state != "running":
            raise CommandError("not running")
        if proc.definition.computer.level == "none":
            raise CommandError("it has no computer (level none): no terminal and no files")
        return proc

    async def _open_terminal(self, msg: TerminalOpen) -> None:
        proc = self._computer(msg.agent)
        if msg.terminal in self.terminals:
            raise CommandError("that terminal is open already")
        if sum(1 for t in self.terminals.values() if t.agent == msg.agent and t.open) >= _terminal.MAX_TERMINALS:
            raise CommandError(f"it has {_terminal.MAX_TERMINALS} terminals open already: close one first")
        if proc.container:
            argv = _terminal.container_command(self.cfg.docker, container_name(msg.agent), msg.terminal, "/work")
            env = {**acp.default_environment(), **_terminal.TERM_ENV, **docker_env()}
            prefix = [self.cfg.docker, "exec", "-i", container_name(msg.agent)]

            async def killer() -> None:
                await _files.ContainerFiles([], prefix).helper("kill", {"terminal": msg.terminal})
            cwd = None
        else:
            argv, env, killer = _terminal.host_command(), _terminal.host_env(msg.terminal), None
            cwd = proc.launch.cwd
        t = _terminal.Terminal(msg.terminal, msg.agent, argv, env, cwd, msg.cols, msg.rows, self._notify, killer)
        try:
            await t.start()
        except _terminal.TerminalError as e:
            raise CommandError(str(e)) from None
        self.terminals[msg.terminal] = t
        task = asyncio.ensure_future(self._forget_terminal(t))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _forget_terminal(self, t: "_terminal.Terminal") -> None:
        """Drop a terminal from the table once it's closed and its processes are gone."""
        while t.open:
            await asyncio.sleep(0.5)
        if t.closing is not None:
            await asyncio.gather(t.closing, return_exceptions=True)
        if self.terminals.get(t.id) is t:
            del self.terminals[t.id]

    def files_of(self, proc: "_agent.AgentProcess") -> "_files.HostFiles | _files.ContainerFiles":
        roots = _files.roots_of(proc.definition, self.cfg)
        if not proc.container:
            return _files.HostFiles(roots)
        return _files.ContainerFiles(roots, [self.cfg.docker, "exec", "-i", container_name(proc.name)])

    async def _files(self, msg: Any) -> dict[str, Any]:
        files = self.files_of(self._computer(msg.agent))
        for agent, upload, folder in self.uploads.stale():
            if agent == msg.agent:
                try:
                    await files.cancel(upload, folder)
                except _files.FilesError:
                    pass
        try:
            match msg:
                case FilesList():
                    out = await files.list(msg.path)
                    return {"roots": [r.view() for r in files.roots], **out}
                case FilesRead():
                    meta, data = await files.read(msg.path, msg.offset, msg.length)
                    return {**meta, "data": _files.b64(data)}
                case FilesWrite():
                    data = _files.unb64(msg.data)
                    self.uploads.touch(msg.agent, msg.upload, msg.dir)
                    try:
                        out = await files.write(msg.upload, msg.dir, msg.name, msg.offset, data, msg.final,
                                                msg.exists)
                    except _files.FilesError:
                        self.uploads.done(msg.upload)
                        raise
                    if msg.final:
                        self.uploads.done(msg.upload)
                    return out
                case FilesCancel():
                    self.uploads.done(msg.upload)
                    await files.cancel(msg.upload, msg.dir)
                    return {}
                case FilesMkdir():
                    return await files.mkdir(msg.path)
        except _files.FilesError as e:
            raise CommandError(str(e)) from None
        raise CommandError("unknown files command")

    async def _volumes(self, name: str) -> list[dict[str, str]]:
        if not NAME_RE.match(name):
            raise CommandError("not an agent name")
        if shutil.which(self.cfg.docker) is None:
            return []           # no Docker here: no volumes either
        out = []
        for suffix, mount in VOLUMES:
            vol = f"{container_name(name)}-{suffix}"
            code, text = await _docker(self.cfg.docker, "volume", "inspect", "--format", "{{.Name}}", vol)
            if code == 0:
                out.append({"name": vol, "mount": mount})
            elif code < 0:
                raise CommandError(f"can't ask Docker: {text}")
        return out

    async def _remove_volumes(self, name: str) -> dict[str, Any]:
        if not NAME_RE.match(name):
            raise CommandError("not an agent name")
        async with self._lock(name):
            proc = self._agents.get(name)
            if proc is not None and proc.state in ("starting", "running"):
                raise CommandError("it's running: stop it first")
            code, _ = await _docker(self.cfg.docker, "container", "inspect", container_name(name))
            if code == 0:
                raise CommandError("its container still exists: stop it first")
            return {"removed": await self._delete_volumes(name)}

    async def _delete_volumes(self, name: str) -> list[str]:
        vols = [v["name"] for v in await self._volumes(name)]
        if vols:
            code, text = await _docker(self.cfg.docker, "volume", "rm", *vols)
            if code != 0:
                raise CommandError(f"Docker didn't delete them: {text.splitlines()[-1] if text else code}")
        log.info("agent %s: volumes deleted: %s", name, ", ".join(vols) or "none")
        return vols

    async def _forget(self, name: str, volumes: bool) -> dict[str, Any]:
        """agent.forget: the agent was deleted. It stops as agent.stop stops it, then what this runner keeps of it
        goes: its containers (itself, its network proxy) and network, found by their valet.agent label (whatever its
        level is now; not by name: agent foo's proxy, valet-agent-foo-egress, is also agent foo-egress's container's
        name), its volumes when asked, and what the runner made in its data dir (_remove_data). A host agent's
        computer.workdir is never touched, even inside that dir."""
        if not NAME_RE.match(name):
            raise CommandError("not an agent name")
        async with self._lock(name):
            await self._stop_now(name)
            if (proc := self._agents.pop(name, None)) is not None:
                await proc.close_outbox()       # its stop went out first
            removed: list[str] = []
            if shutil.which(self.cfg.docker) is not None:
                label = f"label=valet.agent={name}"
                for ls, rm in ((("ps", "-aq"), ("rm", "-f")), (("network", "ls", "-q"), ("network", "rm"))):
                    code, out = await _docker(self.cfg.docker, *ls, "--filter", label)
                    if code == 0 and (ids := out.split()):
                        await _docker(self.cfg.docker, *rm, *ids)     # gone already, or going: nothing to say
                if volumes:
                    removed = await self._delete_volumes(name)
            try:
                kept = await asyncio.to_thread(_remove_data, self.cfg.data / "agents" / name)
            except OSError as e:
                raise CommandError(f"can't remove its files on the runner: {e}") from None
        log.info("agent %s: forgotten (volumes deleted: %s; kept: %s)", name, ", ".join(removed) or "none",
                 kept or "nothing")
        return {"volumes": removed, "kept": kept}

    def agents(self) -> list[AgentState]:
        return [p.agent_state() for p in self._agents.values()]

    def active_turns(self) -> list[ActiveTurn]:
        return [t for p in self._agents.values() for t in p.active_turns()]

    def pending_permissions(self) -> list[str]:
        return [r for p in self._agents.values() for r in p.pending_permissions()]

    def pending_requests(self) -> list[PermissionRequest]:
        return [r for p in self._agents.values() for r in p.pending_requests()]

    async def shutdown(self) -> None:
        """Stop every agent (permission requests close with reason "exited")."""
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*(self._stop(name) for name in list(self._agents)), return_exceptions=True)
        await asyncio.gather(*(p.close_outbox() for p in self._agents.values()), return_exceptions=True)

    # ----------

    def _definition(self, msg: AgentStart) -> AgentDefinition:
        try:
            definition = AgentDefinition.model_validate(msg.definition)
        except ValidationError as e:
            raise CommandError(f"invalid definition: {e.errors()[0]['msg']}") from None
        if definition.name != msg.agent:
            raise CommandError(f"definition is for {definition.name!r}, not {msg.agent!r}")
        return definition

    def _get(self, name: str) -> "_agent.AgentProcess":
        proc = self._agents.get(name)
        if proc is None:
            raise CommandError("not running")
        return proc

    def _lock(self, name: str) -> asyncio.Lock:
        return self._locks.setdefault(name, asyncio.Lock())

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _start(self, definition: AgentDefinition, files: dict[str, str], secrets: dict[str, str] | None = None,
                     gateway: GatewayAccess | None = None) -> None:
        """Start, or restart with the given definition: a running process is stopped first."""
        async with self._lock(definition.name):
            old = self._agents.get(definition.name)
            if old is not None and old.state in ("starting", "running"):
                await asyncio.gather(*self.close_terminals("the agent restarted", definition.name))
                await old.stop()
            proc = _agent.AgentProcess(definition, files, self.cfg, self._send, secrets=secrets, gateway=gateway)
            self._agents[definition.name] = proc
            try:
                await proc.start()
            except Exception as e:
                log.exception("agent %s: start failed", definition.name)
                await proc.stop()
                proc.state, proc.error = "failed", f"start failed: {e}"
                await self._send(AgentStatus(**proc.agent_state().model_dump()).dump())

    async def _stop(self, name: str) -> None:
        async with self._lock(name):
            await self._stop_now(name)

    async def _stop_now(self, name: str) -> None:
        """_stop's work; the caller holds the agent's lock."""
        await asyncio.gather(*self.close_terminals("the agent stopped", name))
        proc = self._agents.get(name)
        if proc is None:
            # Never ran here (say, the runner restarted): still confirm, so the control plane reconciles.
            await self._send(AgentStatus(agent=name, state="stopped").dump())
            return
        await proc.stop()


def _remove_data(folder: Path) -> str | None:
    """Remove what the runner made in a deleted agent's data dir: config/ and egress/, and its own working folder
    (work/: a host agent's without a computer.workdir) when that's empty; then the dir, if nothing's left. Anything
    else stays, a computer.workdir pointed in there included -> the folder kept: work/ if only that stays, else the
    dir; None if it all went. Never follows a link."""
    if folder.is_symlink() or not folder.is_dir():
        folder.unlink(missing_ok=True)
        return None
    for made in ("config", "egress"):
        p = folder / made
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        elif p.is_symlink() or p.exists():
            p.unlink()
    work = folder / "work"
    if work.is_symlink():
        work.unlink()
    elif work.is_dir() and not any(work.iterdir()):
        work.rmdir()
    left = [p.name for p in folder.iterdir()]
    if not left:
        folder.rmdir()
        return None
    return str(work) if left == ["work"] else str(folder)
