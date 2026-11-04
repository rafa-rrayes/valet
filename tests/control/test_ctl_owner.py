"""valet.owner (message, ask, the owner's answers) and valet.agents send_message, through the gateway as an agent calls
them, with the test playing the runner."""
from fastapi.testclient import TestClient

from tests.control.always_on import (
    agent_yaml, answer_new, answer_prompt, control, end_turn, fake_runner, idle, session, start, text_of, wait,
)
from tests.control.conftest import PASSWORD
from valet.control.__main__ import add_runner, set_password
from valet.control.app import create_app
from valet.control.gateway import owner


def call(client, pool, agent, tool, args):
    """A gateway call as `agent` makes it, in another thread (it may wait on the test's runner)."""
    return pool.submit(client.portal.call, control(client).gateway.call, agent, tool, args)


def text(result) -> str:
    return result.content[0].text


def pending(client):
    return client.get("/api/inbox").json()


# ---------- valet.owner ----------

def test_owner_tools_are_allowed_at_every_level(client, connect, rpc):
    with connect() as runner:
        start(client, rpc, runner, level="locked")
        tools = client.get("/api/agents/scout/tools").json()
        decisions = {t["name"]: t["decision"] for s in tools["servers"] for t in s["tools"]}
        assert {k: v for k, v in decisions.items() if k.startswith("valet_owner")} == {
            "valet_owner__message": "allow", "valet_owner__ask": "allow", "valet_owner__schedule_wakeup": "allow",
            "valet_owner__wakeups": "allow", "valet_owner__cancel_wakeup": "allow"}
        assert decisions["valet_agents__send_message"] == "deny"        # the gateway's rule still applies there


def test_an_items_title_is_its_first_line_without_markdown():
    """The title is what a push and the owner webhook's Slack or Discord line show: plain text."""
    assert owner._title("**3 new orders** today\n\n| a | b |") == "3 new orders today"
    assert owner._title("## Backup done") == "Backup done"
    assert owner._title("- [the PR](https://example.com/1) is `merged`") == "the PR is merged"
    assert owner._title("snake_case_name stays") == "snake_case_name stays"
    assert owner._title("x" * 200) == "x" * 119 + "…"


def test_a_message_waits_in_the_inbox_until_dismissed(client, connect, rpc, pool):
    with connect() as runner:
        start(client, rpc, runner, level="locked")
        result = call(client, pool, "scout", "valet_owner__message", {"text": "Backup done.\nAll 3 disks."}).result(5)
        assert not result.is_error and text(result) == "Sent: it's in the owner's inbox."
        [item] = pending(client)
        assert item["kind"] == "message" and item["title"] == "Backup done."
        assert item["text"] == "Backup done.\nAll 3 disks."
        assert item["options"] == [{"optionId": "dismiss", "name": "Dismiss", "kind": "reject_once"}]
        assert item["session_id"] is None
        a = client.get("/api/agents/scout").json()
        assert a["status"] == "idle"                    # a message doesn't make it "waiting on you"
        r = client.post(f"/api/inbox/{item['id']}/answer", json={"option_id": "dismiss", "text": "thanks"})
        assert r.status_code == 422 and r.json()["detail"] == "a message can only be dismissed"
        assert client.post(f"/api/inbox/{item['id']}/answer", json={}).status_code == 422
        r = client.post(f"/api/inbox/{item['id']}/answer", json={"option_id": "dismiss"})
        assert r.status_code == 200 and r.json()["status"] == "answered"
        assert r.json()["answer"] == {"outcome": "selected", "optionId": "dismiss"}
        assert pending(client) == []
        assert client.post(f"/api/inbox/{item['id']}/answer", json={"option_id": "dismiss"}).status_code == 409
        audit = client.get("/api/gateway/calls", params={"agent": "scout"}).json()["calls"]
        assert audit[0]["server"] == "valet.owner" and audit[0]["decision"] == "allow" and audit[0]["outcome"] == "ok"


def test_message_limits(client, connect, rpc, pool, monkeypatch):
    monkeypatch.setattr(owner, "PENDING", 2)
    with connect() as runner:
        start(client, rpc, runner)
        assert call(client, pool, "scout", "valet_owner__message", {"text": " "}).result(5).is_error
        long = call(client, pool, "scout", "valet_owner__message", {"text": "x" * (owner.TEXT_LIMIT + 1)}).result(5)
        assert long.is_error and "at most" in text(long)
        for i in range(2):
            assert not call(client, pool, "scout", "valet_owner__message", {"text": f"m{i}"}).result(5).is_error
        full = call(client, pool, "scout", "valet_owner__ask", {"question": "and?"}).result(5)
        assert full.is_error and "The owner has 2 messages and questions from you not yet read" in text(full)
        bad = call(client, pool, "scout", "valet_owner__ask", {"question": "q", "options": ["a", 3]}).result(5)
        assert bad.is_error


def test_a_question_answered_while_its_turn_runs_comes_back_to_that_session(client, connect, rpc, pool):
    with connect() as runner:
        start(client, rpc, runner)
        session(client, rpc, runner, "S1")
        r, cmd = rpc(runner, client.post, "/api/agents/scout/sessions/S1/prompt", json={"text": "deploy?"},
                     expect="session.prompt")
        result = call(client, pool, "scout", "valet_owner__ask",
                      {"question": "Deploy to production now?", "options": ["Yes", "Not today"]}).result(5)
        assert not result.is_error and text(result).startswith("Asked: it's in the owner's inbox.")
        assert "The answer will come as a message in this session" in text(result)
        [item] = pending(client)
        assert item["kind"] == "question" and item["session_id"] == "S1" and item["turn_id"] == cmd["turn_id"]
        assert item["options"] == [{"optionId": "option-1", "name": "Yes", "kind": "allow_once"},
                                   {"optionId": "option-2", "name": "Not today", "kind": "allow_once"},
                                   {"optionId": "dismiss", "name": "Dismiss", "kind": "reject_once"}]
        ev = client.get("/api/agents/scout/sessions/S1/events").json()["events"]
        req = [e for e in ev if e["kind"] == "permission_request"][0]
        assert req["data"]["kind"] == "question" and req["data"]["text"] == "Deploy to production now?"
        assert client.get("/api/agents/scout").json()["status"] == "working"     # not "waiting": it carried on

        end_turn(runner, cmd)                   # the turn ends; the question stays
        wait(lambda: idle(client, "S1"))
        assert [i["id"] for i in pending(client)] == [item["id"]]

        r = client.post(f"/api/inbox/{item['id']}/answer", json={"option_id": "nope"})
        assert r.status_code == 422
        r = client.post(f"/api/inbox/{item['id']}/answer", json={"option_id": "dismiss", "text": "x"})
        assert r.status_code == 422
        fut = pool.submit(client.post, f"/api/inbox/{item['id']}/answer",
                          json={"option_id": "option-2", "text": "the migration isn't reviewed"})
        cmd = answer_prompt(runner)
        r = fut.result(5)
        assert r.status_code == 200 and r.json()["answer"] == {"outcome": "selected", "optionId": "option-2",
                                                                 "text": "the migration isn't reviewed"}
        assert cmd["session_id"] == "S1"
        said = text_of(cmd)
        assert said.startswith("Valet: The owner answered the question you asked at ")
        assert said.endswith("\n\nYour question: Deploy to production now?\n\nThe owner's choice: Not today\n\n"
                             "The owner's words: the migration isn't reviewed")
        ev = client.get("/api/agents/scout/sessions/S1/events").json()["events"]
        last = [e for e in ev if e["kind"] == "user_message"][-1]
        assert last["data"]["source"] == "answer" and last["data"]["label"] == "The owner's answer"


def test_an_answer_in_words_goes_to_the_main_session_and_waits_for_a_stopped_agent(client, connect, rpc, pool):
    with connect() as runner:
        start(client, rpc, runner)
        result = call(client, pool, "scout", "valet_owner__ask", {"question": "Which city?"}).result(5)  # no turn
        assert "The answer will come as a message in your main session" in text(result)     # not "this session"
        [item] = pending(client)
        assert item["session_id"] is None and item["options"] == [owner.DISMISS]
        r, _ = rpc(runner, client.post, "/api/agents/scout/stop", expect="agent.stop")
        runner.status("scout", "stopped")
        r = client.post(f"/api/inbox/{item['id']}/answer", json={"text": "Lisbon"})
        assert r.status_code == 200 and r.json()["answer"] == {"outcome": "text", "text": "Lisbon"}
        db = control(client).db
        [q] = db.query("SELECT * FROM prompt_queue")
        assert q["session_id"] is None and q["source"] == "answer" and q["text"].endswith("The owner's answer: Lisbon")
        assert client.get("/api/agents/scout").json()["queued"] == 1

        start(client, rpc, runner, create=False)    # back: a main session is made, and the answer goes there
        answer_new(runner, "M1")
        cmd = answer_prompt(runner)
        assert cmd["session_id"] == "M1" and text_of(cmd).endswith("The owner's answer: Lisbon")
        assert client.get("/api/agents/scout").json()["main_session"] == "M1"


def test_a_dismissed_question_prompts_nobody(client, connect, rpc, pool):
    with connect() as runner:
        start(client, rpc, runner)
        call(client, pool, "scout", "valet_owner__ask", {"question": "Lunch?"}).result(5)
        [item] = pending(client)
        r = client.post(f"/api/inbox/{item['id']}/answer", json={"option_id": "dismiss"})
        assert r.status_code == 200
        assert control(client).db.one("SELECT COUNT(*) AS n FROM turns")["n"] == 0
        assert control(client).db.one("SELECT COUNT(*) AS n FROM prompt_queue")["n"] == 0


def test_messages_and_questions_outlive_a_restart_and_the_agents_stop(cfg, rpc, pool):
    set_password(cfg, PASSWORD)
    token = add_runner(cfg, "rr")
    with TestClient(create_app(cfg)) as first:
        assert first.post("/api/login", json={"password": PASSWORD}).status_code == 200
        with fake_runner(first, token) as runner:
            start(first, rpc, runner)
            call(first, pool, "scout", "valet_owner__message", {"text": "hi"}).result(5)
            call(first, pool, "scout", "valet_owner__ask", {"question": "ok?"}).result(5)
            r, _ = rpc(runner, first.post, "/api/agents/scout/stop", expect="agent.stop")
            runner.status("scout", "stopped")
            assert len(pending(first)) == 2
    with TestClient(create_app(cfg)) as again:
        assert again.post("/api/login", json={"password": PASSWORD}).status_code == 200
        items = pending(again)
        assert sorted(i["kind"] for i in items) == ["message", "question"]
        q = next(i for i in items if i["kind"] == "question")
        assert again.post(f"/api/inbox/{q['id']}/answer", json={"text": "yes"}).status_code == 200


# ---------- send_message ----------

def test_agents_message_each_other(client, connect, rpc, pool):
    with connect() as runner:
        start(client, rpc, runner, "scout", level="trusted")
        start(client, rpc, runner, "bee", level="trusted")
        client.put("/api/agents/bee/definition", json={"definition": {**client.get("/api/agents/bee").json()[
            "definition"], "display_name": "Busy Bee"}})
        fut = call(client, pool, "bee", "valet_agents__send_message", {"agent": "scout", "text": "The build is red."})
        answer_new(runner, "M1")
        cmd = answer_prompt(runner)
        result = fut.result(5)
        assert not result.is_error and result.structured_content == {
            "delivered": "started", "agent": "scout", "detail": "scout is reading it now"}
        assert cmd["agent"] == "scout" and cmd["session_id"] == "M1"
        assert text_of(cmd) == ("Valet: a message from agent `bee` (Busy Bee), another of the owner's agents. To reply, use "
                                "valet.agents send_message to bee.\n\nThe build is red.")
        ev = client.get("/api/agents/scout/sessions/M1/events").json()["events"][0]
        assert ev["data"]["source"] == "agent:bee" and ev["data"]["label"] == "From bee"
        again = call(client, pool, "bee", "valet_agents__send_message", {"agent": "scout", "text": "Still red."})
        assert again.result(5).structured_content["delivered"] == "queued"
        end_turn(runner, cmd)
        assert text_of(answer_prompt(runner)).endswith("Still red.")


def test_send_message_refusals(client, connect, rpc, pool, monkeypatch):
    from valet.control.gateway import builtin
    monkeypatch.setattr(builtin, "SEND_LIMIT", 1)
    with connect() as runner:
        start(client, rpc, runner, "scout", level="trusted")
        client.post("/api/agents", json={"name": "bee", "agent_yaml": agent_yaml("bee")})
        cases = [({"agent": "ghost", "text": "x"}, "no agent named 'ghost'"),
                 ({"agent": "scout", "text": "x"}, "that's you"),
                 ({"agent": "bee", "text": "x"}, "bee can't take a message now: bee is stopped."),
                 ({"agent": "bee", "text": ""}, "needs `text`")]
        for args, says in cases:
            result = call(client, pool, "scout", "valet_agents__send_message", args).result(5)
            assert result.is_error and says in text(result), (args, text(result))
        start(client, rpc, runner, "bee", create=False)
        fut = call(client, pool, "scout", "valet_agents__send_message", {"agent": "bee", "text": "one"})
        answer_new(runner, "B1")
        answer_prompt(runner)
        assert not fut.result(5).is_error
        limited = call(client, pool, "scout", "valet_agents__send_message", {"agent": "bee", "text": "two"}).result(5)
        assert limited.is_error and "You've sent 1 messages" in text(limited)
