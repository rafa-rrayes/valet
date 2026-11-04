"""DeepSeek Harness and the MCP gateway with the real thing: dsh in a container (the agent-base image) gets the gateway
from its own config (an `mcp-client` row in dsh.patch.yml, the token in its env), not from ACP's mcpServers, so a
call can wait longer than the 60 s dsh's ACP server allows.

- keyless: dsh connects to the gateway with its token and lists its tools as it starts, on the open network, under
  `gateway` (through its proxy) and at level `none`; the token isn't on any command line, in the config dir, in
  ~/.dsh or in stderr, and `dsh --dump-config` shows the header's expression, not its value.
- keyed (DEEPSEEK_API_KEY, one short turn): the model calls a tool that asks; the ask is answered after more than
  60 s, and the tool still runs and its result reaches the model.

Opt-in: VALET_TEST_DOCKER=1 and the image built. The container reaches the control plane at
http://host.docker.internal:<port> (VALET_TEST_GATEWAY_URL overrides). Names start with `m2n-`; the containers,
networks and volumes a test makes are removed at its end."""
import asyncio
import contextlib
import json
import logging
import os
import time
import uuid
from dataclasses import replace

import pytest

from tests import fake_mcp
from tests.e2e.stack import ControlServer, Stack, eventually, kinds, texts
from valet.runner.harness import deepseek
from valet.shared.config import PASSTHROUGH_ENV

pytestmark = pytest.mark.docker
SLOW = 180.0
AGENT = """\
name: {name}
harness: deepseek
computer: {computer}
permissions: ask
tools:
  mcp: [{servers}]
"""
REGISTRY = f"""\
servers:
  fake:
    description: The test upstream
    command: {json.dumps(fake_mcp.COMMAND[0])}
    args: [{json.dumps(fake_mcp.COMMAND[1])}]
"""


async def sh(*args: str) -> tuple[int, str]:
    p = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await p.communicate()
    return p.returncode, out.decode(errors="replace").strip()


@contextlib.asynccontextmanager
async def run_agent(tmp_path, computer: str, servers: str = "valet.agents", passthrough: bool = False):
    """A stack whose gateway records who listed its tools, and a running dsh agent; yields (stack, name, listed)."""
    name = "m2n-" + uuid.uuid4().hex[:6]
    stack = Stack(tmp_path, "load")
    url = os.environ.get("VALET_TEST_GATEWAY_URL") or f"http://host.docker.internal:{stack.ccfg.port}"
    stack.ccfg = replace(stack.ccfg, gateway_url=url, rpc_timeout=SLOW)
    stack.control = ControlServer(stack.ccfg)
    stack.rcfg = replace(stack.rcfg, harness_commands={},
                         image=os.environ.get("VALET_AGENT_IMAGE", "valet/agent-base"),
                         passthrough={k: os.environ[k] for k in PASSTHROUGH_ENV if passthrough and os.environ.get(k)})
    listed: list[str] = []
    await stack.start()
    try:
        gw = stack.control.server.config.app.state.control.gateway
        tools = gw.tools

        async def recording(d, *a, **kw):
            listed.append(d.name)
            return await tools(d, *a, **kw)
        gw.tools = recording
        if "fake" in servers:
            r = await stack.http.put("/api/gateway/registry", json={"text": REGISTRY})
            assert r.status_code == 200, r.text
        r = await stack.http.post("/api/agents", json={"name": name, "agent_yaml": AGENT.format(
            name=name, computer=computer, servers=servers)})
        assert r.status_code == 201, r.text
        r = await stack.http.post(f"/api/agents/{name}/start")
        assert r.status_code == 200, r.text

        async def running():
            a = await stack.agent(name)
            assert a["state"] != "failed", a["error"]
            return a["state"] == "running"
        await eventually(running, timeout=SLOW, what="the container agent running")
        yield stack, name, listed
    finally:
        await stack.close()
        await sh("docker", "rm", "-f", f"valet-agent-{name}", f"valet-agent-{name}-egress")
        await sh("docker", "network", "rm", f"valet-agent-{name}-net")
        await sh("docker", "volume", "rm", "-f", f"valet-agent-{name}-home", f"valet-agent-{name}-work")
        _, left = await sh("docker", "ps", "-a", "--filter", f"label=valet.agent={name}", "--format", "{{.Names}}")
        assert not left, left


@pytest.mark.parametrize("computer", ["{level: container}", "{level: container, network: gateway}", "{level: none}"],
                         ids=["open", "gateway", "none"])
async def test_dsh_reaches_the_gateway_from_its_config(tmp_path, computer, caplog):
    caplog.set_level(logging.DEBUG, logger="valet")
    async with run_agent(tmp_path, computer) as (stack, name, listed):
        await eventually(lambda: name in listed, timeout=60, what="dsh listing the gateway's tools")
        proc = stack.runner.supervisor._agents[name]
        token = proc.gateway.token
        assert proc.compiled.gateway_in_config and proc.mcp_servers() == []
        # Not on a command line (the docker CLI's, or any process's in the container), not in a file it can read.
        assert all(token not in a for a in proc.launch.args)
        code, out = await sh("docker", "exec", f"valet-agent-{name}", "sh", "-c",
                             'for p in /proc/[0-9]*; do tr "\\0" " " < $p/cmdline 2>/dev/null; echo; done')
        assert code == 0 and "dsh" in out and token not in out
        code, out = await sh("docker", "exec", f"valet-agent-{name}", "sh", "-c",
                             f'grep -rlF "{token}" /valet "$HOME" 2>/dev/null; true')
        assert code == 0 and out == "", out
        # In dsh's env (the header reads it); not in the env of the commands it runs (dsh drops names like *TOKEN).
        code, out = await sh("docker", "exec", f"valet-agent-{name}", "sh", "-c",
                             f'tr "\\0" "\\n" < /proc/1/environ | grep -c "^{deepseek.GATEWAY_TOKEN_ENV}="')
        assert out == "1", out
        code, dump = await sh("docker", "exec", f"valet-agent-{name}", "dsh", "--profile", "acp", "--patch",
                              "/valet/dsh.patch.yml", "--dump-config")
        assert code == 0, dump
        assert "valet-gateway" in dump and "toolCallTimeoutMs: 960000" in dump, dump
        assert token not in dump and f"process.env.{deepseek.GATEWAY_TOKEN_ENV}" in dump
        assert all(token not in line for line in proc._stderr)
        assert not any("mcp-client(valet)" in line for line in proc._stderr), list(proc._stderr)
        assert all(token not in r.getMessage() for r in caplog.records)


@pytest.mark.skipif(not os.environ.get("DEEPSEEK_API_KEY"), reason="needs DEEPSEEK_API_KEY")
async def test_an_ask_answered_after_a_minute_still_runs(tmp_path):
    async with run_agent(tmp_path, "{level: container}", "fake", passthrough=True) as (stack, name, listed):
        await eventually(lambda: name in listed, timeout=60, what="dsh listing the gateway's tools")
        r = await stack.http.post(f"/api/agents/{name}/sessions", timeout=SLOW)
        assert r.status_code == 201, r.text
        sid = r.json()["id"]
        turn = await stack.prompt(name, sid, "Call the add_note tool from the valet MCP server with the text "
                                             "\"patience\", then reply with only the exact text it returned.")
        [item] = await eventually(stack.pending, timeout=SLOW, what="the gateway's ask in the inbox")
        assert item["kind"] == "tool" and (item["tool"]["server"], item["tool"]["name"]) == ("fake", "add_note")
        asked = time.monotonic()
        await asyncio.sleep(70)                         # dsh's ACP mount would have given up at 60 s
        r = await stack.http.post(f"/api/inbox/{item['id']}/answer", json={"option_id": "allow_once"})
        assert r.status_code == 200, r.text

        async def ended():
            evs = [e for e in await stack.events(name, sid) if e["turn_id"] == turn]
            return evs if "turn_end" in kinds(evs) else None
        evs = await eventually(ended, timeout=SLOW, what="the turn to end")
        print(f"\nanswered after {time.monotonic() - asked:.0f} s; reply: {texts(evs)!r}")
        end = next(e["data"] for e in evs if e["kind"] == "turn_end")
        assert end["error"] is None, end
        r = await stack.http.get("/api/gateway/calls", params={"agent": name})
        calls = [(c["server"], c["tool"], c["decision"], c["outcome"]) for c in r.json()["calls"]]
        assert ("fake", "add_note", "ask", "ok") in calls, calls
        assert "noted: patience" in texts(evs), texts(evs)
