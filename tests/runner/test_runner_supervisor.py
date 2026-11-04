"""Supervisor: protocol commands in, AgentProcess calls and replies out."""
import asyncio

import pytest

from runner_helpers import host_agent, runner_cfg
from valet.runner.supervisor import CommandError, Supervisor
from valet.shared.protocol import parse_control_message


def cmd(type_: str, **fields) -> object:
    return parse_control_message({"type": type_, "id": "c-" + type_, **fields})


def start_cmd(name: str = "scout", **definition) -> object:
    d = host_agent(name, **definition).model_dump(mode="json")
    return cmd("agent.start", agent=name, definition=d, files={"persona.md": "hi"})


@pytest.fixture
async def sup(tmp_path, sent):
    s = Supervisor(runner_cfg(tmp_path), sent)
    yield s
    await s.shutdown()


async def test_start_replies_at_once_then_reports(sup, sent):
    assert await sup.handle(start_cmd()) == {}
    await sent.wait_for("agent.status", state="running")
    assert [a.state for a in sup.agents()] == ["running"]
    assert sup.agents()[0].capabilities["loadSession"] is True


async def test_full_turn_through_commands(sup, sent):
    await sup.handle(start_cmd())
    await sent.wait_for("agent.status", state="running")
    s = await sup.handle(cmd("session.new", agent="scout"))
    sid = s["session_id"]
    prompt = [{"type": "text", "text": "permission"}]
    assert await sup.handle(cmd("session.prompt", agent="scout", session_id=sid, turn_id="T1", prompt=prompt)) == {}
    req = await sent.wait_for("permission.request")
    assert [t.model_dump() for t in sup.active_turns()] == [{"agent": "scout", "session_id": sid, "turn_id": "T1"}]
    assert sup.pending_permissions() == [req["request_id"]]
    outcome = {"outcome": "selected", "optionId": "allow"}
    assert await sup.handle(cmd("permission.answer", agent="scout", request_id=req["request_id"], outcome=outcome)) == {}
    end = await sent.wait_for("turn.end", turn_id="T1")
    assert end["stop_reason"] == "end_turn"
    assert sup.active_turns() == [] and sup.pending_permissions() == []


async def test_errors(sup, sent):
    with pytest.raises(CommandError, match="not running"):
        await sup.handle(cmd("session.new", agent="nobody"))
    with pytest.raises(CommandError, match="not running"):
        await sup.handle(cmd("session.prompt", agent="nobody", session_id="s", turn_id="t", prompt=[]))
    with pytest.raises(CommandError, match="unknown request"):
        await sup.handle(cmd("permission.answer", agent="nobody", request_id="r", outcome={"outcome": "cancelled"}))
    with pytest.raises(CommandError, match="invalid definition"):
        await sup.handle(cmd("agent.start", agent="scout", definition={"name": "scout", "bogus": 1}))
    with pytest.raises(CommandError, match="not 'other'"):
        await sup.handle(cmd("agent.start", agent="other", definition={"name": "scout"}))


async def test_stop_unknown_agent_still_confirms(sup, sent):
    assert await sup.handle(cmd("agent.stop", agent="ghost")) == {}
    assert sent.of("agent.status")[-1] == {"type": "agent.status", "agent": "ghost", "state": "stopped",
                                           "error": None, "capabilities": None, "agent_info": None}


async def test_restart_and_stop(sup, sent):
    await sup.handle(start_cmd())
    await sent.wait_for("agent.status", state="running")
    await sup.handle(start_cmd(model="other"))
    async with asyncio.timeout(10):
        while [m["state"] for m in sent.of("agent.status")] != ["starting", "running", "stopped", "starting",
                                                                  "running"]:
            await asyncio.sleep(0.01)
    assert await sup.handle(cmd("agent.stop", agent="scout")) == {}
    assert sent.of("agent.status")[-1]["state"] == "stopped"
    assert [a.state for a in sup.agents()] == ["stopped"]


async def test_failed_start_reports_failed(sup, sent):
    await sup.handle(start_cmd(harness="codex"))
    failed = await sent.wait_for("agent.status", state="failed")
    assert failed["error"] == "harness 'codex' isn't available yet"


async def test_shutdown_stops_everything(tmp_path, sent):
    sup = Supervisor(runner_cfg(tmp_path), sent)
    await sup.handle(start_cmd("a"))
    await sup.handle(start_cmd("b"))
    await sent.wait_for("agent.status", agent="a", state="running")
    await sent.wait_for("agent.status", agent="b", state="running")
    await sup.shutdown()
    assert {a.agent: a.state for a in sup.agents()} == {"a": "stopped", "b": "stopped"}
