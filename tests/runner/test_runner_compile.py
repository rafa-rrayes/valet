"""Harness compilation and launch plans: pure functions, no processes, so the exact docker argv is pinned here."""
import json
from pathlib import Path

import pytest
import yaml

from valet.runner.harness import Compiled, HarnessError, compile_agent, runnable
from valet.runner.harness import claude, deepseek
from valet.runner.launch import CONFIG_MOUNT, LaunchError, config_dir, container_name, plan
from valet.shared.config import RunnerConfig
from valet.shared.definition import AgentDefinition

KEYS = {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat-test", "ANTHROPIC_API_KEY": "sk-ant-test", "DEEPSEEK_API_KEY": "sk-ds"}


def cfg(**kw) -> RunnerConfig:
    return RunnerConfig(**{"control_url": "ws://x/ws/runner", "token": "vrun_t", "data": Path("/srv/runner"), **kw})


def agent(**kw) -> AgentDefinition:
    return AgentDefinition.model_validate({"name": "scout", **kw})


# ---------- claude ----------

def test_claude_minimal():
    c = claude.compile(agent(), {}, cfg(), CONFIG_MOUNT)
    managed = {**claude.LOCKDOWN, "permissions": {"allow": claude.READ_ONLY_TOOLS}}
    # skills: [] -- no skill but the ones its definition gives it, and it gives none
    assert c == Compiled(argv=["claude-agent-acp"], env={"CLAUDE_AGENT_ACP_PROTOCOL": "v1"},
                         session_meta={"claudeCode": {"options": {"managedSettings": managed, "strictMcpConfig": True,
                                                                  "skills": []}}},
                         mode="default", mode_strict=True, hosts=["api.anthropic.com"])


def test_claude_model_and_persona():
    c = claude.compile(agent(model="claude-opus-5-5"), {"persona.md": "Be terse.\n"}, cfg(), CONFIG_MOUNT)
    assert c.env == {"CLAUDE_AGENT_ACP_PROTOCOL": "v1", "ANTHROPIC_MODEL": "claude-opus-5-5"}
    assert c.session_meta["systemPrompt"] == {"append": "Be terse."}
    assert c.files == {}


def test_claude_persona_only_when_directed_and_non_empty():
    for d, files in [(agent(directives=[]), {"persona.md": "x"}), (agent(), {"persona.md": "  \n"})]:
        assert "systemPrompt" not in claude.compile(d, files, cfg(), CONFIG_MOUNT).session_meta


def test_claude_gets_only_claude_keys():
    c = claude.compile(agent(), {}, cfg(passthrough=KEYS), CONFIG_MOUNT)
    assert c.env == {"CLAUDE_AGENT_ACP_PROTOCOL": "v1", "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat-test",
                     "ANTHROPIC_API_KEY": "sk-ant-test"}


def test_runner_reads_every_harness_key_from_its_env():
    env = {"VALET_RUNNER_TOKEN": "vrun_x", "HOME": "/home/x", "OTHER_SECRET": "no", **KEYS}
    assert RunnerConfig.from_env(env).passthrough == KEYS


# ---------- deepseek ----------

GATE_ROW_START = '- insert:\n    - id: valet-approval\n      name: "/srv/cfg/dsh_approval_gate.mjs"\n'
ACP_WAITS = "- id: acp\n  inject: [acpAppStartup, valetApprovalGate]\n"


def test_deepseek_on_the_host_minimal():
    c = deepseek.compile(agent(harness="deepseek", computer={"level": "host"}), {}, cfg(), "/srv/cfg")
    assert c.argv == ["dsh", "--profile", "acp", "--patch", "/srv/cfg/dsh.patch.yml"]
    assert c.env == {"DSH_TELEMETRY_MODE": "DISABLED", "DSH_AGENTS_HOME": "/srv/cfg/agents"}
    assert c.files["dsh_approval_gate.mjs"] == deepseek.GATE_SOURCE
    patch = c.files["dsh.patch.yml"]
    assert patch.startswith(deepseek.PATCH_HEADER + GATE_ROW_START) and patch.endswith(ACP_WAITS)
    # On the host dsh keeps its own sandbox, as the one preset: nothing in the user's ~/.dsh can pick another.
    assert deepseek.PERMISSION_ROW.format(sandbox="workspace-write", approval="ask") in patch


def test_deepseek_gate_asks_for_everything_but_reading_and_bookkeeping():
    patch = deepseek.compile(agent(harness="deepseek"), {}, cfg(), "/srv/cfg").files["dsh.patch.yml"]
    allow = json.loads(patch.split("allow: ", 1)[1].split("\n", 1)[0])
    assert {"read", "grep", "glob", "todo_write"} <= set(allow)
    assert not {"bash", "write", "edit", "web_fetch"} & set(allow)


def test_deepseek_in_a_container_turns_its_own_sandbox_off_but_keeps_asking():
    c = deepseek.compile(agent(harness="deepseek"), {}, cfg(), CONFIG_MOUNT)
    assert c.argv == ["dsh", "--profile", "acp", "--patch", "/valet/dsh.patch.yml"]
    patch = c.files["dsh.patch.yml"]
    assert "        mode: ask\n" in patch
    assert deepseek.PERMISSION_ROW.format(sandbox="danger-full-access", approval="ask") in patch
    assert "sandbox: danger-full-access\n        approval: ask\n" in patch
    assert '"/valet/dsh_approval_gate.mjs"' in patch


def test_deepseek_persona_model_and_keys():
    c = deepseek.compile(agent(harness="deepseek", model="deepseek-v4-pro"), {"persona.md": "You are Scout.\n\n"},
                         cfg(passthrough=KEYS), CONFIG_MOUNT)
    assert c.env == {"DEEPSEEK_API_KEY": "sk-ds", "DSH_TELEMETRY_MODE": "DISABLED", "DSH_AGENTS_HOME": "/valet/agents"}
    assert c.files["agents/AGENTS.md"] == "You are Scout.\n"
    assert c.files["dsh.patch.yml"].endswith(
        ACP_WAITS + '  config:\n    provider: "deepseek-official"\n    model: "deepseek-v4-pro"\n')
    assert c.session_meta == {}


def test_deepseek_persona_only_when_directed_and_non_empty():
    for d, files in [(agent(harness="deepseek", directives=[]), {"persona.md": "x"}),
                     (agent(harness="deepseek"), {"persona.md": " \n"})]:
        assert "agents/AGENTS.md" not in deepseek.compile(d, files, cfg(), CONFIG_MOUNT).files


# ---------- directives: both harnesses get the same layers, joined in the definition's order ----------

LAYERS = {"owner.md": "# About Alice\nHis cat is Bartolomeu.\n", "authority/full.md": "# Standing authority\nAct.\n",
          "persona.md": "You are Scout. Say {{hi}}.\n", "environment.md": "# Your environment\n\nA container.\n",
          "authority/strict.md": "Never seen: not listed.\n"}
JOINED = ("# About Alice\nHis cat is Bartolomeu.\n\n# Standing authority\nAct.\n\nYou are Scout. Say {{hi}}.\n\n"
          "# Your environment\n\nA container.")
FULL = ["owner", "authority/full", "persona.md", "environment"]


def test_claude_appends_every_layer_to_the_system_prompt():
    c = claude.compile(agent(directives=FULL), LAYERS, cfg(), CONFIG_MOUNT)
    assert c.session_meta["systemPrompt"] == {"append": JOINED}
    assert c.files == {}
    # The order is the definition's; an empty owner profile is left out.
    c = claude.compile(agent(directives=["environment", "owner", "persona.md"]), {**LAYERS, "owner.md": ""}, cfg(),
                       CONFIG_MOUNT)
    assert c.session_meta["systemPrompt"]["append"] == ("# Your environment\n\nA container.\n\n"
                                                        "You are Scout. Say {{hi}}.")


def test_deepseek_writes_every_layer_to_agents_md():
    c = deepseek.compile(agent(harness="deepseek", directives=FULL), LAYERS, cfg(), CONFIG_MOUNT)
    assert c.files["agents/AGENTS.md"] == JOINED + "\n"     # literal: dsh doesn't interpolate AGENTS.md
    assert "systemPrompt" not in c.session_meta
    c = deepseek.compile(agent(harness="deepseek", directives=["owner", "memory"]), LAYERS, cfg(), CONFIG_MOUNT)
    assert c.files["agents/AGENTS.md"] == "# About Alice\nHis cat is Bartolomeu.\n"


def test_nothing_to_say_means_no_instructions():
    for d in (agent(directives=["owner", "environment"]), agent(harness="deepseek", directives=["owner"])):
        c = compile_agent(d, {"persona.md": "Unlisted."}, cfg(), CONFIG_MOUNT)
        assert "systemPrompt" not in c.session_meta and "agents/AGENTS.md" not in c.files


def test_deepseek_provider_route_and_quoting():
    c = deepseek.compile(agent(harness="deepseek", provider="my-route", model='m" !!js x\nnext: 1'), {}, cfg(),
                         CONFIG_MOUNT)
    lines = c.files["dsh.patch.yml"].splitlines()
    assert lines[-2:] == ['    provider: "my-route"', '    model: "m\\" !!js x\\nnext: 1"']   # one quoted scalar


def test_deepseek_provider_needs_a_model():
    with pytest.raises(HarnessError, match="needs a model"):
        deepseek.compile(agent(harness="deepseek", provider="deepseek"), {}, cfg(), CONFIG_MOUNT)


# ---------- dispatch ----------

def test_compile_agent_dispatches_and_override_replaces_argv_only():
    assert compile_agent(agent(harness="deepseek"), {}, cfg(), CONFIG_MOUNT).argv[0] == "dsh"
    c = compile_agent(agent(model="m"), {}, cfg(harness_commands={"claude": ["python", "fake.py"]}), CONFIG_MOUNT)
    assert c.argv == ["python", "fake.py"]
    assert c.env["ANTHROPIC_MODEL"] == "m"


def test_unknown_harness():
    with pytest.raises(HarnessError, match="harness 'codex' isn't available yet"):
        compile_agent(agent(harness="codex"), {}, cfg(), CONFIG_MOUNT)


def test_unknown_harness_with_a_command_runs_as_plain_acp():
    c = compile_agent(agent(harness="mine"), {}, cfg(harness_commands={"mine": ["my-acp", "--stdio"]}), CONFIG_MOUNT)
    assert c == Compiled(argv=["my-acp", "--stdio"])


# ---------- launch ----------

def test_config_dir():
    assert config_dir(agent(), cfg()) == (Path("/srv/runner/agents/scout/config"), "/valet")
    host = config_dir(agent(computer={"level": "host"}), cfg())
    assert host == (Path("/srv/runner/agents/scout/config"), "/srv/runner/agents/scout/config")


def test_config_dir_is_absolute_for_docker():
    local, _ = config_dir(agent(), cfg(data=Path("data/runner")))
    assert local.is_absolute()


def test_plan_host():
    c = Compiled(argv=["claude-agent-acp", "--x"], env={"A": "1"})
    p = plan(agent(computer={"level": "host"}), c, cfg())
    assert (p.command, p.args, p.env) == ("claude-agent-acp", ["--x"], {"A": "1"})
    assert p.cwd == p.session_cwd == "/srv/runner/agents/scout/work"


def test_plan_container_default():
    c = Compiled(argv=["claude-agent-acp"], env={"CLAUDE_AGENT_ACP_PROTOCOL": "v1", "ANTHROPIC_API_KEY": "sk-secret"})
    p = plan(agent(), c, cfg())
    assert p.command == "docker" and p.cwd is None and p.session_cwd == "/work"
    assert p.env == c.env
    assert p.args == [
        "run", "-i", "--rm", "--init", "--name", "valet-agent-scout", "--label", "valet.agent=scout",
        "-v", "valet-agent-scout-home:/home/agent", "-v", "valet-agent-scout-work:/work",
        "-v", "/srv/runner/agents/scout/config:/valet:ro", "-w", "/work",
        "-e", "ANTHROPIC_API_KEY", "-e", "CLAUDE_AGENT_ACP_PROTOCOL",
        "valet/agent-base", "claude-agent-acp"]
    assert "sk-secret" not in " ".join(p.args)     # values travel in the docker CLI's env, never on argv


def test_plan_container_mounts():
    d = agent(computer={"level": "container", "mounts": [
        {"host": "/data/a", "path": "/mnt/a"}, {"host": "/data/b", "path": "/mnt/b", "readonly": True}]})
    p = plan(d, Compiled(argv=["acp", "--stdio"]), cfg())
    assert p.args == [
        "run", "-i", "--rm", "--init", "--name", "valet-agent-scout", "--label", "valet.agent=scout",
        "-v", "valet-agent-scout-home:/home/agent", "-v", "valet-agent-scout-work:/work",
        "-v", "/srv/runner/agents/scout/config:/valet:ro", "-w", "/work",
        "-v", "/data/a:/mnt/a", "-v", "/data/b:/mnt/b:ro",
        "valet/agent-base", "acp", "--stdio"]
    assert p.egress is None             # network open: Docker's default network, no proxy


def test_plan_container_image_and_docker_binary():
    p = plan(agent(computer={"image": "valet/agent-full"}), Compiled(argv=["a", "b"]),
             cfg(docker="/usr/bin/podman", image="ignored"))
    assert p.command == "/usr/bin/podman"
    assert p.args[-3:] == ["valet/agent-full", "a", "b"]
    assert "--network" not in p.args


def test_plan_runner_default_image():
    p = plan(agent(), Compiled(argv=["acp"]), cfg(image="my/image:1"))
    assert p.args[-2:] == ["my/image:1", "acp"]


def test_plan_without_a_command():
    with pytest.raises(LaunchError, match="the harness has no command"):
        plan(agent(), Compiled(argv=[]), cfg())


def test_container_name():
    assert container_name("scout") == "valet-agent-scout"


# ---------- permission levels ----------

@pytest.mark.parametrize("level,container,host", [
    ("locked", "default", "default"),
    ("ask", "default", "default"),
    ("trusted", "bypassPermissions", "acceptEdits"),     # free in its own computer; on the host it asks to run
    ("unrestricted", "bypassPermissions", "bypassPermissions"),
])
def test_claude_mode_per_level_and_computer(level, container, host):
    assert claude.compile(agent(permissions=level), {}, cfg(), CONFIG_MOUNT).mode == container
    assert claude.compile(agent(permissions=level, computer={"level": "host"}), {}, cfg(), "/srv/cfg").mode == host


def test_claude_locked_has_only_read_only_tools():
    c = claude.compile(agent(permissions="locked"), {"persona.md": "Be terse."}, cfg(), CONFIG_MOUNT)
    assert c.session_meta["systemPrompt"] == {"append": "Be terse."}           # next to the persona, not instead
    assert c.session_meta["claudeCode"]["options"]["tools"] == claude.READ_ONLY_TOOLS
    assert not {"Bash", "Edit", "Write", "MultiEdit", "NotebookEdit", "WebFetch", "Task"} & set(claude.READ_ONLY_TOOLS)
    for level in ("ask", "trusted", "unrestricted"):
        assert "tools" not in claude.compile(agent(permissions=level), {}, cfg(), CONFIG_MOUNT).session_meta[
            "claudeCode"]["options"]


@pytest.mark.parametrize("level", ["locked", "ask", "trusted", "unrestricted"])
def test_claude_counts_only_valets_permission_rules_and_hooks(level):
    """Managed settings, at every level: allow rules and hooks the agent wrote into its own settings files don't
    count. In a container reading and searching never ask (the allow rules live here: `allowedTools` wouldn't
    count under the lock); on the host Claude Code's own asking stays."""
    for computer, allow in (("container", {"permissions": {"allow": claude.READ_ONLY_TOOLS}}), ("host", {}),
                            ("none", {})):
        c = claude.compile(agent(permissions=level, computer={"level": computer}), {}, cfg(), CONFIG_MOUNT)
        options = c.session_meta["claudeCode"]["options"]
        assert options["managedSettings"] == {"allowManagedPermissionRulesOnly": True, "allowManagedHooksOnly": True,
                                              **allow}, computer
        assert "allowedTools" not in options


@pytest.mark.parametrize("level,gate,container,host", [
    # gate mode; dsh's one preset (sandbox, approval) in a container (its sandbox can't work there) and on the host
    ("locked", "deny", ("danger-full-access", "ask"), ("workspace-write", "ask")),
    ("ask", "ask", ("danger-full-access", "ask"), ("workspace-write", "ask")),
    ("trusted", "pass", ("danger-full-access", "never"), ("workspace-write", "ask")),
    ("unrestricted", "pass", ("danger-full-access", "never"), ("danger-full-access", "never")),
])
def test_deepseek_gate_mode_and_dsh_policy_per_level_and_computer(level, gate, container, host):
    for computer, (sandbox, approval) in (("container", container), ("host", host)):
        c = deepseek.compile(agent(harness="deepseek", permissions=level, computer={"level": computer}), {}, cfg(),
                             "/srv/cfg")
        assert c.mode is None                   # dsh has no session modes: the gate and the patch carry the level
        rows = yaml.safe_load(c.files["dsh.patch.yml"])     # every combination is a valid patch
        gate_row = rows[0]["insert"][0]
        assert gate_row["id"] == "valet-approval" and gate_row["config"]["mode"] == gate
        assert gate_row["config"]["allow"] == list(deepseek.ALLOW)
        permission = [r for r in rows if r.get("id") == "permission"]
        assert permission == [{"id": "permission", "config": {"presets": {"valet": {
            "sandbox": sandbox, "approval": approval}}, "defaultPreset": "valet"}}], computer
        assert rows[-1]["id"] == "acp"


# ---------- what a runner can run (its hello) ----------

def test_runnable_harnesses_have_a_key_or_a_command():
    assert runnable(cfg()) == []
    assert runnable(cfg(passthrough={"CLAUDE_CODE_OAUTH_TOKEN": "t"})) == ["claude"]
    assert runnable(cfg(passthrough={"ANTHROPIC_API_KEY": "sk"})) == ["claude"]
    assert runnable(cfg(passthrough={"DEEPSEEK_API_KEY": "sk"})) == ["deepseek"]
    assert runnable(cfg(passthrough=KEYS)) == ["claude", "deepseek"]
    # A command makes it runnable without a key (the tests' fake agent, a plain ACP agent Valet has no adapter for).
    assert runnable(cfg(harness_commands={"mine": ["my-acp"], "claude": ["fake"]})) == ["claude", "mine"]


def test_runnable_names_only():
    assert not any(v in str(runnable(cfg(passthrough=KEYS))) for v in KEYS.values())


# ---------- skills ----------

SKILL = "---\nname: code-word\ndescription: Use when asked for the code word.\n---\n\nSay PELICAN.\n"
SKILL_FILES = {"persona.md": "Be terse.", "skills/code-word/SKILL.md": SKILL,
               "skills/code-word/refs/words.md": "PELICAN\n", "skills/other/SKILL.md": "---\nname: other\n---\n",
               "skills/Bad Name/SKILL.md": "x", "skills/../escape": "x", "notes.md": "not a skill"}


def test_claude_skills_are_a_read_only_plugin_and_the_only_skills_it_has():
    c = claude.compile(agent(), SKILL_FILES, cfg(), CONFIG_MOUNT)
    assert c.files == {"claude-plugin/.claude-plugin/plugin.json": claude.PLUGIN_MANIFEST,
                       "claude-plugin/skills/code-word/SKILL.md": SKILL,
                       "claude-plugin/skills/code-word/refs/words.md": "PELICAN\n",
                       "claude-plugin/skills/other/SKILL.md": "---\nname: other\n---\n"}
    assert json.loads(claude.PLUGIN_MANIFEST)["name"] == "valet"
    options = c.session_meta["claudeCode"]["options"]
    assert options["plugins"] == [{"type": "local", "path": "/valet/claude-plugin", "skipMcpDiscovery": True}]
    assert options["skills"] == ["valet:code-word", "valet:other"]      # the SDK's filter: these and no others
    assert options["managedSettings"]["permissions"]["allow"] == [*claude.READ_ONLY_TOOLS, "Skill"]
    assert c.session_meta["systemPrompt"] == {"append": "Be terse."}
    assert "tools" not in options


def test_claude_skills_on_the_host_and_when_locked():
    c = claude.compile(agent(computer={"level": "host"}, permissions="locked"), SKILL_FILES, cfg(), "/srv/cfg")
    options = c.session_meta["claudeCode"]["options"]
    assert options["plugins"][0]["path"] == "/srv/cfg/claude-plugin"
    assert options["managedSettings"]["permissions"] == {"allow": ["Skill"]}    # loading one never asks
    assert options["tools"] == [*claude.READ_ONLY_TOOLS, "Skill"]               # locked: it can still load them
    without = claude.compile(agent(permissions="locked"), {}, cfg(), CONFIG_MOUNT).session_meta["claudeCode"]["options"]
    assert without["tools"] == claude.READ_ONLY_TOOLS and "plugins" not in without and without["skills"] == []


def test_deepseek_skills_and_the_only_root_it_scans():
    c = deepseek.compile(agent(harness="deepseek"), SKILL_FILES, cfg(), CONFIG_MOUNT)
    assert {k: v for k, v in c.files.items() if k.startswith("skills/")} == {
        "skills/code-word/SKILL.md": SKILL, "skills/code-word/refs/words.md": "PELICAN\n",
        "skills/other/SKILL.md": "---\nname: other\n---\n"}
    rows = yaml.safe_load(c.files["dsh.patch.yml"])
    assert {"id": "skill-filesystem", "config": {"includeDefaultRoots": False, "customSkillDirs": ["/valet/skills"],
                                                  "watch": False}} in rows
    assert "skill" in deepseek.ALLOW                    # the gate lets the skill tool load them at every level


def test_deepseek_scans_only_valets_root_even_without_skills():
    """The workspace's .dsh/skills or .agents/skills would win a name over Valet's, and ~/.dsh/skills is the
    agent's to write: none of them is scanned, skills or not."""
    c = deepseek.compile(agent(harness="deepseek", computer={"level": "host"}), {}, cfg(), "/srv/cfg")
    rows = yaml.safe_load(c.files["dsh.patch.yml"])
    row = next(r for r in rows if r.get("id") == "skill-filesystem")
    assert row["config"] == {"includeDefaultRoots": False, "customSkillDirs": ["/srv/cfg/skills"], "watch": False}
    assert not any(k.startswith("skills/") for k in c.files)
    assert rows[-1]["id"] == "acp"


def test_level_none_keeps_its_skills_and_nothing_else_built_in():
    """No computer means no built-in tools, but skills are the owner's read-only text, so loading one stays: a narrow
    agent's skills say how to use its gateway tools."""
    d = agent(computer={"level": "none"}, permissions="trusted")
    options = claude.compile(d, SKILL_FILES, cfg(), CONFIG_MOUNT).session_meta["claudeCode"]["options"]
    assert options["tools"] == ["Skill"] and options["skills"] == ["valet:code-word", "valet:other"]
    assert options["managedSettings"]["permissions"] == {"allow": ["Skill"]}
    assert claude.compile(d, {}, cfg(), CONFIG_MOUNT).session_meta["claudeCode"]["options"]["tools"] == []

    ds = agent(harness="deepseek", computer={"level": "none"}, permissions="trusted")
    rows = yaml.safe_load(deepseek.compile(ds, SKILL_FILES, cfg(), CONFIG_MOUNT).files["dsh.patch.yml"])
    off = {r["id"] for r in rows if r.get("disabled")}
    assert off == set(deepseek.NONE_ROWS) - {"tool-skill"}
    gate = next(r for r in rows if "insert" in r)["insert"][0]["config"]
    assert gate["mode"] == "deny" and gate["allow"] == ["skill"]
    rows = yaml.safe_load(deepseek.compile(ds, {}, cfg(), CONFIG_MOUNT).files["dsh.patch.yml"])
    assert {r["id"] for r in rows if r.get("disabled")} == set(deepseek.NONE_ROWS)
    assert next(r for r in rows if "insert" in r)["insert"][0]["config"]["allow"] == []


def test_claude_uses_only_valets_mcp_servers():
    # A host agent runs as the runner's user in the owner's folders: without strictMcpConfig Claude Code would load
    # ~/.claude.json's servers (user and project scope) and .mcp.json, and call them around the gateway.
    for level in ("none", "container", "host"):
        c = claude.compile(agent(computer={"level": level}), {}, cfg(), CONFIG_MOUNT)
        assert c.session_meta["claudeCode"]["options"]["strictMcpConfig"] is True
