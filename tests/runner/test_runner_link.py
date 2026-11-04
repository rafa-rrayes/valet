"""Runner <-> control plane link, against a small stand-in control plane on 127.0.0.1."""
import asyncio
import json
import uuid

import pytest
from websockets.asyncio.server import serve

from runner_helpers import Sent, host_agent, runner_cfg
from valet.runner.runner import Runner

TOKEN = "vrun_test"


class FakeControl:
    """Accepts the runner's socket, records every frame it sends, and lets a test issue commands."""

    def __init__(self) -> None:
        self.got = Sent()
        self.hellos: list[dict] = []
        self.ws = None
        self.auth: list[str | None] = []

    async def handler(self, ws) -> None:
        self.auth.append(ws.request.headers.get("Authorization"))
        if self.auth[-1] != f"Bearer {TOKEN}":
            await ws.close(4401, "unknown runner token")
            return
        self.hellos.append(json.loads(await ws.recv()))
        await ws.send(json.dumps({"type": "welcome", "runner": "r1", "version": "0.2.0"}))
        self.ws = ws
        async for raw in ws:
            self.got.append(json.loads(raw))

    async def call(self, type_: str, **fields) -> dict:
        cid = uuid.uuid4().hex
        await self.ws.send(json.dumps({"type": type_, "id": cid, **fields}))
        return await self.got.wait_for("reply", id=cid)


@pytest.fixture
async def control():
    c = FakeControl()
    async with serve(c.handler, "127.0.0.1", 0) as server:
        c.url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/ws/runner"
        yield c


def make_runner(control: FakeControl, tmp_path, token: str = TOKEN) -> Runner:
    cfg = runner_cfg(tmp_path, control_url=control.url, token=token, reconnect_max=1.0)
    return Runner(cfg)


async def started(runner: Runner) -> asyncio.Task:
    task = asyncio.create_task(runner.run())
    async with asyncio.timeout(10):
        await runner.connected.wait()
    return task


async def start_agent(control: FakeControl) -> str:
    r = await control.call("agent.start", agent="scout", definition=host_agent().model_dump(mode="json"), files={})
    assert r == {"type": "reply", "id": r["id"], "ok": True, "result": {}, "error": None}
    await control.got.wait_for("agent.status", state="running")
    return (await control.call("session.new", agent="scout"))["result"]["session_id"]


async def test_hello_commands_and_stream(control, tmp_path):
    runner = make_runner(control, tmp_path)
    task = await started(runner)
    try:
        assert control.auth == [f"Bearer {TOKEN}"]
        assert control.hellos == [{"type": "hello", "protocol": 1, "version": "0.2.0", "boot": runner._boot,
                                   "seq": 0, "agents": [], "turns": [], "pending_permissions": [],
                                   "harnesses": ["claude"], "features": ["terminal", "files", "volumes", "forget"]}]
        sid = await start_agent(control)
        r = await control.call("session.prompt", agent="scout", session_id=sid, turn_id="T1",
                               prompt=[{"type": "text", "text": "hello"}])
        assert r["ok"] is True
        end = await control.got.wait_for("turn.end", turn_id="T1")
        updates = control.got.of("session.update", session_id=sid)
        assert control.got.index(end) > control.got.index(updates[-1])
        assert control.got.texts(sid) == ["echo: ", "hello"]

        busy = await control.call("session.new", agent="nobody")
        assert (busy["ok"], busy["error"]) == (False, "not running")
        await control.ws.send(json.dumps({"type": "bogus", "id": "x1"}))
        bad = await control.got.wait_for("reply", id="x1")
        assert (bad["ok"], bad["error"]) == (False, "bad command")
    finally:
        await runner.close()
        await asyncio.wait_for(task, 10)
    assert control.got.of("agent.status")[-1]["state"] == "stopped"


async def test_reconnect_hello_carries_state(control, tmp_path):
    runner = make_runner(control, tmp_path)
    task = await started(runner)
    try:
        sid = await start_agent(control)
        await control.call("session.prompt", agent="scout", session_id=sid, turn_id="T1",
                           prompt=[{"type": "text", "text": "permission"}])
        req = await control.got.wait_for("permission.request")
        runner.connected.clear()
        await control.ws.close(1012, "restarting")
        async with asyncio.timeout(10):
            await runner.connected.wait()
        hello = control.hellos[-1]
        assert len(control.hellos) == 2
        assert [(a["agent"], a["state"]) for a in hello["agents"]] == [("scout", "running")]
        assert hello["turns"] == [{"agent": "scout", "session_id": sid, "turn_id": "T1"}]
        [pending] = hello["pending_permissions"]
        assert {**pending, "seq": req["seq"]} == req       # the whole request: control can rebuild a lost item
        r = await control.call("permission.answer", agent="scout", request_id=req["request_id"],
                               outcome={"outcome": "selected", "optionId": "allow"})
        assert r["ok"] is True
        end = await control.got.wait_for("turn.end", turn_id="T1")
        assert end["stop_reason"] == "end_turn"
    finally:
        await runner.close()
        await asyncio.wait_for(task, 10)


async def test_bad_token_is_fatal(control, tmp_path):
    runner = make_runner(control, tmp_path, token="vrun_wrong")
    with pytest.raises(RuntimeError, match="4401"):
        await asyncio.wait_for(runner.run(), 10)


async def test_unreachable_control_retries_until_closed(tmp_path):
    cfg = runner_cfg(tmp_path, control_url="ws://127.0.0.1:9/ws/runner", reconnect_max=0.5)
    runner = Runner(cfg)
    task = asyncio.create_task(runner.run())
    await asyncio.sleep(0.3)
    assert not task.done()
    await runner.close()
    await asyncio.wait_for(task, 5)
