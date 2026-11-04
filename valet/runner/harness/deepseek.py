"""DeepSeek Harness (`dsh`, pinned in docker/agent-base) through its own ACP server, the `acp` profile.

dsh speaks ACP v1 natively: sessions persist under $DSH_HOME (~/.dsh, so the agent's home volume) and continue with
session/resume (no session/load, no replay). Its permission prompts are one-shot allow or reject.

What Valet compiles for it, all of it in the agent's read-only config dir:
- the directives (shared/directives.assemble: owner, authority, persona, environment, in the definition's order) ->
  agents/AGENTS.md, with DSH_AGENTS_HOME pointing there: dsh reads it as user-global instructions. AGENTS.md is
  literal text; dsh's system-prompt persona fields would interpolate `{{name}}` and fail on unknown names.
  DSH_AGENTS_HOME is always set, so the agent's instructions come only from its definition.
- skills (shared/skills.py) -> skills/<name>/..., and the `skill-filesystem` row (skills_row) scans that directory
  and no other: not the workspace's .dsh/skills or .agents/skills (a skill there would win a name over Valet's), not
  $DSH_HOME/skills. So the agent has exactly the skills its definition gives it, and can't edit them. Its `skill`
  tool loads them; the gate lets it run at every level.
- dsh.patch.yml, a `--patch` layer (each row in it replaces that row's whole config):
  - Valet's approval gate (dsh_approval_gate.mjs, written next to the patch), which carries the permission level
    for every tool call except the read-only and bookkeeping tools in ALLOW and the MCP gateway's tools (which the
    gateway decides): locked denies, ask asks, trusted and unrestricted let it run (GATE_MODES). dsh itself only
    asks when a sandboxed command wants out of its sandbox.
    The `acp` row waits for the gate's service, so a gate that doesn't load stops the agent from starting.
  - the `permission` row (permission_row), always one preset: in a container dsh's own sandbox is off;
    unrestricted on the host turns it off there too. dsh keeps a resumed session on the preset it began with, so
    the gate also puts every top-level session, new or resumed, on that preset: a level change reaches old sessions.
  - model / provider -> the `acp` row's config: provider route and model for every session. Without a model,
    dsh's shipped default applies.
  - when the network is restricted, `tool-web` without web_search (it runs at DeepSeek, outside the computer, so
    no network policy holds it); at computer level `none`, every built-in tool's row off (NONE_ROWS), and the gate
    denies anything but the gateway's tools as well.
  - the MCP gateway, when agent.start has one (gateway_row): an `mcp-client` row of its own instead of ACP
    `mcpServers`, so its calls can wait as long as the gateway holds them (dsh's ACP server builds its MCP clients
    with a 60 s limit per call, and a call that asks the owner waits up to VALET_GATEWAY_ASK_TIMEOUT). The token is in
    the environment (GATEWAY_TOKEN_ENV), never in the file.
- DEEPSEEK_API_KEY from the runner, and telemetry off (DSH_TELEMETRY_MODE=DISABLED).
- hosts: the provider route's (ROUTE_HOSTS); a route Valet doesn't know has none it can name."""
import json
from pathlib import Path

from valet.shared import skills
from valet.shared.config import RunnerConfig
from valet.shared.definition import AgentDefinition
from valet.shared.directives import assemble
from valet.shared.protocol import GatewayAccess

from . import Compiled, HarnessError, credentials, gateway_wait_ms

COMMAND = ["dsh", "--profile", "acp"]
KEYS = ("DEEPSEEK_API_KEY",)
AGENTS_DIR = "agents"
PATCH_FILE = "dsh.patch.yml"
PATCH_HEADER = "# Written by Valet from the agent's definition. Each row replaces that row's whole config.\n"
# The definition's provider names a vendor (DESIGN.md); dsh names its provider routes. Other values pass through
# as dsh route ids.
PROVIDERS = {"deepseek": "deepseek-official"}
# Where each provider route Valet knows reaches its model (and, for deepseek-official, its model list and web
# search). Valet only routes to deepseek-official so far; dsh's other routes come from user settings it doesn't write.
ROUTE_HOSTS = {"deepseek-official": ["api.deepseek.com"]}
# dsh's built-in tools, one profile row each (dsh 0.2.1-rc.5): at computer level `none` they're all off, and the
# agent has only the gateway's tools. `plan-mode` brings exit_plan_mode, `mcp-resources` the MCP resource tools
# (list/read; the gateway serves tools only).
NONE_ROWS = ("tool-bash", "tool-pwsh", "tool-jobs", "tool-fs", "tool-fs-search", "tool-skill", "tool-subagent-control",
             "tool-subagent-list-agents", "tool-subagent", "tool-subagent-fork", "tool-workflow", "tool-todo",
             "tool-goal", "tool-web", "tool-working-directory", "plan-mode", "mcp-resources")
# The `tool-web` row with web_search off and the rest as the profile ships it (a patch row replaces its whole config).
NO_SEARCH_ROW = """\
- id: tool-web
  config:
    search: false
    fetch: true
    searchTimeoutMs: 60000
"""
# Tools that run without asking: they read, or only touch dsh's own bookkeeping (todos, goals, jobs, subagents,
# whose own tool calls pass the gate too). Anything else, including tools added later, asks.
ALLOW = ("read", "read_image", "glob", "grep", "web_search", "skill", "todo_write", "working_directory",
         "exit_plan_mode", "create_goal", "get_goal", "update_goal", "job_list", "job_output", "job_kill",
         "list_agents", "subagent", "subagent_fork", "send_message", "interrupt_agent", "workflow")
# The gateway's tools (dsh names an MCP tool mcp__<server>__<tool>; the runner names the gateway "valet"): the
# gateway decides each call from the same permission level, and asks the owner itself, so the gate lets them through.
GATEWAY_PREFIX = "mcp__valet__"
GATE_FILE = "dsh_approval_gate.mjs"
GATE_SOURCE = Path(__file__).with_name(GATE_FILE).read_text()
GATE_MODES = {"locked": "deny", "ask": "ask", "trusted": "pass", "unrestricted": "pass"}
# dsh confines shell commands with bwrap or Landlock and fails closed without one. In a container neither works
# (Docker's default seccomp profile blocks bwrap's namespaces, and Docker Desktop's kernel has no Landlock), and the
# container is the boundary anyway: sessions get full access inside it, and Valet's gate carries the level. On the
# host dsh keeps its own sandbox (workspace-write, asking to leave it) unless the agent is unrestricted.
# The row always holds exactly one preset, `valet`: dsh's default preset is a setting (in DSH_HOME, which on the host
# is the user's own ~/.dsh), and with nothing else in the table no setting can pick a freer one.
PERMISSION_ROW = """\
- id: permission
  config:
    presets:
      valet:
        sandbox: {sandbox}
        approval: {approval}
    defaultPreset: valet
"""


# dsh-skill-filesystem with Valet's skills directory as its only root (no watching: the config dir changes only
# between starts).
SKILLS_ROW = """\
- id: skill-filesystem
  config:
    includeDefaultRoots: false
    customSkillDirs: [{root}]
    watch: false
"""
# The MCP gateway as dsh's own MCP client (the plugin dsh-acp mounts for ACP mcpServers, as a profile row), where
# toolCallTimeoutMs can be set. serverName `valet` keeps the tools' names, mcp__valet__<tool>, which GATEWAY_PREFIX
# and the environment layer use. The header is a `!!js` expression dsh evaluates when it loads the profile: the token
# comes from its environment, so the config dir (world-readable) has only the URL, and `dsh --dump-config` prints the
# expression. dsh leaves names like ...TOKEN out of the environment of the commands it runs. Unlike ACP's mount, a
# gateway that can't be reached doesn't stop dsh: the client retries (10 times over about two and a half minutes,
# its defaults), and the tools appear once it connects.
GATEWAY_ROW = """\
- insert:
    - id: valet-gateway
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: valet
        transport: streamable-http
        url: {url}
        headers:
          Authorization: !!js "'Bearer ' + process.env.{env}"
        toolCallTimeoutMs: {timeout_ms}
"""
GATEWAY_TOKEN_ENV = "VALET_GATEWAY_TOKEN"


def permission_row(definition: AgentDefinition) -> str:
    """dsh's own policy under the gate, its one preset: in a container (levels container and none) full access
    (approval `never` once the level lets it run freely, else `ask`); on the host workspace-write and `ask`, or full
    access and `never` when unrestricted."""
    free = definition.permissions in ("trusted", "unrestricted")
    if definition.computer.level != "host":           # a container, with a computer or (level none) without
        return PERMISSION_ROW.format(sandbox="danger-full-access", approval="never" if free else "ask")
    if definition.permissions == "unrestricted":
        return PERMISSION_ROW.format(sandbox="danger-full-access", approval="never")
    return PERMISSION_ROW.format(sandbox="workspace-write", approval="ask")


def compile(definition: AgentDefinition, files: dict[str, str], cfg: RunnerConfig, config_dir: str,
            gateway: GatewayAccess | None = None) -> Compiled:
    """argv COMMAND + `--patch <config_dir>/dsh.patch.yml`; env the runner's KEYS, DSH_TELEMETRY_MODE=DISABLED,
    DSH_AGENTS_HOME=<config_dir>/agents; files agents/AGENTS.md (the directives, assembled from `files`, when they
    come to any text), skills/<name>/<path> (the files' skills), and dsh.patch.yml (the gate with the level's mode,
    + permission_row, + SKILLS_ROW on <config_dir>/skills, always, + the acp row with a model, + tool rows for the
    network and computer level: at `none` every built-in tool is off but `skill`, which stays when it has skills,
    + gateway_row when it has a gateway and MCP servers, its token as GATEWAY_TOKEN_ENV in env, gateway_in_config).
    A provider without a model is a HarnessError: the acp row is replaced whole, so it needs both. hosts from
    ROUTE_HOSTS (None for a route it doesn't know)."""
    if definition.provider and not definition.model:
        raise HarnessError("harness 'deepseek' needs a model when a provider is set")
    argv = list(COMMAND)
    env = {**credentials(cfg, KEYS), "DSH_TELEMETRY_MODE": "DISABLED", "DSH_AGENTS_HOME": f"{config_dir}/{AGENTS_DIR}"}
    out: dict[str, str] = {}
    text = assemble(definition.directives, files)
    if text:
        out[f"{AGENTS_DIR}/AGENTS.md"] = text + "\n"
    out[GATE_FILE] = GATE_SOURCE
    own = skills.by_skill(files)
    for name, fs in sorted(own.items()):
        for path, text in sorted(fs.items()):
            out[f"{skills.SKILLS_DIR}/{name}/{path}"] = text
    prefixes = [GATEWAY_PREFIX] if definition.gateway_servers() else []
    bare = definition.computer.level == "none"
    gate = (gate_row(f"{config_dir}/{GATE_FILE}", "deny", prefixes, allow=("skill",) if own else ()) if bare
            else gate_row(f"{config_dir}/{GATE_FILE}", GATE_MODES[definition.permissions], prefixes))
    rows = [gate, permission_row(definition), SKILLS_ROW.format(root=json.dumps(f"{config_dir}/{skills.SKILLS_DIR}"))]
    provider = definition.provider or "deepseek"
    route = PROVIDERS.get(provider, provider)
    rows.append(acp_row(route, definition.model))
    if bare:
        rows += [f"- id: {row}\n  disabled: true\n" for row in NONE_ROWS if not (own and row == "tool-skill")]
    elif definition.computer.policy not in (None, "open"):
        rows.append(NO_SEARCH_ROW)
    mounted = gateway is not None and bool(definition.gateway_servers())
    if mounted:
        rows.append(gateway_row(gateway))
        env[GATEWAY_TOKEN_ENV] = gateway.token
    out[PATCH_FILE] = PATCH_HEADER + "".join(rows)
    argv += ["--patch", f"{config_dir}/{PATCH_FILE}"]
    hosts = ROUTE_HOSTS.get(route)
    return Compiled(argv=argv, env=env, files=out, hosts=list(hosts) if hosts else None, gateway_in_config=mounted)


def gateway_row(gateway: GatewayAccess) -> str:
    """The gateway's MCP client row: its URL (a JSON string, so it can't add YAML structure or tags), the token read
    from GATEWAY_TOKEN_ENV, and the gateway's own limit per call with a margin (harness.gateway_wait_ms)."""
    return GATEWAY_ROW.format(url=json.dumps(gateway.url), env=GATEWAY_TOKEN_ENV,
                              timeout_ms=gateway_wait_ms(gateway))


def gate_row(path: str, mode: str, allow_prefixes: list[str] = (), allow: tuple[str, ...] = ALLOW) -> str:
    """Mount the gate (by absolute path: dsh resolves package names from its profile) with its mode, the tools it
    always lets through (ALLOW), and tool-name prefixes it lets through (the gateway's)."""
    row = (f"- insert:\n    - id: valet-approval\n      name: {json.dumps(path)}\n      config:\n"
           f"        mode: {mode}\n        allow: {json.dumps(list(allow))}\n")
    if allow_prefixes:
        row += f"        allowPrefixes: {json.dumps(list(allow_prefixes))}\n"
    return row


def acp_row(provider: str, model: str | None) -> str:
    """The `acp` row: it waits for the gate's service (next to the profile's own startup service), and with a model
    its config sets provider route and model for every session. Values are JSON strings (valid YAML), so a
    definition can't inject YAML structure or dsh's `!!js` tags."""
    row = "- id: acp\n  inject: [acpAppStartup, valetApprovalGate]\n"
    if model:
        row += f"  config:\n    provider: {json.dumps(provider)}\n    model: {json.dumps(model)}\n"
    return row
