"""session/request_permission -> inbox item -> the owner answers -> the harness gets the answer."""
import pytest

from tests.e2e.stack import end_of, eventually, kinds, texts


async def ask(stack):
    """Ready agent + session + a turn that is waiting on a permission request. -> (sid, turn, item)."""
    await stack.ready_agent()
    sid = (await stack.new_session())["id"]
    turn = await stack.prompt("echo", sid, "permission")
    [item] = await stack.wait_pending()
    return sid, turn, item


@pytest.mark.parametrize("option,status,reply", [("allow", "completed", "allowed"), ("reject", "failed", "rejected")])
async def test_answer(stack, option, status, reply):
    feed = await stack.feed()
    sid, turn, item = await ask(stack)

    assert item["kind"] == "permission" and item["status"] == "pending"
    assert item["agent"] == "echo" and item["session_id"] == sid and item["turn_id"] == turn
    assert item["title"] == "Write hello.txt"
    assert item["tool_call"]["title"] == "Write hello.txt"
    assert [(o["optionId"], o["kind"]) for o in item["options"]] == [
        ("allow", "allow_once"), ("allow-always", "allow_always"), ("reject", "reject_once")]
    assert item["answer"] is None and item["answered_at"] is None
    assert (await stack.http.get(f"/api/inbox/{item['id']}")).json() == item

    # Live: the inbox frame, the inline permission_request event, and the agent waiting.
    await feed.wait(lambda f: f["type"] == "inbox" and f["item"]["id"] == item["id"], what="inbox frame")
    req = await feed.wait(lambda f: f["type"] == "event" and f["event"]["kind"] == "permission_request",
                          what="permission_request event")
    assert req["event"]["data"]["requestId"] == item["id"]
    assert req["event"]["data"]["options"] == item["options"]
    assert (await stack.agent("echo"))["status"] == "waiting"

    r = await stack.http.post(f"/api/inbox/{item['id']}/answer", json={"option_id": option})
    assert r.status_code == 200, r.text
    answered = r.json()
    assert answered["status"] == "answered" and answered["answered_at"]
    assert answered["answer"] == {"outcome": "selected", "optionId": option}

    evs = await stack.turn("echo", sid, turn)
    # The answer event is written when the runner acks, which may race the harness's next update.
    [answer] = [e for e in evs if e["kind"] == "permission_answer"]
    rest = [e for e in evs if e is not answer]
    assert kinds(rest) == ["user_message", "tool_call", "permission_request", "tool_call_update", "agent_message",
                           "turn_end"]
    assert answer["id"] > rest[2]["id"]
    assert answer["data"] == {"requestId": item["id"], "outcome": {"outcome": "selected", "optionId": option},
                              "status": "answered"}
    assert rest[3]["data"]["status"] == status
    assert rest[3]["data"]["toolCallId"] == rest[1]["data"]["toolCallId"]
    assert texts(evs) == reply
    assert end_of(evs) == {"stopReason": "end_turn", "error": None}

    assert await stack.pending() == []
    r = await stack.http.get("/api/inbox", params={"status": "all"})
    assert [(i["id"], i["status"]) for i in r.json()] == [(item["id"], "answered")]
    assert (await stack.agent("echo"))["status"] == "idle"
    await feed.wait(lambda f: f["type"] == "inbox" and f["item"]["status"] == "answered", what="answered frame")

    # Answering twice: not pending any more.
    r = await stack.http.post(f"/api/inbox/{item['id']}/answer", json={"option_id": option})
    assert r.status_code == 409


async def test_answer_must_be_an_option(stack):
    sid, turn, item = await ask(stack)
    r = await stack.http.post(f"/api/inbox/{item['id']}/answer", json={"option_id": "maybe"})
    assert r.status_code == 422
    assert (await stack.http.get(f"/api/inbox/{item['id']}")).json()["status"] == "pending"
    r = await stack.http.post("/api/inbox/nope/answer", json={"option_id": "allow"})
    assert r.status_code == 404
    r = await stack.http.post(f"/api/inbox/{item['id']}/answer", json={"option_id": "allow"})
    assert r.status_code == 200
    assert texts(await stack.turn("echo", sid, turn)) == "allowed"


async def test_cancel_while_waiting(stack):
    feed = await stack.feed()
    sid, turn, item = await ask(stack)
    r = await stack.http.post(f"/api/agents/echo/sessions/{sid}/cancel")
    assert r.status_code == 200

    evs = await stack.turn("echo", sid, turn)
    assert end_of(evs) == {"stopReason": "cancelled", "error": None}
    assert texts(evs) == "cancelled"

    async def closed():
        evs = await stack.events("echo", sid)
        return [e["data"] for e in evs if e["kind"] == "permission_answer"]
    assert await eventually(closed, what="permission_answer") == [
        {"requestId": item["id"], "outcome": None, "status": "expired"}]

    assert (await stack.http.get(f"/api/inbox/{item['id']}")).json()["status"] == "expired"
    assert await stack.pending() == []
    await feed.wait(lambda f: f["type"] == "inbox" and f["item"]["status"] == "expired", what="expired frame")
    r = await stack.http.post(f"/api/inbox/{item['id']}/answer", json={"option_id": "allow"})
    assert r.status_code == 409


async def test_pending_survives_client_reload(stack):
    first = await stack.feed()
    sid, turn, item = await ask(stack)
    await first.close()

    # A reloaded client gets the pending item in its hello and from the inbox endpoint.
    second = await stack.feed()
    assert [i["id"] for i in second.hello["inbox"]] == [item["id"]]
    assert [a["status"] for a in second.hello["agents"] if a["name"] == "echo"] == ["waiting"]
    assert [i["id"] for i in await stack.pending()] == [item["id"]]
    # The session's history still shows the request inline.
    evs = await stack.events("echo", sid)
    assert [e["data"]["requestId"] for e in evs if e["kind"] == "permission_request"] == [item["id"]]

    r = await stack.http.post(f"/api/inbox/{item['id']}/answer", json={"option_id": "allow"})
    assert r.status_code == 200
    assert texts(await stack.turn("echo", sid, turn)) == "allowed"


async def test_pending_survives_control_restart(stack):
    """The runner keeps the ACP request open while the control plane is down; the item stays pending in the DB
    and the hello on reconnect lists it, so the owner can still answer it."""
    sid, turn, item = await ask(stack)
    await stack.restart_control()
    got = (await stack.http.get(f"/api/inbox/{item['id']}")).json()
    assert got["status"] == "pending"
    r = await stack.http.post(f"/api/inbox/{item['id']}/answer", json={"option_id": "allow"})
    assert r.status_code == 200, r.text
    evs = await stack.turn("echo", sid, turn)
    assert texts(evs) == "allowed"
    assert end_of(evs) == {"stopReason": "end_turn", "error": None}


async def test_stop_agent_expires_pending(stack):
    sid, turn, item = await ask(stack)
    r = await stack.http.post("/api/agents/echo/stop")
    assert r.status_code == 200
    await stack.wait_state("echo", "stopped")
    agent = await stack.agent("echo")
    assert agent["desired"] == "stopped" and agent["status"] == "stopped"

    async def expired():
        return (await stack.http.get(f"/api/inbox/{item['id']}")).json()["status"] == "expired"
    await eventually(expired, what="item expired")
    evs = await stack.turn("echo", sid, turn)
    assert end_of(evs)["error"]      # "agent stopped" from the runner, or "interrupted"
    assert (await stack.session("echo", sid))["state"] == "idle"

    r = await stack.http.post(f"/api/inbox/{item['id']}/answer", json={"option_id": "allow"})
    assert r.status_code == 409
