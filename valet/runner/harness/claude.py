"""Claude Code through the ACP adapter @agentclientprotocol/claude-agent-acp (pinned in docker/agent-base)."""
import json

from valet.shared import skills
from valet.shared.config import RunnerConfig
from valet.shared.definition import AgentDefinition
from valet.shared.directives import assemble
from valet.shared.protocol import GatewayAccess

from . import Compiled, credentials, gateway_wait_ms

COMMAND = ["claude-agent-acp"]
KEYS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")   # The owner's subscription token, or an API key

# Permission level -> Claude Code permission mode, which the runner sets with ACP session/set_mode on every session
# it creates, loads or resumes (a resumed session would otherwise carry on in its transcript's last mode).
# `trusted` lets it loose inside its own computer; on the host it only accepts edits and still asks before commands.
MODES = {"locked": "default", "ask": "default", "trusted": "bypassPermissions", "unrestricted": "bypassPermissions"}
HOST_TRUSTED = "acceptEdits"
# Reading, searching and planning; nothing that edits, runs or fetches. `locked`: the only tools it has. In a
# container they never ask, at any level (Claude Code would ask before a web search or a read outside the session's
# cwd); on the host Claude Code's own asking stays. (Claude Code 2.1.312 has TodoWrite off.)
READ_ONLY_TOOLS = ["Read", "Glob", "Grep", "WebSearch", "TodoWrite"]
# Claude Code's policy tier (managed settings outrank every settings file), sent with every session: only its
# permission rules and hooks count. The agent can write its own settings (~/.claude, .claude/ in its workspace), and
# allow rules or hooks it wrote at a freer level would otherwise outlive a downgrade. Under this lock `allowedTools`
# no longer counts either, so what Valet pre-approves is a managed allow rule.
LOCKDOWN = {"allowManagedPermissionRulesOnly": True, "allowManagedHooksOnly": True}
# The MCP gateway's tools (the server the runner names "valet": mcp__valet__<tool>) never ask in Claude Code, at any
# level and on the host too: the gateway decides each call from the same permission level and asks the owner itself, so
# asking here as well would ask twice. A managed allow rule, since `allowedTools` doesn't count under LOCKDOWN.
GATEWAY_TOOLS = "mcp__valet"
# How long Claude Code waits for one MCP call, in ms, from its environment (read in Claude Code 2.1.312): the call's
# limit, and how long a call to an HTTP server may go without a word from it, 5 minutes by default. A gateway call
# that asks the owner stays silent until the owner answers (up to VALET_GATEWAY_ASK_TIMEOUT), so both are set to what the
# gateway may take (harness.gateway_wait_ms). The gateway is the only MCP server a Valet agent has.
MCP_WAIT_ENV = ("MCP_TOOL_TIMEOUT", "CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT")
# Skills (shared/skills.py) are a local plugin in the config dir, read-only like the rest: the Agent SDK's `plugins`
# option loads PLUGIN_DIR, whose skills/ holds the agent's skills, and Claude Code names them <PLUGIN>:<skill>. The
# SDK's `skills` option is a filter on what the model is offered and the Skill tool loads: it lists exactly those,
# so the agent has no skill but the ones its definition gives it (none it wrote into ~/.claude or .claude/, none of
# Claude Code's bundled ones). Loading one never asks: it's the owner's text (a managed allow rule for the tool).
PLUGIN_DIR = "claude-plugin"
PLUGIN = "valet"
PLUGIN_MANIFEST = json.dumps({"name": PLUGIN, "description": "The agent's skills, from its Valet definition"}) + "\n"
SKILL_TOOL = "Skill"
# Where Claude Code reaches its model, with a subscription token or an API key. Watched through the proxy (Claude
# Code 2.1.312, claude-agent-acp 0.99.1), starting and a turn with a tool call reached nothing else. WebFetch's
# domain check (api/web/domain_info) is on this host too.
HOSTS = ["api.anthropic.com"]
# Claude Code's web search runs on Anthropic's side, outside the agent's computer, so a network policy can't hold it
# to anything: when the network is restricted it's denied (a managed deny rule), and `locked` doesn't list it.
WEB_SEARCH = "WebSearch"
# Only the MCP servers Valet gives it (the gateway, in the session's mcpServers): never the ones in the user's
# ~/.claude.json (user and project scopes) or a project's .mcp.json, which a host agent, running as the runner's user
# in the owner's own folders, would otherwise load and call around the gateway, its rules and its audit log.
STRICT_MCP = {"strictMcpConfig": True}


def compile(definition: AgentDefinition, files: dict[str, str], cfg: RunnerConfig, config_dir: str,
            gateway: GatewayAccess | None = None) -> Compiled:
    """argv COMMAND; env {"CLAUDE_AGENT_ACP_PROTOCOL": "v1"} + the runner's KEYS + {"ANTHROPIC_MODEL": model} if a
    model is set + MCP_WAIT_ENV (gateway_wait_ms) with a gateway; session_meta {"systemPrompt": {"append":
    directives}} when the definition's directives come to any text (shared/directives.assemble over `files`: owner,
    authority, persona, environment, memory, in its order; the adapter appends it to Claude Code's preset prompt),
    and {"claudeCode": {"options":
    {"managedSettings": LOCKDOWN + {"permissions": {"allow": READ_ONLY_TOOLS off the host, + GATEWAY_TOOLS with
    gateway servers (gateway_servers(): tools.mcp, and valet.memory with memory), + SKILL_TOOL with skills, "deny":
    [WEB_SEARCH] when the network is restricted}}} + for
    `locked` {"tools": READ_ONLY_TOOLS (+ SKILL_TOOL with skills)}, "skills": ["valet:<name>", ...] (always: []
    without skills), and with skills "plugins": the local plugin in PLUGIN_DIR}}; mode from MODES; files: the plugin
    (its manifest, and skills/<name>/<path> from the files' skills/). `tools` names built-in tools only: the
    gateway's MCP tools stay, and for `locked` the gateway lists only read-only ones. At computer level `none` the
    only built-in tool is SKILL_TOOL, with skills ({"tools": []} without): the rest are the gateway's. hosts HOSTS.
    The adapter serves ACP v1 and a draft v2; v1 is what the acp library speaks. The gateway itself isn't in the
    config: the runner passes it in session/new."""
    env = {"CLAUDE_AGENT_ACP_PROTOCOL": "v1", **credentials(cfg, KEYS)}
    if definition.model:
        env["ANTHROPIC_MODEL"] = definition.model
    if gateway is not None and definition.gateway_servers():
        env.update({name: str(gateway_wait_ms(gateway)) for name in MCP_WAIT_ENV})
    meta = {}
    text = assemble(definition.directives, files)
    if text:
        meta["systemPrompt"] = {"append": text}
    level, host = definition.permissions, definition.computer.level == "host"
    own = skills.by_skill(files)
    bare = definition.computer.level == "none"
    walled = definition.computer.policy not in (None, "open")
    read_only = [t for t in READ_ONLY_TOOLS if not (walled and t == WEB_SEARCH)]
    managed = dict(LOCKDOWN)
    rules: dict[str, list[str]] = {}
    allow = [] if host or bare else list(read_only)
    if definition.gateway_servers():
        allow.append(GATEWAY_TOOLS)
    if own:
        allow.append(SKILL_TOOL)
    if allow:
        rules["allow"] = allow
    if walled and not bare:
        rules["deny"] = [WEB_SEARCH]
    if rules:
        managed["permissions"] = rules
    options = {"managedSettings": managed, **STRICT_MCP}
    skill_tool = [SKILL_TOOL] if own else []
    if bare:
        options["tools"] = skill_tool
    elif level == "locked":
        options["tools"] = read_only + skill_tool
    out: dict[str, str] = {}
    if own:
        out[f"{PLUGIN_DIR}/.claude-plugin/plugin.json"] = PLUGIN_MANIFEST
        for name, fs in sorted(own.items()):
            for path, text in sorted(fs.items()):
                out[f"{PLUGIN_DIR}/{skills.SKILLS_DIR}/{name}/{path}"] = text
        options["plugins"] = [{"type": "local", "path": f"{config_dir}/{PLUGIN_DIR}", "skipMcpDiscovery": True}]
    options["skills"] = [f"{PLUGIN}:{name}" for name in sorted(own)]
    meta["claudeCode"] = {"options": options}
    mode = MODES[level]
    if level == "trusted" and host:
        mode = HOST_TRUSTED
    return Compiled(argv=list(COMMAND), env=env, session_meta=meta, files=out, mode=mode,
                    mode_strict=level in ("locked", "ask"), hosts=list(HOSTS))
