"""M4 as one product, with the real thing: a container agent (Claude Code or DeepSeek Harness, from the agent-base
image) at `locked`, with memory, the shared space, valet.owner and two `tools.rules` rules (one lets it add a shared
note, which `locked` alone would refuse; one hides valet.agents), is prompted by one of its webhooks (a trigger, in
its main session). In that one turn it saves the webhook's code word in its memory, adds a shared note, and messages
Alice; the owner's notification webhook gets the message and the note, once each. Opt-in: VALET_TEST_DOCKER=1, the
image built, and the harness's key in the environment (CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY;
DEEPSEEK_API_KEY). One turn per harness. The container reaches the control plane at
http://host.docker.internal:<port> (VALET_TEST_GATEWAY_URL overrides it)."""
import asyncio
import os
import uuid
from dataclasses import replace

import httpx
import pytest

from tests.e2e.stack import ControlServer, Stack, end_of, eventually, kinds
from tests.fake_webhook import FakeWebhook
from valet.runner.harness import claude, deepseek
from valet.shared.config import PASSTHROUGH_ENV

pytestmark = pytest.mark.docker
SLOW = 240.0
AGENT = """\
name: {name}
harness: {harness}
computer:
  level: container
permissions: locked
memory: true
directives: [persona.md, environment, memory]
tools:
  mcp: [valet.shared, valet.owner, valet.agents]
  rules:
    valet.shared: {{shared_add_note: allow}}
    valet.agents: {{"*": deny}}
triggers:
  - webhook: codes
    prompt: >-
      The webhook's data has a code word (its `code` field). Do these three things with your Valet MCP tools, in
      this order, then end your turn: 1. save it in your Valet memory with the valet.memory `write` tool, a file
      named `facts` whose text is "The code word is <the code word>." 2. add a note to the shared space with
      `shared_add_note`, whose text is "Code word: <the code word>". 3. message Alice with valet.owner's `message`
      tool, the text "Saved <the code word>." Then reply with just "done".
"""


@pytest.mark.parametrize("harness", [claude, deepseek], ids=["claude", "deepseek"])
async def test_a_webhook_turn_writes_memory_shares_a_note_and_messages_alice(tmp_path, harness):
    if not any(os.environ.get(k) for k in harness.KEYS):
        pytest.skip(f"needs one of {', '.join(harness.KEYS)}")
    name = f"m4k-{'cc' if harness is claude else 'ds'}-" + uuid.uuid4().hex[:6]
    word = "JABUTICABA" + uuid.uuid4().hex[:4].upper()
    hook = FakeWebhook().start()
    stack = Stack(tmp_path, "load")
    url = os.environ.get("VALET_TEST_GATEWAY_URL") or f"http://host.docker.internal:{stack.ccfg.port}"
    stack.ccfg = replace(stack.ccfg, gateway_url=url, rpc_timeout=SLOW)
    stack.control = ControlServer(stack.ccfg)
    stack.rcfg = replace(stack.rcfg, harness_commands={},
                         image=os.environ.get("VALET_AGENT_IMAGE", "valet/agent-base"),
                         passthrough={k: os.environ[k] for k in PASSTHROUGH_ENV if os.environ.get(k)})
    await stack.start()
    try:
        assert (await stack.http.put("/api/notifications", json={"webhook_url": hook.url})).status_code == 200
        r = await stack.http.post("/api/agents", json={
            "name": name, "persona": "You are a test agent. Keep replies short.\n",
            "agent_yaml": AGENT.format(name=name, harness="claude" if harness is claude else "deepseek")})
        assert r.status_code == 201, r.text
        r = await stack.http.post(f"/api/agents/{name}/start")
        assert r.status_code == 200, r.text

        async def running():
            a = await stack.agent(name)
            assert a["state"] != "failed", a["error"]
            return a["state"] == "running"
        await eventually(running, timeout=SLOW, what="the container agent running")
        assert not stack.rcfg.harness_commands      # the real adapter, in the container

        # The rules: shared_add_note listed by its rule though `locked` denies it; valet.agents hidden by "*".
        tools = (await stack.http.get(f"/api/agents/{name}/tools")).json()
        by = {(s["server"], t["tool"]): t for s in tools["servers"] for t in s["tools"]}
        assert by[("valet.shared", "shared_add_note")]["decision"] == "allow"
        assert by[("valet.shared", "shared_add_note")]["source"] == "rule"
        assert by[("valet.shared", "shared_add_note")]["level_decision"] == "deny"
        assert {t["decision"] for (srv, _), t in by.items() if srv == "valet.agents"} == {"deny"}

        # The trigger: Alice's webhook, with the code word as its data, prompts the agent's main session.
        [trigger] = (await stack.http.get(f"/api/agents/{name}/triggers")).json()["triggers"]
        async with httpx.AsyncClient(base_url=stack.base, timeout=SLOW) as sender:
            r = await sender.post(f"/hooks/{name}/codes", params={"key": trigger["key"]}, json={"code": word})
        assert r.status_code == 202, r.text
        assert r.json()["outcome"] == "started", r.json()
        sid, turn = r.json()["session_id"], r.json()["turn_id"]
        assert (await stack.agent(name))["main_session"] == sid

        async def ended():
            evs = [e for e in await stack.events(name, sid) if e["turn_id"] == turn]
            return evs if "turn_end" in kinds(evs) else None
        evs = await eventually(ended, timeout=SLOW, what="the webhook's turn to end")
        assert end_of(evs)["error"] is None, end_of(evs)
        [user] = [e for e in evs if e["kind"] == "user_message"]
        assert user["data"]["source"] == "webhook:codes", user

        # Each through the gateway, decided as the rules and the servers say, nothing asked of Alice.
        r = await stack.http.get("/api/gateway/calls", params={"agent": name})
        calls = {(c["server"], c["tool"], c["decision"], c["outcome"]) for c in r.json()["calls"]}
        assert {("valet.memory", "write", "allow", "ok"), ("valet.shared", "shared_add_note", "allow", "ok"),
                ("valet.owner", "message", "allow", "ok")} <= calls, calls
        assert not any(srv == "valet.agents" for srv, *_ in calls), calls

        # Memory: a commit in the definitions repo.
        mem = (await stack.http.get(f"/api/agents/{name}/memory")).json()
        texts = [(await stack.http.get(f"/api/agents/{name}/memory/{f['name']}")).json()["text"]
                 for f in mem["files"]]
        assert any(word in t for t in texts), texts
        # The shared space: the agent's note.
        items = (await stack.http.get("/api/shared")).json()["items"]
        notes = [i for i in items if i["added_by"] == name and i["kind"] == "note"]
        assert len(notes) == 1, items
        note = (await stack.http.get(f"/api/shared/{notes[0]['id']}")).json()
        assert word in note["text"], note
        # The inbox: valet.owner's message, durable, nothing else pending.
        pending = await stack.pending()
        assert [i["kind"] for i in pending] == ["message"], pending
        assert word in pending[0]["text"], pending

        # The owner's webhook: the message and the note, once each.
        await eventually(lambda: len(hook.bodies) >= 2, timeout=30, what="the notifications")
        await asyncio.sleep(1)
        events = sorted((b["event"], b["kind"]) for b in hook.bodies)
        assert events == [("inbox", "message"), ("shared", "note")], hook.bodies
        runs = (await stack.http.get(f"/api/agents/{name}/trigger-runs")).json()["runs"]
        assert [(x["outcome"], x["session_id"]) for x in runs] == [("started", sid)], runs
    finally:
        hook.stop()
        await stack.close()
        for args in (["rm", "-f", f"valet-agent-{name}"],
                     ["volume", "rm", "-f", f"valet-agent-{name}-home", f"valet-agent-{name}-work"]):
            p = await asyncio.create_subprocess_exec("docker", *args, stdout=asyncio.subprocess.DEVNULL,
                                                     stderr=asyncio.subprocess.DEVNULL)
            await p.wait()
