"""Supervisor + AgentProcess against the fake ACP agent at computer level host, with a list-collecting send:
the runner side of docs/PROTOCOL.md without a control plane."""
import asyncio
import json
import uuid

import pytest

from tests.fake_agent import COMMAND as FAKE_AGENT
from valet.runner.supervisor import CommandError, Supervisor
from valet.shared.config import RunnerConfig
from valet.shared.protocol import (
    AgentStart, AgentStop, PermissionAnswer, SessionCancel, SessionNew, SessionPrompt,
)

TIMEOUT = 10.0


class Sink:
    """The runner's send: collects every message it emits."""

    def __init__(self) -> None:
        self.msgs: list[dict] = []

    async def __call__(self, msg: dict) -> None:
        self.msgs.append(msg)

    def of(self, type_: str, **match) -> list[dict]:
        return [m for m in self.msgs if m["type"] == type_ and all(m.get(k) == v for k, v in match.items())]

    async def wait(self, type_: str, n: int = 1, **match) -> list[dict]:
        async with asyncio.timeout(TIMEOUT):
            while len(found := self.of(type_, **match)) < n:
                await asyncio.sleep(0.02)
        return found


def _id() -> str:
    return uuid.uuid4().hex


@pytest.fixture
async def runner(tmp_path):
    """runner(sessions="load") -> (Supervisor, Sink) with agent "echo" running."""
    sups: list[Supervisor] = []

    async def _make(sessions: str = "load"):
        sink = Sink()
        cfg = RunnerConfig(control_url="ws://127.0.0.1:1/ws/runner", token="vrun_test", data=tmp_path / "runner",
                           harness_commands={"claude": FAKE_AGENT + ["--sessions", sessions]})
        sup = Supervisor(cfg, sink)
        sups.append(sup)
        await start(sup, sink)
        return sup, sink

    yield _make
    for s in sups:
        await s.shutdown()


DEFINITION = {"name": "echo", "computer": {"level": "host"}}


async def start(sup, sink, n: int = 1):
    assert await sup.handle(AgentStart(id=_id(), agent="echo", definition=DEFINITION,
                                       files={"persona.md": "Be terse."})) == {}
    await sink.wait("agent.status", n, state="running")


async def new_session(sup) -> str:
    r = await sup.handle(SessionNew(id=_id(), agent="echo"))
    assert r["session_id"].startswith("fake-")
    return r["session_id"]


async def prompt(sup, sid: str, text: str) -> str:
    turn = _id()
    assert await sup.handle(SessionPrompt(id=_id(), agent="echo", session_id=sid, turn_id=turn,
                                          prompt=[{"type": "text", "text": text}])) == {}
    return turn


def texts(sink, turn) -> str:
    return "".join(m["update"]["content"]["text"] for m in sink.of("session.update", turn_id=turn)
                   if m["update"]["sessionUpdate"] == "agent_message_chunk")


async def test_start_reports_status(runner):
    sup, sink = await runner()
    statuses = sink.of("agent.status")
    assert [s["state"] for s in statuses] == ["starting", "running"]
    running = statuses[-1]
    assert running["agent"] == "echo" and running["error"] is None
    assert running["capabilities"]["loadSession"] is True
    assert running["agent_info"] == {"name": "fake-agent", "version": "0"}
    [state] = sup.agents()
    assert state.state == "running" and state.agent == "echo"


async def test_turn_streams_in_order(runner):
    sup, sink = await runner()
    sid = await new_session(sup)
    turn = await prompt(sup, sid, "hello there")
    [end] = await sink.wait("turn.end", turn_id=turn)
    assert end == {"type": "turn.end", "agent": "echo", "session_id": sid, "turn_id": turn,
                   "stop_reason": "end_turn", "error": None}
    ups = sink.of("session.update", turn_id=turn)
    assert [u["update"]["sessionUpdate"] for u in ups] == ["agent_thought_chunk", "agent_message_chunk",
                                                          "agent_message_chunk"]
    assert all(u["replay"] is False and u["session_id"] == sid for u in ups)
    assert texts(sink, turn) == "echo: hello there"
    # turn.end comes after every update of its turn.
    assert sink.msgs.index(end) > max(sink.msgs.index(u) for u in ups)
    assert sup.active_turns() == []


async def test_persona_in_session_meta(runner):
    sup, sink = await runner()
    sid = await new_session(sup)
    turn = await prompt(sup, sid, "meta")
    await sink.wait("turn.end", turn_id=turn)
    assert json.loads(texts(sink, turn))["systemPrompt"] == {"append": "Be terse."}


async def test_busy_cancel(runner):
    sup, sink = await runner()
    sid = await new_session(sup)
    turn = await prompt(sup, sid, "slow")
    await sink.wait("session.update", turn_id=turn)
    assert [t.turn_id for t in sup.active_turns()] == [turn]
    with pytest.raises(CommandError, match="busy"):
        await prompt(sup, sid, "again")
    assert await sup.handle(SessionCancel(id=_id(), agent="echo", session_id=sid)) == {}
    [end] = await sink.wait("turn.end", turn_id=turn)
    assert end["stop_reason"] == "cancelled" and end["error"] is None


@pytest.mark.parametrize("option,reply", [("allow", "allowed"), ("reject", "rejected")])
async def test_permission(runner, option, reply):
    sup, sink = await runner()
    sid = await new_session(sup)
    turn = await prompt(sup, sid, "permission")
    [req] = await sink.wait("permission.request")
    assert req["session_id"] == sid and req["turn_id"] == turn
    assert req["tool_call"]["title"] == "Write hello.txt"
    assert [o["optionId"] for o in req["options"]] == ["allow", "allow-always", "reject"]
    assert sup.pending_permissions() == [req["request_id"]]
    # The tool_call update went up before the request.
    assert sink.msgs.index(sink.of("session.update", turn_id=turn)[0]) < sink.msgs.index(req)

    with pytest.raises(CommandError, match="unknown request"):
        await sup.handle(PermissionAnswer(id=_id(), agent="echo", request_id="nope",
                                          outcome={"outcome": "selected", "optionId": "allow"}))
    assert await sup.handle(PermissionAnswer(id=_id(), agent="echo", request_id=req["request_id"],
                                             outcome={"outcome": "selected", "optionId": option})) == {}
    [end] = await sink.wait("turn.end", turn_id=turn)
    assert end["stop_reason"] == "end_turn"
    assert texts(sink, turn) == reply
    assert sup.pending_permissions() == []
    with pytest.raises(CommandError, match="unknown request"):
        await sup.handle(PermissionAnswer(id=_id(), agent="echo", request_id=req["request_id"],
                                          outcome={"outcome": "selected", "optionId": option}))


async def test_cancel_closes_pending_permission(runner):
    sup, sink = await runner()
    sid = await new_session(sup)
    turn = await prompt(sup, sid, "permission")
    [req] = await sink.wait("permission.request")
    await sup.handle(SessionCancel(id=_id(), agent="echo", session_id=sid))
    [closed] = await sink.wait("permission.closed")
    assert closed["request_id"] == req["request_id"] and closed["reason"] == "cancelled"
    [end] = await sink.wait("turn.end", turn_id=turn)
    assert end["stop_reason"] == "cancelled"
    assert texts(sink, turn) == "cancelled"


async def test_harness_error(runner):
    sup, sink = await runner()
    sid = await new_session(sup)
    turn = await prompt(sup, sid, "fail")
    [end] = await sink.wait("turn.end", turn_id=turn)
    assert end["stop_reason"] is None and end["error"]


async def test_crash(runner):
    sup, sink = await runner()
    sid = await new_session(sup)
    turn = await prompt(sup, sid, "crash")
    [end] = await sink.wait("turn.end", turn_id=turn)
    assert end["error"] == "agent exited"
    [failed] = await sink.wait("agent.status", state="failed")
    assert "3" in failed["error"]
    assert sink.msgs.index(end) < sink.msgs.index(failed)
    with pytest.raises(CommandError, match="not running"):
        await new_session(sup)


async def test_crash_with_pending_permission(runner):
    """Exit while a permission request is pending: closed with reason exited."""
    sup, sink = await runner()
    sid = await new_session(sup)
    turn = await prompt(sup, sid, "permission")
    [req] = await sink.wait("permission.request")
    await sup.handle(AgentStop(id=_id(), agent="echo"))
    [closed] = await sink.wait("permission.closed")
    assert closed["request_id"] == req["request_id"] and closed["reason"] == "exited"
    [end] = await sink.wait("turn.end", turn_id=turn)
    assert end["error"]
    assert sink.of("agent.status")[-1]["state"] == "stopped"
    assert sup.pending_permissions() == [] and sup.active_turns() == []


@pytest.mark.parametrize("sessions,replays", [("load", 2), ("resume", 0)])
async def test_restart_resumes_session(runner, sessions, replays):
    sup, sink = await runner(sessions)
    sid = await new_session(sup)
    await sink.wait("turn.end", turn_id=await prompt(sup, sid, "before"))
    await start(sup, sink, n=2)                 # restart: a new harness process that doesn't know sid
    turn = await prompt(sup, sid, "after")
    [end] = await sink.wait("turn.end", turn_id=turn)
    assert end["stop_reason"] == "end_turn"
    assert texts(sink, turn) == "echo: after"
    replayed = [m for m in sink.of("session.update", session_id=sid) if m["replay"]]
    assert len(replayed) == replays
    assert all(m["turn_id"] is None for m in replayed)


async def test_restart_without_resume(runner):
    sup, sink = await runner("none")
    sid = await new_session(sup)
    await sink.wait("turn.end", turn_id=await prompt(sup, sid, "before"))
    await start(sup, sink, n=2)
    with pytest.raises(CommandError, match="cannot resume session"):
        await prompt(sup, sid, "after")
    assert sup.active_turns() == []


async def test_unknown_agent(runner):
    sup, sink = await runner()
    with pytest.raises(CommandError, match="not running"):
        await sup.handle(SessionNew(id=_id(), agent="ghost"))
    with pytest.raises(CommandError, match="unknown request"):
        await sup.handle(PermissionAnswer(id=_id(), agent="ghost", request_id="r",
                                          outcome={"outcome": "cancelled"}))


async def test_stop(runner):
    sup, sink = await runner()
    assert await sup.handle(AgentStop(id=_id(), agent="echo")) == {}
    assert sink.of("agent.status")[-1]["state"] == "stopped"
    assert [a.state for a in sup.agents()] == ["stopped"]


async def test_unavailable_level_fails(tmp_path):
    sink = Sink()
    sup = Supervisor(RunnerConfig(control_url="ws://127.0.0.1:1", token="t", data=tmp_path), sink)
    try:
        await sup.handle(AgentStart(id=_id(), agent="nope", definition={"name": "nope", "harness": "codex"}))
        [failed] = await sink.wait("agent.status", state="failed")
        assert failed["error"] == "harness 'codex' isn't available yet"
    finally:
        await sup.shutdown()
