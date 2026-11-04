"""The owner's terminals (runner/terminal.py) on a real PTY, and through the Supervisor for a host agent: output,
input, resize, the ack window, the shell exiting, closing (its process group goes), the agent stopping."""
import asyncio
import base64
import os
import signal
import time
import uuid

import pytest

from runner_helpers import host_agent, runner_cfg
from valet.runner.supervisor import CommandError, Supervisor
from valet.runner.terminal import Terminal, host_command, host_env
from valet.shared.protocol import TERMINAL_WINDOW, parse_control_message


class Frames(list):
    """A `notify` that keeps what it's given."""

    def __call__(self, msg: dict) -> None:
        self.append(msg)

    def output(self, terminal: str | None = None) -> bytes:
        return b"".join(base64.b64decode(m["data"]) for m in self
                        if m["type"] == "terminal.output" and terminal in (None, m["terminal"]))

    def closed(self, terminal: str | None = None) -> list[dict]:
        return [m for m in self if m["type"] == "terminal.closed" and terminal in (None, m["terminal"])]

    async def wait(self, pred, timeout: float = 10.0, what: str = "frames"):
        deadline = time.monotonic() + timeout
        while not pred():
            if time.monotonic() > deadline:
                raise AssertionError(f"timed out waiting for {what}: {self.output()[-500:]!r} {self.closed()}")
            await asyncio.sleep(0.02)


def tid() -> str:
    return uuid.uuid4().hex


async def shell(tmp_path, frames, cols=80, rows=24) -> Terminal:
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "TERM": "xterm-256color", "PS1": "$ "}
    t = Terminal(tid(), "scout", ["/bin/sh"], env, str(tmp_path), cols, rows, frames)
    await t.start()
    return t


async def test_output_input_and_resize(tmp_path):
    frames = Frames()
    t = await shell(tmp_path, frames, cols=100, rows=30)
    try:
        t.input(b"echo hi-$((1+2)); pwd; stty size\n")
        await frames.wait(lambda: b"30 100" in frames.output(), what="stty size")
        out = frames.output()
        assert b"hi-3" in out and str(tmp_path).encode() in out
        t.resize(120, 40)
        t.input(b"stty size\n")
        await frames.wait(lambda: b"40 120" in frames.output(), what="the new size")
    finally:
        await t.close("closed")
    assert frames.closed() == [{"type": "terminal.closed", "terminal": t.id, "code": None, "reason": "closed"}]


async def test_the_shell_exiting_reports_its_status(tmp_path):
    frames = Frames()
    t = await shell(tmp_path, frames)
    t.input(b"echo bye; exit 3\n")
    await frames.wait(lambda: frames.closed(), what="terminal.closed")
    assert b"bye" in frames.output()
    assert frames.closed()[0]["code"] == 3 and frames.closed()[0]["reason"] is None
    assert frames[-1]["type"] == "terminal.closed"     # after its last output
    assert not t.open


async def test_closing_ends_what_it_runs(tmp_path):
    frames = Frames()
    t = await shell(tmp_path, frames)
    t.input(b"sleep 300 & jobs -p; sleep 301\n")
    await frames.wait(lambda: any(line.strip().isdigit() for line in frames.output().splitlines()), what="the pid")
    pid = next(int(line) for line in frames.output().splitlines() if line.strip().isdigit())
    await t.close("closed")
    await asyncio.sleep(0.2)
    # The shell's process group (it and its foreground command) is gone.
    with pytest.raises(ProcessLookupError):
        os.killpg(t._proc.pid, 0)
    try:
        os.kill(pid, signal.SIGKILL)        # tidy up the background one if it outlived the shell
    except ProcessLookupError:
        pass
    assert len(frames.closed()) == 1


async def test_output_waits_for_acks(tmp_path):
    frames = Frames()
    t = await shell(tmp_path, frames)
    try:
        t.input(b"head -c 3000000 /dev/zero | tr '\\000' x; echo; echo DO''NE\n")
        await frames.wait(lambda: len(frames.output()) >= TERMINAL_WINDOW, what="a window of output")
        await asyncio.sleep(0.3)
        held = len(frames.output())
        assert TERMINAL_WINDOW <= held < TERMINAL_WINDOW + 64 * 1024 + 4096     # it stopped reading
        await asyncio.sleep(0.3)
        assert len(frames.output()) == held
        while b"DONE" not in frames.output():
            t.ack(len(frames.output()))         # the control plane passed everything on
            await asyncio.sleep(0.01)
        assert frames.output().count(b"x") >= 3000000
    finally:
        await t.close("closed")


async def test_host_shell_env_is_clean(monkeypatch):
    monkeypatch.setenv("VALET_RUNNER_TOKEN", "vrun_secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    env = host_env("abc")
    assert env["VALET_TERMINAL"] == "abc" and env["TERM"] == "xterm-256color"
    assert "VALET_RUNNER_TOKEN" not in env and "ANTHROPIC_API_KEY" not in env
    assert os.path.exists(host_command()[0]) and host_command()[1] == "-l"


# ---------- through the supervisor ----------

def cmd(type_: str, **fields) -> object:
    return parse_control_message({"type": type_, "id": "c-" + uuid.uuid4().hex[:6], **fields})


def one_way(type_: str, **fields) -> object:
    return parse_control_message({"type": type_, **fields})


@pytest.fixture
async def sup(tmp_path, sent):
    frames = Frames()
    s = Supervisor(runner_cfg(tmp_path), sent, frames)
    s.frames = frames
    yield s
    await s.shutdown()


async def start(sup, sent, name="scout", **definition):
    d = host_agent(name, **definition).model_dump(mode="json")
    await sup.handle(cmd("agent.start", agent=name, definition=d, files={"persona.md": "hi"}))
    await sent.wait_for("agent.status", agent=name, state="running")


async def test_a_host_agents_terminal_opens_in_its_working_folder(sup, sent, tmp_path):
    await start(sup, sent)
    t = tid()
    assert await sup.handle(cmd("terminal.open", agent="scout", terminal=t, cols=90, rows=20)) == {}
    sup.one_way(one_way("terminal.input", terminal=t, data=base64.b64encode(b"pwd; stty size\n").decode()))
    work = os.path.realpath(tmp_path / "runner" / "agents" / "scout" / "work")
    await sup.frames.wait(lambda: b"20 90" in sup.frames.output(t), what="pwd and size")
    assert work.encode() in sup.frames.output(t)
    sup.one_way(one_way("terminal.resize", terminal=t, cols=70, rows=10))
    sup.one_way(one_way("terminal.input", terminal=t, data=base64.b64encode(b"stty size\n").decode()))
    await sup.frames.wait(lambda: b"10 70" in sup.frames.output(t), what="resized")
    assert await sup.handle(cmd("terminal.close", terminal=t)) == {}
    await sup.frames.wait(lambda: sup.frames.closed(t), what="closed")
    assert sup.frames.closed(t)[0]["reason"] == "closed"
    assert await sup.handle(cmd("terminal.close", terminal=t)) == {}      # gone already: fine


async def test_stopping_the_agent_closes_its_terminals(sup, sent):
    await start(sup, sent)
    t = tid()
    await sup.handle(cmd("terminal.open", agent="scout", terminal=t))
    await sup.handle(cmd("agent.stop", agent="scout"))
    await sup.frames.wait(lambda: sup.frames.closed(t), what="closed")
    assert sup.frames.closed(t)[0]["reason"] == "the agent stopped"
    with pytest.raises(CommandError, match="not running"):
        await sup.handle(cmd("terminal.open", agent="scout", terminal=tid()))


async def test_no_terminal_without_a_computer_or_beyond_the_limit(sup, sent, monkeypatch):
    with pytest.raises(CommandError, match="not running"):
        await sup.handle(cmd("terminal.open", agent="ghost", terminal=tid()))
    await start(sup, sent)
    sup._agents["scout"].definition = sup._agents["scout"].definition.model_copy(
        update={"computer": sup._agents["scout"].definition.computer.model_copy(update={"level": "none"})})
    with pytest.raises(CommandError, match="no computer"):
        await sup.handle(cmd("terminal.open", agent="scout", terminal=tid()))
    sup._agents["scout"].definition = host_agent("scout")
    monkeypatch.setattr("valet.runner.terminal.MAX_TERMINALS", 2)
    for _ in range(2):
        await sup.handle(cmd("terminal.open", agent="scout", terminal=tid()))
    with pytest.raises(CommandError, match="2 terminals open"):
        await sup.handle(cmd("terminal.open", agent="scout", terminal=tid()))
    await asyncio.gather(*sup.close_terminals("test"))
    assert len(sup.frames.closed()) == 2


async def test_input_for_an_unknown_terminal_is_dropped(sup):
    sup.one_way(one_way("terminal.input", terminal="nope", data="aGk="))
    sup.one_way(one_way("terminal.ack", terminal="nope", bytes=10))
    assert sup.frames == []
