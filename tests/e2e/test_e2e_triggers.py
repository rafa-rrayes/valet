"""Always-on end to end: the real control plane (on a ManualClock, in UTC) and runner, the fake agent. Schedules
that fire and catch up across a restart, webhooks, a queue that outlives a control restart, Retry, agents messaging
each other, valet.owner questions and wake-ups, and a main session the harness lost."""
import json
from datetime import UTC, datetime

import httpx

from tests.e2e.stack import Stack, end_of, eventually, texts
from valet.control.clock import ManualClock

AGENT = """\
name: {name}
harness: claude
computer:
  level: host
permissions: trusted
directives: [persona.md]
tools:
  mcp: [valet.agents, valet.owner]
{triggers}"""


def ts(*args: int) -> float:
    return datetime(*args, tzinfo=UTC).timestamp()


async def agent(stack: Stack, name: str = "echo", triggers: str = "") -> None:
    r = await stack.http.post("/api/agents", json={"name": name, "agent_yaml": AGENT.format(
        name=name, triggers=f"triggers:\n{triggers}" if triggers else "")})
    assert r.status_code == 201, r.text
    await stack.start_agent(name)


async def runs(stack: Stack, name: str = "echo") -> list[dict]:
    return (await stack.http.get(f"/api/agents/{name}/trigger-runs")).json()["runs"]


async def main_session(stack: Stack, name: str = "echo") -> str:
    async def check():
        return (await stack.agent(name))["main_session"]
    return await eventually(check, what=f"{name}'s main session")


async def turns_done(stack: Stack, name: str, sid: str, n: int) -> list[dict]:
    """Wait until the session has `n` ended turns and is idle; its events."""
    async def check():
        evs = await stack.events(name, sid)
        s = await stack.session(name, sid)
        return evs if s["state"] == "idle" and sum(e["kind"] == "turn_end" for e in evs) >= n else None
    return await eventually(check, what=f"{n} turns in {sid}")


async def schedule_state(stack: Stack, name: str = "echo") -> None:
    """The scheduler has seen the agent's schedules (their first runs count from then)."""
    async def check():
        return (await stack.agent(name))["next_trigger"]
    await eventually(check, what="the schedule seen")


async def test_a_schedule_fires_and_catches_up_after_a_control_restart(make_stack):
    clock = ManualClock(ts(2026, 11, 2, 7, 59, 30))
    stack = await make_stack(clock=clock, timezone="UTC")
    await agent(stack, triggers='  - schedule: "0 * * * *"\n    prompt: Hourly check.\n')
    await schedule_state(stack)
    assert (await stack.agent("echo"))["next_trigger"]["at"] == ts(2026, 11, 2, 8)

    clock.t = ts(2026, 11, 2, 8, 0, 1)
    sid = await main_session(stack)
    evs = await turns_done(stack, "echo", sid, 1)
    assert texts(evs) == 'echo: Valet: a scheduled prompt: every hour, on the hour ("0 * * * *").\n\nHourly check.'
    user = [e for e in evs if e["kind"] == "user_message"][0]
    assert user["data"]["source"] == "schedule" and user["data"]["label"] == "Every hour, on the hour"
    [run] = await runs(stack)
    assert run["outcome"] == "started" and run["session_id"] == sid

    # The control plane is down from 08:00 to 11:02: 09:00 and 10:00 are missed, 11:00 catches up.
    await stack.control.stop()
    clock.t = ts(2026, 11, 2, 11, 2)
    await stack.control.start()
    await eventually(stack.runner_connected, what="runner reconnected")
    evs = await turns_done(stack, "echo", sid, 2)
    assert "It was due at 2026-11-02 11:00" in texts(evs)

    async def recorded():
        r = await runs(stack)
        return r if len(r) == 3 else None
    started, missed, _ = await eventually(recorded, what="the runs")
    assert started["outcome"] == "started" and started["scheduled_for"] == ts(2026, 11, 2, 11)
    assert missed["outcome"] == "missed" and missed["count"] == 2


async def test_a_webhook_prompts_the_agent(make_stack):
    stack = await make_stack(timezone="UTC")
    await agent(stack, triggers="  - webhook: deploys\n    prompt: Summarize the deploy.\n")
    [hook] = (await stack.http.get("/api/agents/echo/triggers")).json()["triggers"]
    async with httpx.AsyncClient(base_url=stack.base) as sender:
        r = await sender.post(f"/hooks/echo/deploys?key={hook['key']}", json={"service": "api", "ok": True})
        assert r.status_code == 202, r.text
        assert r.json()["outcome"] == "started"
        r2 = await sender.post("/hooks/echo/deploys", json={}, headers={"Authorization": "Bearer nope"})
        assert r2.status_code == 404
    evs = await stack.turn("echo", r.json()["session_id"], r.json()["turn_id"])
    said = texts(evs)
    assert said.startswith("echo: Valet: webhook `deploys` was called at ")
    assert "Summarize the deploy.\n\n--- BEGIN UNTRUSTED WEBHOOK DATA" in said
    assert '"service": "api"' in said and end_of(evs)["error"] is None


async def test_a_queued_prompt_survives_a_control_restart(stack):
    await stack.ready_agent()
    sid = (await stack.new_session())["id"]
    turn = await stack.prompt("echo", sid, "slow")
    await eventually(lambda: stack.session("echo", sid), what="session")
    r = await stack.http.post(f"/api/agents/echo/sessions/{sid}/prompt", json={"text": "next one"})
    assert r.status_code == 202 and r.json()["queued"]

    await stack.restart_control()           # the slow turn goes on in the runner; the queue is in the database
    assert (await stack.session("echo", sid))["queued"] == 1
    r = await stack.http.post(f"/api/agents/echo/sessions/{sid}/cancel")
    assert r.status_code == 200
    evs = await turns_done(stack, "echo", sid, 2)
    assert texts(evs) == "workingecho: next one"
    assert [e["data"]["content"]["text"] for e in evs if e["kind"] == "user_message"] == ["slow", "next one"]
    assert end_of([e for e in evs if e["turn_id"] == turn])["stopReason"] == "cancelled"


async def test_retry_an_interrupted_turn(stack):
    feed = await stack.feed()
    await stack.ready_agent()
    sid = (await stack.new_session())["id"]
    turn = await stack.prompt("echo", sid, "slow")
    await eventually(lambda: feed.find(lambda f: f["type"] == "event" and f["event"]["turn_id"] == turn
                                       and f["event"]["kind"] == "agent_message"), what="slow turn working")
    mark = len(feed.frames)
    await stack.stop_runner()
    await stack.start_runner()
    evs = await stack.turn("echo", sid, turn)
    assert end_of(evs)["error"]
    await stack.wait_restarted(feed, mark)

    r = await stack.http.post(f"/api/agents/echo/sessions/{sid}/turns/{turn}/retry")
    assert r.status_code == 202, r.text
    again = r.json()["turn_id"]
    await eventually(lambda: feed.find(lambda f: f["type"] == "event" and f["event"]["turn_id"] == again
                                       and f["event"]["kind"] == "agent_message"), what="the retry working")
    first = [e for e in await stack.events("echo", sid) if e["turn_id"] == again][0]
    assert first["kind"] == "user_message" and first["data"]["retry_of"] == turn
    assert (await stack.http.post(f"/api/agents/echo/sessions/{sid}/turns/{turn}/retry")).status_code == 409
    await stack.http.post(f"/api/agents/echo/sessions/{sid}/cancel")
    await stack.turn("echo", sid, again)


async def test_agents_talk_and_ask_alice(stack):
    await agent(stack, "alpha")
    await agent(stack, "beta")
    sid = (await stack.new_session("alpha"))["id"]
    evs = await stack.run_turn("alpha", sid, 'mcp valet_agents__send_message {"agent": "beta", "text": "hello beta"}')
    assert texts(evs).startswith("ok: ")
    beta_main = await main_session(stack, "beta")
    evs = await turns_done(stack, "beta", beta_main, 1)
    assert texts(evs) == ("echo: Valet: a message from agent `alpha`, another of the owner's agents. To reply, use "
                          "valet.agents send_message to alpha.\n\nhello beta")

    question = {"question": "Ship it?", "options": ["Yes", "Wait"]}
    evs = await stack.run_turn("alpha", sid, "mcp valet_owner__ask " + json.dumps(question))
    assert texts(evs).startswith("ok: Asked: it's in the owner's inbox.")
    [item] = await stack.pending()
    assert item["kind"] == "question" and item["session_id"] == sid and item["text"] == "Ship it?"
    assert (await stack.agent("alpha"))["status"] == "idle"
    r = await stack.http.post(f"/api/inbox/{item['id']}/answer", json={"option_id": "option-1"})
    assert r.status_code == 200, r.text
    evs = await turns_done(stack, "alpha", sid, 3)
    last = [e for e in evs if e["kind"] == "user_message"][-1]
    assert last["data"]["source"] == "answer"
    assert last["data"]["content"]["text"].endswith("Your question: Ship it?\n\nThe owner's choice: Yes")


async def test_a_wakeup_comes_back_to_its_session(make_stack):
    clock = ManualClock(ts(2026, 11, 2, 9))
    stack = await make_stack(clock=clock, timezone="UTC")
    await agent(stack)
    sid = (await stack.new_session())["id"]
    evs = await stack.run_turn("echo", sid, 'mcp valet_owner__schedule_wakeup {"prompt": "check CI", '
                                            '"in_minutes": 15}')
    assert texts(evs) == "ok: Wake-up 1 set for 2026-11-02T09:15+00:00 (UTC); it comes in this session."
    assert (await stack.agent("echo"))["next_trigger"]["kind"] == "wakeup"
    clock.t = ts(2026, 11, 2, 9, 15)
    evs = await turns_done(stack, "echo", sid, 2)
    assert texts(evs).endswith("echo: Valet: the wake-up you scheduled at 2026-11-02 09:00 for 2026-11-02 09:15."
                               "\n\ncheck CI")


async def test_a_lost_main_session_is_replaced(make_stack):
    """A harness that can't resume sessions loses the main session at a restart; the next scheduled prompt fails
    there, so Valet makes a new main session and sends it again."""
    clock = ManualClock(ts(2026, 11, 2, 7, 59, 30))
    stack = await make_stack(sessions="none", clock=clock, timezone="UTC")
    await agent(stack, triggers='  - schedule: "0 8 * * *"\n    prompt: Morning.\n')
    await schedule_state(stack)
    clock.t = ts(2026, 11, 2, 8, 0, 1)
    first = await main_session(stack)
    await turns_done(stack, "echo", first, 1)
    await stack.restart_agent()

    clock.t = ts(2026, 11, 3, 8, 0, 1)

    async def replaced():
        sid = (await stack.agent("echo"))["main_session"]
        return sid if sid and sid != first else None
    second = await eventually(replaced, what="a new main session")
    evs = await turns_done(stack, "echo", second, 1)
    assert texts(evs).endswith("Morning.")
    old = await stack.events("echo", first)
    assert "cannot resume session" in old[-1]["data"]["error"]
    assert (await stack.session("echo", first))["main"] is False
