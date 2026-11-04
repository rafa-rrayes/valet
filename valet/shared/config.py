"""Environment -> config. Read once at startup and passed around, never at import time, so tests build their own."""
import os
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent


@dataclass(frozen=True)
class ControlConfig:
    data: Path                      # VALET_DATA: db, definitions repo; default <repo>/data
    definitions: Path               # VALET_DEFINITIONS: the git repo of agents/; default <data>/definitions
    host: str = "127.0.0.1"         # VALET_HOST
    port: int = 8780                # VALET_PORT
    public_url: str = "http://127.0.0.1:8780"   # VALET_PUBLIC_URL; https -> Secure cookies
    rpc_timeout: float = 60.0       # VALET_RPC_TIMEOUT: seconds to wait for a runner's reply
    trust_cf_ip: bool = False       # VALET_TRUST_CF_IP=1: behind a Cloudflare tunnel; trust cf-connecting-ip from loopback
    # The key secrets are encrypted with (control/secrets.py), VALET_MASTER_KEY (64 hex
    # characters), else the file VALET_KEY_FILE (default <data>/master.key: 32 random bytes, mode 0600, created on
    # first use).
    master_key: bytes | None = field(default=None, repr=False)
    key_file: Path | None = None
    # VALET_GATEWAY_URL: the control plane as agents reach it, for the MCP gateway (/mcp/agents/<name>). Default
    # the public URL; a container can't use 127.0.0.1 (Docker Desktop: http://host.docker.internal:<port>).
    gateway_url: str = ""
    gateway_ask_timeout: float = 600.0    # VALET_GATEWAY_ASK_TIMEOUT: seconds a gateway call waits for the owner
    gateway_call_timeout: float = 300.0   # VALET_GATEWAY_CALL_TIMEOUT: seconds an upstream tool call may take
    # Push to the native apps (control/notify.py), with an APNs auth key from the Apple developer account: off until
    # the key file, its ID and the team ID are all set.
    apns_key_file: Path | None = None     # VALET_APNS_KEY_FILE: the .p8 key
    apns_key_id: str = ""                 # VALET_APNS_KEY_ID
    apns_team_id: str = ""                # VALET_APNS_TEAM_ID
    apns_topic: str = "br.com.rrayes.valet"   # VALET_APNS_TOPIC: the apps' bundle ID
    apns_sandbox: bool = False            # VALET_APNS_SANDBOX=1: development builds (Apple's sandbox endpoint)
    apns_url: str = ""                    # VALET_APNS_URL: another APNs endpoint (tests); default Apple's, by sandbox
    # The browser (control/browser/chrome.py): VALET_CHROME, a Chrome binary (default Playwright's Chromium);
    # VALET_HEADLESS 1/0, unset: headless unless DISPLAY is set; VALET_BROWSER_IDLE: seconds before an unused
    # browser is stopped; VALET_CHROME_NO_SANDBOX 1/0, unset (None): Chrome without its own sandbox only as root.
    # A container is the boundary there: Docker blocks the user namespaces Chrome's sandbox needs (docker/control).
    chrome: str | None = None
    headless: bool = True
    browser_idle: float = 1800.0
    chrome_no_sandbox: bool | None = None
    max_file_mb: int = 100          # VALET_MAX_FILE_MB: the largest file the shared space takes, in MiB
    # Triggers (control/scheduler.py): VALET_TZ, the zone schedules are read in (an IANA name; default the control
    # plane's own), and VALET_TRIGGER_GRACE, how late a run due while the agent couldn't take it may still fire.
    timezone: str = ""
    trigger_grace: float = 600.0
    # VALET_OWNER_NAME: what Valet's texts call the person it works for (shared/owner.py); "the owner" if empty.
    owner_name: str = "the owner"

    def __post_init__(self) -> None:
        if not self.gateway_url:
            object.__setattr__(self, "gateway_url", self.public_url)

    @property
    def db_path(self) -> Path:
        return self.data / "valet.db"

    @property
    def key_path(self) -> Path:
        return self.key_file or self.data / "master.key"

    @property
    def shared_dir(self) -> Path:
        """The shared space's files: <data>/shared/<id>/<name> (control/space.py)."""
        return self.data / "shared"

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "ControlConfig":
        data = Path(env.get("VALET_DATA") or ROOT / "data")
        port = int(env.get("VALET_PORT") or 8780)
        return cls(
            data=data,
            definitions=Path(env.get("VALET_DEFINITIONS") or data / "definitions"),
            host=env.get("VALET_HOST") or "127.0.0.1",
            port=port,
            public_url=(env.get("VALET_PUBLIC_URL") or f"http://127.0.0.1:{port}").rstrip("/"),
            rpc_timeout=float(env.get("VALET_RPC_TIMEOUT") or 60),
            trust_cf_ip=(env.get("VALET_TRUST_CF_IP") or "").lower() in ("1", "true", "yes"),
            master_key=_master_key(env.get("VALET_MASTER_KEY")),
            key_file=Path(env["VALET_KEY_FILE"]) if env.get("VALET_KEY_FILE") else None,
            gateway_url=(env.get("VALET_GATEWAY_URL") or "").rstrip("/"),
            gateway_ask_timeout=float(env.get("VALET_GATEWAY_ASK_TIMEOUT") or 600),
            gateway_call_timeout=float(env.get("VALET_GATEWAY_CALL_TIMEOUT") or 300),
            apns_key_file=Path(env["VALET_APNS_KEY_FILE"]) if env.get("VALET_APNS_KEY_FILE") else None,
            apns_key_id=(env.get("VALET_APNS_KEY_ID") or "").strip(),
            apns_team_id=(env.get("VALET_APNS_TEAM_ID") or "").strip(),
            apns_topic=(env.get("VALET_APNS_TOPIC") or "").strip() or "br.com.rrayes.valet",
            apns_sandbox=(env.get("VALET_APNS_SANDBOX") or "").lower() in ("1", "true", "yes"),
            apns_url=(env.get("VALET_APNS_URL") or "").rstrip("/"),
            chrome=env.get("VALET_CHROME") or None,
            headless=_headless(env.get("VALET_HEADLESS"), env.get("DISPLAY")),
            browser_idle=float(env.get("VALET_BROWSER_IDLE") or 1800),
            chrome_no_sandbox=_flag(env.get("VALET_CHROME_NO_SANDBOX")),
            max_file_mb=int(env.get("VALET_MAX_FILE_MB") or 100),
            timezone=_timezone(env.get("VALET_TZ")),
            trigger_grace=float(env.get("VALET_TRIGGER_GRACE") or 600),
            owner_name=(env.get("VALET_OWNER_NAME") or "").strip() or "the owner",
        )


def _headless(value: str | None, display: str | None) -> bool:
    """VALET_HEADLESS=1: headless; 0: a window; unset: headless unless there's a DISPLAY (Xvfb on a server)."""
    v = (value or "").strip().lower()
    if v in ("1", "true", "yes"):
        return True
    if v in ("0", "false", "no"):
        return False
    return not display


def _timezone(name: str | None) -> str:
    """VALET_TZ, checked: a name zoneinfo doesn't know is refused at startup rather than at the first schedule."""
    name = (name or "").strip()
    if name:
        from valet.shared.cron import CronError, zone
        try:
            zone(name)
        except CronError as e:
            raise ValueError(f"VALET_TZ: {e}") from None
    return name


def _flag(value: str | None) -> bool | None:
    """1/true/yes: True; 0/false/no: False; unset or anything else: None (the default applies)."""
    v = (value or "").strip().lower()
    return True if v in ("1", "true", "yes") else False if v in ("0", "false", "no") else None


def _master_key(text: str | None) -> bytes | None:
    if not text:
        return None
    try:
        key = bytes.fromhex(text.strip())
    except ValueError:
        key = b""
    if len(key) != 32:
        raise ValueError("VALET_MASTER_KEY must be 64 hex characters (a 32-byte key)")
    return key


# Harness credentials the runner reads from its own env. Each harness adapter passes on only its own (harness.KEYS):
# Claude agents use the owner's subscription, CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token` (ANTHROPIC_API_KEY still
# works, and Claude Code prefers it when both are set); DeepSeek Harness agents use DEEPSEEK_API_KEY.
PASSTHROUGH_ENV = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY")


@dataclass(frozen=True)
class RunnerConfig:
    control_url: str                # VALET_CONTROL_URL: ws(s)://host:port/ws/runner
    token: str                      # VALET_RUNNER_TOKEN: vrun_..., from `valet-control add-runner <name>`
    data: Path                      # VALET_RUNNER_DATA: host-level workspaces; default <repo>/data/runner
    image: str = "valet/agent-base" # VALET_AGENT_IMAGE: default image for container agents
    docker: str = "docker"          # VALET_DOCKER: docker CLI binary
    # VALET_HARNESS_CMD_<HARNESS>="cmd args...": replaces a harness's ACP command (tests: the fake agent; dev: a
    # local adapter build). Keys are lowercase harness names.
    harness_commands: dict[str, list[str]] = field(default_factory=dict)
    passthrough: dict[str, str] = field(default_factory=dict)   # PASSTHROUGH_ENV names that are set, with values
    reconnect_max: float = 30.0     # VALET_RECONNECT_MAX: backoff ceiling, seconds

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "RunnerConfig":
        token = env.get("VALET_RUNNER_TOKEN") or ""
        if not token:
            raise ValueError("VALET_RUNNER_TOKEN is not set: run `valet-control add-runner <name>` on the control plane")
        prefix = "VALET_HARNESS_CMD_"
        return cls(
            control_url=env.get("VALET_CONTROL_URL") or "ws://127.0.0.1:8780/ws/runner",
            token=token,
            data=Path(env.get("VALET_RUNNER_DATA") or ROOT / "data" / "runner"),
            image=env.get("VALET_AGENT_IMAGE") or "valet/agent-base",
            docker=env.get("VALET_DOCKER") or "docker",
            harness_commands={k[len(prefix):].lower(): shlex.split(v) for k, v in env.items()
                              if k.startswith(prefix) and v.strip()},
            passthrough={k: env[k] for k in PASSTHROUGH_ENV if env.get(k)},
            reconnect_max=float(env.get("VALET_RECONNECT_MAX") or 30),
        )
