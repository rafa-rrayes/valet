"""A container agent's network policy at run time (launch.Egress): its own internal Docker network and the egress
proxy on it (egress.mjs), the only other container there, which also sits on Docker's default network and lets
through what the policy allows.

The network is `--internal` (no route out) with `inhibit_ipv4`: Docker gives the bridge no address on the host, so
the agent can't reach the Docker host's own services through it either (on Linux that's the runner's machine; on
Docker Desktop its VM). One proxy per agent, so who's asking is never in doubt: the agent is alone on the network.

The proxy is a `docker run -i` child of the runner, like the harness: it prints {"event":"ready"} once it listens
(start waits for it), one JSON line per decision after that (blocked requests are logged), and exits when its stdin
closes, so it doesn't outlive the runner."""
import asyncio
import collections
import contextlib
import json
import logging
import os
import shutil
from collections.abc import Callable
from pathlib import Path

import acp

from .launch import OUT_NETWORK, Egress

log = logging.getLogger(__name__)

SCRIPT = Path(__file__).with_name("egress.mjs")
READY_TIMEOUT = 60.0        # a cold container start included
DOCKER_TIMEOUT = 30.0
STDERR_LINES = 20


class EgressError(Exception):
    """The agent's network couldn't be set up; the agent fails to start with this message."""


def docker_env() -> dict[str, str]:
    """The runner's DOCKER_* variables (DOCKER_HOST, DOCKER_CONTEXT...), so the CLI reaches the same daemon."""
    return {k: v for k, v in os.environ.items() if k.startswith("DOCKER_")}


async def docker(cli: str, *args: str) -> tuple[int, str]:
    """Run the docker CLI -> (exit code, its output, stderr included). (-1, why) if it can't run or times out."""
    try:
        proc = await asyncio.create_subprocess_exec(
            cli, *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, env={**acp.default_environment(), **docker_env()})
    except OSError as e:
        return -1, str(e)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), DOCKER_TIMEOUT)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, f"docker {args[0]} timed out"
    return proc.returncode, out.decode(errors="replace").strip()


class Proxy:
    def __init__(self, agent: str, plan: Egress, cli: str, on_exit: Callable[[int | None], None]) -> None:
        """on_exit(code): the proxy stopped on its own while the agent runs (called once, not after stop())."""
        self.agent = agent
        self.plan = plan
        self.cli = cli
        self._on_exit = on_exit
        self._proc: asyncio.subprocess.Process | None = None
        self._tasks: set[asyncio.Task] = set()
        self._stderr: collections.deque[str] = collections.deque(maxlen=STDERR_LINES)
        self._stopping = False

    async def start(self) -> None:
        """Clear what an earlier run left, create the network, write the proxy's files, start it and wait until it
        listens, then connect it to OUT_NETWORK. Raises EgressError saying what failed (everything is undone)."""
        try:
            await self._start()
        except EgressError:
            await self.stop()
            raise

    async def _start(self) -> None:
        p = self.plan
        await self._clear()
        code, out = await docker(self.cli, "network", "create", "--internal",
                                 "-o", "com.docker.network.bridge.inhibit_ipv4=true",
                                 "--label", f"valet.agent={self.agent}", p.network)
        if code:
            raise EgressError(f"cannot create its network {p.network}: {out}")
        try:
            if p.dir.exists():
                shutil.rmtree(p.dir)
            p.dir.mkdir(parents=True)
            (p.dir / "egress.mjs").write_text(SCRIPT.read_text())
            (p.dir / "rules.json").write_text(json.dumps(p.rules, indent=2) + "\n")
            for f in (p.dir, *p.dir.iterdir()):     # read by the proxy's user, whatever its uid
                f.chmod(0o755 if f.is_dir() else 0o644)
        except OSError as e:
            raise EgressError(f"cannot write its network proxy's files: {e}") from None
        try:
            self._proc = await asyncio.create_subprocess_exec(
                self.cli, *p.args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env={**acp.default_environment(), **docker_env()})
        except OSError as e:
            raise EgressError(f"cannot run {self.cli}: {e}") from None
        self._spawn(self._drain_stderr())
        try:
            await asyncio.wait_for(self._ready(), READY_TIMEOUT)
        except TimeoutError:
            raise EgressError(self._why("its network proxy didn't start in time")) from None
        code, out = await docker(self.cli, "network", "connect", OUT_NETWORK, p.container)
        if code:
            raise EgressError(f"cannot connect its network proxy to {OUT_NETWORK}: {out}")
        self._spawn(self._drain_stdout())
        self._spawn(self._watch())
        log.info("agent %s: network proxy up, allowing %s", self.agent,
                 ", ".join(f"{r.get('host') or '*.' + r['domain']}:{'/'.join(map(str, r['ports']))}"
                           for r in p.rules["allow"]) or "nothing")

    async def _ready(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        while line := await self._proc.stdout.readline():
            if _event(line).get("event") == "ready":
                return
        await self._proc.wait()
        raise EgressError(self._why(f"its network proxy exited with code {self._proc.returncode}"))

    def _why(self, reason: str) -> str:
        tail = "\n".join(self._stderr)
        return f"{reason}\n{tail}" if tail else reason

    async def stop(self) -> None:
        """Stop the proxy and remove the network. The agent's container must be gone first (the network can't be
        removed while it's attached)."""
        self._stopping = True
        proc, self._proc = self._proc, None
        if proc is not None and proc.stdin is not None:
            with contextlib.suppress(OSError):
                proc.stdin.close()          # the proxy exits on EOF
        await self._clear()
        if proc is not None:
            try:
                await asyncio.wait_for(proc.wait(), DOCKER_TIMEOUT)
            except TimeoutError:
                proc.kill()
                await proc.wait()
        for t in list(self._tasks):
            if t is not asyncio.current_task():
                t.cancel()

    async def _clear(self) -> None:
        """Remove the proxy's container and the network, if they're there. Docker removes a stopped `--rm`
        container on its own a moment later, and the network can't go until then: a few tries."""
        await docker(self.cli, "rm", "-f", self.plan.container)
        for _ in range(10):
            code, out = await docker(self.cli, "network", "rm", self.plan.network)
            if code == 0 or "not found" in out or code < 0:
                return
            await asyncio.sleep(0.5)
        log.warning("agent %s: cannot remove network %s: %s", self.agent, self.plan.network, out)

    # ----------

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _watch(self) -> None:
        proc = self._proc
        if proc is None:
            return
        code = await proc.wait()
        if not self._stopping:
            log.warning("agent %s: network proxy exited with code %s", self.agent, code)
            self._on_exit(code)

    async def _drain_stdout(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        with contextlib.suppress(ValueError, OSError):
            while line := await proc.stdout.readline():
                e = _event(line)
                where = f"{e.get('host')}:{e.get('port')}"
                if e.get("event") == "deny":
                    log.info("agent %s: network blocked %s %s", self.agent, e.get("method"), where)
                elif e.get("event") == "error":
                    log.info("agent %s: network can't reach %s: %s", self.agent, where, e.get("error"))
                else:
                    log.debug("agent %s: network %s", self.agent, line.decode(errors="replace").rstrip())

    async def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        with contextlib.suppress(ValueError, OSError):
            while line := await proc.stderr.readline():
                text = line.decode(errors="replace").rstrip()
                self._stderr.append(text)
                log.info("agent %s network proxy: %s", self.agent, text)


def _event(line: bytes) -> dict:
    try:
        e = json.loads(line)
    except ValueError:
        return {}
    return e if isinstance(e, dict) else {}
