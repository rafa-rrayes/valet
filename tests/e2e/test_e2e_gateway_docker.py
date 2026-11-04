"""The gateway with the real thing: a Claude Code (or DeepSeek Harness) agent in a container (the agent-base image)
gets Valet's gateway through ACP and calls `valet.agents` through it, without the harness asking: at `ask` (Claude's
managed allow rule, dsh's gate prefix) and, for Claude, at `locked` (its `tools` list names built-in tools only). Opt-in: VALET_TEST_DOCKER=1, the image built,
and the harness's key in the environment (CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY; DEEPSEEK_API_KEY). The
container reaches the control plane at http://host.docker.internal:<port> (Docker Desktop; on Linux the runner's
--add-host maps it to the host, and the control plane must listen where the bridge can reach it);
VALET_TEST_GATEWAY_URL overrides that."""
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
permissions: {permissions}
tools:
  mcp: [valet.agents]
"""


@pytest.mark.parametrize("harness, permissions", [(claude, "ask"), (claude, "locked"), (deepseek, "ask")],
                         ids=["claude-ask", "claude-locked", "deepseek-ask"])
async def test_a_container_agent_calls_valet_agents(tmp_path, harness, permissions):
    if not any(os.environ.get(k) for k in harness.KEYS):
        pytest.skip(f"needs one of {', '.join(harness.KEYS)}")
    name = "gwprobe-" + uuid.uuid4().hex[:6]
    stack = Stack(tmp_path, "load")
    url = os.environ.get("VALET_TEST_GATEWAY_URL") or f"http://host.docker.internal:{stack.ccfg.port}"
    stack.ccfg = replace(stack.ccfg, gateway_url=url, rpc_timeout=SLOW)
    stack.control = ControlServer(stack.ccfg)
    stack.rcfg = replace(stack.rcfg, harness_commands={},
                         image=os.environ.get("VALET_AGENT_IMAGE", "valet/agent-base"),
                         passthrough={k: os.environ[k] for k in PASSTHROUGH_ENV if os.environ.get(k)})
    await stack.start()
    try:
        r = await stack.http.post("/api/agents", json={"name": name, "agent_yaml": AGENT.format(
            name=name, harness="claude" if harness is claude else "deepseek", permissions=permissions)})
        assert r.status_code == 201, r.text
        r = await stack.http.post(f"/api/agents/{name}/start")
        assert r.status_code == 200, r.text

        async def running():
            a = await stack.agent(name)
            assert a["state"] != "failed", a["error"]
            return a["state"] == "running"
        await eventually(running, timeout=SLOW, what="the container agent running")
        assert (await stack.agent(name))["capabilities"]["mcpCapabilities"]["http"] is True
        assert not stack.rcfg.harness_commands      # the real adapter, in the container

        r = await stack.http.post(f"/api/agents/{name}/sessions", timeout=SLOW)
        assert r.status_code == 201, r.text
        sid = r.json()["id"]
        turn = await stack.prompt(name, sid, "Call the list_agents tool from the valet MCP server, then reply with "
                                             "only the value of the `you` field it returned.")

        async def ended():
            evs = [e for e in await stack.events(name, sid) if e["turn_id"] == turn]
            return evs if "turn_end" in kinds(evs) else None
        evs = await eventually(ended, timeout=SLOW, what="the turn to end")
        end = next(e["data"] for e in evs if e["kind"] == "turn_end")
        assert end["error"] is None, end
        assert name in texts(evs), texts(evs)
        assert "permission_request" not in kinds(evs)   # the harness didn't ask: the gateway decides its tools
        r = await stack.http.get("/api/gateway/calls", params={"agent": name})
        calls = [(c["server"], c["tool"], c["decision"], c["outcome"]) for c in r.json()["calls"]]
        assert ("valet.agents", "list_agents", "allow", "ok") in calls, calls
        assert await stack.pending() == []          # read-only: nothing asked, in the harness or in the gateway
    finally:
        await stack.close()
        for vol in (f"valet-agent-{name}-home", f"valet-agent-{name}-work"):
            p = await asyncio.create_subprocess_exec("docker", "volume", "rm", "-f", vol,
                                                     stdout=asyncio.subprocess.DEVNULL,
                                                     stderr=asyncio.subprocess.DEVNULL)
            await p.wait()
