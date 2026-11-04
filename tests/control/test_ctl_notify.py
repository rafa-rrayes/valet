"""Notifications (control/notify.py): what an inbox item says, the webhook to a local receiver, push to a local
HTTP/2 stand-in for APNs (tests/fake_apns.py), the routes, and the hub's hook: once per new pending item."""
import dataclasses
import logging
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from tests.control.test_ctl_api import _running, _session, _wait
from tests.fake_apns import FakeApns, check_jwt
from tests.fake_webhook import FakeWebhook
from valet.control import notify
from valet.control.__main__ import add_runner, set_password
from valet.control.app import create_app

PASSWORD = "correct horse battery"
TEAM, KID = "TEAM123456", "KEY1234567"
PHONE, MAC, DEAD = "a1" * 32, "b2" * 32, "dd" * 32

TOOL_ITEM = {"id": "i1", "kind": "tool", "agent": "scout", "session_id": "S1", "turn_id": "T1", "status": "pending",
             "title": "github: Create issue", "created_at": 1.0, "answered_at": None, "answer": None,
             "tool_call": {"toolCallId": "gateway-i1", "rawInput": {"title": "Bug", "token": "do-not-send"}},
             "options": [{"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"},
                         {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"}],
             "tool": {"server": "github", "name": "create_issue", "title": "Create issue", "description": "Open one.",
                      "arguments": {"title": "Bug", "token": "do-not-send"}, "annotations": None}}
PERM_ITEM = {**TOOL_ITEM, "id": "R1", "kind": "permission", "title": "Write hello.txt", "tool": None,
             "tool_call": {"toolCallId": "c1", "title": "Write hello.txt"}}


# ---------- what an item says ----------

def test_what_each_kind_says():
    link = "https://valet.example/#/inbox"
    assert notify.describe(TOOL_ITEM, "Scout") == ("Scout needs your OK", "github › Create issue")
    assert notify.describe(PERM_ITEM, "Scout") == ("Scout needs your OK", "Write hello.txt")
    assert notify.describe({**PERM_ITEM, "kind": "question", "title": "Which repo?"}, "Scout") == \
        ("Scout asks", "Which repo?")
    assert notify.describe({**PERM_ITEM, "kind": "someday", "title": "  a\nnew   kind "}, "Scout") == \
        ("Scout", "a new kind")
    assert notify.describe({**PERM_ITEM, "title": "x" * 500}, "S")[1] == "x" * 239 + "…"
    assert notify.line(TOOL_ITEM, "Scout", link) == \
        f"🔐 Valet: Scout wants to use github › Create issue. Answer in the inbox: {link}"
    assert notify.line(PERM_ITEM, "Scout", link) == \
        f"🔐 Valet: Scout asks: Write hello.txt. Answer in the inbox: {link}"
    assert notify.line({**PERM_ITEM, "kind": "question", "title": "Which repo?"}, "Scout", link) == \
        f"🔔 Valet: Scout asks: Which repo? Answer in the inbox: {link}"
    assert notify.line({**PERM_ITEM, "kind": "message", "title": "Deployed v2"}, "Scout", link) == \
        f"🔔 Valet: Scout says: Deployed v2. See it in the inbox: {link}"
    fields = notify.item_fields(TOOL_ITEM)
    assert fields == {"item_id": "i1", "agent": "scout", "kind": "tool", "title": "github: Create issue",
                      "session_id": "S1", "created_at": 1.0,
                      "options": [{"optionId": "allow_once", "name": "Allow once"},
                                  {"optionId": "reject_once", "name": "Reject"}],
                      "tool": {"server": "github", "name": "create_issue", "title": "Create issue"}}
    assert "do-not-send" not in str(fields)                     # arguments stay home
    assert notify.push_payload(TOOL_ITEM, "t", "b") == {
        "aps": {"alert": {"title": "t", "body": "b"}, "sound": "default", "category": "VALET_INBOX",
                "interruption-level": "time-sensitive", "thread-id": "scout",
                "mutable-content": 1},
        "item_id": "i1", "agent": "scout", "kind": "tool"}


@pytest.mark.parametrize("url,ok", [
    ("https://hooks.slack.com/services/T/B/x", True), ("http://127.0.0.1:9/hook", True),
    ("https://discord.com/api/webhooks/1/abc?wait=true", True),
    ("ftp://x.example/a", False), ("javascript:alert(1)", False), ("https://", False), ("hooks.slack.com/x", False),
    ("https://a.example/ x", False), ("https://a.example/\nx", False), ("https://a.example:99999/", False),
    ("https://a.example/" + "x" * 2000, False), ("", False), (None, False), (3, False),
])
def test_webhook_url_rule(url, ok):
    assert (notify.webhook_url_error(url) is None) is ok


def test_jwt_is_es256_with_the_teams_claims():
    key = ec.generate_private_key(ec.SECP256R1())
    token = notify.es256_jwt(key, KID, TEAM, 1_700_000_000)
    assert check_jwt(token, key.public_key(), TEAM, KID) == {"iss": TEAM, "iat": 1_700_000_000}
    other = ec.generate_private_key(ec.SECP256R1())
    assert check_jwt(token, other.public_key(), TEAM, KID) is None
    assert check_jwt(token, key.public_key(), "OTHERTEAM1", KID) is None


# ---------- fixtures: a key, a fake APNs, a webhook receiver ----------

@pytest.fixture
def key():
    return ec.generate_private_key(ec.SECP256R1())


@pytest.fixture
def apns(key):
    fake = FakeApns(key.public_key(), TEAM, KID).start()
    yield fake
    fake.stop()


@pytest.fixture
def hook():
    fake = FakeWebhook().start()
    yield fake
    fake.stop()


@pytest.fixture
def push_cfg(cfg, key, apns, tmp_path):
    p8 = tmp_path / "AuthKey.p8"
    p8.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption()))
    return dataclasses.replace(cfg, apns_key_file=p8, apns_key_id=KID, apns_team_id=TEAM, apns_url=apns.url,
                               public_url="https://valet.example")


def signed_in(c: TestClient) -> TestClient:
    """A bearer token: an https VALET_PUBLIC_URL makes the cookie Secure, which the test client won't send."""
    r = c.post("/api/login", json={"password": PASSWORD, "client": "native"})
    c.headers["Authorization"] = f"Bearer {r.json()['token']}"
    return c


@pytest.fixture
def push_client(push_cfg):
    set_password(push_cfg, PASSWORD)
    with TestClient(create_app(push_cfg)) as c:
        yield signed_in(c)


# ---------- the routes ----------

def test_notification_settings(client, hook, caplog):
    r = client.get("/api/notifications")
    assert r.json() == {"webhook_url": None, "push": {"configured": False, "error": None,
                                                      "topic": "br.com.rrayes.valet", "sandbox": False,
                                                      "devices": 0}}
    assert client.post("/api/notifications/test").status_code == 422          # nothing to test yet
    bad = client.put("/api/notifications", json={"webhook_url": "ftp://x.example/a"})
    assert bad.status_code == 422 and bad.json()["detail"] == "webhook_url: an http:// or https:// URL"
    r = client.put("/api/notifications", json={"webhook_url": f"  {hook.url}  "})
    assert r.status_code == 200 and r.json()["webhook_url"] == hook.url
    assert client.get("/api/notifications").json()["webhook_url"] == hook.url

    r = client.post("/api/notifications/test")
    assert r.status_code == 200 and r.json() == {"webhook": {"ok": True, "error": None}, "push": None}
    [body] = hook.bodies
    text = "✅ Valet: notifications reach this webhook."
    assert body == {"event": "test", "text": text, "content": text, "url": "http://127.0.0.1:8780/#/inbox"}

    hook.status = 404
    caplog.set_level(logging.INFO)
    r = client.post("/api/notifications/test")
    assert r.json()["webhook"] == {"ok": False, "error": "127.0.0.1 answered 404"}
    ours = [r.getMessage() for r in caplog.records if r.name.startswith("valet")]
    assert "webhook (test) failed: 127.0.0.1 answered 404" in ours
    assert not any("T0KEN" in m for m in ours)           # the URL is never logged (`valet-control serve` keeps
    #                                                      httpx's own request lines, which have it, out too)

    assert client.put("/api/notifications", json={"webhook_url": ""}).json()["webhook_url"] is None
    assert client.put("/api/notifications", json={"webhook_url": None}).json()["webhook_url"] is None
    assert client.post("/api/notifications/test").status_code == 422


def test_unreachable_webhook_says_so(client):
    client.put("/api/notifications", json={"webhook_url": "http://127.0.0.1:9/hook/T0KEN"})
    r = client.post("/api/notifications/test")
    assert r.status_code == 200
    assert r.json()["webhook"]["ok"] is False and r.json()["webhook"]["error"].startswith("can't reach 127.0.0.1")


def test_devices(client):
    assert client.get("/api/devices").json() == []
    r = client.post("/api/devices", json={"token": PHONE.upper(), "platform": "ios", "name": " Alice's\niPhone "})
    assert r.status_code == 200
    phone = r.json()
    assert phone == {**phone, "platform": "ios", "name": "Alice's iPhone", "last_push": None}
    assert "token" not in phone
    mac = client.post("/api/devices", json={"token": MAC, "platform": "macos"}).json()
    assert mac["name"] == "Mac"
    # The same token again is the same device, updated.
    again = client.post("/api/devices", json={"token": PHONE, "platform": "ios", "name": "Phone"}).json()
    assert again["id"] == phone["id"] and again["name"] == "Phone"
    assert [d["id"] for d in client.get("/api/devices").json()] == [phone["id"], mac["id"]]
    for body, msg in [({"token": "xyz", "platform": "ios"}, "token"), ({"token": "ab" * 8, "platform": "ios"}, "token"),
                      ({"token": PHONE, "platform": "android"}, "platform"), ({}, "token")]:
        r = client.post("/api/devices", json=body)
        assert r.status_code == 422 and r.json()["detail"].startswith(msg), r.json()
    assert client.get("/api/notifications").json()["push"]["devices"] == 2
    assert client.delete(f"/api/devices/{mac['id']}").json() == {"ok": True}
    assert client.delete(f"/api/devices/{mac['id']}").status_code == 404
    assert [d["id"] for d in client.get("/api/devices").json()] == [phone["id"]]


def test_routes_need_the_owner(cfg):
    set_password(cfg, PASSWORD)
    with TestClient(create_app(cfg)) as c:
        for method, path in [("GET", "/api/notifications"), ("PUT", "/api/notifications"),
                             ("POST", "/api/notifications/test"), ("GET", "/api/devices"), ("POST", "/api/devices"),
                             ("DELETE", "/api/devices/1"), ("GET", "/api/gateway/grants"),
                             ("DELETE", "/api/gateway/grants/1")]:
            assert c.request(method, path, json={}).status_code == 401, path


# ---------- push ----------

def test_push_reaches_every_device_and_drops_dead_ones(push_client, apns):
    c = push_client
    assert c.get("/api/notifications").json()["push"] == {
        "configured": True, "error": None, "topic": "br.com.rrayes.valet", "sandbox": False, "devices": 0}
    for token in (PHONE, MAC, DEAD, "ee" * 32, "ff" * 32):
        c.post("/api/devices", json={"token": token, "platform": "ios"})
    apns.answers[DEAD] = (410, {"reason": "Unregistered"})
    apns.answers["ee" * 32] = (400, {"reason": "BadDeviceToken"})
    apns.answers["ff" * 32] = (500, {"reason": "InternalServerError"})
    r = c.post("/api/notifications/test")
    assert r.json() == {"webhook": None, "push": {"sent": 2, "failed": 1, "removed": 2,
                                                  "error": "APNs answered 500 InternalServerError"}}
    assert len(apns.pushes) == 5 and apns.connections == 1             # one HTTP/2 connection for all of them
    first = apns.pushes[0]
    assert first["token"] == PHONE and first["claims"]["iss"] == TEAM
    assert {k: first["headers"][k] for k in ("apns-topic", "apns-push-type", "apns-priority")} == {
        "apns-topic": "br.com.rrayes.valet", "apns-push-type": "alert", "apns-priority": "10"}
    assert first["payload"]["aps"]["alert"] == {"title": "Valet", "body": "Notifications reach this device."}
    devices = c.get("/api/devices").json()
    assert len(devices) == 3 and all(d["last_push"] for d in devices[:2]) and devices[2]["last_push"] is None


def test_a_bad_key_turns_push_off_without_breaking_anything(push_cfg, apns):
    push_cfg.apns_key_file.write_text("not a key")
    set_password(push_cfg, PASSWORD)
    with TestClient(create_app(push_cfg)) as c:
        signed_in(c)
        assert c.get("/api/notifications").json()["push"]["error"] == "VALET_APNS_KEY_FILE: not a P-256 .p8 key"
        c.post("/api/devices", json={"token": PHONE, "platform": "ios"})
        r = c.post("/api/notifications/test")
        assert r.json()["push"] == {"sent": 0, "failed": 1, "removed": 0,
                                    "error": "VALET_APNS_KEY_FILE: not a P-256 .p8 key"}
    assert apns.pushes == []
    gone = dataclasses.replace(push_cfg, apns_key_file=push_cfg.apns_key_file.with_name("missing.p8"))
    assert notify.Apns(gone).key_error() == "VALET_APNS_KEY_FILE: No such file or directory"


def test_sandbox_and_production_endpoints(cfg):
    assert notify.Apns(cfg).base == "https://api.push.apple.com"
    assert notify.Apns(dataclasses.replace(cfg, apns_sandbox=True)).base == "https://api.sandbox.push.apple.com"
    assert notify.Apns(dataclasses.replace(cfg, apns_url="http://x:1")).base == "http://x:1"
    assert notify.Apns(cfg).configured is False


# ---------- the hook: every new pending item, once ----------

def test_a_permission_item_notifies_once(push_client, push_cfg, apns, hook, pool):
    c = push_client
    c.put("/api/notifications", json={"webhook_url": hook.url})
    c.post("/api/devices", json={"token": PHONE, "platform": "ios"})
    from tests.control.conftest import FakeRunner

    def rpc(runner, fn, *args, expect, result=None, error=None, **kwargs):
        fut = pool.submit(fn, *args, **kwargs)
        cmd = runner.recv(expect)
        runner.reply(cmd, result or {}, error)
        return fut.result(timeout=10), cmd

    token = add_runner(push_cfg, "rr")

    def connect(**hello):
        cm = c.websocket_connect("/ws/runner", headers={"Authorization": f"Bearer {token}"})

        class _CM:
            def __enter__(self):
                r = FakeRunner(cm.__enter__())
                r.hello(**hello)
                return r

            def __exit__(self, *exc):
                return cm.__exit__(*exc)
        return _CM()

    options = [{"optionId": "allow", "name": "Allow", "kind": "allow_once"},
               {"optionId": "reject", "name": "Reject", "kind": "reject_once"}]
    with connect() as runner:
        _running(c, connect, rpc, runner)
        c.put("/api/agents/scout/definition", json={"definition": {**c.get("/api/agents/scout").json()["definition"],
                                                                   "display_name": "Scout"}})
        _session(c, rpc, runner)
        r, _ = rpc(runner, c.post, "/api/agents/scout/sessions/S1/prompt", json={"text": "write"},
                   expect="session.prompt")
        turn = r.json()["turn_id"]
        req = {"agent": "scout", "session_id": "S1", "turn_id": turn, "request_id": "R1",
               "tool_call": {"toolCallId": "c1", "title": "Write hello.txt", "rawInput": {"content": "private"}},
               "options": options}
        runner.send(type="permission.request", **req)
        _wait(lambda: hook.bodies and apns.pushes)
    [body] = hook.bodies
    line = "🔐 Valet: Scout asks: Write hello.txt. Answer in the inbox: https://valet.example/#/inbox"
    assert body == {"event": "inbox", "text": line, "content": line, "item_id": "R1", "agent": "scout",
                    "kind": "permission", "title": "Write hello.txt", "session_id": "S1",
                    "created_at": body["created_at"], "options": [{"optionId": "allow", "name": "Allow"},
                                                                  {"optionId": "reject", "name": "Reject"}],
                    "url": "https://valet.example/#/inbox"}
    [push] = apns.pushes
    assert push["token"] == PHONE and push["headers"]["apns-collapse-id"] == "R1"
    assert push["payload"] == {"aps": {"alert": {"title": "Scout needs your OK", "body": "Write hello.txt"},
                                       "sound": "default", "category": "VALET_INBOX",
                                       "interruption-level": "time-sensitive", "thread-id": "scout",
                "mutable-content": 1},
                               "item_id": "R1", "agent": "scout", "kind": "permission"}
    assert "private" not in str(hook.bodies)

    # The runner reconnects and lists the same request in its hello: the item is known, nobody is told again.
    with connect(agents=[{"agent": "scout", "state": "running"}],
                 turns=[{"agent": "scout", "session_id": "S1", "turn_id": turn}], pending=[req]):
        _wait(lambda: c.get("/api/inbox").json())
        time.sleep(0.3)
    assert len(hook.bodies) == 1 and len(apns.pushes) == 1
    assert c.get("/api/inbox/R1").json()["status"] == "pending"


def test_no_notification_setup_sends_nothing(client, connect, rpc):
    """The offline default: no webhook, no APNs key. An item is created and nothing reaches out."""
    with connect() as runner:
        _running(client, connect, rpc, runner)
        _session(client, rpc, runner)
        r, _ = rpc(runner, client.post, "/api/agents/scout/sessions/S1/prompt", json={"text": "x"},
                   expect="session.prompt")
        runner.send(type="permission.request", agent="scout", session_id="S1", turn_id=r.json()["turn_id"],
                    request_id="R1", tool_call={}, options=[{"optionId": "ok", "name": "OK", "kind": "allow_once"}])
        _wait(lambda: client.get("/api/inbox").json())
    n = client.app.state.control.notifier
    assert n.webhook_url() is None and not n.apns.configured and n.apns._client is None


def test_an_agents_addition_to_shared_notifies(push_client, apns, hook):
    """space.shared_added is where push and the webhook start for what agents add; the owner's own additions
    don't notify. Never the note's text."""
    c = push_client
    c.put("/api/notifications", json={"webhook_url": hook.url})
    c.post("/api/devices", json={"token": PHONE, "platform": "ios"})
    c.app.state.control.defs.create("scout")
    shared = c.app.state.control.shared
    c.portal.call(shared.add_note, "scout", "owner", "Mine", "My own note.")
    item = c.portal.call(shared.add_note, "scout", "scout", "Found it", "The answer is 42.")
    _wait(lambda: hook.bodies and apns.pushes)
    [push] = apns.pushes
    assert push["headers"]["apns-collapse-id"] == f"shared-{item['id']}"
    assert push["payload"] == {"aps": {"alert": {"title": "scout added to Shared", "body": "Found it"},
                                       "sound": "default", "thread-id": "shared"},
                               "kind": "shared", "shared_id": item["id"], "agent": "scout"}
    [body] = hook.bodies
    url = f"https://valet.example/#/shared/{item['id']}"
    line = f"📎 Valet: scout added a note to Shared: Found it. Open it: {url}"
    assert body == {"event": "shared", "text": line, "content": line, "shared_id": item["id"], "kind": "note",
                    "title": "Found it", "added_by": "scout", "url": url}
    assert "answer is 42" not in str(body) and "answer is 42" not in str(push)    # (the push's iat may hold 42)
