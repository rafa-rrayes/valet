"""Where a harness runs: the computer level decides the command line around the harness's ACP command, and the
network policy the egress proxy around a container. Pure functions of (definition, compiled harness, config) so
they're tested without Docker; agent.py and egress.py carry them out."""
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from valet.shared.config import RunnerConfig
from valet.shared.definition import AgentDefinition, secret_name_error

from .harness import Compiled


class LaunchError(Exception):
    """A definition this runner can't launch (yet); reported as agent.status failed with this message."""


@dataclass(frozen=True)
class Egress:
    """A container agent's network policy, carried out by egress.py: the agent's own internal network, and its proxy
    (egress.mjs) on that network as PROXY_ALIAS and on OUT_NETWORK as well."""
    network: str                # the agent's internal network
    container: str              # the proxy's container
    dir: Path                   # on the runner's disk: egress.mjs and rules.json, mounted read-only in the proxy
    rules: dict[str, Any]       # rules.json: {"port": PROXY_PORT, "allow": [{"host"|"domain": str, "ports": [int]}]}
    args: list[str]             # `docker run` arguments for the proxy (after the docker CLI itself)


@dataclass(frozen=True)
class Launch:
    command: str                # executable to spawn on the runner's machine
    args: list[str]
    # Added to the spawned process's environment (see docs/M0.md for the base env). Holds the harness's keys and the
    # agent's secrets, so it's kept out of repr().
    env: dict[str, str] = field(repr=False)
    cwd: str | None             # working directory of the spawned process
    session_cwd: str            # the cwd sent in ACP session/new and session/load: a path inside the computer
    egress: Egress | None = None    # a container whose network isn't open: its proxy, started before it


CONFIG_MOUNT = "/valet"      # where a container sees its compiled config dir, read-only
GATEWAY_HOST = "host.docker.internal:host-gateway"
PROXY_ALIAS = "valet-egress"     # the proxy's name on the agent's network
PROXY_PORT = 3128
OUT_NETWORK = "bridge"           # Docker's default network: the proxy's way out
# Ports an allowlisted domain is reached on. A model host is 443 unless its adapter says otherwise; the gateway, its
# URL's port.
WEB_PORTS = [80, 443]
# Set inside a container whose network isn't open, as `-e NAME=value` (they aren't secret, and in the docker CLI's
# own environment they would send the CLI itself to a proxy it can't reach). Lowercase too: curl reads only a
# lowercase http_proxy. Node's fetch and http modules honour them with NODE_USE_ENV_PROXY=1 (Node 24). Nothing else
# is reachable, so a program that ignores them just can't connect.
PROXY_URL = f"http://{PROXY_ALIAS}:{PROXY_PORT}"
PROXY_ENV = {"HTTP_PROXY": PROXY_URL, "HTTPS_PROXY": PROXY_URL, "http_proxy": PROXY_URL, "https_proxy": PROXY_URL,
             "NO_PROXY": "localhost,127.0.0.1,::1", "no_proxy": "localhost,127.0.0.1,::1", "NODE_USE_ENV_PROXY": "1"}


def container_name(agent: str) -> str:
    return f"valet-agent-{agent}"


def network_name(agent: str) -> str:
    return f"valet-agent-{agent}-net"


def egress_name(agent: str) -> str:
    return f"valet-agent-{agent}-egress"


def config_dir(definition: AgentDefinition, cfg: RunnerConfig) -> tuple[Path, str]:
    """The agent's compiled config dir (harness.Compiled.files): (on the runner's disk, as the harness sees it).
    <cfg.data>/agents/<name>/config, which a container sees at CONFIG_MOUNT."""
    local = cfg.data.absolute() / "agents" / definition.name / "config"
    return local, str(local) if definition.computer.level == "host" else CONFIG_MOUNT


def _host_port(text: str, default: int) -> tuple[str, int]:
    host, sep, port = text.rpartition(":")
    return (host, int(port)) if sep and port.isdigit() else (text, default)


def egress_rules(policy: str, hosts: list[str], gateway: str | None, allowlist: list[str]) -> list[dict[str, Any]]:
    """What a restricted container may reach: the harness's model hosts ("host" for port 443, or "host:port"),
    the gateway's host and port when it has one, and for `allowlist` each domain (and its subdomains) on WEB_PORTS."""
    rules: list[dict[str, Any]] = []
    for h in hosts:
        host, port = _host_port(h, 443)
        rules.append({"host": host, "ports": [port]})
    if gateway:
        u = urlsplit(gateway)
        if u.hostname:
            rules.append({"host": u.hostname, "ports": [u.port or (443 if u.scheme == "https" else 80)]})
    if policy == "allowlist":
        rules += [{"domain": d, "ports": list(WEB_PORTS)} for d in allowlist]
    return rules


def egress_plan(definition: AgentDefinition, compiled: Compiled, cfg: RunnerConfig, gateway: str | None) -> Egress:
    """The proxy of a container whose policy isn't open. It runs the runner's default image (it needs `node`), with
    no capabilities and a read-only root, and reaches the control plane's host as host.docker.internal on Linux too.
    A harness whose model hosts are unknown can only work under `allowlist` (its host listed): otherwise a
    LaunchError."""
    name, policy = definition.name, definition.computer.policy
    if compiled.hosts is None and policy != "allowlist":
        raise LaunchError(f"network '{policy}' lets the agent reach only its model, and Valet doesn't know where this "
                          "harness's model is: use network 'allowlist' with its host listed, or 'open'")
    rules = {"port": PROXY_PORT,
             "allow": egress_rules(policy, compiled.hosts or [], gateway, definition.computer.allowlist)}
    local = cfg.data.absolute() / "agents" / name / "egress"
    args = ["run", "-i", "--rm", "--init", "--name", egress_name(name), "--label", f"valet.agent={name}",
            "--label", "valet.role=egress", "--network", network_name(name), "--network-alias", PROXY_ALIAS,
            "--add-host", GATEWAY_HOST, "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "-v", f"{local}:/egress:ro", cfg.image, "node", "/egress/egress.mjs", "/egress/rules.json"]
    return Egress(network=network_name(name), container=egress_name(name), dir=local, rules=rules, args=args)


def host_workdir(definition: AgentDefinition, cfg: RunnerConfig) -> str:
    """A host agent's working folder, its harness's cwd and its sessions': computer.workdir, which must exist, else
    its own <cfg.data>/agents/<name>/work, which the runner creates."""
    return definition.computer.workdir or str(cfg.data / "agents" / definition.name / "work")


def plan(definition: AgentDefinition, compiled: Compiled, cfg: RunnerConfig,
         secrets: Mapping[str, str] | None = None, gateway: str | None = None) -> Launch:
    """container -> `docker run -i --rm ...` (exact argv in docs/M0.md), with the config dir mounted read-only at
    CONFIG_MOUNT; none -> the same without the workspace volume (an empty /work every start; no mounts either, the
    definition says so, and the harness compiles without built-in tools); host -> the ACP command itself in its
    working folder (host_workdir). `secrets` (agent.start's) join the harness's env; a reserved name (PROXY_ENV's
    are), or one the harness sets itself, is a LaunchError naming it. `gateway`: the URL of the agent's endpoint on
    Valet's MCP gateway, when it has one.

    A container's network policy (Computer.policy): `open` -> Docker's default network, plus `--add-host
    host.docker.internal:host-gateway` with a gateway (Docker Desktop has that name anyway; on Linux it maps to the
    host, so VALET_GATEWAY_URL=http://host.docker.internal:<port> works on both). `gateway`, `allowlist` -> its own
    internal network, PROXY_ENV, and `egress` (egress_plan): the proxy that is its only way out. The host has no
    policy: it uses the runner's network."""
    computer, name = definition.computer, definition.name
    if not compiled.argv:
        raise LaunchError("the harness has no command")
    walled = computer.policy not in (None, "open")
    env = dict(compiled.env)
    for key in sorted(secrets or {}):
        if err := secret_name_error(key):
            raise LaunchError(f"secret {key}: {err}")
        if key in env:
            raise LaunchError(f"secret {key}: the harness sets {key} itself")
        env[key] = secrets[key]
    if computer.level == "host":
        work = host_workdir(definition, cfg)
        return Launch(command=compiled.argv[0], args=list(compiled.argv[1:]), env=env, cwd=work, session_cwd=work)

    cname = container_name(name)
    args = ["run", "-i", "--rm", "--init", "--name", cname, "--label", f"valet.agent={name}",
            "-v", f"{cname}-home:/home/agent"]
    if computer.level == "container":
        args += ["-v", f"{cname}-work:/work"]
    args += ["-v", f"{config_dir(definition, cfg)[0]}:{CONFIG_MOUNT}:ro", "-w", "/work"]
    egress = None
    if walled:
        egress = egress_plan(definition, compiled, cfg, gateway)
        args += ["--network", egress.network]
        for key, value in PROXY_ENV.items():
            args += ["-e", f"{key}={value}"]
    elif gateway:
        args += ["--add-host", GATEWAY_HOST]
    for m in computer.mounts:
        args += ["-v", f"{m.host}:{m.path}" + (":ro" if m.readonly else "")]
    # Values travel in the docker CLI's own environment: `-e KEY` copies them, so keys and secrets never hit argv.
    for key in sorted(env):
        args += ["-e", key]
    args.append(computer.image or cfg.image)
    args += compiled.argv
    return Launch(command=cfg.docker, args=args, env=env, cwd=None, session_cwd="/work", egress=egress)
