"""Prompt -> the harness streams -> events in the store and on the live client feed."""
import asyncio
import json

import httpx
import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from tests.e2e.stack import end_of, eventually, kinds, texts


async def test_prompt_streams_to_client(stack):
    feed = await stack.feed()
    await stack.ready_agent()
    agent = await stack.agent("echo")
    assert agent["harness"] == "claude" and agent["level"] == "host" and agent["runner"] == "testrunner"
    assert agent["connected"] and agent["desired"] == "running" and agent["status"] == "idle"
    assert agent["capabilities"]["loadSession"] is True

    s = await stack.new_session()
    sid = s["id"]
    assert sid.startswith("fake-") and s["state"] == "idle" and s["turn_id"] is None
    await feed.wait(lambda f: f["type"] == "session" and f["session"]["id"] == sid, what="session frame")

    turn = await stack.prompt("echo", sid, "hello there")
    end = await feed.wait(lambda f: f["type"] == "event" and f["event"]["kind"] == "turn_end"
                          and f["event"]["turn_id"] == turn, what="turn_end frame")
    assert end["event"]["data"] == {"stopReason": "end_turn", "error": None}

    live = feed.events(sid, turn)
    assert kinds(live) == ["user_message", "agent_thought", "agent_message", "agent_message", "turn_end"]
    assert live[0]["data"] == {"content": {"type": "text", "text": "hello there"}, "source": "owner", "label": "You"}
    assert texts(live, "agent_thought") == "thinking"
    assert texts(live) == "echo: hello there"
    ids = [e["id"] for e in live]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)
    assert all(e["agent"] == "echo" and e["session_id"] == sid for e in live)

    # The store holds exactly what was streamed.
    assert await stack.events("echo", sid) == live

    # Session frames went working -> idle; the session is titled by its first prompt.
    states = [f["session"]["state"] for f in feed.frames if f["type"] == "session" and f["session"]["id"] == sid]
    assert "working" in states and states[-1] == "idle"
    s = await stack.session("echo", sid)
    assert s["state"] == "idle" and s["turn_id"] is None and s["title"] == "hello there"
    r = await stack.http.get("/api/agents/echo/sessions")
    assert [x["id"] for x in r.json()] == [sid]


async def test_tool_call_updates(stack):
    await stack.ready_agent()
    sid = (await stack.new_session())["id"]
    evs = await stack.run_turn("echo", sid, "tool")
    assert kinds(evs) == ["user_message", "tool_call", "tool_call_update", "tool_call_update", "agent_message",
                          "turn_end"]
    call = evs[1]["data"]
    assert call["title"] == "Run ls" and call["kind"] == "execute" and call["status"] == "in_progress"
    assert call["rawInput"] == {"command": "ls"}
    assert "sessionUpdate" not in call
    assert evs[2]["data"]["toolCallId"] == call["toolCallId"]
    assert evs[2]["data"]["content"][0]["content"]["text"] == "output"
    assert evs[3]["data"]["status"] == "completed"
    assert texts(evs) == "done"


async def test_persona_reaches_harness(stack):
    """persona.md is compiled into the claude harness's session/new _meta (systemPrompt.append)."""
    await stack.ready_agent(persona="Answer in haiku.\n")
    sid = (await stack.new_session())["id"]
    evs = await stack.run_turn("echo", sid, "meta")
    meta = json.loads(texts(evs))
    assert meta["systemPrompt"]["append"].strip() == "Answer in haiku."


async def test_harness_error_ends_turn(stack):
    await stack.ready_agent()
    sid = (await stack.new_session())["id"]
    evs = await stack.run_turn("echo", sid, "fail")
    end = end_of(evs)
    assert end["stopReason"] is None and end["error"]
    assert (await stack.session("echo", sid))["state"] == "idle"
    # The session stays usable.
    assert texts(await stack.run_turn("echo", sid, "again")) == "echo: again"


async def test_busy_and_cancel(stack):
    feed = await stack.feed()
    await stack.ready_agent()
    sid = (await stack.new_session())["id"]

    r = await stack.http.post(f"/api/agents/echo/sessions/{sid}/cancel")
    assert r.status_code == 409

    turn = await stack.prompt("echo", sid, "slow")
    await feed.wait(lambda f: f["type"] == "event" and f["event"]["turn_id"] == turn
                    and f["event"]["kind"] == "agent_message", what="slow turn working")
    s = await stack.session("echo", sid)
    assert s["state"] == "working" and s["turn_id"] == turn
    assert (await stack.agent("echo"))["status"] == "working"

    # A prompt for a working session waits in its queue, and goes as soon as the running turn ends.
    r = await stack.http.post(f"/api/agents/echo/sessions/{sid}/prompt", json={"text": "second"})
    assert r.status_code == 202 and r.json()["queued"]
    assert (await stack.session("echo", sid))["queued"] == 1

    r = await stack.http.post(f"/api/agents/echo/sessions/{sid}/cancel")
    assert r.status_code == 200 and r.json() == {"ok": True}
    evs = await stack.turn("echo", sid, turn)
    assert end_of(evs) == {"stopReason": "cancelled", "error": None}
    assert texts(evs) == "working"
    await eventually(lambda: feed.find(lambda f: f["type"] == "event" and f["event"]["kind"] == "turn_end"
                                       and f["event"]["turn_id"] != turn), what="the queued prompt's turn")

    async def idle():
        s = await stack.session("echo", sid)
        return s if s["state"] == "idle" and s["queued"] == 0 else None
    s = await eventually(idle, what="session idle")
    assert s["turn_id"] is None
    assert texts(await stack.events("echo", sid)) == "workingecho: second"
    assert (await stack.agent("echo"))["status"] == "idle"
    assert texts(await stack.run_turn("echo", sid, "after")) == "echo: after"


async def test_prompt_validation(stack):
    await stack.ready_agent()
    sid = (await stack.new_session())["id"]
    r = await stack.http.post(f"/api/agents/echo/sessions/{sid}/prompt", json={"text": ""})
    assert r.status_code == 422
    r = await stack.http.post("/api/agents/echo/sessions/nope/prompt", json={"text": "hi"})
    assert r.status_code == 404
    r = await stack.http.get("/api/agents/echo/sessions/nope")
    assert r.status_code == 404


async def test_two_sessions_are_separate(stack):
    await stack.ready_agent()
    a = (await stack.new_session())["id"]
    b = (await stack.new_session())["id"]
    assert a != b
    await stack.run_turn("echo", a, "first")
    await stack.run_turn("echo", b, "second")
    assert texts(await stack.events("echo", a)) == "echo: first"
    assert texts(await stack.events("echo", b)) == "echo: second"
    r = await stack.http.get("/api/agents/echo/sessions")
    assert [x["id"] for x in r.json()] == [b, a]     # most recently updated first


async def test_auth_required(stack):
    async with httpx.AsyncClient(base_url=stack.base) as anon:
        r = await anon.get("/api/agents")
        assert r.status_code == 401 and r.json() == {"detail": "sign in first"}
        r = await anon.get("/api/me")
        assert r.json()["owner"] is False and r.json()["setup"] is True
        r = await anon.post("/api/login", json={"password": "wrong password!", "client": "native"})
        assert r.status_code == 401
    async with connect(f"ws://127.0.0.1:{stack.ccfg.port}/ws/client") as ws:
        with pytest.raises(ConnectionClosed) as e:
            await asyncio.wait_for(ws.recv(), 5)
    assert e.value.rcvd.code == 4401
