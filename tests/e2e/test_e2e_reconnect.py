"""Things that drop and come back: the client socket, the runner link, the harness process."""
import asyncio

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from tests.e2e.stack import end_of, eventually, kinds, texts
from valet.runner.runner import Runner
from valet.shared.config import RunnerConfig


async def test_client_reconnect_with_cursor(stack):
    feed = await stack.feed()
    await stack.ready_agent()
    sid = (await stack.new_session())["id"]
    await stack.run_turn("echo", sid, "one")
    await eventually(lambda: any(e["kind"] == "turn_end" for e in feed.events(sid)), what="live turn_end")
    cursor = feed.events()[-1]["id"]
    await feed.close()

    # While no client is connected, another turn runs.
    turn2 = await stack.prompt("echo", sid, "two")
    missed = await stack.turn("echo", sid, turn2)

    again = await stack.feed()
    assert again.hello["last_event_id"] >= missed[-1]["id"]
    caught_up = await stack.events("echo", sid, after=cursor)
    assert caught_up == missed
    assert texts(caught_up) == "echo: two"

    # Paging: limit and `more`.
    r = await stack.http.get(f"/api/agents/echo/sessions/{sid}/events", params={"after": 0, "limit": 2})
    page = r.json()
    assert len(page["events"]) == 2 and page["more"] is True
    r = await stack.http.get(f"/api/agents/echo/sessions/{sid}/events",
                             params={"after": page["events"][-1]["id"], "limit": 2000})
    assert r.json()["more"] is False
    assert page["events"] + r.json()["events"] == await stack.events("echo", sid)

    # New events reach the new socket.
    turn3 = await stack.prompt("echo", sid, "three")
    await again.wait(lambda f: f["type"] == "event" and f["event"]["turn_id"] == turn3
                     and f["event"]["kind"] == "turn_end", what="turn_end on the new socket")


async def test_control_restart_keeps_agent_running(make_stack):
    """The runner's link drops and comes back; the harness process (and its in-memory session) survives. With
    --sessions none a restarted harness couldn't continue the session, so a working prompt proves it wasn't."""
    stack = await make_stack(sessions="none")
    await stack.ready_agent()
    sid = (await stack.new_session())["id"]
    await stack.run_turn("echo", sid, "before")

    await stack.restart_control()
    feed = await stack.feed()
    assert [r["connected"] for r in feed.hello["runners"] if r["name"] == "testrunner"] == [True]
    agent = await stack.wait_state("echo", "running")
    assert agent["connected"] is True
    assert texts(await stack.run_turn("echo", sid, "after")) == "echo: after"


async def test_turn_running_during_control_restart(stack):
    """A turn that keeps running while the control plane is away still ends properly afterwards."""
    await stack.ready_agent()
    sid = (await stack.new_session())["id"]
    turn = await stack.prompt("echo", sid, "slow")
    await eventually(lambda: _has(stack, sid, turn, "agent_message"), what="slow turn started")
    await stack.restart_control()
    s = await stack.session("echo", sid)
    assert s["state"] == "working" and s["turn_id"] == turn
    r = await stack.http.post(f"/api/agents/echo/sessions/{sid}/cancel")
    assert r.status_code == 200
    evs = await stack.turn("echo", sid, turn)
    assert end_of(evs) == {"stopReason": "cancelled", "error": None}


async def test_output_while_control_is_away_is_kept(stack):
    """What the agent says while the runner is cut off is resent after the reconnect: no hole in the record,
    nothing twice, and turn_end still last."""
    await stack.ready_agent()
    sid = (await stack.new_session())["id"]
    turn = await stack.prompt("echo", sid, "ticks")
    await eventually(lambda: _has(stack, sid, turn, "agent_message"), what="ticks started")
    await stack.control.stop()
    await asyncio.sleep(1.0)            # ticks keep coming while the control plane is down
    await stack.control.start()
    evs = await stack.turn("echo", sid, turn)
    assert texts(evs) == "".join(f"tick {i} " for i in range(20))
    assert kinds(evs)[-1] == "turn_end" and end_of(evs) == {"stopReason": "end_turn", "error": None}


async def test_permission_asked_while_control_is_away(stack):
    """A permission request raised while the runner is cut off still reaches the inbox, and the answer still
    reaches the agent."""
    await stack.ready_agent()
    sid = (await stack.new_session())["id"]
    turn = await stack.prompt("echo", sid, "later")
    await stack.control.stop()
    await asyncio.sleep(2.0)            # the request is raised now
    await stack.control.start()
    [item] = await stack.wait_pending()
    assert item["turn_id"] == turn and item["title"] == "Write hello.txt"
    r = await stack.http.post(f"/api/inbox/{item['id']}/answer", json={"option_id": "allow"})
    assert r.status_code == 200, r.text
    evs = await stack.turn("echo", sid, turn)
    assert texts(evs) == "allowed"
    assert kinds(evs).count("permission_request") == 1


async def _has(stack, sid, turn, kind):
    return any(e["turn_id"] == turn and e["kind"] == kind for e in await stack.events("echo", sid))


async def test_runner_restart_interrupts_and_restarts(stack):
    """The runner goes away (its agents with it) and comes back: the control plane marks the lost turn
    interrupted, shows the runner offline meanwhile, starts the agent again (desired=running) and the old session
    continues through session/load."""
    feed = await stack.feed()
    await stack.ready_agent()
    sid = (await stack.new_session())["id"]
    await stack.run_turn("echo", sid, "before")
    turn = await stack.prompt("echo", sid, "slow")
    await eventually(lambda: _has(stack, sid, turn, "agent_message"), what="slow turn started")

    mark = len(feed.frames)
    await stack.stop_runner()
    await feed.wait(lambda f: f["type"] == "runner" and not f["runner"]["connected"], start=mark,
                    what="runner offline frame")
    await feed.wait(lambda f: f["type"] == "agent" and not f["agent"]["connected"], start=mark,
                    what="agent offline frame")
    r = await stack.http.get("/api/runners")
    assert [x["connected"] for x in r.json()] == [False]

    mark = len(feed.frames)
    await stack.start_runner()
    evs = await stack.turn("echo", sid, turn)
    assert end_of(evs)["error"]       # "agent stopped" (runner shut down) or "interrupted" (reconciled)
    await stack.wait_restarted(feed, mark)
    s = await stack.session("echo", sid)
    assert s["state"] == "idle" and s["turn_id"] is None

    evs = await stack.run_turn("echo", sid, "after")
    assert texts(evs) == "echo: after"
    # Replays from session/load aren't stored again.
    assert "(replayed)" not in texts(await stack.events("echo", sid))


@pytest.mark.parametrize("sessions", ["load", "resume"])
async def test_agent_restart_resumes_session(make_stack, sessions):
    stack = await make_stack(sessions=sessions)
    await stack.ready_agent()
    sid = (await stack.new_session())["id"]
    await stack.run_turn("echo", sid, "before")

    await stack.restart_agent()

    evs = await stack.run_turn("echo", sid, "after")
    assert end_of(evs) == {"stopReason": "end_turn", "error": None}
    assert texts(evs) == "echo: after"
    all_evs = await stack.events("echo", sid)
    assert "(replayed)" not in texts(all_evs) + texts(all_evs, "user_message")
    assert kinds(all_evs).count("user_message") == 2


async def test_agent_restart_without_resume(make_stack):
    """A harness with neither session/load nor resume can't continue an old session: the prompt is refused (502)
    and the turn ends with that error; new sessions work."""
    stack = await make_stack(sessions="none")
    await stack.ready_agent()
    sid = (await stack.new_session())["id"]
    await stack.run_turn("echo", sid, "before")
    await stack.restart_agent()

    r = await stack.http.post(f"/api/agents/echo/sessions/{sid}/prompt", json={"text": "after"})
    assert r.status_code == 502, r.text
    evs = await stack.events("echo", sid)
    assert evs[-1]["kind"] == "turn_end" and "cannot resume session" in evs[-1]["data"]["error"]
    assert (await stack.session("echo", sid))["state"] == "idle"

    sid2 = (await stack.new_session())["id"]
    assert texts(await stack.run_turn("echo", sid2, "fresh")) == "echo: fresh"


async def test_crash_mid_turn(stack):
    await stack.ready_agent()
    sid = (await stack.new_session())["id"]
    evs = await stack.run_turn("echo", sid, "crash")
    assert texts(evs) == "crashing"
    assert end_of(evs)["error"] in ("agent exited", "interrupted")
    agent = await stack.wait_state("echo", "failed")
    assert agent["status"] == "failed" and "3" in (agent["error"] or "")
    assert (await stack.session("echo", sid))["state"] == "idle"

    # Starting it again continues the session (session/load).
    await stack.start_agent()
    assert texts(await stack.run_turn("echo", sid, "back")) == "echo: back"


async def test_start_without_runner_then_runner_connects(make_stack):
    """desired=running sticks while no runner is connected; the runner's hello triggers the start."""
    stack = await make_stack(runner=False)
    await stack.create_agent()
    r = await stack.http.post("/api/agents/echo/start")
    assert r.status_code == 503
    assert (await stack.agent("echo"))["desired"] == "running"
    r = await stack.http.post("/api/agents/echo/sessions")
    assert r.status_code == 503
    await stack.start_runner()
    await stack.wait_state("echo", "running")
    sid = (await stack.new_session())["id"]
    assert texts(await stack.run_turn("echo", sid, "hi")) == "echo: hi"


async def test_stop_and_start(stack):
    await stack.ready_agent()
    r = await stack.http.post("/api/agents/echo/stop")
    assert r.status_code == 200
    a = await stack.wait_state("echo", "stopped")
    assert a["desired"] == "stopped" and a["status"] == "stopped"
    r = await stack.http.post("/api/agents/echo/sessions")
    assert r.status_code == 503
    await stack.start_agent()
    sid = (await stack.new_session())["id"]
    assert texts(await stack.run_turn("echo", sid, "hi")) == "echo: hi"


async def test_bad_runner_token_is_fatal(stack):
    cfg = RunnerConfig(control_url=stack.rcfg.control_url, token="vrun_not-a-real-token", data=stack.rcfg.data,
                       reconnect_max=1.0)
    runner = Runner(cfg)
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(runner.run(), 10)
    await runner.close()


async def test_runner_replaced(stack):
    """A second connection with the same token replaces the first (4409)."""
    url, token = stack.rcfg.control_url, stack.rcfg.token
    hello = '{"type":"hello","protocol":1,"version":"0.2.0","agents":[],"turns":[],"pending_permissions":[]}'
    await stack.stop_runner()
    async with connect(url, additional_headers={"Authorization": f"Bearer {token}"}) as one:
        await one.send(hello)
        assert '"welcome"' in await one.recv()
        async with connect(url, additional_headers={"Authorization": f"Bearer {token}"}) as two:
            await two.send(hello)
            assert '"welcome"' in await two.recv()
            with pytest.raises(ConnectionClosed) as e:
                async with asyncio.timeout(5):
                    while True:
                        await one.recv()
            assert e.value.rcvd.code == 4409
