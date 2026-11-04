"""valet.owner with the real thing: a Claude Code agent in a container, at `locked`, messages Alice and asks a
question through the gateway (valet.owner decides its own calls, so neither the gateway nor the harness asks);
Alice's answer comes back as a prompt in the same session, and the agent acts on it. Opt-in: VALET_TEST_DOCKER=1,
the image built, and CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY in the environment. The container reaches the
control plane at http://host.docker.internal:<port> (VALET_TEST_GATEWAY_URL overrides it)."""
import asyncio
import os
import uuid
from dataclasses import replace

import pytest

from tests.e2e.stack import ControlServer, Stack, eventually, kinds, texts
from valet.runner.harness import claude
from valet.shared.config import PASSTHROUGH_ENV

pytestmark = pytest.mark.docker
SLOW = 240.0
AGENT = """\
name: {name}
harness: claude
computer:
  level: container
permissions: locked
directives: [persona.md, environment]
tools:
  mcp: [valet.owner]
"""
FIRST = ("Do exactly two things with the valet MCP server's tools, then end your turn without waiting: "
         "1. call `message` with the text \"Starting on the logo.\" "
         "2. call `ask` with the question \"Which color should the logo be?\" and the options [\"Teal\", \"Orange\"]. "
         "When Alice's answer arrives later as a new message, reply with only the chosen color in capital letters, "
         "followed by Alice's own words if there are any.")


async def test_a_container_agent_messages_and_asks_alice(tmp_path):
    if not any(os.environ.get(k) for k in claude.KEYS):
        pytest.skip(f"needs one of {', '.join(claude.KEYS)}")
    name = "m4t-owner-" + uuid.uuid4().hex[:6]
    stack = Stack(tmp_path, "load")
    url = os.environ.get("VALET_TEST_GATEWAY_URL") or f"http://host.docker.internal:{stack.ccfg.port}"
    stack.ccfg = replace(stack.ccfg, gateway_url=url, rpc_timeout=SLOW)
    stack.control = ControlServer(stack.ccfg)
    stack.rcfg = replace(stack.rcfg, harness_commands={},
                         image=os.environ.get("VALET_AGENT_IMAGE", "valet/agent-base"),
                         passthrough={k: os.environ[k] for k in PASSTHROUGH_ENV if os.environ.get(k)})
    await stack.start()
    try:
        r = await stack.http.post("/api/agents", json={"name": name, "agent_yaml": AGENT.format(name=name)})
        assert r.status_code == 201, r.text
        r = await stack.http.post(f"/api/agents/{name}/start")
        assert r.status_code == 200, r.text

        async def running():
            a = await stack.agent(name)
            assert a["state"] != "failed", a["error"]
            return a["state"] == "running"
        await eventually(running, timeout=SLOW, what="the container agent running")
        r = await stack.http.post(f"/api/agents/{name}/sessions", timeout=SLOW)
        assert r.status_code == 201, r.text
        sid = r.json()["id"]

        async def turns_ended(n):
            evs = await stack.events(name, sid)
            return evs if kinds(evs).count("turn_end") >= n else None

        await stack.prompt(name, sid, FIRST)
        evs = await eventually(lambda: turns_ended(1), timeout=SLOW, what="the first turn to end")
        end = next(e["data"] for e in evs if e["kind"] == "turn_end")
        assert end["error"] is None, end
        r = await stack.http.get("/api/gateway/calls", params={"agent": name})
        calls = {(c["server"], c["tool"], c["decision"], c["outcome"]) for c in r.json()["calls"]}
        assert {("valet.owner", "message", "allow", "ok"), ("valet.owner", "ask", "allow", "ok")} <= calls, calls

        items = {i["kind"]: i for i in await stack.pending()}
        assert set(items) == {"message", "question"}, items
        assert items["message"]["text"] == "Starting on the logo."
        question = items["question"]
        assert "logo" in question["text"].lower()
        assert [o["name"] for o in question["options"]] == ["Teal", "Orange", "Dismiss"]
        assert (await stack.agent(name))["status"] == "idle"      # neither item makes it wait on Alice

        r = await stack.http.post(f"/api/inbox/{items['message']['id']}/answer", json={"option_id": "dismiss"})
        assert r.status_code == 200, r.text
        r = await stack.http.post(f"/api/inbox/{question['id']}/answer",
                                  json={"option_id": "option-2", "text": "with a white outline"})
        assert r.status_code == 200, r.text

        evs = await eventually(lambda: turns_ended(2), timeout=SLOW, what="the answer's turn to end")
        answer = [e for e in evs if e["kind"] == "user_message"][-1]
        assert answer["data"]["source"] == "answer"
        assert "Alice's choice: Orange" in answer["data"]["content"]["text"]
        reply = texts([e for e in evs if e["turn_id"] == answer["turn_id"]])
        assert "ORANGE" in reply and "white outline" in reply.lower(), reply
        # Nothing asked of Alice at `locked` but the agent's own items, which show in the conversation too.
        asks = [e["data"].get("kind") for e in evs if e["kind"] == "permission_request"]
        assert sorted(asks) == ["message", "question"], asks
    finally:
        await stack.close()
        for vol in (f"valet-agent-{name}-home", f"valet-agent-{name}-work"):
            p = await asyncio.create_subprocess_exec("docker", "volume", "rm", "-f", vol,
                                                     stdout=asyncio.subprocess.DEVNULL,
                                                     stderr=asyncio.subprocess.DEVNULL)
            await p.wait()
