"""Network policies and the `none` computer level, offline: what a definition allows, the exact docker argv per
policy, the proxy's rules, what each harness compiles to, and the runner driving a stand-in `docker` through the
proxy's life (network, proxy, agent; and back)."""
import json
import stat
import sys
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from runner_helpers import FAKE, Sent, host_agent, runner_cfg
from valet.runner.agent import AgentProcess
from valet.runner.egress import SCRIPT
from valet.runner.harness import Compiled, claude, deepseek
from valet.runner.launch import (
    CONFIG_MOUNT, PROXY_ENV, LaunchError, egress_name, egress_rules, network_name, plan,
)
from valet.shared.config import RunnerConfig
from valet.shared.definition import AgentDefinition, secret_name_error, validation_errors

GW = "http://host.docker.internal:8780/mcp/agents/scout"


def cfg(**kw) -> RunnerConfig:
    return RunnerConfig(**{"control_url": "ws://x/ws/runner", "token": "vrun_t", "data": Path("/srv/runner"), **kw})


def agent(**kw) -> AgentDefinition:
    return AgentDefinition.model_validate({"name": "scout", **kw})


def computer(**kw) -> AgentDefinition:
    return agent(computer=kw)


CLAUDE = Compiled(argv=["claude-agent-acp"], env={"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat"}, hosts=["api.anthropic.com"])


# ---------- the definition ----------

@pytest.mark.parametrize("level,network,policy", [
    ("container", "open", "open"), ("container", "gateway", "gateway"), ("container", "allowlist", "allowlist"),
    ("container", "none", "gateway"),       # no network at all couldn't reach the model: the same as gateway
    ("none", "open", "gateway"), ("none", "allowlist", "gateway"), ("none", "none", "gateway"),
    ("host", "gateway", None), ("host", "none", None), ("host", "open", None),
])
def test_policy_per_level_and_network(level, network, policy):
    assert computer(level=level, network=network).computer.policy == policy


def test_allowlist_takes_domains_and_keeps_them_plain():
    d = computer(network="allowlist", allowlist=["GitHub.com", " docs.python.org. ", "*.npmjs.org", "10.0.0.5"])
    assert d.computer.allowlist == ["github.com", "docs.python.org", "npmjs.org", "10.0.0.5"]


@pytest.mark.parametrize("bad", ["com", "https://github.com", "github.com/foo", "github.com:443", "*.com", "a..b",
                                 "-x.com", "x_y.com", "", "exa mple.com", "*.*.x.com", "localhost"])
def test_allowlist_refuses_what_isnt_a_domain(bad):
    with pytest.raises(ValidationError) as e:
        computer(network="allowlist", allowlist=["ok.org", bad])
    assert validation_errors(e.value) == [{"field": "computer.allowlist.1",
                                           "msg": "a domain like github.com (its subdomains come with it)"}]


def test_allowlist_refuses_a_domain_twice():
    with pytest.raises(ValidationError) as e:
        computer(allowlist=["github.com", "GitHub.com"])
    assert validation_errors(e.value) == [{"field": "computer.allowlist", "msg": "github.com is listed twice"}]


def test_no_computer_has_no_shared_folders():
    with pytest.raises(ValidationError) as e:
        computer(level="none", mounts=[{"host": "/data", "path": "/work/data"}])
    assert validation_errors(e.value) == [{"field": "computer.mounts",
                                           "msg": "an agent with no computer has no shared folders"}]
    assert computer(level="none").computer.mounts == []


def test_the_proxy_variables_cant_be_secrets():
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "NODE_USE_ENV_PROXY"):
        assert secret_name_error(name), name
    assert all(secret_name_error(k) for k in PROXY_ENV if k.isupper())


# ---------- launch plans ----------

def test_open_is_dockers_default_network():
    p = plan(computer(network="open"), CLAUDE, cfg(), gateway=GW)
    assert p.egress is None and "--network" not in p.args
    assert not any(a.startswith("HTTPS_PROXY") for a in p.args)


def test_gateway_policy_argv_and_its_proxy():
    p = plan(computer(network="gateway"), CLAUDE, cfg(), gateway=GW)
    assert p.args == [
        "run", "-i", "--rm", "--init", "--name", "valet-agent-scout", "--label", "valet.agent=scout",
        "-v", "valet-agent-scout-home:/home/agent", "-v", "valet-agent-scout-work:/work",
        "-v", "/srv/runner/agents/scout/config:/valet:ro", "-w", "/work",
        "--network", "valet-agent-scout-net",
        "-e", "HTTP_PROXY=http://valet-egress:3128", "-e", "HTTPS_PROXY=http://valet-egress:3128",
        "-e", "http_proxy=http://valet-egress:3128", "-e", "https_proxy=http://valet-egress:3128",
        "-e", "NO_PROXY=localhost,127.0.0.1,::1", "-e", "no_proxy=localhost,127.0.0.1,::1",
        "-e", "NODE_USE_ENV_PROXY=1",
        "-e", "CLAUDE_CODE_OAUTH_TOKEN", "valet/agent-base", "claude-agent-acp"]
    assert "sk-ant-oat" not in " ".join(p.args)
    e = p.egress
    assert (e.network, e.container, e.dir) == (network_name("scout"), egress_name("scout"),
                                               Path("/srv/runner/agents/scout/egress"))
    assert e.rules == {"port": 3128, "allow": [{"host": "api.anthropic.com", "ports": [443]},
                                               {"host": "host.docker.internal", "ports": [8780]}]}
    assert e.args == [
        "run", "-i", "--rm", "--init", "--name", "valet-agent-scout-egress", "--label", "valet.agent=scout",
        "--label", "valet.role=egress", "--network", "valet-agent-scout-net", "--network-alias", "valet-egress",
        "--add-host", "host.docker.internal:host-gateway", "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "-v", "/srv/runner/agents/scout/egress:/egress:ro",
        "valet/agent-base", "node", "/egress/egress.mjs", "/egress/rules.json"]


def test_the_proxy_runs_the_runners_image_not_the_agents():
    p = plan(computer(network="gateway", image="me/custom:1"), CLAUDE, cfg(image="valet/agent-base:7"))
    assert p.args[-2:] == ["me/custom:1", "claude-agent-acp"]
    assert "valet/agent-base:7" in p.egress.args


def test_without_a_gateway_only_the_model():
    p = plan(computer(network="gateway"), CLAUDE, cfg())
    assert p.egress.rules["allow"] == [{"host": "api.anthropic.com", "ports": [443]}]


def test_network_none_is_the_gateway_policy():
    a = plan(computer(network="none"), CLAUDE, cfg(), gateway=GW)
    b = plan(computer(network="gateway"), CLAUDE, cfg(), gateway=GW)
    assert (a.args, a.egress) == (b.args, b.egress)
    assert "none" not in a.args


def test_allowlist_adds_its_domains_on_web_ports():
    p = plan(computer(network="allowlist", allowlist=["github.com", "pypi.org"]), CLAUDE, cfg(), gateway=GW)
    assert p.egress.rules["allow"] == [
        {"host": "api.anthropic.com", "ports": [443]}, {"host": "host.docker.internal", "ports": [8780]},
        {"domain": "github.com", "ports": [80, 443]}, {"domain": "pypi.org", "ports": [80, 443]}]
    # The list only counts under allowlist (it's kept when the policy changes, for coming back to it).
    assert plan(computer(network="gateway", allowlist=["github.com"]), CLAUDE, cfg()).egress.rules["allow"] == [
        {"host": "api.anthropic.com", "ports": [443]}]


def test_level_none_is_a_container_without_a_workspace_on_the_gateway_policy():
    p = plan(computer(level="none", network="open"), CLAUDE, cfg(), gateway=GW)
    assert p.session_cwd == "/work" and p.cwd is None
    assert p.args[:12] == [
        "run", "-i", "--rm", "--init", "--name", "valet-agent-scout", "--label", "valet.agent=scout",
        "-v", "valet-agent-scout-home:/home/agent",         # sessions resume: its home is kept
        "-v", "/srv/runner/agents/scout/config:/valet:ro"]
    assert "valet-agent-scout-work:/work" not in p.args    # an empty /work at every start
    assert p.args[p.args.index("--network") + 1] == "valet-agent-scout-net"
    assert p.egress.rules["allow"] == [{"host": "api.anthropic.com", "ports": [443]},
                                       {"host": "host.docker.internal", "ports": [8780]}]


def test_the_host_ignores_network_policies():
    for network in ("none", "gateway", "allowlist"):
        p = plan(agent(computer={"level": "host", "network": network, "allowlist": ["x.org"]}), CLAUDE, cfg(),
                 gateway="http://127.0.0.1:8780/mcp/agents/scout")
        assert p.egress is None and p.command == "claude-agent-acp" and "HTTPS_PROXY" not in p.env


def test_unknown_model_hosts_need_an_allowlist():
    plain = Compiled(argv=["my-acp"])               # a harness Valet has no adapter for: hosts None
    with pytest.raises(LaunchError, match="doesn't know where this harness's model is"):
        plan(computer(network="gateway"), plain, cfg())
    p = plan(computer(network="allowlist", allowlist=["models.example.com"]), plain, cfg())
    assert p.egress.rules["allow"] == [{"domain": "models.example.com", "ports": [80, 443]}]
    assert plan(computer(network="open"), plain, cfg()).egress is None


def test_egress_rules_ports():
    assert egress_rules("gateway", ["a.example:8443", "b.example"], "https://valet.example.com/mcp/agents/x", []) == [
        {"host": "a.example", "ports": [8443]}, {"host": "b.example", "ports": [443]},
        {"host": "valet.example.com", "ports": [443]}]
    assert egress_rules("gateway", [], "http://valet.lan/mcp/agents/x", []) == [{"host": "valet.lan", "ports": [80]}]


def test_a_secret_cant_take_a_proxy_variable(tmp_path):
    with pytest.raises(LaunchError, match="NODE_USE_ENV_PROXY: reserved"):
        plan(computer(network="gateway"), CLAUDE, cfg(), secrets={"NODE_USE_ENV_PROXY": "0"})


# ---------- what the harnesses compile to ----------

def test_claude_without_a_computer_has_only_the_gateways_tools():
    for level in ("locked", "ask", "trusted", "unrestricted"):
        d = agent(permissions=level, computer={"level": "none"}, tools={"mcp": ["github"]})
        options = claude.compile(d, {}, cfg(), CONFIG_MOUNT).session_meta["claudeCode"]["options"]
        assert options["tools"] == []                   # no built-in tools at all
        assert options["managedSettings"] == {**claude.LOCKDOWN, "permissions": {"allow": ["mcp__valet"]}}
    bare = claude.compile(agent(computer={"level": "none"}), {}, cfg(), CONFIG_MOUNT)
    assert bare.session_meta["claudeCode"]["options"] == {"tools": [], "managedSettings": claude.LOCKDOWN,
                                                          **claude.STRICT_MCP, "skills": []}
    assert bare.hosts == ["api.anthropic.com"]


@pytest.mark.parametrize("network", ["gateway", "allowlist", "none"])
def test_claude_has_no_web_search_when_its_network_is_restricted(network):
    for level in ("ask", "trusted", "unrestricted"):
        options = claude.compile(agent(permissions=level, computer={"network": network}), {}, cfg(),
                                 CONFIG_MOUNT).session_meta["claudeCode"]["options"]
        rules = options["managedSettings"]["permissions"]
        assert rules["deny"] == ["WebSearch"] and "WebSearch" not in rules["allow"]
    locked = claude.compile(agent(permissions="locked", computer={"network": network}), {}, cfg(), CONFIG_MOUNT)
    assert locked.session_meta["claudeCode"]["options"]["tools"] == ["Read", "Glob", "Grep", "TodoWrite"]


def test_claude_keeps_web_search_with_open_internet_and_on_the_host():
    for c in ({"network": "open"}, {"level": "host", "network": "gateway"}):
        options = claude.compile(agent(computer=c), {}, cfg(), CONFIG_MOUNT).session_meta["claudeCode"]["options"]
        assert "deny" not in options["managedSettings"].get("permissions", {})


def patch(c: Compiled) -> list[dict]:
    return yaml.safe_load(c.files["dsh.patch.yml"])


def test_deepseek_without_a_computer_has_only_the_gateways_tools():
    for level in ("locked", "ask", "trusted", "unrestricted"):
        d = agent(harness="deepseek", permissions=level, computer={"level": "none"}, tools={"mcp": ["github"]})
        c = deepseek.compile(d, {}, cfg(), CONFIG_MOUNT)
        rows = patch(c)
        gate = rows[0]["insert"][0]["config"]
        assert gate == {"mode": "deny", "allow": [], "allowPrefixes": ["mcp__valet__"]}
        off = [r["id"] for r in rows if r.get("disabled") is True]
        assert off == list(deepseek.NONE_ROWS)
        assert {"tool-bash", "tool-fs", "tool-web", "tool-subagent", "plan-mode"} <= set(off)
        assert rows[1] == {"id": "permission", "config": {"presets": {"valet": {
            "sandbox": "danger-full-access", "approval": "never" if level in ("trusted", "unrestricted") else "ask"}},
            "defaultPreset": "valet"}}
        assert c.hosts == ["api.deepseek.com"]


@pytest.mark.parametrize("network,search", [("open", True), ("gateway", False), ("allowlist", False),
                                            ("none", False)])
def test_deepseek_web_search_only_with_open_internet(network, search):
    rows = patch(deepseek.compile(agent(harness="deepseek", computer={"network": network}), {}, cfg(), CONFIG_MOUNT))
    web = [r for r in rows if r.get("id") == "tool-web"]
    if search:
        assert web == []
    else:
        assert web == [{"id": "tool-web", "config": {"search": False, "fetch": True, "searchTimeoutMs": 60000}}]
    assert not any(r.get("disabled") for r in rows)


def test_deepseek_model_hosts_by_provider_route():
    hosts = lambda **kw: deepseek.compile(agent(harness="deepseek", **kw), {}, cfg(), CONFIG_MOUNT).hosts
    assert hosts() == ["api.deepseek.com"]
    assert hosts(provider="deepseek", model="deepseek-v4-pro") == ["api.deepseek.com"]
    assert hosts(provider="openrouter", model="x/y") is None        # a route Valet doesn't know


# ---------- the runner carries it out (a stand-in docker) ----------

def stand_in_docker(tmp_path: Path) -> tuple[Path, Path]:
    """A `docker` that logs each call; `run` of the proxy says it's ready and waits for its stdin to close, `run` of
    the agent is the fake ACP agent; everything else succeeds."""
    log, script = tmp_path / "docker.log", tmp_path / "docker"
    script.write_text(f"""#!/bin/sh
echo "$@" >> {log}
if [ "$1" = run ]; then
  case "$*" in
    *valet.role=egress*) echo '{{"event":"ready","port":3128}}'; echo '{{"event":"deny","method":"CONNECT","host":"x.org","port":443}}'; exec cat >/dev/null ;;
    *) exec {sys.executable} {FAKE} ;;
  esac
fi
exit 0
""")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script, log


async def test_runner_sets_up_the_network_then_the_agent_and_takes_it_down(tmp_path, sent):
    script, log = stand_in_docker(tmp_path)
    cfg_ = runner_cfg(tmp_path, docker=str(script), harness_commands={})
    p = AgentProcess(host_agent(computer={"level": "container", "network": "allowlist", "allowlist": ["x.org"]}), {},
                     cfg_, sent)
    await p.start()
    assert p.state == "running", p.error
    up = log.read_text().splitlines()
    assert up[:5] == [
        "rm -f valet-agent-scout",
        "rm -f valet-agent-scout-egress",
        "network rm valet-agent-scout-net",
        ("network create --internal -o com.docker.network.bridge.inhibit_ipv4=true --label valet.agent=scout "
         "valet-agent-scout-net"),
        (f"run -i --rm --init --name valet-agent-scout-egress --label valet.agent=scout --label valet.role=egress "
         f"--network valet-agent-scout-net --network-alias valet-egress --add-host host.docker.internal:host-gateway "
         f"--read-only --cap-drop ALL --security-opt no-new-privileges -v {tmp_path}/runner/agents/scout/egress:/egress:ro "
         f"valet/agent-base node /egress/egress.mjs /egress/rules.json")]
    assert up[5] == "network connect bridge valet-agent-scout-egress"
    assert up[6].startswith("run -i --rm --init --name valet-agent-scout ") and "--network valet-agent-scout-net" in up[6]
    egress = tmp_path / "runner" / "agents" / "scout" / "egress"
    assert (egress / "egress.mjs").read_text() == SCRIPT.read_text()
    assert json.loads((egress / "rules.json").read_text())["allow"][-1] == {"domain": "x.org", "ports": [80, 443]}

    await p.stop()
    down = log.read_text().splitlines()[len(up):]
    assert down[:3] == ["stop valet-agent-scout", "rm -f valet-agent-scout-egress", "network rm valet-agent-scout-net"]
    assert p.state == "stopped"


async def test_an_agent_whose_proxy_dies_fails(tmp_path, sent):
    script, _ = stand_in_docker(tmp_path)
    p = AgentProcess(host_agent(computer={"level": "container", "network": "gateway"}), {},
                     runner_cfg(tmp_path, docker=str(script), harness_commands={}), sent)
    await p.start()
    assert p.state == "running", p.error
    p._egress._proc.kill()
    failed = await sent.wait_for("agent.status", state="failed")
    assert failed["error"].startswith("its network proxy exited with code")
    assert p._egress is None


async def test_a_proxy_that_doesnt_start_fails_the_agent_and_cleans_up(tmp_path, sent):
    log, script = tmp_path / "docker.log", tmp_path / "docker"
    script.write_text(f"""#!/bin/sh
echo "$@" >> {log}
case "$*" in *valet.role=egress*) echo "node: cannot find /egress/egress.mjs" >&2; exit 9 ;; esac
exit 0
""")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    p = AgentProcess(host_agent(computer={"level": "container", "network": "gateway"}), {},
                     runner_cfg(tmp_path, docker=str(script), harness_commands={}), sent)
    await p.start()
    assert p.state == "failed"
    assert p.error == "its network proxy exited with code 9\nnode: cannot find /egress/egress.mjs"
    lines = log.read_text().splitlines()
    assert lines[-2:] == ["rm -f valet-agent-scout-egress", "network rm valet-agent-scout-net"]
    assert not any(l.startswith("run -i --rm --init --name valet-agent-scout ") for l in lines)   # no agent


async def test_a_network_docker_wont_create_fails_the_agent(tmp_path, sent):
    log, script = tmp_path / "docker.log", tmp_path / "docker"
    script.write_text(f"""#!/bin/sh
echo "$@" >> {log}
if [ "$1 $2" = "network create" ]; then echo "Error response from daemon: pool overlaps" >&2; exit 1; fi
exit 0
""")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    p = AgentProcess(host_agent(computer={"level": "none"}), {},
                     runner_cfg(tmp_path, docker=str(script), harness_commands={}), Sent())
    await p.start()
    assert p.state == "failed"
    assert p.error == ("cannot create its network valet-agent-scout-net: Error response from daemon: pool overlaps")
