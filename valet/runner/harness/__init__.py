"""Harness adapters: compile a definition into what one harness needs to run (its ACP command, env, config files,
and the extra ACP `_meta` for session/new). One module per harness; HARNESSES maps the definition's `harness` value.

An adapter is `compile(definition, files, cfg, config_dir, gateway) -> Compiled`. `config_dir` is where the agent's
compiled config files appear inside its computer (launch.config_dir): read-only to the agent, rewritten on every
start. `gateway` is agent.start's (the agent's MCP gateway endpoint and token, or None): the runner hands it to the
harness in every session/new, unless the adapter put it in the harness's own config (Compiled.gateway_in_config)."""
from dataclasses import dataclass, field, replace
from typing import Any

from valet.shared.config import RunnerConfig
from valet.shared.definition import AgentDefinition
from valet.shared.protocol import GatewayAccess


class HarnessError(Exception):
    """Unknown harness, or a definition the harness can't express; reported as agent.status failed."""


@dataclass(frozen=True)
class Compiled:
    argv: list[str]                                     # the ACP command inside the computer
    # env for the harness process: its keys, and the gateway's token when its config reads it, so kept out of repr()
    env: dict[str, str] = field(default_factory=dict, repr=False)
    session_meta: dict[str, Any] = field(default_factory=dict)  # ACP _meta for session/new and session/load
    files: dict[str, str] = field(default_factory=dict) # path relative to config_dir -> text
    mode: str | None = None     # ACP session mode the runner sets on every session (session/set_mode), if any
    mode_strict: bool = False   # the mode is the level's floor: a session that can't be put in it doesn't run
    # Where the harness reaches its model: "host" (port 443) or "host:port". A container whose network isn't open
    # reaches these and nothing else of its own (launch.plan). None: unknown (a harness Valet has no adapter for, or a
    # provider route it doesn't know), so only network `open`, or `allowlist` with the host listed, can work.
    hosts: list[str] | None = None
    # The harness reaches the MCP gateway through its own config (`files`, the token in `env`), not as an ACP MCP
    # server: its sessions get no mcpServers.
    gateway_in_config: bool = False


GATEWAY_TIMEOUT = 900.0     # seconds the gateway may hold a call when agent.start doesn't say (its defaults, 600 + 300)
GATEWAY_MARGIN = 60.0       # on top: the call's own round trip, so the gateway's answer (or its refusal) gets back


def gateway_wait_ms(gateway: GatewayAccess) -> int:
    """How long a harness's MCP client should wait for one gateway call, in milliseconds: the gateway's own limit
    (GatewayAccess.timeout, else GATEWAY_TIMEOUT) plus GATEWAY_MARGIN."""
    timeout = gateway.timeout if gateway.timeout and gateway.timeout > 0 else GATEWAY_TIMEOUT
    return int((timeout + GATEWAY_MARGIN) * 1000)


def credentials(cfg: RunnerConfig, names: tuple[str, ...]) -> dict[str, str]:
    """The runner's passthrough env limited to `names`: an agent gets its own harness's keys and no others."""
    return {k: v for k, v in cfg.passthrough.items() if k in names}


def compile_agent(definition: AgentDefinition, files: dict[str, str], cfg: RunnerConfig,
                  config_dir: str, gateway: GatewayAccess | None = None) -> Compiled:
    """Dispatch on definition.harness; cfg.harness_commands[harness], when set, replaces argv. A harness Valet
    has no adapter for still runs if the runner names its command: a plain ACP agent, nothing compiled."""
    override = cfg.harness_commands.get(definition.harness)
    adapter = HARNESSES.get(definition.harness)
    if adapter is None:
        if override:
            return Compiled(argv=list(override))
        raise HarnessError(f"harness '{definition.harness}' isn't available yet")
    compiled = adapter(definition, files, cfg, config_dir, gateway)
    return replace(compiled, argv=list(override)) if override else compiled


from . import claude, deepseek  # noqa: E402  (adapters import Compiled from here)

HARNESSES = {"claude": claude.compile, "deepseek": deepseek.compile}
KEYS = {"claude": claude.KEYS, "deepseek": deepseek.KEYS}


def runnable(cfg: RunnerConfig) -> list[str]:
    """Harnesses this runner can run: an adapter whose key it has, or any harness it has a command for. Sent in
    the hello so the settings page can tell which choices work; names only."""
    have = {h for h, keys in KEYS.items() if credentials(cfg, keys)}
    return sorted(have | set(cfg.harness_commands))
