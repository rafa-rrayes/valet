"""Directives with the real thing: a Claude Code (or DeepSeek Harness) agent in a container (the agent-base image)
is told the owner profile and its environment, joined with its preset and persona by its adapter (Claude's system
prompt append; dsh's AGENTS.md), and answers from them: a fact only owner.md has, and its own Valet name, which only
the generated environment says. Opt-in: VALET_TEST_DOCKER=1, the image built, and the harness's key in the
environment (CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY; DEEPSEEK_API_KEY). One short turn per harness."""
import asyncio
import os
import uuid
from dataclasses import replace

import pytest

from tests.e2e.stack import ControlServer, Stack, eventually, kinds, texts
from valet.runner.harness import claude, deepseek
from valet.shared.config import PASSTHROUGH_ENV

pytestmark = pytest.mark.docker
SLOW = 180.0
AGENT = """\
name: {name}
harness: {harness}
computer:
  level: container
permissions: trusted
directives: [owner, authority/full, persona.md, environment]
"""
CAT = "Bartolomeu Quixaba"
OWNER = f"# About Alice\n\n- Lives in São Paulo.\n- His cat is called {CAT}.\n"


@pytest.mark.parametrize("harness", [claude, deepseek], ids=["claude", "deepseek"])
async def test_a_container_agent_knows_the_owner_and_its_environment(tmp_path, harness):
    if not any(os.environ.get(k) for k in harness.KEYS):
        pytest.skip(f"needs one of {', '.join(harness.KEYS)}")
    name = "dir-" + uuid.uuid4().hex[:6]
    stack = Stack(tmp_path, "load")
    url = os.environ.get("VALET_TEST_GATEWAY_URL") or f"http://host.docker.internal:{stack.ccfg.port}"
    stack.ccfg = replace(stack.ccfg, gateway_url=url, rpc_timeout=SLOW)
    stack.control = ControlServer(stack.ccfg)
    stack.rcfg = replace(stack.rcfg, harness_commands={},
                         image=os.environ.get("VALET_AGENT_IMAGE", "valet/agent-base"),
                         passthrough={k: os.environ[k] for k in PASSTHROUGH_ENV if os.environ.get(k)})
    await stack.start()
    try:
        r = await stack.http.put("/api/owner", json={"text": OWNER})
        assert r.status_code == 200, r.text
        r = await stack.http.post("/api/agents", json={"name": name, "agent_yaml": AGENT.format(
            name=name, harness="claude" if harness is claude else "deepseek")})
        assert r.status_code == 201, r.text
        told = (await stack.http.get(f"/api/agents/{name}/directives")).json()
        assert told["error"] is None and CAT in told["text"] and f"You are `{name}`" in told["text"]
        r = await stack.http.post(f"/api/agents/{name}/start")
        assert r.status_code == 200, r.text

        async def running():
            a = await stack.agent(name)
            assert a["state"] != "failed", a["error"]
            return a["state"] == "running"
        await eventually(running, timeout=SLOW, what="the container agent running")
        assert not stack.rcfg.harness_commands      # the real adapter, in the container

        r = await stack.http.post(f"/api/agents/{name}/sessions", timeout=SLOW)
        assert r.status_code == 201, r.text
        sid = r.json()["id"]
        turn = await stack.prompt(name, sid, "Without using any tools, answer from your instructions: what is "
                                             "Alice's cat called, and what is your agent name in Valet? Reply with "
                                             "just the two, separated by a comma.")

        async def ended():
            evs = [e for e in await stack.events(name, sid) if e["turn_id"] == turn]
            return evs if "turn_end" in kinds(evs) else None
        evs = await eventually(ended, timeout=SLOW, what="the turn to end")
        end = next(e["data"] for e in evs if e["kind"] == "turn_end")
        assert end["error"] is None, end
        reply = texts(evs)
        assert "Quixaba" in reply and name in reply, reply
    finally:
        await stack.close()
        for vol in (f"valet-agent-{name}-home", f"valet-agent-{name}-work"):
            p = await asyncio.create_subprocess_exec("docker", "volume", "rm", "-f", vol,
                                                     stdout=asyncio.subprocess.DEVNULL,
                                                     stderr=asyncio.subprocess.DEVNULL)
            await p.wait()
