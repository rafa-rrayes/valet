"""Network policies and the `none` level with the real thing: each harness in a container (the agent-base image)
behind its own egress proxy, with the real control plane's gateway. Opt-in: VALET_TEST_DOCKER=1, the image built,
and the harness's key (CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY; DEEPSEEK_API_KEY). One tiny prompt per test:
they spend real credit.

- allowlist: from inside the container, curl reaches a listed site and gets the proxy's 403 for any other, and
  nothing at all without the proxy; the model answers, and calls `valet.agents` through the gateway (both only
  through the proxy).
- none: the harness has no built-in tools, only the gateway's; /work is empty.

The container reaches the control plane at http://host.docker.internal:<port> (VALET_TEST_GATEWAY_URL overrides).
Names start with `net-`; containers, networks and volumes are removed at the end."""
import asyncio
import contextlib
import json
import logging
import os
import re
import uuid
from dataclasses import replace

import pytest

from tests.e2e.stack import ControlServer, Stack, eventually, kinds, texts
from valet.runner.harness import claude, deepseek
from valet.shared.config import PASSTHROUGH_ENV

pytestmark = pytest.mark.docker
SLOW = 180.0
SITE = "example.com"            # allowlisted
OTHER = "github.com"            # not
AGENT = """\
name: {name}
harness: {harness}
computer:
  level: {level}
  network: allowlist
  allowlist: [{site}]
permissions: ask
tools:
  mcp: [valet.agents]
"""
HARNESSES = [pytest.param(claude, id="claude"), pytest.param(deepseek, id="deepseek")]


async def sh(*args: str) -> tuple[int, str]:
    p = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await p.communicate()
    return p.returncode, out.decode(errors="replace").strip()


async def curl(name: str, *args: str) -> str:
    """curl inside the agent's container -> "<http code> <error text>"."""
    _, out = await sh("docker", "exec", f"valet-agent-{name}", "curl", "-sS", "-m", "15", "-o", "/dev/null",
                      "-w", "%{http_code}", *args)
    return out


@contextlib.asynccontextmanager
async def run_agent(tmp_path, harness, level: str, caplog):
    """Start a stack and the agent; yield (stack, name); clean up everything it made in Docker."""
    if not any(os.environ.get(k) for k in harness.KEYS):
        pytest.skip(f"needs one of {', '.join(harness.KEYS)}")
    caplog.set_level(logging.INFO, logger="valet.runner")
    caplog.set_level(logging.DEBUG, logger="valet.runner.egress")     # every allowed connection too
    name = "net-" + uuid.uuid4().hex[:6]
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
            name=name, harness="claude" if harness is claude else "deepseek", level=level, site=SITE)})
        assert r.status_code == 201, r.text
        r = await stack.http.post(f"/api/agents/{name}/start")
        assert r.status_code == 200, r.text

        async def running():
            a = await stack.agent(name)
            assert a["state"] != "failed", a["error"]
            return a["state"] == "running"
        await eventually(running, timeout=SLOW, what="the container agent running")
        yield stack, name
    finally:
        await stack.close()
        await sh("docker", "rm", "-f", f"valet-agent-{name}", f"valet-agent-{name}-egress")
        await sh("docker", "network", "rm", f"valet-agent-{name}-net")
        await sh("docker", "volume", "rm", "-f", f"valet-agent-{name}-home", f"valet-agent-{name}-work")
        _, left = await sh("docker", "ps", "-a", "--filter", f"label=valet.agent={name}", "--format", "{{.Names}}")
        assert not left, left


async def one_turn(stack, name: str, text: str):
    r = await stack.http.post(f"/api/agents/{name}/sessions", timeout=SLOW)
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    turn = await stack.prompt(name, sid, text)

    async def ended():
        evs = [e for e in await stack.events(name, sid) if e["turn_id"] == turn]
        return evs if "turn_end" in kinds(evs) else None
    evs = await eventually(ended, timeout=SLOW, what="the turn to end")
    end = next(e["data"] for e in evs if e["kind"] == "turn_end")
    assert end["error"] is None, end
    return evs


def blocked(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "network blocked" in r.getMessage()]


def reached(caplog) -> set[str]:
    """host:port of every connection the proxy let through (its debug log lines)."""
    out = set()
    for r in caplog.records:
        msg = r.getMessage()
        if r.name == "valet.runner.egress" and ": network {" in msg:
            e = json.loads(msg.split(": network ", 1)[1])
            if e.get("event") == "allow":
                out.add(f"{e['host']}:{e['port']}")
    return out


@pytest.mark.parametrize("harness", HARNESSES)
async def test_allowlist_reaches_the_model_the_gateway_and_listed_sites_only(tmp_path, harness, caplog):
    async with run_agent(tmp_path, harness, "container", caplog) as (stack, name):
        _, nets = await sh("docker", "inspect", "-f", "{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}",
                           f"valet-agent-{name}")
        assert nets.split() == [f"valet-agent-{name}-net"]
        _, internal = await sh("docker", "network", "inspect", "-f", "{{.Internal}}", f"valet-agent-{name}-net")
        assert internal == "true"

        # From inside the container, without the model.
        assert await curl(name, f"https://{SITE}/") == "200"
        assert await curl(name, f"http://www.{SITE}/") in ("200", "301", "302")    # subdomains, plain HTTP
        other = await curl(name, f"https://{OTHER}/")
        assert "CONNECT tunnel failed, response 403" in other, other
        assert any(f"{OTHER}:443" in m for m in blocked(caplog))
        direct = await curl(name, "--noproxy", "*", f"https://{SITE}/")           # no route out but the proxy
        assert direct.endswith("000") and "Could not resolve host" in direct, direct
        _, gw_ip = await sh("docker", "network", "inspect", "-f", "{{range .IPAM.Config}}{{.Gateway}}{{end}}",
                            f"valet-agent-{name}-net")
        assert gw_ip == ""                  # no address for the Docker host on its network (inhibit_ipv4)

        # The model answers, and the gateway works, through the proxy.
        evs = await one_turn(stack, name, "Call the list_agents tool from the valet MCP server, then reply with only "
                                          "the value of the `you` field it returned.")
        assert name in texts(evs), texts(evs)
        assert "permission_request" not in kinds(evs)
        r = await stack.http.get("/api/gateway/calls", params={"agent": name})
        assert ("valet.agents", "list_agents", "allow", "ok") in [
            (c["server"], c["tool"], c["decision"], c["outcome"]) for c in r.json()["calls"]]
        print(f"\n{harness.__name__}: blocked {blocked(caplog)}; reached {sorted(reached(caplog))}")
        model = (harness.HOSTS if harness is claude else harness.ROUTE_HOSTS["deepseek-official"])[0]
        assert not any(model in m for m in blocked(caplog))


@pytest.mark.parametrize("harness", HARNESSES)
async def test_no_computer_has_only_the_gateways_tools(tmp_path, harness, caplog):
    async with run_agent(tmp_path, harness, "none", caplog) as (stack, name):
        code, work = await sh("docker", "exec", f"valet-agent-{name}", "ls", "-A", "/work")
        assert code == 0 and work == ""
        _, mounts = await sh("docker", "inspect", "-f", "{{range .Mounts}}{{.Destination}} {{end}}",
                             f"valet-agent-{name}")
        assert sorted(mounts.split()) == ["/home/agent", "/valet"]
        evs = await one_turn(stack, name, "List the names of every tool you can call, comma-separated, exactly as "
                                          "you call them, and nothing else.")
        reply = texts(evs)
        assert "list_agents" in reply, reply
        for builtin in ("Bash", "bash", "Read", "read", "Write", "write", "Edit", "edit", "WebFetch", "web_fetch",
                        "WebSearch", "web_search", "Glob", "glob", "Grep", "grep", "Task", "subagent", "todo_write",
                        "list_mcp_resources", "read_mcp_resource", "exit_plan_mode"):
            assert not re.search(rf"(?<![\w_]){builtin}(?![\w_])", reply), (builtin, reply)
        print(f"\n{harness.__name__}: tools: {reply}; blocked {blocked(caplog)}; reached {sorted(reached(caplog))}")
