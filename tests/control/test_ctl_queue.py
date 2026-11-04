"""The prompt queue, Retry, the main session and Agent.activity (control/prompts.py, control/activity.py), with the
test playing the runner."""
import pytest

from tests.control.always_on import (
    answer_new, answer_prompt, control, end_turn, idle, session, start, text_of, wait,
)
from valet.control import prompts as prompts_module


def prompt(client, text, sid="S1", name="scout"):
    return client.post(f"/api/agents/{name}/sessions/{sid}/prompt", json={"text": text})


# ---------- the queue ----------

def test_prompts_queue_behind_a_running_turn_and_drain_in_order(client, connect, rpc):
    with connect() as runner:
        start(client, rpc, runner)
        session(client, rpc, runner)
        r, first = rpc(runner, prompt, client, "one", expect="session.prompt")
        assert r.status_code == 202 and set(r.json()) == {"turn_id"}
        q2, q3, q4 = (prompt(client, t).json()["queued"] for t in ("two", "three", "four"))
        s = client.get("/api/agents/scout/sessions/S1").json()
        assert s["queued"] == 3 and [q["text"] for q in s["queue"]] == ["two", "three", "four"]
        assert s["queue"][0] == {"id": q2, "session_id": "S1", "text": "two", "source": "owner", "label": "You",
                                 "retry_of": None, "created_at": s["queue"][0]["created_at"], "attachments": []}
        assert client.get("/api/agents/scout").json()["queued"] == 3
        assert [q["id"] for q in client.get("/api/agents/scout/sessions/S1/queue").json()["queue"]] == [q2, q3, q4]

        assert client.delete(f"/api/agents/scout/sessions/S1/queue/{q3}").json() == {"ok": True}
        assert client.delete(f"/api/agents/scout/sessions/S1/queue/{q3}").status_code == 404
        assert client.delete("/api/agents/scout/sessions/S2/queue/1").status_code == 404

        end_turn(runner, first)
        second = answer_prompt(runner)
        assert text_of(second) == "two"
        end_turn(runner, second)
        fourth = answer_prompt(runner)
        assert text_of(fourth) == "four"
        end_turn(runner, fourth)
        wait(lambda: idle(client, "S1"))
        assert client.get("/api/agents/scout/sessions/S1").json()["queued"] == 0
        events = client.get("/api/agents/scout/sessions/S1/events").json()["events"]
        assert [e["data"]["content"]["text"] for e in events if e["kind"] == "user_message"] == ["one", "two", "four"]


def test_a_long_queue_is_refused(client, connect, rpc, monkeypatch):
    monkeypatch.setattr(prompts_module, "QUEUE_LIMIT", 2)
    with connect() as runner:
        start(client, rpc, runner)
        session(client, rpc, runner)
        rpc(runner, prompt, client, "one", expect="session.prompt")
        assert prompt(client, "two").status_code == 202 and prompt(client, "three").status_code == 202
        r = prompt(client, "four")
        assert r.status_code == 429 and r.json()["detail"] == "2 prompts are already waiting in this session"


def test_the_owners_prompt_to_a_stopped_agent_still_fails_at_once(client, connect, rpc):
    with connect() as runner:
        start(client, rpc, runner)
        session(client, rpc, runner)
    r = prompt(client, "hello?")
    assert r.status_code == 503 and r.json()["detail"] == "scout's runner rr is offline"
    turn = control(client).db.one("SELECT * FROM turns")
    assert turn["state"] == "failed" and turn["source"] == "owner"


def test_queued_prompts_wait_for_the_agent_and_go_when_it_is_back(client, connect, rpc):
    """A prompt queued for a working session survives the runner going away; the hello that brings the agent back
    interrupts the lost turn, and the queue drains from there."""
    with connect() as runner:
        start(client, rpc, runner)
        session(client, rpc, runner)
        rpc(runner, prompt, client, "one", expect="session.prompt")
        qid = prompt(client, "two").json()["queued"]
    with connect(agents=[{"agent": "scout", "state": "running"}]) as runner:     # the turn didn't survive
        cmd = answer_prompt(runner)
        assert text_of(cmd) == "two" and cmd["session_id"] == "S1"
        db = control(client).db
        assert db.one("SELECT COUNT(*) AS n FROM prompt_queue")["n"] == 0
        assert [t["state"] for t in db.query("SELECT state FROM turns ORDER BY started_at, rowid")] == \
            ["interrupted", "running"]
    del qid


# ---------- Retry ----------

def test_retry_an_interrupted_turn(client, connect, rpc):
    with connect() as runner:
        start(client, rpc, runner)
        session(client, rpc, runner)
        r, cmd = rpc(runner, prompt, client, "do the thing", expect="session.prompt")
        turn = r.json()["turn_id"]
    with connect(agents=[{"agent": "scout", "state": "running"}]) as runner:
        db = control(client).db
        wait(lambda: db.one("SELECT state FROM turns WHERE id = ?", (turn,))["state"] == "interrupted")
        url = f"/api/agents/scout/sessions/S1/turns/{turn}/retry"
        r, again = rpc(runner, client.post, url, expect="session.prompt")
        assert r.status_code == 202 and r.json()["turn_id"] == again["turn_id"] != turn
        assert text_of(again) == "do the thing"
        ev = client.get("/api/agents/scout/sessions/S1/events").json()["events"]
        retried = [e for e in ev if e["kind"] == "user_message"][-1]
        assert retried["data"]["retry_of"] == turn and retried["data"]["source"] == "owner"
        assert db.one("SELECT retry_of FROM turns WHERE id = ?", (again["turn_id"],))["retry_of"] == turn

        r = client.post(url)
        assert r.status_code == 409 and r.json()["detail"] == "this turn was already retried"
        r = client.post(f"/api/agents/scout/sessions/S1/turns/{again['turn_id']}/retry")
        assert r.status_code == 409 and "this one is running" in r.json()["detail"]
        assert client.post("/api/agents/scout/sessions/S1/turns/nope/retry").status_code == 404
        end_turn(runner, again)
        wait(lambda: idle(client, "S1"))
        r = client.post(f"/api/agents/scout/sessions/S1/turns/{again['turn_id']}/retry")
        assert r.status_code == 409 and "this one is done" in r.json()["detail"]


def test_retry_keeps_the_source_and_queues_behind_a_turn(client, connect, rpc):
    with connect() as runner:
        start(client, rpc, runner)
        session(client, rpc, runner)
        r, cmd = rpc(runner, prompt, client, "first", expect="session.prompt")
        end_turn(runner, cmd, stop_reason=None, error="the model fell over")
        wait(lambda: idle(client, "S1"))
        failed = r.json()["turn_id"]
        control(client).db.execute("UPDATE turns SET source = 'webhook:gh', source_label = 'Webhook gh' WHERE id = ?",
                                   (failed,))
        r, running = rpc(runner, prompt, client, "second", expect="session.prompt")
        r = client.post(f"/api/agents/scout/sessions/S1/turns/{failed}/retry")
        assert r.status_code == 202 and r.json()["queued"]
        [q] = client.get("/api/agents/scout/sessions/S1/queue").json()["queue"]
        assert q["retry_of"] == failed and q["source"] == "webhook:gh" and q["label"] == "Webhook gh"
        assert client.post(f"/api/agents/scout/sessions/S1/turns/{failed}/retry").status_code == 409
        end_turn(runner, running)
        cmd = answer_prompt(runner)
        assert text_of(cmd) == "first"


def test_retry_needs_the_agent(client, connect, rpc):
    with connect() as runner:
        start(client, rpc, runner)
        session(client, rpc, runner)
        r, _ = rpc(runner, prompt, client, "first", expect="session.prompt")
    turn = r.json()["turn_id"]
    with connect() as runner:          # a runner that doesn't have scout: the turn is interrupted, scout restarts
        cmd = runner.recv("agent.start")
        runner.reply(cmd, {})
        r = client.post(f"/api/agents/scout/sessions/S1/turns/{turn}/retry")
        assert r.status_code == 503 and r.json()["detail"].endswith(": start it, then retry")


# ---------- the main session ----------

def test_a_lost_main_session_is_replaced_and_the_prompt_moves_there(client, connect, rpc, pool):
    with connect() as runner:
        start(client, rpc, runner)
        c = control(client)
        fut = pool.submit(client.portal.call, c.prompts.deliver, "scout", prompts_module.MAIN, "Valet: x\n\nhello",
                          "schedule", "Every day at 08:00")
        answer_new(runner, "M1")
        first = answer_prompt(runner)
        assert fut.result(5).turn_id == first["turn_id"]
        end_turn(runner, first)
        wait(lambda: idle(client, "M1"))
        # The harness lost M1 (its home was wiped): the next prompt there fails with "cannot resume session".
        fut = pool.submit(client.portal.call, c.prompts.deliver, "scout", prompts_module.MAIN, "Valet: y\n\nagain",
                          "schedule", "Every day at 08:00")
        cmd = answer_prompt(runner, error="cannot resume session")
        assert cmd["session_id"] == "M1"
        answer_new(runner, "M2")                # a new main session...
        moved = answer_prompt(runner)           # ...and the prompt that never reached the model, there
        assert moved["session_id"] == "M2" and text_of(moved) == "Valet: y\n\nagain"
        fut.result(5)
        a = client.get("/api/agents/scout").json()
        assert a["main_session"] == "M2"
        assert client.get("/api/agents/scout/sessions/M1").json()["main"] is False
        assert client.get("/api/agents/scout/sessions/M2").json()["main"] is True


def test_an_owner_prompt_that_hits_a_lost_main_session_is_not_resent(client, connect, rpc, pool):
    with connect() as runner:
        start(client, rpc, runner)
        c = control(client)
        fut = pool.submit(client.portal.call, c.prompts.deliver, "scout", prompts_module.MAIN, "Valet: x\n\nhello",
                          "schedule")
        answer_new(runner, "M1")
        end_turn(runner, answer_prompt(runner))
        fut.result(5)
        wait(lambda: idle(client, "M1"))
        r, _ = rpc(runner, prompt, client, "typed by Alice", "M1", expect="session.prompt",
                   error="cannot resume session")
        assert r.status_code == 502
        wait(lambda: client.get("/api/agents/scout").json()["main_session"] is None)
        assert c.db.one("SELECT COUNT(*) AS n FROM prompt_queue")["n"] == 0


# ---------- the Agent shape ----------

def test_activity_follows_the_running_turn(client, connect, rpc):
    with connect() as runner:
        start(client, rpc, runner)
        session(client, rpc, runner)
        r, cmd = rpc(runner, prompt, client, "go", expect="session.prompt")
        turn = cmd["turn_id"]

        def upd(update):
            runner.send(type="session.update", agent="scout", session_id="S1", turn_id=turn, replay=False,
                        update=update)

        def activity():
            return client.get("/api/agents/scout").json()["activity"]
        assert activity() is None
        upd({"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "hmm"}})
        wait(lambda: activity() == "thinking")
        upd({"sessionUpdate": "tool_call", "toolCallId": "c1", "title": "Read   README.md", "kind": "read",
             "status": "pending"})
        wait(lambda: activity() == "Read README.md")
        upd({"sessionUpdate": "tool_call_update", "toolCallId": "c1", "status": "completed"})
        wait(lambda: activity() == "thinking")
        upd({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "done"}})
        wait(lambda: activity() == "writing")
        end_turn(runner, cmd)
        wait(lambda: client.get("/api/agents/scout").json()["status"] == "idle")
        assert activity() is None


@pytest.mark.parametrize("data,expected", [
    ({"kind": "tool_call", "data": {"toolCallId": "a", "title": "x" * 200}}, "x" * 119 + "…"),
    ({"kind": "tool_call", "data": {"toolCallId": "a"}}, "using a tool"),
])
def test_activity_titles(data, expected):
    import asyncio

    from valet.control.activity import Activity

    async def run():
        seen = []

        async def emit(agent):
            seen.append(agent)
        a = Activity(emit)
        a.note("scout", data["kind"], data["data"])
        await asyncio.sleep(0)
        return a.get("scout"), seen
    what, seen = asyncio.run(run())
    assert what == expected and seen == ["scout"]


def test_activity_frames_are_throttled():
    import asyncio

    from valet.control import activity as mod

    async def run():
        seen = []

        async def emit(agent):
            seen.append(agent)
        a = mod.Activity(emit)
        for i in range(20):
            a.note("scout", "tool_call", {"toolCallId": str(i), "title": f"step {i}"})
        await asyncio.sleep(0.01)
        assert seen == ["scout"]                   # the first at once, the rest held back
        await asyncio.sleep(mod.THROTTLE + 0.1)
        return seen, a.get("scout")
    seen, last = asyncio.run(run())
    assert seen == ["scout", "scout"] and last == "step 19"        # one trailing frame carries the last change
