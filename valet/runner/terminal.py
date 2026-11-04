"""The owner's terminals in an agent's computer (terminal.open and friends, docs/PROTOCOL.md): one shell on a PTY each.

A container agent's shell is `docker exec -it` into its own container (as its user, in /work, with its env); the
docker CLI runs here on a PTY of the runner's, which it puts in raw mode and forwards both ways, resizes included. A
host agent's is the runner user's login shell in its working folder. Either way the runner holds the PTY's master:
it reads what the shell writes and sends it up as terminal.output (never logged, never kept), writes the owner's
keystrokes, and sets the window size.

Flow control: once protocol.TERMINAL_WINDOW bytes are out without the control plane's terminal.ack, the runner stops
reading the PTY, so the shell blocks on a full PTY instead of the link carrying a flood (`cat` of a big file).

Closing: the shell's process group gets SIGHUP (the docker CLI, for a container), then SIGKILL if it lingers. The
shell inside a container isn't the CLI's child, and survives it, so `kill` (computer.cjs) first HUPs, then kills,
every process in the container whose env carries the terminal's VALET_TERMINAL."""
import asyncio
import contextlib
import fcntl
import logging
import os
import pty
import pwd
import signal
import struct
import termios
from collections.abc import Awaitable, Callable
from typing import Any

import acp

from valet.shared.protocol import TERMINAL_WINDOW, TerminalClosed, TerminalOutput

from .files import b64

log = logging.getLogger(__name__)

READ_CHUNK = 64 * 1024
WRITE_BACKLOG = 1024 * 1024     # keystrokes and pastes waiting for a shell that doesn't read; more is dropped
MAX_TERMINALS = 8               # per agent
DRAIN = 0.5                     # seconds the reader gets to drain once the shell has exited
KILL_AFTER = 2.0                # seconds between SIGHUP and SIGKILL
SHELL = "if command -v bash >/dev/null 2>&1; then exec bash -l; fi; exec sh -l"   # a container's login shell
TERM_ENV = {"TERM": "xterm-256color", "COLORTERM": "truecolor"}

Notify = Callable[[dict[str, Any]], None]   # sends one unnumbered message up, if connected (Runner.notify)


class TerminalError(Exception):
    pass


def _controlling_tty() -> None:
    """In the child, before exec: a session of its own, with the PTY (its stdin) as its controlling terminal, so
    job control works and resizing the PTY sends it SIGWINCH."""
    os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


def container_command(docker: str, container: str, terminal: str, workdir: str) -> list[str]:
    return [docker, "exec", "-i", "-t", "-e", f"TERM={TERM_ENV['TERM']}", "-e", f"COLORTERM={TERM_ENV['COLORTERM']}",
            "-e", f"VALET_TERMINAL={terminal}", "-w", workdir, container, "sh", "-c", SHELL]


def host_command() -> list[str]:
    """The runner user's login shell."""
    try:
        shell = pwd.getpwuid(os.getuid()).pw_shell
    except KeyError:
        shell = ""
    return [shell if shell and os.path.exists(shell) else "/bin/sh", "-l"]


def host_env(terminal: str) -> dict[str, str]:
    """A clean environment for a host shell: the basics (acp.default_environment), the locale, the terminal's type.
    Nothing of the runner's own (its token, the harnesses' keys)."""
    env = {**acp.default_environment(), **TERM_ENV, "VALET_TERMINAL": terminal}
    env.update({k: v for k, v in os.environ.items() if k in ("LANG", "LC_ALL", "LC_CTYPE")})
    return env


class Terminal:
    def __init__(self, terminal: str, agent: str, argv: list[str], env: dict[str, str], cwd: str | None,
                 cols: int, rows: int, notify: Notify,
                 killer: Callable[[], Awaitable[None]] | None = None) -> None:
        self.id = terminal
        self.agent = agent
        self.argv = argv
        self.env = env
        self.cwd = cwd
        self.size = (cols, rows)
        self._notify = notify
        self._killer = killer           # a container's: ends its processes inside the container
        self._master: int | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._loop = asyncio.get_running_loop()
        self._unacked = 0
        self._reading = False
        self._wbuf = bytearray()
        self._writing = False
        self._dropped = False
        self._eof = asyncio.Event()
        self._closed = False
        self._waiter: asyncio.Task | None = None
        self.closing: asyncio.Task | None = None

    @property
    def open(self) -> bool:
        return not self._closed

    async def start(self) -> None:
        master, slave = pty.openpty()
        try:
            self._winsize(master, *self.size)
            self._proc = await asyncio.create_subprocess_exec(
                *self.argv, stdin=slave, stdout=slave, stderr=slave, env=self.env, cwd=self.cwd,
                preexec_fn=_controlling_tty)
        except OSError as e:
            os.close(master)
            raise TerminalError(f"can't start a shell: {e}") from None
        finally:
            os.close(slave)
        os.set_blocking(master, False)
        self._master = master
        self._resume()
        self._waiter = asyncio.create_task(self._wait())
        log.info("terminal %s of %s opened (pid %s)", self.id, self.agent, self._proc.pid)

    # ---------- the PTY ----------

    @staticmethod
    def _winsize(fd: int, cols: int, rows: int) -> None:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def _resume(self) -> None:
        if not self._reading and self._master is not None and not self._eof.is_set():
            self._loop.add_reader(self._master, self._readable)
            self._reading = True

    def _pause(self) -> None:
        if self._reading and self._master is not None:
            self._loop.remove_reader(self._master)
        self._reading = False

    def _readable(self) -> None:
        try:
            data = os.read(self._master, READ_CHUNK)
        except BlockingIOError:
            return
        except OSError:
            data = b""          # EIO: every process holding the PTY's other side is gone
        if not data:
            self._pause()
            self._eof.set()
            return
        self._unacked += len(data)
        self._notify(TerminalOutput(terminal=self.id, data=b64(data)).dump())
        if self._unacked >= TERMINAL_WINDOW:
            self._pause()

    def ack(self, n: int) -> None:
        self._unacked = max(0, self._unacked - n)
        if self._unacked < TERMINAL_WINDOW // 2 and not self._closed:
            self._resume()

    def input(self, data: bytes) -> None:
        if self._closed or self._master is None:
            return
        room = WRITE_BACKLOG - len(self._wbuf)
        if len(data) > room:
            if not self._dropped:
                log.warning("terminal %s: the shell isn't reading; input dropped", self.id)
                self._dropped = True
            data = data[:max(0, room)]
        self._wbuf += data
        self._flush()

    def _flush(self) -> None:
        if self._master is None:
            return
        while self._wbuf:
            try:
                n = os.write(self._master, self._wbuf)
            except BlockingIOError:
                break
            except OSError:
                self._wbuf.clear()
                break
            del self._wbuf[:n]
        if self._wbuf and not self._writing:
            self._loop.add_writer(self._master, self._flush)
            self._writing = True
        elif not self._wbuf and self._writing:
            self._loop.remove_writer(self._master)
            self._writing = False
            self._dropped = False

    def resize(self, cols: int, rows: int) -> None:
        if self._master is None or self._closed:
            return
        self.size = (cols, rows)
        with contextlib.suppress(OSError):
            self._winsize(self._master, cols, rows)

    # ---------- ending ----------

    async def _wait(self) -> None:
        """The shell exiting on its own: what it wrote last goes up, then terminal.closed with its status."""
        code = await self._proc.wait()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._eof.wait(), DRAIN)
        if self._closed:
            return
        while self._reading and self._unacked < TERMINAL_WINDOW:     # what's left in the PTY, within the window
            before = self._unacked
            self._readable()
            if self._unacked == before:
                break
        self._finish(code=code)
        if self._killer is not None:        # what it left running in the container
            with contextlib.suppress(Exception):
                await self._killer()

    def close(self, reason: str) -> asyncio.Task:
        """End it (terminal.close, the agent stopping, the link dropping): terminal.closed with `reason` at once, the
        processes ended in the background. Returns that task."""
        if self.closing is None:
            self.closing = asyncio.ensure_future(self._end(reason))
        return self.closing

    async def _end(self, reason: str) -> None:
        if self._closed:
            return
        self._finish(reason=reason)
        if self._killer is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._killer(), 10)
        proc = self._proc
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGHUP)
            try:
                await asyncio.wait_for(proc.wait(), KILL_AFTER)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, signal.SIGKILL)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(proc.wait(), KILL_AFTER)
        if self._waiter is not None and self._waiter is not asyncio.current_task():
            self._waiter.cancel()

    def _finish(self, code: int | None = None, reason: str | None = None) -> None:
        """Once: stop reading and writing, close the master, send terminal.closed."""
        if self._closed:
            return
        self._closed = True
        self._pause()
        if self._writing and self._master is not None:
            self._loop.remove_writer(self._master)
            self._writing = False
        if self._master is not None:
            with contextlib.suppress(OSError):
                os.close(self._master)
            self._master = None
        self._notify(TerminalClosed(terminal=self.id, code=code, reason=reason).dump())
        log.info("terminal %s of %s closed (%s)", self.id, self.agent,
                 reason or (f"exit {code}" if code is not None else "ended"))
