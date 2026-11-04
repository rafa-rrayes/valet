"""Gateway asks with scopes, end to end: the real control plane and runner, the fake agent calling the gateway as a
harness does, tests/fake_mcp.py upstream. "Allow for an hour" leaves a grant, "Always allow" a rule in the agent's
definition; rules beat the level; every new ask notifies (webhook, push) once."""
import asyncio
import dataclasses
import subprocess
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from tests.e2e.stack import ControlServer, Stack, eventually, texts
from tests.e2e.test_e2e_gateway import AGENT, agent_client, calls, gw_agent, put_registry, result_text, secret_env  # noqa: F401
from tests.fake_apns import FakeApns
from tests.fake_webhook import FakeWebhook

RULED = AGENT + "  rules:\n{rules}"


async def answer(stack: Stack, item: dict, option: str) -> dict:
    r = await stack.http.post(f"/api/inbox/{item['id']}/answer", json={"option_id": option})
    assert r.status_code == 200, r.text
    return r.json()


async def decisions(stack: Stack, name: str = "gw") -> dict[str, tuple[str, str, str]]:
    """tool -> (decision, source, level_decision) from GET /api/agents/{name}/tools."""
    r = await stack.http.get(f"/api/agents/{name}/tools")
    assert r.status_code == 200, r.text
    return {t["tool"]: (t["decision"], t["source"], t["level_decision"])
            for s in r.json()["servers"] if s["server"] == "fake" for t in s["tools"]}


async def grants(stack: Stack, **params) -> list[dict]:
    r = await stack.http.get("/api/gateway/grants", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def git(root, *args) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout.strip()


async def test_allow_for_an_hour_leaves_a_grant_until_revoked(stack):
    sid = await gw_agent(stack, level="ask")
    turn = await stack.prompt("gw", sid, 'mcp fake__add_note {"text": "milk"}')
    [item] = await stack.wait_pending()
    assert [o["name"] for o in item["options"]] == ["Allow once", "Allow for an hour", "Always allow", "Reject"]
    done = await answer(stack, item, "allow_hour")
    assert done["answer"] == {"outcome": "selected", "optionId": "allow_hour"}
    assert texts(await stack.turn("gw", sid, turn)) == "ok: noted: milk"

    [g] = await grants(stack, agent="gw")
    assert (g["agent"], g["server"], g["tool"]) == ("gw", "fake", "add_note")
    assert g["expires_at"] - time.time() == pytest.approx(3600, abs=30)
    assert await grants(stack, agent="other") == []
    seen = await decisions(stack)
    assert seen["add_note"] == ("allow", "grant", "ask")
    assert seen["delete_note"] == ("ask", "level", "ask")            # the grant is for that tool only

    async with agent_client(stack) as client:
        assert result_text(await client.call_tool("fake__add_note", {"text": "eggs"})) == "noted: eggs"
        assert await stack.pending() == []                               # no ask within the hour
        # Revoked: it asks again from the next call.
        r = await stack.http.delete(f"/api/gateway/grants/{g['id']}")
        assert r.status_code == 200 and await grants(stack) == []
        assert (await stack.http.delete(f"/api/gateway/grants/{g['id']}")).status_code == 404
        task = asyncio.create_task(client.call_tool("fake__add_note", {"text": "jam"}))
        [again] = await stack.wait_pending()
        await answer(stack, again, "reject_once")
        assert "The owner rejected this call" in result_text(await task)
    rows = await calls(stack, agent="gw")
    assert [(c["tool"], c["decision"], c["outcome"]) for c in rows] == [
        ("add_note", "ask", "rejected"), ("add_note", "allow", "ok"), ("add_note", "ask", "ok")]
    # Grants don't go into what the agent is told at its start: they run out.
    env = (await stack.http.get("/api/agents/gw/directives")).json()
    assert env["error"] is None


async def test_always_allow_writes_a_rule_into_the_definition(stack):
    sid = await gw_agent(stack, level="ask")
    feed = await stack.feed()
    turn = await stack.prompt("gw", sid, "mcp fake__poke {}")
    [item] = await stack.wait_pending()
    await answer(stack, item, "allow_always")
    assert texts(await stack.turn("gw", sid, turn)) == "ok: poked"

    root = stack.ccfg.definitions
    await eventually(lambda: git(root, "log", "-1", "--format=%s") == "Inbox: always allow fake poke for gw",
                     what="the rule's commit")
    detail = await stack.agent("gw")
    assert detail["definition"]["tools"]["rules"] == {"fake": {"poke": "allow"}}
    assert "  rules:\n    fake:\n      poke: allow\n" in detail["files"]["agent.yaml"]
    assert detail["restart_needed"] is False                         # applies at the next call, no restart
    await feed.wait(lambda f: f["type"] == "agent" and f["agent"]["name"] == "gw", what="agent frame")
    assert (await decisions(stack))["poke"] == ("allow", "rule", "ask")
    assert await grants(stack) == []

    async with agent_client(stack) as client:
        assert result_text(await client.call_tool("fake__poke", {})) == "poked"
    assert await stack.pending() == []
    # It's an ordinary rule now: the owner takes it back by saving the definition without it.
    d = detail["definition"]
    d["tools"]["rules"] = {}
    r = await stack.http.put("/api/agents/gw/definition", json={"definition": d})
    assert r.status_code == 200 and "rules" not in r.json()["files"]["agent.yaml"]
    assert (await decisions(stack))["poke"] == ("ask", "level", "ask")


async def test_rules_beat_the_level(stack):
    rules = "    fake: {add_note: allow, echo: deny}\n"
    await put_registry(stack)
    r = await stack.http.post("/api/agents", json={"name": "gw", "agent_yaml": RULED.format(
        name="gw", level="locked", servers="valet.agents, fake", rules=rules)})
    assert r.status_code == 201, r.text
    await stack.start_agent("gw")
    seen = await decisions(stack)
    assert seen["add_note"] == ("allow", "rule", "deny") and seen["echo"] == ("deny", "rule", "allow")
    assert seen["delete_note"] == ("deny", "level", "deny")
    async with agent_client(stack) as client:
        names = [t.name for t in (await client.list_tools()).tools]
        assert "fake__add_note" in names                              # listed even at locked
        assert "fake__echo" not in names and "fake__delete_note" not in names
        assert result_text(await client.call_tool("fake__add_note", {"text": "x"})) == "noted: x"
        refused = await client.call_tool("fake__echo", {"text": "x"})
        assert refused.is_error and result_text(refused) == ("Valet doesn't allow fake echo for this agent: The owner's "
                                                              "rule for it says no.")
        level = await client.call_tool("fake__delete_note", {"name": "x"})
        assert "its permission level is locked" in result_text(level)
        # A rule alone changing is no restart.
        r = await stack.http.put("/api/agents/gw/files/agent.yaml", json={"text": RULED.format(
            name="gw", level="locked", servers="valet.agents, fake", rules="    fake: {add_note: allow}\n")})
        assert r.status_code == 200 and (await stack.agent("gw"))["restart_needed"] is False
        assert result_text(await client.call_tool("fake__echo", {"text": "z"})) == "echo: z"

        # "*" covers the server's other tools; a tool's own rule still wins. Applies at the next call.
        r = await stack.http.put("/api/agents/gw/files/agent.yaml", json={"text": RULED.format(
            name="gw", level="unrestricted", servers="fake", rules="    fake: {'*': ask, echo: allow}\n")})
        assert r.status_code == 200, r.text
        assert result_text(await client.call_tool("fake__echo", {"text": "y"})) == "echo: y"
        task = asyncio.create_task(client.call_tool("fake__secret_length", {}))
        [item] = await stack.wait_pending()
        assert item["tool"]["name"] == "secret_length"
        await answer(stack, item, "allow_once")
        assert not (await task).is_error

    # The environment layer the agent gets at its start says so.
    r = await stack.http.put("/api/agents/gw/files/agent.yaml", json={"text": RULED.format(
        name="gw", level="locked", servers="fake", rules=rules) + "directives: [environment]\n"})
    assert r.status_code == 200, r.text
    env = (await stack.http.post("/api/agents/gw/directives/preview", json={})).json()["text"]
    assert "The owner has also set rules for some of them, which come first" in env
    assert "Runs: echo" not in env and "Runs: add_note" in env and "Blocked: echo" in env


async def test_every_new_ask_notifies_once(tmp_path):
    key = ec.generate_private_key(ec.SECP256R1())
    apns = FakeApns(key.public_key(), "TEAM123456", "KEY1234567").start()
    hook = FakeWebhook().start()
    p8 = tmp_path / "AuthKey.p8"
    p8.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption()))
    stack = Stack(tmp_path / "s", "load")
    stack.ccfg = dataclasses.replace(stack.ccfg, apns_key_file=p8, apns_key_id="KEY1234567",
                                     apns_team_id="TEAM123456", apns_url=apns.url)
    stack.control = ControlServer(stack.ccfg)
    await stack.start()
    try:
        assert (await stack.http.put("/api/notifications", json={"webhook_url": hook.url})).status_code == 200
        r = await stack.http.post("/api/devices", json={"token": "ab" * 32, "platform": "ios", "name": "Phone"})
        assert r.status_code == 200
        sid = await gw_agent(stack, level="ask")
        turn = await stack.prompt("gw", sid, 'mcp fake__add_note {"text": "private words"}')
        [item] = await stack.wait_pending()
        await eventually(lambda: hook.bodies and apns.pushes, what="the notifications")
        [body] = hook.bodies
        assert body["event"] == "inbox" and body["kind"] == "tool" and body["item_id"] == item["id"]
        assert body["tool"] == {"server": "fake", "name": "add_note", "title": None}
        assert body["text"] == (f"🔐 Valet: gw wants to use fake › add_note. Answer in the inbox: "
                                f"{stack.base}/#/inbox")
        assert [o["optionId"] for o in body["options"]] == ["allow_once", "allow_hour", "allow_always", "reject_once"]
        assert "private words" not in str(body)
        [push] = apns.pushes
        assert push["claims"] is not None and push["payload"]["kind"] == "tool"
        assert push["payload"]["aps"]["alert"] == {"title": "gw needs your OK", "body": "fake › add_note"}
        assert push["payload"]["aps"]["category"] == "VALET_INBOX"
        await answer(stack, item, "allow_once")
        await stack.turn("gw", sid, turn)

        # A harness's own permission request: the same hook.
        turn = await stack.prompt("gw", sid, "permission")
        [perm] = await stack.wait_pending()
        assert perm["kind"] == "permission"
        await eventually(lambda: len(hook.bodies) == 2 and len(apns.pushes) == 2, what="the second notification")
        assert hook.bodies[1]["kind"] == "permission" and hook.bodies[1]["title"] == "Write hello.txt"
        assert apns.pushes[1]["payload"]["item_id"] == perm["id"]
        # The control plane restarts with it pending: the runner's hello lists it again, and nobody is told twice.
        await stack.restart_control()
        await eventually(lambda: stack.pending(), what="the item, still pending")
        await asyncio.sleep(0.5)
        assert len(hook.bodies) == 2 and len(apns.pushes) == 2
        await answer(stack, (await stack.pending())[0], "allow")
        await stack.turn("gw", sid, turn)
    finally:
        await stack.close()
        apns.stop()
        hook.stop()
