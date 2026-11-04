"""Seams the integration checks of `m4` found, end to end (the real control plane and runner, the fake agent): what
the prompt queue does when the agent under a running turn goes down."""
from tests.e2e.stack import Stack, end_of, eventually, texts


async def queued(stack: Stack, sid: str, name: str = "echo") -> list[str]:
    r = await stack.http.get(f"/api/agents/{name}/sessions/{sid}/queue")
    assert r.status_code == 200, r.text
    return [q["text"] for q in r.json()["queue"]]


async def working(stack: Stack, sid: str, turn: str) -> None:
    async def check():
        return any(e["turn_id"] == turn and e["kind"] == "agent_message" for e in await stack.events("echo", sid))
    await eventually(check, what="the turn working")


async def test_a_queue_waits_through_a_runner_restart(stack):
    """The runner shuts down (a restart, an update) mid-turn: the turn ends with an error (Retry), and the prompt
    queued behind it waits for the agent to come back instead of being sent to an agent that's going down."""
    feed = await stack.feed()
    await stack.ready_agent()
    sid = (await stack.new_session())["id"]
    turn = await stack.prompt("echo", sid, "slow")
    await working(stack, sid, turn)
    r = await stack.http.post(f"/api/agents/echo/sessions/{sid}/prompt", json={"text": "next one"})
    assert r.status_code == 202 and r.json()["queued"]

    await stack.stop_runner()
    assert end_of(await stack.turn("echo", sid, turn))["error"]
    assert await queued(stack, sid) == ["next one"]
    mark = len(feed.frames)
    await stack.start_runner()
    await stack.wait_restarted(feed, mark)

    async def delivered():
        evs = await stack.events("echo", sid)
        return evs if "echo: next one" in texts(evs) and texts(evs).endswith("next one") else None
    evs = await eventually(delivered, what="the queued prompt delivered")
    turns = [e["turn_id"] for e in evs if e["kind"] == "user_message"]
    assert len(turns) == 2 and end_of([e for e in evs if e["turn_id"] == turns[1]])["error"] is None
    assert await queued(stack, sid) == []


async def test_a_queue_waits_for_an_agent_that_crashed(stack):
    """The harness exits mid-turn: the agent is failed, and what was queued waits for its next start."""
    await stack.ready_agent()
    sid = (await stack.new_session())["id"]
    turn = await stack.prompt("echo", sid, "crash")
    r = await stack.http.post(f"/api/agents/echo/sessions/{sid}/prompt", json={"text": "then this"})
    assert r.status_code == 202 and r.json()["queued"]
    assert end_of(await stack.turn("echo", sid, turn))["error"]
    await stack.wait_state("echo", "failed")
    assert await queued(stack, sid) == ["then this"]
    assert [e["turn_id"] for e in await stack.events("echo", sid) if e["kind"] == "user_message"] == [turn]

    await stack.start_agent()

    async def delivered():
        evs = await stack.events("echo", sid)
        return evs if texts(evs).endswith("echo: then this") else None
    await eventually(delivered, what="the queued prompt delivered")
    assert await queued(stack, sid) == []
