"""DeepSeek Harness gets the MCP gateway in its own config: an `mcp-client` row in dsh.patch.yml with a per-call
limit above the gateway's own (dsh's ACP mount waits 60 s), the token in its env and nowhere in the config dir, and
no ACP mcpServers in its sessions. Claude Code keeps the ACP server, with its MCP waits raised to match."""
import json
from pathlib import Path

import pytest
import yaml
from runner_helpers import Sent, fake_cmd, host_agent, runner_cfg
from valet.runner.agent import AgentProcess
from valet.runner.harness import GATEWAY_MARGIN, GATEWAY_TIMEOUT, claude, compile_agent, deepseek, gateway_wait_ms
from valet.runner.launch import CONFIG_MOUNT, config_dir, plan
from valet.shared.config import RunnerConfig
from valet.shared.definition import AgentDefinition
from valet.shared.protocol import GatewayAccess

TOKEN = "vagt_s3cret-token"
GATEWAY = GatewayAccess(url="http://host.docker.internal:8700/mcp/agents/scout", token=TOKEN, timeout=900)


class _Loader(yaml.SafeLoader):
    """YAML with dsh's `!!js` tag kept as ("js", source)."""


_Loader.add_constructor("tag:yaml.org,2002:js", lambda loader, node: ("js", loader.construct_scalar(node)))


def rows(patch: str) -> list[dict]:
    return yaml.load(patch, Loader=_Loader)


def gateway_rows(patch: str) -> list[dict]:
    return [r for row in rows(patch) for r in row.get("insert", []) if r.get("id") == "valet-gateway"]


def cfg(**kw) -> RunnerConfig:
    return RunnerConfig(**{"control_url": "ws://x/ws/runner", "token": "vrun_t", "data": Path("/srv/runner"), **kw})


def dsh(**kw) -> AgentDefinition:
    return AgentDefinition.model_validate({"name": "scout", "harness": "deepseek", "tools": {"mcp": ["valet.agents"]},
                                           **kw})


# ---------- what the adapter compiles ----------

def test_the_gateway_is_a_profile_row_with_a_long_limit():
    c = deepseek.compile(dsh(), {}, cfg(), CONFIG_MOUNT, GATEWAY)
    assert c.gateway_in_config
    [row] = gateway_rows(c.files["dsh.patch.yml"])
    assert row == {"id": "valet-gateway", "name": "@deepseek-ai/dsh-mcp-client", "config": {
        "serverName": "valet", "transport": "streamable-http", "url": GATEWAY.url,
        "headers": {"Authorization": ("js", "'Bearer ' + process.env.VALET_GATEWAY_TOKEN")},
        "toolCallTimeoutMs": int((900 + GATEWAY_MARGIN) * 1000)}}
    # The row comes last, after the gate that lets mcp__valet__* through.
    assert rows(c.files["dsh.patch.yml"])[-1]["insert"] == [row]
    assert c.env[deepseek.GATEWAY_TOKEN_ENV] == TOKEN


def test_the_token_stays_out_of_the_files_argv_and_repr():
    c = deepseek.compile(dsh(), {}, cfg(), CONFIG_MOUNT, GATEWAY)
    assert all(TOKEN not in text for text in c.files.values())
    assert all(TOKEN not in arg for arg in c.argv)
    assert TOKEN not in repr(c)
    # In a container it's an env var of `docker run`'s process, passed by name (-e NAME), not on the command line.
    launch = plan(dsh(), c, cfg(), gateway=GATEWAY.url)
    assert all(TOKEN not in arg for arg in launch.args)
    assert launch.env[deepseek.GATEWAY_TOKEN_ENV] == TOKEN
    assert deepseek.GATEWAY_TOKEN_ENV in launch.args


@pytest.mark.parametrize("timeout, ms", [(None, (GATEWAY_TIMEOUT + GATEWAY_MARGIN) * 1000), (0, 960_000),
                                         (30, 90_000), (1200.5, 1_260_500)])
def test_the_limit_follows_the_gateways(timeout, ms):
    g = GatewayAccess(url=GATEWAY.url, token=TOKEN, timeout=timeout)
    assert gateway_wait_ms(g) == int(ms)
    [row] = gateway_rows(deepseek.compile(dsh(), {}, cfg(), CONFIG_MOUNT, g).files["dsh.patch.yml"])
    assert row["config"]["toolCallTimeoutMs"] == int(ms)


def test_no_row_without_a_gateway_or_mcp_servers():
    for c in (deepseek.compile(dsh(), {}, cfg(), CONFIG_MOUNT),
              deepseek.compile(dsh(tools={}), {}, cfg(), CONFIG_MOUNT, GATEWAY)):
        assert not c.gateway_in_config
        assert gateway_rows(c.files["dsh.patch.yml"]) == []
        assert deepseek.GATEWAY_TOKEN_ENV not in c.env


@pytest.mark.parametrize("computer", [{"level": "none"}, {"network": "gateway"}, {"network": "none"},
                                      {"network": "allowlist", "allowlist": ["example.com"]}, {"level": "host"}])
def test_every_level_and_network_gets_the_row(computer):
    c = deepseek.compile(dsh(computer=computer), {}, cfg(), CONFIG_MOUNT, GATEWAY)
    assert c.gateway_in_config and len(gateway_rows(c.files["dsh.patch.yml"])) == 1
    # A restricted container reaches it through its proxy: the gateway's host and port are in the proxy's rules.
    if computer.get("level") != "host":
        launch = plan(dsh(computer=computer), c, cfg(), gateway=GATEWAY.url)
        if launch.egress is not None:
            assert {"host": "host.docker.internal", "ports": [8700]} in launch.egress.rules["allow"]


def test_a_url_cant_add_yaml():
    evil = GatewayAccess(url='http://x/"\n- id: permission\n  disabled: true\n#', token=TOKEN)
    patch = deepseek.compile(dsh(), {}, cfg(), CONFIG_MOUNT, evil).files["dsh.patch.yml"]
    [row] = gateway_rows(patch)
    assert row["config"]["url"] == evil.url
    assert not any(r.get("disabled") for r in rows(patch))


def test_an_override_command_keeps_the_config():
    c = compile_agent(dsh(), {}, cfg(harness_commands={"deepseek": ["my-dsh"]}), CONFIG_MOUNT, GATEWAY)
    assert c.argv == ["my-dsh"] and c.gateway_in_config and c.env[deepseek.GATEWAY_TOKEN_ENV] == TOKEN


def test_claude_waits_as_long_as_the_gateway():
    """Claude Code keeps the ACP `http` server; its per-call and idle limits (MCP_TOOL_TIMEOUT,
    CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT) are raised to the gateway's, when it has one."""
    d = AgentDefinition.model_validate({"name": "scout", "tools": {"mcp": ["valet.agents"]}})
    c = claude.compile(d, {}, cfg(), CONFIG_MOUNT, GATEWAY)
    assert not c.gateway_in_config
    assert {k: c.env[k] for k in claude.MCP_WAIT_ENV} == {k: "960000" for k in claude.MCP_WAIT_ENV}
    for plain in (claude.compile(d, {}, cfg(), CONFIG_MOUNT),
                  claude.compile(AgentDefinition(name="scout"), {}, cfg(), CONFIG_MOUNT, GATEWAY)):
        assert not set(claude.MCP_WAIT_ENV) & set(plain.env)


# ---------- what the runner does with it ----------

def dsh_runner(tmp_path: Path, *extra: str) -> RunnerConfig:
    return runner_cfg(tmp_path, harness_commands={"deepseek": fake_cmd(*extra)})


async def _say(proc: AgentProcess, sent: Sent, sid: str, turn: str, prompt: str) -> str:
    await proc.prompt(sid, turn, [{"type": "text", "text": prompt}])
    await sent.wait_for("turn.end", turn_id=turn)
    return sent.texts(sid)[-1]


async def test_dsh_sessions_get_no_mcp_servers(tmp_path, sent):
    d = host_agent(harness="deepseek", tools={"mcp": ["valet.agents"]})
    first = AgentProcess(d, {}, dsh_runner(tmp_path), sent, gateway=GATEWAY)
    await first.start()
    try:
        assert first.state == "running", first.error
        sid = (await first.new_session())["session_id"]
        assert json.loads(await _say(first, sent, sid, "t1", "mcp")) == []
        env = json.loads(await _say(first, sent, sid, "t2", f"env {deepseek.GATEWAY_TOKEN_ENV}"))
        assert env == {deepseek.GATEWAY_TOKEN_ENV: TOKEN}
        local = config_dir(d, first.cfg)[0]
        patch = (local / "dsh.patch.yml").read_text()
        assert len(gateway_rows(patch)) == 1
        assert all(TOKEN not in f.read_text() for f in local.rglob("*") if f.is_file())
    finally:
        await first.stop()
    # A session loaded (or resumed) by a later process gets none either.
    for caps, sid2 in (("load", sid), ("resume", "old-session")):
        p = AgentProcess(d, {}, dsh_runner(tmp_path, "--caps", caps), sent, gateway=GATEWAY)
        await p.start()
        try:
            assert json.loads(await _say(p, sent, sid2, f"t-{caps}", "mcp")) == []
        finally:
            await p.stop()
    # Nothing else the runner sent has it (the fake agent printed it in turn t2, asked to).
    assert all(TOKEN not in json.dumps(m) for m in sent if m.get("turn_id") != "t2")
