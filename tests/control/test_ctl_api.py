"""The control plane over HTTP and its two WebSockets, with the test playing the runner."""
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from valet.control.__main__ import add_runner, revoke_runner, set_password
from valet.control.app import create_app
from valet.shared.definition import TEMPLATE

PASSWORD = "correct horse battery"
HOST_YAML = "name: scout\nharness: claude\nmodel: m1\ncomputer:\n  level: host\n"


# ---------- auth and plumbing ----------
def test_login_flow(cfg):
    with TestClient(create_app(cfg)) as c:
        assert c.get("/api/me").json() == {"owner": False, "setup": False, "api": 1, "version": "0.2.0"}
        assert c.post("/api/login", json={"password": PASSWORD}).status_code == 409
        set_password(cfg, PASSWORD)
        assert c.get("/api/agents").status_code == 401
        assert c.get("/api/agents").json() == {"detail": "sign in first"}
        assert c.post("/api/login", json={"password": "wrong password!"}).status_code == 401
        r = c.post("/api/login", json={"password": PASSWORD, "client": "web"})
        assert r.json() == {"ok": True}
        cookie = r.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie and "secure" not in cookie
        assert c.get("/api/me").json()["owner"] is True
        assert c.get("/api/agents").json() == []
        assert c.post("/api/logout").json() == {"ok": True}
        assert c.get("/api/agents").status_code == 401

        native = c.post("/api/login", json={"password": PASSWORD, "client": "native", "device": "phone"}).json()
        assert native["token"].startswith("vown_") and native["expires_in"] == 30 * 86400
        c.cookies.clear()
        assert c.get("/api/agents", headers={"Authorization": f"Bearer {native['token']}"}).status_code == 200
        assert c.get("/api/me", headers={"Authorization": f"Bearer {native['token']}"}).json()["owner"] is True


def test_login_rate_limit(cfg):
    set_password(cfg, PASSWORD)
    with TestClient(create_app(cfg)) as c:
        for _ in range(5):
            assert c.post("/api/login", json={"password": "nope nope nope"}).status_code == 401
        assert c.post("/api/login", json={"password": PASSWORD}).status_code == 429
        # A client-supplied cf-connecting-ip isn't trusted (VALET_TRUST_CF_IP is off): rotating it doesn't help.
        other = {"cf-connecting-ip": "203.0.113.9"}
        assert c.post("/api/login", json={"password": PASSWORD}, headers=other).status_code == 429


def test_headers_and_no_docs(client):
    r = client.get("/api/me")
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert "default-src 'self'" in r.headers["content-security-policy"]
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_ui_is_revalidated_on_every_load(client):
    """Without this, browsers keep a stale app.js after an update (seen in the M0 test pass)."""
    for path in ("/", "/static/app.js", "/static/markdown.js", "/static/app.css"):
        r = client.get(path)
        assert r.status_code == 200 and r.headers["cache-control"] == "no-cache", path
        if path.startswith("/static/"):     # revalidation is a 304, not a re-download
            assert client.get(path, headers={"if-none-match": r.headers["etag"]}).status_code == 304, path


# ---------- definitions ----------
def test_agents_crud(client):
    r = client.post("/api/agents", json={"name": "scout"})
    assert r.status_code == 201
    a = r.json()
    assert a["files"]["agent.yaml"] == TEMPLATE.format(name="scout")
    assert a["definition"]["name"] == "scout" and a["level"] == "container" and a["harness"] == "claude"
    assert a["status"] == "stopped" and a["desired"] == "stopped" and a["connected"] is False
    commit = a["commit"]
    assert client.post("/api/agents", json={"name": "scout"}).status_code == 409
    assert client.post("/api/agents", json={"name": "Bad Name"}).status_code == 422
    assert client.post("/api/agents", json={"name": "x", "agent_yaml": "name: y\n"}).status_code == 422
    assert client.get("/api/agents/ghost").status_code == 404
    assert client.get("/api/agents/ghost/sessions").status_code == 404
    assert [x["name"] for x in client.get("/api/agents").json()] == ["scout"]

    r = client.put("/api/agents/scout/files/persona.md", json={"text": "Terse.\n"})
    assert r.status_code == 200 and r.json()["commit"] != commit
    r = client.put("/api/agents/scout/files/agent.yaml", json={"text": "name: scout\nnope: 1\n"})
    assert r.status_code == 422 and "nope" in r.json()["detail"]
    assert client.put("/api/agents/scout/files/other.txt", json={"text": "x"}).status_code == 404
    assert client.put("/api/agents/ghost/files/persona.md", json={"text": "x"}).status_code == 404
    d = client.get("/api/agents/scout").json()
    assert d["files"] == {"agent.yaml": TEMPLATE.format(name="scout"), "persona.md": "Terse.\n"}
    r = client.put("/api/agents/scout/files/agent.yaml", json={"text": HOST_YAML})
    assert client.get("/api/agents/scout").json()["level"] == "host"


def test_invalid_definition_on_disk(client, cfg):
    client.post("/api/agents", json={"name": "scout"})
    (cfg.definitions / "agents" / "scout" / "agent.yaml").write_text("name: scout\nharness: [\n")
    a = client.get("/api/agents/scout").json()
    assert a["definition"] is None and a["level"] is None and "YAML" in a["definition_error"]
    assert client.post("/api/agents/scout/start").status_code == 422


# ---------- runner socket ----------
def test_runner_auth(client, runner_token, cfg):
    with client.websocket_connect("/ws/runner", headers={"Authorization": "Bearer vrun_bogus"}) as ws:
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_json()
        assert e.value.code == 4401
    with client.websocket_connect("/ws/runner", headers={"Authorization": f"Bearer {runner_token}"}) as ws:
        ws.send_json({"type": "agent.status", "agent": "x", "state": "running"})
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_json()
        assert e.value.code == 4400
    with client.websocket_connect("/ws/runner", headers={"Authorization": f"Bearer {runner_token}"}) as ws:
        ws.send_json({"type": "hello", "protocol": 2, "version": "x"})
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_json()
        assert e.value.code == 4400
    revoke_runner(cfg, "rr")
    with client.websocket_connect("/ws/runner", headers={"Authorization": f"Bearer {runner_token}"}) as ws:
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_json()
        assert e.value.code == 4401


def test_runner_registry_and_replace(client, connect):
    assert client.get("/api/runners").json() == [{"name": "rr", "connected": False, "last_seen": None,
                                                  "version": None, "harnesses": []}]
    with connect() as first:
        r = client.get("/api/runners").json()[0]
        assert r["connected"] is True and r["version"] == "test" and r["last_seen"]
        with connect() as second:
            with pytest.raises(WebSocketDisconnect) as e:
                first.recv()
            assert e.value.code == 4409
            assert client.get("/api/runners").json()[0]["connected"] is True
            assert second.ws is not first.ws
    assert client.get("/api/runners").json()[0]["connected"] is False


def _running(client, connect, rpc, runner, name="scout"):
    """Create an agent and start it on `runner`; returns the agent.start command."""
    client.post("/api/agents", json={"name": name, "agent_yaml": HOST_YAML.replace("scout", name)})
    r, cmd = rpc(runner, client.post, f"/api/agents/{name}/start", expect="agent.start")
    assert r.status_code == 200 and r.json()["desired"] == "running" and r.json()["runner"] == "rr"
    runner.status(name, "starting")
    runner.status(name, "running", capabilities={"loadSession": True}, agent_info={"name": "fake"})
    return cmd


def _session(client, rpc, runner, sid="S1", name="scout"):
    r, cmd = rpc(runner, client.post, f"/api/agents/{name}/sessions", expect="session.new",
                 result={"session_id": sid, "modes": None})
    assert r.status_code == 201, r.text
    assert cmd["agent"] == name
    return r.json()


def _wait(predicate, timeout=5.0):
    import time
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not reached")


def test_conversation_with_permission(client, connect, rpc):
    with client.websocket_connect("/ws/client") as live, connect() as runner:
        hello = live.receive_json()
        assert hello["type"] == "hello" and hello["last_event_id"] == 0 and hello["inbox"] == []
        start = _running(client, connect, rpc, runner)
        assert start["definition"]["name"] == "scout" and start["definition"]["model"] == "m1"
        assert "persona.md" in start["files"]
        _wait(lambda: client.get("/api/agents/scout").json()["status"] == "idle")
        assert client.get("/api/agents/scout").json()["capabilities"] == {"loadSession": True}

        s = _session(client, rpc, runner)
        assert s == {**s, "agent": "scout", "id": "S1", "title": None, "state": "idle", "turn_id": None}

        r, cmd = rpc(runner, client.post, "/api/agents/scout/sessions/S1/prompt", json={"text": "write a file"},
                     expect="session.prompt")
        assert r.status_code == 202
        turn = r.json()["turn_id"]
        assert cmd["turn_id"] == turn and cmd["prompt"] == [{"type": "text", "text": "write a file"}]
        s = client.get("/api/agents/scout/sessions/S1").json()
        assert s["state"] == "working" and s["turn_id"] == turn and s["title"] == "write a file"
        assert client.get("/api/agents/scout").json()["status"] == "working"
        # A prompt while the turn runs waits in the session's queue (tests/control/test_ctl_queue.py has the rest);
        # taken out again here, so this conversation goes on as before.
        r = client.post("/api/agents/scout/sessions/S1/prompt", json={"text": "again"})
        assert r.status_code == 202 and set(r.json()) == {"queued", "session_id"}
        assert client.delete(f"/api/agents/scout/sessions/S1/queue/{r.json()['queued']}").json() == {"ok": True}

        def upd(update, replay=False):
            runner.send(type="session.update", agent="scout", session_id="S1", turn_id=turn, replay=replay,
                        update=update)
        upd({"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": "dup"}})
        upd({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "old"}}, replay=True)
        upd({"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "hmm"}})
        upd({"sessionUpdate": "tool_call", "toolCallId": "c1", "title": "Write hello.txt", "kind": "edit",
             "status": "pending"})
        options = [{"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                   {"optionId": "reject", "name": "Reject", "kind": "reject_once"}]
        runner.send(type="permission.request", agent="scout", session_id="S1", turn_id=turn, request_id="R1",
                    tool_call={"toolCallId": "c1", "title": "Write hello.txt"}, options=options)
        _wait(lambda: client.get("/api/inbox").json())
        item = client.get("/api/inbox").json()[0]
        assert item["id"] == "R1" and item["status"] == "pending" and item["title"] == "Write hello.txt"
        assert item["options"] == options and item["session_id"] == "S1" and item["turn_id"] == turn
        assert client.get("/api/agents/scout").json()["status"] == "waiting"
        assert client.get("/api/inbox/R1").json() == item
        assert client.post("/api/inbox/R1/answer", json={"option_id": "maybe"}).status_code == 422

        r, cmd = rpc(runner, client.post, "/api/inbox/R1/answer", json={"option_id": "allow"},
                     expect="permission.answer")
        assert r.status_code == 200 and r.json()["status"] == "answered"
        assert r.json()["answer"] == {"outcome": "selected", "optionId": "allow"}
        assert cmd["request_id"] == "R1" and cmd["outcome"] == {"outcome": "selected", "optionId": "allow"}
        assert client.post("/api/inbox/R1/answer", json={"option_id": "allow"}).status_code == 409
        assert client.get("/api/inbox").json() == []
        assert len(client.get("/api/inbox?status=all").json()) == 1

        upd({"sessionUpdate": "tool_call_update", "toolCallId": "c1", "status": "completed"})
        upd({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "done"}})
        runner.send(type="turn.end", agent="scout", session_id="S1", turn_id=turn, stop_reason="end_turn",
                    error=None)
        _wait(lambda: client.get("/api/agents/scout/sessions/S1").json()["state"] == "idle")
        assert client.get("/api/agents/scout").json()["status"] == "idle"

        events = client.get("/api/agents/scout/sessions/S1/events").json()
        assert events["more"] is False
        kinds = [e["kind"] for e in events["events"]]
        assert kinds == ["user_message", "agent_thought", "tool_call", "permission_request", "permission_answer",
                         "tool_call_update", "agent_message", "turn_end"]
        ev = events["events"]
        assert ev[0]["data"] == {"content": {"type": "text", "text": "write a file"}, "source": "owner", "label": "You"}
        assert ev[2]["data"]["toolCallId"] == "c1" and "sessionUpdate" not in ev[2]["data"]
        assert ev[3]["data"] == {"requestId": "R1", "toolCall": {"toolCallId": "c1", "title": "Write hello.txt"},
                                 "options": options}
        assert ev[4]["data"] == {"requestId": "R1", "outcome": {"outcome": "selected", "optionId": "allow"},
                                 "status": "answered"}
        assert ev[-1]["data"] == {"stopReason": "end_turn", "error": None}
        assert all(e["turn_id"] == turn for e in ev)

        page = client.get(f"/api/agents/scout/sessions/S1/events?after={ev[2]['id']}&limit=2").json()
        assert [e["id"] for e in page["events"]] == [ev[3]["id"], ev[4]["id"]] and page["more"] is True
        assert client.get("/api/agents/scout/sessions/S1/events?limit=0").status_code == 422
        assert client.get("/api/agents/scout/sessions/S1/events?limit=2001").status_code == 422

        # the live socket saw every event, in order, plus state frames
        seen = []
        while len([m for m in seen if m["type"] == "event"]) < len(ev):
            seen.append(live.receive_json())
        assert [m["event"]["id"] for m in seen if m["type"] == "event"] == [e["id"] for e in ev]
        types = {m["type"] for m in seen}
        assert {"agent", "session", "inbox", "runner", "event"} <= types

        sessions = client.get("/api/agents/scout/sessions").json()
        assert [x["id"] for x in sessions] == ["S1"] and sessions[0]["turn_id"] is None


def test_harness_title_replaces_prompt_title(client, connect, rpc):
    """The first prompt titles a session until the harness's session_info says otherwise; then the harness's wins."""
    with client.websocket_connect("/ws/client") as live, connect() as runner:
        live.receive_json()
        _running(client, connect, rpc, runner)
        _session(client, rpc, runner)
        r, _ = rpc(runner, client.post, "/api/agents/scout/sessions/S1/prompt", json={"text": "  " + "x" * 100},
                   expect="session.prompt")
        turn = r.json()["turn_id"]

        def title():
            return client.get("/api/agents/scout/sessions/S1").json()["title"]
        assert title() == "x" * 80

        def info(data, replay=False):
            runner.send(type="session.update", agent="scout", session_id="S1", turn_id=turn, replay=replay,
                        update={"sessionUpdate": "session_info_update", **data})
        info({"title": None, "updatedAt": "2026-10-30T15:35:53.949Z"})  # no title: the fallback stays
        info({"title": "Old", "updatedAt": "2026-10-30T15:35:53.949Z"}, replay=True)  # already stored: ignored
        info({"title": "Model identity", "updatedAt": "2026-10-30T15:35:53.949Z"})
        info({"title": "Model identity", "updatedAt": "2026-10-30T15:36:41.349Z"})  # unchanged: no frame
        _wait(lambda: title() == "Model identity")
        runner.send(type="turn.end", agent="scout", session_id="S1", turn_id=turn, stop_reason="end_turn", error=None)
        _wait(lambda: client.get("/api/agents/scout/sessions/S1").json()["state"] == "idle")

        rpc(runner, client.post, "/api/agents/scout/sessions/S1/prompt", json={"text": "next"}, expect="session.prompt")
        assert title() == "Model identity"   # a later prompt doesn't take it back
        kinds = [e["kind"] for e in client.get("/api/agents/scout/sessions/S1/events").json()["events"]]
        assert kinds.count("session_info") == 3

        frames = []
        while len(frames) < 5:
            m = live.receive_json()
            if m["type"] == "session":
                frames.append((m["session"]["title"], m["session"]["state"]))
        # created, first prompt, the harness's title (once), turn end, second prompt
        assert frames == [(None, "idle"), ("x" * 80, "working"), ("Model identity", "working"),
                          ("Model identity", "idle"), ("Model identity", "working")]


def test_answer_after_request_gone(client, connect, rpc):
    with connect() as runner:
        _running(client, connect, rpc, runner)
        _session(client, rpc, runner)
        r, _ = rpc(runner, client.post, "/api/agents/scout/sessions/S1/prompt", json={"text": "hi"},
                   expect="session.prompt")
        turn = r.json()["turn_id"]
        for rid in ("R1", "R2"):
            runner.send(type="permission.request", agent="scout", session_id="S1", turn_id=turn, request_id=rid,
                        tool_call={}, options=[{"optionId": "ok", "name": "OK", "kind": "allow_once"}])
        _wait(lambda: len(client.get("/api/inbox").json()) == 2)
        assert client.get("/api/inbox").json()[0]["title"] == "Permission request"
        r, _ = rpc(runner, client.post, "/api/inbox/R1/answer", json={"option_id": "ok"},
                   expect="permission.answer", error="unknown request")
        assert r.status_code == 410
        assert r.json()["item"]["status"] == "expired"
        runner.send(type="permission.closed", agent="scout", session_id="S1", request_id="R2", reason="cancelled")
        _wait(lambda: client.get("/api/inbox").json() == [])
        runner.send(type="permission.request", agent="scout", session_id="S1", turn_id=turn, request_id="R3",
                    tool_call={}, options=[{"optionId": "ok", "name": "OK", "kind": "allow_once"}])
        _wait(lambda: client.get("/api/inbox").json())
    # The runner is away: it still holds the request, so the item stays pending and the owner can retry.
    r = client.post("/api/inbox/R3/answer", json={"option_id": "ok"})
    assert r.status_code == 503
    assert client.get("/api/inbox/R3").json()["status"] == "pending"
    events = client.get("/api/agents/scout/sessions/S1/events").json()["events"]
    answers = [e["data"] for e in events if e["kind"] == "permission_answer"]
    assert answers == [{"requestId": rid, "outcome": None, "status": "expired"} for rid in ("R1", "R2")]
    req = {"agent": "scout", "session_id": "S1", "turn_id": turn, "request_id": "R3", "tool_call": {},
           "options": [{"optionId": "ok", "name": "OK", "kind": "allow_once"}]}
    with connect(agents=[{"agent": "scout", "state": "running"}],
                 turns=[{"agent": "scout", "session_id": "S1", "turn_id": turn}], pending=[req]) as runner:
        r, _ = rpc(runner, client.post, "/api/inbox/R3/answer", json={"option_id": "ok"},
                   expect="permission.answer")
        assert r.status_code == 200 and r.json()["status"] == "answered"


def test_answer_reply_lost(client, connect, rpc, pool):
    """The answer reached the harness but the link dropped before the reply: the item stays pending (503), and
    the runner's resent permission.closed "answered" records it; a retry then returns the answered item."""
    with connect(boot="B1") as runner:
        _running(client, connect, rpc, runner)
        _session(client, rpc, runner)
        r, _ = rpc(runner, client.post, "/api/agents/scout/sessions/S1/prompt", json={"text": "hi"},
                   expect="session.prompt")
        turn = r.json()["turn_id"]
        runner.send(type="permission.request", agent="scout", session_id="S1", turn_id=turn, request_id="R1",
                    tool_call={}, options=[{"optionId": "ok", "name": "OK", "kind": "allow_once"}], seq=1)
        _wait(lambda: client.get("/api/inbox").json())
        fut = pool.submit(client.post, "/api/inbox/R1/answer", json={"option_id": "ok"})
        runner.recv("permission.answer")
    assert fut.result(timeout=10).status_code == 503
    assert client.get("/api/inbox/R1").json()["status"] == "pending"
    outcome = {"outcome": "selected", "optionId": "ok"}
    with connect(boot="B1", seq=2, agents=[{"agent": "scout", "state": "running"}],
                 turns=[{"agent": "scout", "session_id": "S1", "turn_id": turn}], last_seq=1) as runner:
        runner.send(type="permission.closed", agent="scout", session_id="S1", request_id="R1", reason="answered",
                    outcome=outcome, seq=2)
        _wait(lambda: client.get("/api/inbox/R1").json()["status"] == "answered")
        assert client.get("/api/inbox/R1").json()["answer"] == outcome
        assert client.post("/api/inbox/R1/answer", json={"option_id": "ok"}).status_code == 409
    events = client.get("/api/agents/scout/sessions/S1/events").json()["events"]
    assert [e["data"]["status"] for e in events if e["kind"] == "permission_answer"] == ["answered"]


def test_prompt_refused_and_cancel(client, connect, rpc):
    with connect() as runner:
        _running(client, connect, rpc, runner)
        _session(client, rpc, runner)
        r, _ = rpc(runner, client.post, "/api/agents/scout/sessions/S1/prompt", json={"text": "hi"},
                   expect="session.prompt", error="not running")
        assert r.status_code == 503
        s = client.get("/api/agents/scout/sessions/S1").json()
        assert s["state"] == "idle" and s["turn_id"] is None
        last = client.get("/api/agents/scout/sessions/S1/events").json()["events"][-1]
        assert last["kind"] == "turn_end" and last["data"] == {"stopReason": None, "error": "not running"}
        r, _ = rpc(runner, client.post, "/api/agents/scout/sessions/S1/prompt", json={"text": "hi"},
                   expect="session.prompt", error="cannot resume session")
        assert r.status_code == 502

        assert client.post("/api/agents/scout/sessions/S1/cancel").status_code == 409
        r, _ = rpc(runner, client.post, "/api/agents/scout/sessions/S1/prompt", json={"text": "slow"},
                   expect="session.prompt")
        turn = r.json()["turn_id"]
        r, cmd = rpc(runner, client.post, "/api/agents/scout/sessions/S1/cancel", expect="session.cancel")
        assert r.json() == {"ok": True} and cmd["session_id"] == "S1"
        runner.send(type="turn.end", agent="scout", session_id="S1", turn_id=turn, stop_reason="cancelled")
        _wait(lambda: client.get("/api/agents/scout/sessions/S1").json()["state"] == "idle")
    assert client.post("/api/agents/scout/sessions/S1/prompt", json={"text": "hi"}).status_code == 503
    assert client.post("/api/agents/scout/sessions/S1/prompt", json={"text": ""}).status_code == 422
    assert client.post("/api/agents/scout/sessions/ghost/prompt", json={"text": "x"}).status_code == 404
    assert client.post("/api/agents/scout/sessions").status_code == 503


def test_agent_failure_interrupts(client, connect, rpc):
    with connect() as runner:
        _running(client, connect, rpc, runner)
        _session(client, rpc, runner)
        r, _ = rpc(runner, client.post, "/api/agents/scout/sessions/S1/prompt", json={"text": "crash"},
                   expect="session.prompt")
        turn = r.json()["turn_id"]
        runner.send(type="permission.request", agent="scout", session_id="S1", turn_id=turn, request_id="R1",
                    tool_call={}, options=[])
        runner.status("scout", "failed", error="exit 3")
        _wait(lambda: client.get("/api/agents/scout").json()["status"] == "failed")
        a = client.get("/api/agents/scout").json()
        assert a["error"] == "exit 3" and a["desired"] == "running"
        assert client.get("/api/inbox/R1").json()["status"] == "expired"
        events = client.get("/api/agents/scout/sessions/S1/events").json()["events"]
        assert events[-1]["kind"] == "turn_end" and events[-1]["data"]["error"] == "interrupted"
        assert client.get("/api/agents/scout/sessions/S1").json()["state"] == "idle"
        # a late turn.end for the interrupted turn changes nothing
        runner.send(type="turn.end", agent="scout", session_id="S1", turn_id=turn, error="agent exited")
        r, _ = rpc(runner, client.post, "/api/agents/scout/stop", expect="agent.stop")
        assert r.json()["desired"] == "stopped"
        assert [e["kind"] for e in client.get("/api/agents/scout/sessions/S1/events").json()["events"]].count(
            "turn_end") == 1


def test_reconnect_reconciles(client, connect, rpc):
    with connect() as runner:
        _running(client, connect, rpc, runner)
        _session(client, rpc, runner)
        r, _ = rpc(runner, client.post, "/api/agents/scout/sessions/S1/prompt", json={"text": "x"},
                   expect="session.prompt")
        turn = r.json()["turn_id"]
        for rid in ("R1", "R2"):
            runner.send(type="permission.request", agent="scout", session_id="S1", turn_id=turn, request_id=rid,
                        tool_call={}, options=[])
        _wait(lambda: len(client.get("/api/inbox").json()) == 2)
    a = client.get("/api/agents/scout").json()
    assert a["connected"] is False and a["state"] == "running"     # offline isn't stopped
    assert client.get("/api/agents/scout/sessions/S1").json()["state"] == "working"

    # still running, one request still pending: nothing changes
    alive = {"agent": "scout", "state": "running"}
    with connect(agents=[alive], turns=[{"agent": "scout", "session_id": "S1", "turn_id": turn}],
                 pending=[{"agent": "scout", "session_id": "S1", "turn_id": turn, "request_id": "R1",
                           "tool_call": {}, "options": []}]) as runner:
        _wait(lambda: client.get("/api/inbox/R2").json()["status"] == "expired")
        assert client.get("/api/inbox/R1").json()["status"] == "pending"
        assert client.get("/api/agents/scout/sessions/S1").json()["state"] == "working"
        assert client.get("/api/agents/scout").json()["connected"] is True

    # the runner restarted: nothing listed -> turn interrupted, request expired, agent started again
    with connect() as runner:
        cmd = runner.recv("agent.start")
        runner.reply(cmd)
        assert cmd["agent"] == "scout"
        _wait(lambda: client.get("/api/inbox/R1").json()["status"] == "expired")
        assert client.get("/api/agents/scout/sessions/S1").json()["state"] == "idle"
        last = client.get("/api/agents/scout/sessions/S1/events").json()["events"][-1]
        assert last["kind"] == "turn_end" and last["data"]["error"] == "interrupted"


def test_reconnect_stops_agent_stopped_while_away(client, connect, rpc):
    with connect() as runner:
        _running(client, connect, rpc, runner)
    assert client.post("/api/agents/scout/stop").json()["desired"] == "stopped"     # runner offline: nothing sent
    with connect(agents=[{"agent": "scout", "state": "running"}]) as runner:
        cmd = runner.recv("agent.stop")
        assert cmd["agent"] == "scout"
        runner.reply(cmd)


def test_start_without_runner(client, connect):
    client.post("/api/agents", json={"name": "scout"})
    r = client.post("/api/agents/scout/start")
    assert r.status_code == 503
    assert client.get("/api/agents/scout").json()["desired"] == "running"
    with connect() as runner:
        cmd = runner.recv("agent.start")
        runner.reply(cmd)
        _wait(lambda: client.get("/api/agents/scout").json()["runner"] == "rr")
    # a definition pinned to another runner isn't started here
    client.post("/api/agents", json={"name": "pinned", "agent_yaml": "name: pinned\nrunner: mac\n"})
    assert client.post("/api/agents/pinned/start").status_code == 503
    assert client.post("/api/agents/scout/stop").json()["desired"] == "stopped"     # offline: just recorded


def test_rpc_timeout(tmp_path):
    from valet.shared.config import ControlConfig
    cfg = ControlConfig(data=tmp_path / "d", definitions=tmp_path / "d" / "defs", rpc_timeout=0.2)
    set_password(cfg, PASSWORD)
    token = add_runner(cfg, "rr")
    with TestClient(create_app(cfg)) as c:
        c.post("/api/login", json={"password": PASSWORD})
        c.post("/api/agents", json={"name": "scout"})
        with c.websocket_connect("/ws/runner", headers={"Authorization": f"Bearer {token}"}) as ws:
            ws.send_json({"type": "hello", "protocol": 1, "version": "t"})
            ws.receive_json()
            r = c.post("/api/agents/scout/start")
            assert r.status_code == 504
            ws.receive_json()       # the unanswered agent.start


def test_client_socket(client, cfg):
    with client.websocket_connect("/ws/client") as ws:
        assert ws.receive_json()["type"] == "hello"
    token = client.post("/api/login", json={"password": PASSWORD, "client": "native"}).json()["token"]
    client.cookies.clear()
    with client.websocket_connect("/ws/client") as ws:
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_json()
        assert e.value.code == 4401
    with client.websocket_connect(f"/ws/client?token={token}") as ws:
        hello = ws.receive_json()
        assert hello["runners"] == [] and hello["agents"] == []


def test_pending_survives_restart(cfg, connect, client, rpc):
    with connect() as runner:
        _running(client, connect, rpc, runner)
        _session(client, rpc, runner)
        r, _ = rpc(runner, client.post, "/api/agents/scout/sessions/S1/prompt", json={"text": "x"},
                   expect="session.prompt")
        runner.send(type="permission.request", agent="scout", session_id="S1", turn_id=r.json()["turn_id"],
                    request_id="R1", tool_call={"title": "rm -rf"}, options=[])
        _wait(lambda: client.get("/api/inbox").json())
    with TestClient(create_app(cfg)) as c2:
        c2.post("/api/login", json={"password": PASSWORD})
        assert [i["id"] for i in c2.get("/api/inbox").json()] == ["R1"]
        with c2.websocket_connect("/ws/client") as ws:
            assert [i["title"] for i in ws.receive_json()["inbox"]] == ["rm -rf"]


def test_revoked_runner_is_disconnected(client, connect, cfg, monkeypatch):
    from valet.control import hub
    monkeypatch.setattr(hub, "TOKEN_CHECK", 0.05)
    with connect() as runner:
        revoke_runner(cfg, "rr")
        with pytest.raises(WebSocketDisconnect) as e:
            runner.recv()
        assert e.value.code == 4401
    assert client.get("/api/runners").json() == []


def test_static_ui(cfg, tmp_path, monkeypatch):
    from valet.control import app as app_module
    static = tmp_path / "static"
    static.mkdir()
    monkeypatch.setattr(app_module, "STATIC", static)
    with TestClient(create_app(cfg)) as c:
        assert c.get("/").status_code == 404
        (static / "index.html").write_text("<!doctype html><title>Valet</title>")
        (static / "app.js").write_text("console.log(1)")
        r = c.get("/")
        assert r.status_code == 200 and "Valet" in r.text and r.headers["x-frame-options"] == "DENY"
        assert c.get("/static/app.js").text == "console.log(1)"
        assert c.get("/static/../app.py").status_code == 404


def test_resent_notifications_apply_once_then_reconcile(client, connect, rpc):
    """After a reconnect the runner resends what came after welcome.last_seq; duplicates are dropped and the
    hello's snapshot is applied only once caught up, so a turn that ended while away ends as it really did."""
    alive = {"agent": "scout", "state": "running"}
    with connect(boot="B1", last_seq=0) as runner:
        _running(client, connect, rpc, runner)
        _session(client, rpc, runner)
        r, _ = rpc(runner, client.post, "/api/agents/scout/sessions/S1/prompt", json={"text": "x"},
                   expect="session.prompt")
        turn = r.json()["turn_id"]

        def say(text, seq):
            runner.send(type="session.update", agent="scout", session_id="S1", turn_id=turn, seq=seq,
                        update={"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}})
        say("a", 1)
        _wait(lambda: len(client.get("/api/agents/scout/sessions/S1/events").json()["events"]) == 2)
    # Away: the runner produced seq 2 and 3 (the turn's end); its hello comes after seq 3 and lists no turn.
    with connect(boot="B1", seq=3, agents=[alive], last_seq=1) as runner:
        assert client.get("/api/agents/scout/sessions/S1").json()["state"] == "working"
        say("a", 1)
        say("b", 2)
        runner.send(type="turn.end", agent="scout", session_id="S1", turn_id=turn, stop_reason="end_turn", seq=3)
        _wait(lambda: client.get("/api/agents/scout/sessions/S1").json()["state"] == "idle")
    events = client.get("/api/agents/scout/sessions/S1/events").json()["events"]
    assert [(e["kind"], e["data"].get("content", {}).get("text")) for e in events] == [
        ("user_message", "x"), ("agent_message", "a"), ("agent_message", "b"), ("turn_end", None)]
    assert events[-1]["data"] == {"stopReason": "end_turn", "error": None}
    with connect(boot="B2", agents=[alive], last_seq=0):     # a new runner process numbers from scratch
        pass


def test_prompt_in_doubt_stays_running(client, connect, rpc, pool):
    """The link drops after session.prompt was sent: the turn isn't failed (the runner may be running it); the
    next hello settles it."""
    with connect() as runner:
        _running(client, connect, rpc, runner)
        _session(client, rpc, runner)
        fut = pool.submit(client.post, "/api/agents/scout/sessions/S1/prompt", json={"text": "x"})
        cmd = runner.recv("session.prompt")
    r = fut.result(timeout=10)
    assert r.status_code == 503 and "goes on" in r.json()["detail"]
    s = client.get("/api/agents/scout/sessions/S1").json()
    assert s["state"] == "working" and s["turn_id"] == cmd["turn_id"]
    turn = {"agent": "scout", "session_id": "S1", "turn_id": cmd["turn_id"]}
    with connect(agents=[{"agent": "scout", "state": "running"}], turns=[turn]) as runner:
        assert client.get("/api/agents/scout/sessions/S1").json()["state"] == "working"
        runner.send(type="turn.end", agent="scout", session_id="S1", turn_id=cmd["turn_id"], stop_reason="end_turn")
        _wait(lambda: client.get("/api/agents/scout/sessions/S1").json()["state"] == "idle")
    events = client.get("/api/agents/scout/sessions/S1/events").json()["events"]
    assert [e["kind"] for e in events] == ["user_message", "turn_end"]
    assert events[-1]["data"] == {"stopReason": "end_turn", "error": None}


def test_reconnect_marks_unlisted_agents_stopped(client, connect, rpc):
    """The runner restarted while the owner stopped the agent: nothing is started, and the agent no longer
    shows as running."""
    with connect() as runner:
        _running(client, connect, rpc, runner)
    assert client.post("/api/agents/scout/stop").json()["desired"] == "stopped"
    assert client.get("/api/agents/scout").json()["state"] == "running"
    with connect():
        _wait(lambda: client.get("/api/agents/scout").json()["state"] == "stopped")


def test_two_runners_dont_both_start_an_unplaced_agent(client):
    import asyncio

    from valet.shared.protocol import Hello
    c = client.app.state.control
    client.post("/api/agents", json={"name": "scout"})
    assert client.post("/api/agents/scout/start").status_code == 503     # no runner: desired=running, unplaced
    started = []

    async def fake_start(agent, runner):
        started.append((agent, runner))
    c.hub._start_quietly = fake_start

    async def both():
        await c.hub._reconcile("r1", Hello(version="t"))
        await c.hub._reconcile("r2", Hello(version="t"))
        await asyncio.sleep(0.05)
    asyncio.run(both())
    assert started == [("scout", "r1")]
    assert client.get("/api/agents/scout").json()["runner"] == "r1"


def test_runner_speaks_only_for_its_agents(client, connect, rpc, cfg):
    """Another runner can't write into an agent placed elsewhere, end its turns, raise requests in its name,
    or claim an agent nobody placed."""
    with connect() as runner:
        _running(client, connect, rpc, runner)
        _session(client, rpc, runner)
        r, _ = rpc(runner, client.post, "/api/agents/scout/sessions/S1/prompt", json={"text": "x"},
                   expect="session.prompt")
        turn = r.json()["turn_id"]
        runner.send(type="permission.request", agent="scout", session_id="S1", turn_id=turn, request_id="R1",
                    tool_call={}, options=[])
        _wait(lambda: client.get("/api/inbox").json())
        client.post("/api/agents", json={"name": "idle"})
        with connect(token=add_runner(cfg, "r2")) as other:
            other.send(type="session.update", agent="scout", session_id="S1", turn_id=turn,
                       update={"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "fake"}})
            other.send(type="permission.request", agent="scout", session_id="S1", turn_id=turn, request_id="R2",
                       tool_call={}, options=[])
            other.send(type="permission.closed", agent="scout", session_id="S1", request_id="R1", reason="exited")
            other.send(type="turn.end", agent="scout", session_id="S1", turn_id=turn, stop_reason="end_turn")
            other.status("scout", "failed", error="nope")
            other.status("idle", "running")
            # a marker the real runner sends after: once it's in, everything above has been handled
            runner.send(type="session.update", agent="scout", session_id="S1", turn_id=turn,
                        update={"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "real"}})
            _wait(lambda: len(client.get("/api/agents/scout/sessions/S1/events").json()["events"]) == 3)
        events = client.get("/api/agents/scout/sessions/S1/events").json()["events"]
        assert [e["kind"] for e in events] == ["user_message", "permission_request", "agent_message"]
        assert events[-1]["data"]["content"]["text"] == "real"
        assert [(i["id"], i["status"]) for i in client.get("/api/inbox").json()] == [("R1", "pending")]
        assert client.get("/api/agents/scout/sessions/S1").json()["state"] == "working"
        a = client.get("/api/agents/scout").json()
        assert (a["runner"], a["state"], a["error"]) == ("rr", "running", None)
        assert client.get("/api/agents/idle").json()["runner"] is None
