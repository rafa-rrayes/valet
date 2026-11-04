"""End-to-end stack: the real control plane (uvicorn on a free 127.0.0.1 port, in a thread with its own loop, as in
production), a real Runner task in the test's loop, and the fake ACP agent standing in for the Claude adapter at
computer level `host` (RunnerConfig.harness_commands overrides the `claude` command). Tests drive it the way the
apps do: REST with an owner bearer token and the /ws/client live feed."""
import asyncio
import contextlib
import json
import socket
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from websockets.asyncio.client import connect

from tests.fake_agent import COMMAND as FAKE_AGENT
from valet.control.__main__ import add_runner, set_password
from valet.control.app import create_app
from valet.runner.runner import Runner
from valet.shared.config import ControlConfig, RunnerConfig

PASSWORD = "correct horse battery staple"
RUNNER = "testrunner"
TIMEOUT = 15.0

AGENT_YAML = """\
name: {name}
harness: claude
computer:
  level: host
permissions: ask
directives: [persona.md]
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def eventually(fn, timeout: float = TIMEOUT, what: str = "condition"):
    """Poll fn (sync or async) until it returns something truthy; return it."""
    deadline = time.monotonic() + timeout
    while True:
        value = fn()
        if asyncio.iscoroutine(value):
            value = await value
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.02)


class ControlServer:
    """uvicorn in a background thread; stop() and start() again on the same port to restart the control plane.
    `clock`: the always-on parts' time (a ManualClock, for schedules), kept across restarts."""

    def __init__(self, cfg: ControlConfig, clock=None) -> None:
        self.cfg = cfg
        self.clock = clock
        self.server: uvicorn.Server | None = None
        self.thread: threading.Thread | None = None

    async def start(self) -> None:
        config = uvicorn.Config(create_app(self.cfg, clock=self.clock), host=self.cfg.host, port=self.cfg.port,
                                log_level="warning", lifespan="on", timeout_graceful_shutdown=2)
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        await eventually(lambda: self.server.started or not self.thread.is_alive(), what="control start")
        assert self.server.started, "control plane failed to start"

    async def stop(self) -> None:
        if self.server and self.thread:
            self.server.should_exit = True
            await asyncio.to_thread(self.thread.join, 10)
            self.server = self.thread = None


class Feed:
    """One /ws/client connection; a reader task collects every frame."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.frames: list[dict[str, Any]] = []
        self.ws = None
        self.task: asyncio.Task | None = None

    async def open(self) -> "Feed":
        self.ws = await connect(self.url)
        self.task = asyncio.create_task(self._read())
        await self.wait(lambda f: f["type"] == "hello", what="client hello")
        return self

    async def _read(self) -> None:
        with contextlib.suppress(Exception):
            async for raw in self.ws:
                self.frames.append(json.loads(raw))

    @property
    def hello(self) -> dict[str, Any]:
        return self.frames[0]

    def find(self, pred, start: int = 0) -> dict[str, Any] | None:
        return next((f for f in self.frames[start:] if pred(f)), None)

    async def wait(self, pred, start: int = 0, what: str = "frame") -> dict[str, Any]:
        return await eventually(lambda: self.find(pred, start), what=what)

    def events(self, session_id: str | None = None, turn_id: str | None = None) -> list[dict[str, Any]]:
        out = [f["event"] for f in self.frames if f["type"] == "event"]
        if session_id:
            out = [e for e in out if e["session_id"] == session_id]
        if turn_id:
            out = [e for e in out if e["turn_id"] == turn_id]
        return out

    async def close(self) -> None:
        if self.ws:
            await self.ws.close()
        if self.task:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.task, 5)


class Stack:
    def __init__(self, tmp: Path, sessions: str, clock=None, timezone: str = "") -> None:
        port = free_port()
        self.ccfg = ControlConfig(data=tmp / "control", definitions=tmp / "control" / "definitions", port=port,
                                  public_url=f"http://127.0.0.1:{port}", rpc_timeout=10.0, timezone=timezone)
        set_password(self.ccfg, PASSWORD)
        token = add_runner(self.ccfg, RUNNER)
        self.rcfg = RunnerConfig(control_url=f"ws://127.0.0.1:{port}/ws/runner", token=token, data=tmp / "runner",
                                 harness_commands={"claude": FAKE_AGENT + ["--sessions", sessions]},
                                 reconnect_max=1.0)
        self.base = f"http://127.0.0.1:{port}"
        self.control = ControlServer(self.ccfg, clock)
        self.runner: Runner | None = None
        self.runner_task: asyncio.Task | None = None
        self.http: httpx.AsyncClient | None = None
        self.owner_token = ""
        self.feeds: list[Feed] = []

    # ---- lifecycle ----
    async def start(self, runner: bool = True) -> "Stack":
        await self.control.start()
        async with httpx.AsyncClient(base_url=self.base) as anon:
            r = await anon.post("/api/login", json={"password": PASSWORD, "client": "native"})
        assert r.status_code == 200, r.text
        self.owner_token = r.json()["token"]
        assert self.owner_token.startswith("vown_")
        self.http = httpx.AsyncClient(base_url=self.base, timeout=TIMEOUT,
                                      headers={"Authorization": f"Bearer {self.owner_token}"})
        if runner:
            await self.start_runner()
        return self

    async def start_runner(self) -> None:
        self.runner = Runner(self.rcfg)
        self.runner_task = asyncio.create_task(self.runner.run())
        await eventually(self.runner_connected, what="runner connected")

    async def stop_runner(self) -> None:
        if self.runner:
            await self.runner.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.runner_task, 10)
            self.runner = self.runner_task = None

    async def restart_control(self) -> None:
        """Stop the control plane (the runner's socket drops; its agents keep running), start it again on the
        same port and data, and wait for the runner to reconnect."""
        await self.control.stop()
        await self.control.start()
        await eventually(self.runner_connected, what="runner reconnected")

    async def close(self) -> None:
        for f in self.feeds:
            await f.close()
        await self.stop_runner()
        if self.http:
            await self.http.aclose()
        await self.control.stop()

    # ---- api ----
    async def runner_connected(self) -> bool:
        r = await self.http.get("/api/runners") if self.http else None
        return bool(r is not None and r.status_code == 200
                    and any(x["name"] == RUNNER and x["connected"] for x in r.json()))

    async def feed(self) -> Feed:
        f = await Feed(f"ws://127.0.0.1:{self.ccfg.port}/ws/client?token={self.owner_token}").open()
        self.feeds.append(f)
        return f

    async def agent(self, name: str) -> dict[str, Any]:
        r = await self.http.get(f"/api/agents/{name}")
        assert r.status_code == 200, r.text
        return r.json()

    async def create_agent(self, name: str = "echo", persona: str | None = None) -> dict[str, Any]:
        body = {"name": name, "agent_yaml": AGENT_YAML.format(name=name)}
        if persona is not None:
            body["persona"] = persona
        r = await self.http.post("/api/agents", json=body)
        assert r.status_code == 201, r.text
        return r.json()

    async def start_agent(self, name: str = "echo") -> dict[str, Any]:
        r = await self.http.post(f"/api/agents/{name}/start")
        assert r.status_code == 200, r.text
        return await self.wait_state(name, "running")

    async def restart_agent(self, name: str = "echo") -> None:
        """POST start on a running agent and wait for it to go through starting and back to running (polling the
        REST state alone could see the old `running`)."""
        feed = await self.feed()
        r = await self.http.post(f"/api/agents/{name}/start")
        assert r.status_code == 200, r.text
        await self.wait_restarted(feed, 0, name)

    @staticmethod
    async def wait_restarted(feed: "Feed", start: int, name: str = "echo") -> None:
        def state(s):
            return lambda f: f["type"] == "agent" and f["agent"]["name"] == name and f["agent"]["state"] == s
        await feed.wait(state("starting"), start, what=f"{name} starting")
        i = next(i for i in range(start, len(feed.frames)) if state("starting")(feed.frames[i]))
        await feed.wait(state("running"), i, what=f"{name} running")

    async def wait_state(self, name: str, state: str) -> dict[str, Any]:
        async def check():
            a = await self.agent(name)
            return a if a["state"] == state else None
        return await eventually(check, what=f"agent {name} {state}")

    async def ready_agent(self, name: str = "echo", persona: str | None = None) -> None:
        await self.create_agent(name, persona)
        await self.start_agent(name)

    async def new_session(self, name: str = "echo") -> dict[str, Any]:
        r = await self.http.post(f"/api/agents/{name}/sessions")
        assert r.status_code == 201, r.text
        return r.json()

    async def session(self, name: str, sid: str) -> dict[str, Any]:
        r = await self.http.get(f"/api/agents/{name}/sessions/{sid}")
        assert r.status_code == 200, r.text
        return r.json()

    async def prompt(self, name: str, sid: str, text: str) -> str:
        r = await self.http.post(f"/api/agents/{name}/sessions/{sid}/prompt", json={"text": text})
        assert r.status_code == 202, r.text
        return r.json()["turn_id"]

    async def events(self, name: str, sid: str, after: int = 0, limit: int = 2000) -> list[dict[str, Any]]:
        r = await self.http.get(f"/api/agents/{name}/sessions/{sid}/events", params={"after": after, "limit": limit})
        assert r.status_code == 200, r.text
        return r.json()["events"]

    async def turn(self, name: str, sid: str, turn_id: str) -> list[dict[str, Any]]:
        """Wait for the turn's turn_end; return the turn's stored events (a permission_answer may still follow
        the turn_end: the runner can report a closed request after the turn's end)."""
        async def done():
            evs = [e for e in await self.events(name, sid) if e["turn_id"] == turn_id]
            return evs if "turn_end" in kinds(evs) else None
        return await eventually(done, what=f"turn {turn_id} to end")

    async def run_turn(self, name: str, sid: str, text: str) -> list[dict[str, Any]]:
        return await self.turn(name, sid, await self.prompt(name, sid, text))

    async def pending(self) -> list[dict[str, Any]]:
        r = await self.http.get("/api/inbox")
        assert r.status_code == 200, r.text
        return r.json()

    async def wait_pending(self, n: int = 1) -> list[dict[str, Any]]:
        async def check():
            items = await self.pending()
            return items if len(items) >= n else None
        return await eventually(check, what="pending inbox item")


def texts(events: list[dict[str, Any]], kind: str = "agent_message") -> str:
    """Concatenated text of the events of one kind (chunks of one message, per API.md)."""
    return "".join(e["data"]["content"].get("text", "") for e in events if e["kind"] == kind)


def kinds(events: list[dict[str, Any]]) -> list[str]:
    return [e["kind"] for e in events]


def end_of(events: list[dict[str, Any]]) -> dict[str, Any]:
    """The data of the (single) turn_end among a turn's events."""
    [end] = [e["data"] for e in events if e["kind"] == "turn_end"]
    return end

