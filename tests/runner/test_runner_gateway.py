"""The runner's half of the MCP gateway: agent.start's `gateway` reaches the harness as an `http` MCP server named
"valet" in every session/new, load and resume; the harness configs let its tools through without asking twice;
a container can reach the host."""
import json
from dataclasses import replace
from pathlib import Path

import pytest
from runner_helpers import Sent, host_agent, runner_cfg
from valet.runner.agent import AgentProcess
from valet.runner.harness import Compiled, claude, deepseek
from valet.runner.launch import CONFIG_MOUNT, plan
from valet.runner.supervisor import Supervisor
from valet.shared.config import RunnerConfig
from valet.shared.definition import AgentDefinition
from valet.shared.protocol import GatewayAccess, parse_control_message

GATEWAY = GatewayAccess(url="http://host.docker.internal:8700/mcp/agents/scout", token="vagt_t0ken")
VALET = {"type": "http", "name": "valet", "url": GATEWAY.url,
         "headers": [{"name": "Authorization", "value": "Bearer vagt_t0ken"}]}


def text(t: str) -> list[dict]:
    return [{"type": "text", "text": t}]


async def servers(proc: AgentProcess, sent: Sent, sid: str, turn: str) -> list[dict]:
    """The MCP servers the fake agent got for this session."""
    await proc.prompt(sid, turn, text("mcp"))
    await sent.wait_for("turn.end", turn_id=turn)
    return json.loads([t for t in sent.texts(sid) if t.startswith("[")][-1])


async def test_new_and_loaded_sessions_get_the_gateway(tmp_path, sent):
    cfg = runner_cfg(tmp_path)
    first = AgentProcess(host_agent(tools={"mcp": ["valet.agents"]}), {}, cfg, sent, gateway=GATEWAY)
    await first.start()
    sid = (await first.new_session())["session_id"]
    assert await servers(first, sent, sid, "t1") == [VALET]
    await first.stop()

    second = AgentProcess(host_agent(tools={"mcp": ["valet.agents"]}), {}, cfg, sent, gateway=GATEWAY)
    await second.start()
    try:
        assert await servers(second, sent, sid, "t2") == [VALET]       # session/load
    finally:
        await second.stop()


async def test_resumed_sessions_get_the_gateway(tmp_path, sent):
    p = AgentProcess(host_agent(), {}, runner_cfg(tmp_path, "--caps", "resume"), sent, gateway=GATEWAY)
    await p.start()
    try:
        assert await servers(p, sent, "old-session", "t1") == [VALET]
    finally:
        await p.stop()


async def test_no_gateway_without_a_token_or_http_support(tmp_path, sent):
    p = AgentProcess(host_agent(), {}, runner_cfg(tmp_path), sent)
    await p.start()
    try:
        sid = (await p.new_session())["session_id"]
        assert await servers(p, sent, sid, "t1") == []
    finally:
        await p.stop()
    # A harness that can't take HTTP MCP servers starts anyway, without them.
    p = AgentProcess(host_agent(), {}, runner_cfg(tmp_path, "--no-mcp-http"), sent, gateway=GATEWAY)
    await p.start()
    try:
        assert p.state == "running"
        sid = (await p.new_session())["session_id"]
        assert await servers(p, sent, sid, "t2") == []
    finally:
        await p.stop()


async def test_agent_start_hands_the_gateway_to_the_process(tmp_path, sent):
    sup = Supervisor(runner_cfg(tmp_path), sent)
    d = host_agent(tools={"mcp": ["valet.agents"]}).model_dump(mode="json")
    msg = parse_control_message({"type": "agent.start", "id": "c1", "agent": "scout", "definition": d, "files": {},
                                 "gateway": GATEWAY.model_dump()})
    try:
        assert await sup.handle(msg) == {}
        await sent.wait_for("agent.status", state="running")
        assert sup._agents["scout"].gateway == GATEWAY
        sid = (await sup.handle(parse_control_message({"type": "session.new", "id": "c2", "agent": "scout"})))[
            "session_id"]
        assert await servers(sup._agents["scout"], sent, sid, "t1") == [VALET]
    finally:
        await sup.shutdown()


# ---------- what the harnesses and the container get ----------

def cfg(**kw) -> RunnerConfig:
    return RunnerConfig(**{"control_url": "ws://x/ws/runner", "token": "vrun_t", "data": Path("/srv/runner"), **kw})


def agent(**kw) -> AgentDefinition:
    return AgentDefinition.model_validate({"name": "scout", **kw})


@pytest.mark.parametrize("level", ["locked", "ask", "trusted", "unrestricted"])
@pytest.mark.parametrize("computer", ["container", "host"])
def test_claude_lets_the_gateway_tools_through(level, computer):
    """A managed allow rule, at every level and on the host too: Claude Code's sessions run with
    allowManagedPermissionRulesOnly, under which `allowedTools` doesn't count."""
    def options(**kw):
        d = agent(permissions=level, computer={"level": computer}, **kw)
        return claude.compile(d, {}, cfg(), CONFIG_MOUNT).session_meta["claudeCode"]["options"]
    with_gw, without = options(tools={"mcp": ["valet.agents", "github"]}), options()
    read_only = claude.READ_ONLY_TOOLS if computer == "container" else []
    assert with_gw["managedSettings"]["permissions"] == {"allow": [*read_only, "mcp__valet"]}
    assert with_gw["managedSettings"]["allowManagedPermissionRulesOnly"] is True
    assert "allowedTools" not in with_gw
    assert without["managedSettings"].get("permissions", {}).get("allow", []) == read_only
    # locked keeps its built-in tool list; MCP tools aren't built-in, and the gateway lists it only read-only ones
    assert with_gw.get("tools") == (claude.READ_ONLY_TOOLS if level == "locked" else None)


def test_deepseek_gate_lets_the_gateway_tools_through():
    patch = deepseek.compile(agent(harness="deepseek", tools={"mcp": ["valet.agents"]}), {}, cfg(),
                             CONFIG_MOUNT).files["dsh.patch.yml"]
    assert '        allowPrefixes: ["mcp__valet__"]\n' in patch
    plain = deepseek.compile(agent(harness="deepseek"), {}, cfg(), CONFIG_MOUNT).files["dsh.patch.yml"]
    assert "allowPrefixes" not in plain
    assert "config.allowPrefixes" in deepseek.GATE_SOURCE
    # Next to the gate's other duty: every top-level session on the level's one preset (the permission row).
    assert "- id: permission\n" in patch and "defaultPreset: valet" in patch
    assert "ctx.on('session/created'" in deepseek.GATE_SOURCE and "'permissionPresets'" in deepseek.GATE_SOURCE


def test_containers_with_the_gateway_can_reach_the_host():
    c = Compiled(argv=["claude-agent-acp"])
    url = "http://host.docker.internal:8780/mcp/agents/scout"
    with_gw = plan(agent(), c, cfg(), gateway=url).args
    assert with_gw[with_gw.index("--add-host") + 1] == "host.docker.internal:host-gateway"
    assert "--add-host" not in plan(agent(), c, cfg()).args
    assert "--add-host" not in plan(agent(computer={"level": "host"}), c, cfg(), gateway=url).args
    # A restricted network reaches it through its proxy (which has the --add-host), never directly.
    walled = plan(agent(computer={"network": "gateway"}), replace(c, hosts=[]), cfg(), gateway=url)
    assert "--add-host" not in walled.args
    assert {"host": "host.docker.internal", "ports": [8780]} in walled.egress.rules["allow"]
    assert walled.egress.args[walled.egress.args.index("--add-host") + 1] == "host.docker.internal:host-gateway"
