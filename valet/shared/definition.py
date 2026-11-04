"""The agent definition: agents/<name>/agent.yaml + persona.md in the definitions repo.

The model covers the whole agent.yaml shape from DESIGN.md so a definition written today keeps parsing as
milestones land; so far Valet acts on name, display_name, emoji, harness, model, provider, computer (level, image,
mounts, network and its allowlist), secrets (by name, or vault entries), tools.mcp and tools.rules (the MCP
gateway), tools.skills (shared/skills.py), browser (profiles and rules: shared/browser.py), permissions, directives
(owner, authority/<preset>, persona.md, environment, memory: shared/directives.py), memory (valet.memory and the
memory layer), triggers (schedules and webhooks: control/scheduler.py, control/webhooks.py) and runner. Unknown keys
are errors: these files are hand-edited too. The settings page writes them with to_yaml()."""
import re
import unicodedata
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import (
    AfterValidator, BaseModel, ConfigDict, Field, ValidationError, ValidationInfo, field_validator, model_validator,
)

from valet.shared.browser import SiteRules, profile_name_error
from valet.shared import cron
from valet.shared.directives import PERSONA, directives_error, read_layers
from valet.shared.skills import ALL as ALL_SKILLS
from valet.shared.skills import name_error as skill_name_error

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
# A docker image reference (name[:tag][@digest]); in particular never starting with "-" (it lands in argv).
IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*(:[0-9]+)?(/[a-z0-9][a-z0-9._-]*)*"
                      r"(:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?(@[a-z0-9]+:[a-f0-9]{32,})?$")
# An MCP server an agent can be given: Valet's own (valet.agents) or a name from the registry, mcp.yaml (letters,
# digits, '-' and single '_': they become tool-name prefixes).
MCP_SERVER_RE = re.compile(r"^(valet\.[a-z][a-z0-9-]{0,30}|[a-z0-9](?:[a-z0-9-]|_(?!_)){0,30})$")
# A tool's name as MCP defines it (mcp.shared.tool_name_validation), for tools.rules; "*" stands for all of them.
TOOL_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
ALL_TOOLS = "*"
# The owner where an agent's name could stand (who changed a shared item, a memory commit): no agent may take it.
OWNER_NAME = "owner"
# Valet's memory server: every agent with `memory: true` gets it through the gateway, without listing it.
MEMORY_SERVER = "valet.memory"
AGENT_FILE = "agent.yaml"
PERSONA_FILE = PERSONA
AGENTS_DIR = "agents"

# Secrets become env vars of the harness process, so their names are env var names. A secret can't take a name that
# Valet, a harness or the docker CLI reads or sets: Valet's own settings; the harnesses' keys and settings (Claude
# Code reads CLAUDE_* and ANTHROPIC_*, DeepSeek Harness DSH_* and DEEPSEEK_*: a harness's key goes in the runner's
# env, its model and provider in the definition); the docker CLI's (a container agent's secrets pass through its
# env); the dynamic loader's; Node's (both harnesses are Node programs); and what every process gets, or Valet will
# set (network policies: the proxy variables).
SECRET_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
RESERVED_PREFIXES = ("VALET_", "CLAUDE_", "ANTHROPIC_", "DEEPSEEK_", "DSH_", "DOCKER_", "LD_", "DYLD_")
RESERVED_NAMES = frozenset({"HOME", "PATH", "USER", "LOGNAME", "SHELL", "TERM", "PWD", "HOSTNAME", "TMPDIR",
                            "NODE_OPTIONS", "NODE_PATH", "NODE_USE_ENV_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
                            "ALL_PROXY"})
# A site in computer.allowlist: a domain name of two labels or more (never a bare TLD like `com`), which also lets
# its subdomains through. Kept lowercase, without a trailing dot or a leading `*.`.
WORKDIR_MAX = 1024
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")


def secret_name_error(name: str) -> str | None:
    """Why `name` can't be a secret's name, or None if it can."""
    if not isinstance(name, str) or not SECRET_NAME_RE.match(name):
        return "an env var name: uppercase letters, digits and _, starting with a letter, at most 64"
    if name in RESERVED_NAMES or name.startswith(RESERVED_PREFIXES):
        return "reserved: Valet, a harness or the computer sets it"
    return None


class DefinitionError(ValueError):
    """A definition that doesn't parse or validate; the message is shown to the owner as is."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Mount(_Model):
    host: str                       # absolute path on the runner's machine
    path: str                       # absolute path inside the computer
    readonly: bool = False

    @field_validator("host", "path")
    @classmethod
    def _absolute(cls, v: str) -> str:
        # Becomes `-v host:path`: a relative host is a named volume (another agent's home, say), a ':' adds fields.
        if not v.startswith("/") or ":" in v or "," in v or "\0" in v:
            raise ValueError("an absolute path without ':' or ','")
        return v


def _domain(v: str) -> str:
    d = v.strip().lower().removeprefix("*.").removesuffix(".")
    if not DOMAIN_RE.match(d):
        raise ValueError("a domain like github.com (its subdomains come with it)")
    return d


Domain = Annotated[str, AfterValidator(_domain)]


class Computer(_Model):
    level: Literal["none", "container", "host"] = "container"
    image: str | None = None        # None: the runner's default image (VALET_AGENT_IMAGE)
    # What a container reaches besides its model's API (always): see `policy`. Ignored on the host.
    network: Literal["none", "gateway", "allowlist", "open"] = "open"
    allowlist: list[Domain] = []    # network: allowlist; each domain with its subdomains
    mounts: list[Mount] = []
    user: str | None = None         # host level: the Unix user to run as; None = the runner's own (M2)
    # Host level: the folder it works in (its harness's cwd, and its sessions'), an absolute path on the runner's
    # machine that must exist there. None: a folder of its own under the runner's data, <data>/agents/<name>/work.
    # Ignored elsewhere, like `network` on the host.
    workdir: str | None = None

    @field_validator("image")
    @classmethod
    def _image(cls, v: str | None) -> str | None:
        if v is not None and not IMAGE_RE.match(v):
            raise ValueError("a docker image reference like registry/name:tag")
        return v

    @field_validator("workdir")
    @classmethod
    def _workdir(cls, v: str | None) -> str | None:
        """Absolute, without `.` or `..` parts or control characters; repeated and trailing slashes dropped. An
        empty string is None."""
        if not v:
            return None
        if not v.startswith("/") or len(v) > WORKDIR_MAX:
            raise ValueError("an absolute path on the runner's machine, like /Users/rafa/Code/project")
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in v):
            raise ValueError("a path without control characters")
        parts = [p for p in v.split("/") if p]
        if any(p in (".", "..") for p in parts):
            raise ValueError("a path without . or .. in it")
        return "/" + "/".join(parts)

    @field_validator("allowlist")
    @classmethod
    def _allowlist(cls, v: list[str]) -> list[str]:
        if dup := next((d for i, d in enumerate(v) if d in v[:i]), None):
            raise ValueError(f"{dup} is listed twice")
        return v

    @field_validator("mounts")
    @classmethod
    def _mounts(cls, v: list[Mount], info: ValidationInfo) -> list[Mount]:
        if v and info.data.get("level") == "none":
            raise ValueError("an agent with no computer has no shared folders")
        return v

    @property
    def policy(self) -> Literal["gateway", "allowlist", "open"] | None:
        """The network policy in force. Every one lets the harness reach its model's API; `gateway` adds Valet's
        MCP gateway (when the agent has MCP servers), `allowlist` adds that and the listed domains, `open` is
        everything. Level `none` is always `gateway`, and so is `network: none` (a container with no network at all
        couldn't reach its model). None on the host, where the agent has the runner's own network."""
        if self.level == "host":
            return None
        if self.level == "none" or self.network == "none":
            return "gateway"
        return self.network


Rule = Literal["allow", "ask", "deny"]


class Tools(_Model):
    mcp: list[str] = []             # MCP servers through the gateway: Valet's own (valet.agents) or mcp.yaml's names
    skills: list[str] = []          # shared skills (skills/<name>/ in the repo) by name, or ["*"] for all of them
    # The owner's rules for gateway tools, over the permission level: server -> tool (or "*", every tool of that
    # server; a tool's own entry beats it) -> allow | ask | deny. "Always allow" in the inbox writes one. The gateway
    # reads them at every call, so they're left out of the restart fingerprint (control/views.fingerprint).
    rules: dict[str, dict[str, Rule]] = {}

    @field_validator("mcp")
    @classmethod
    def _servers(cls, v: list[str]) -> list[str]:
        for name in v:
            if not MCP_SERVER_RE.match(name):
                raise ValueError(f"{name!r}: a server name from mcp.yaml, or one of Valet's own like valet.agents")
            if name == MEMORY_SERVER:
                raise ValueError(f"{MEMORY_SERVER} comes with `memory: true`: don't list it here")
        return v

    @field_validator("rules")
    @classmethod
    def _rules(cls, v: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
        for server, tools in v.items():
            if not MCP_SERVER_RE.match(server):
                raise ValueError(f"{server!r}: a server name from mcp.yaml, or one of Valet's own like valet.agents")
            for tool in tools:
                if tool != ALL_TOOLS and not TOOL_RE.match(tool):
                    raise ValueError(f"{server}: {tool!r}: a tool's name (letters, digits, '_', '-' and '.', at "
                                     "most 128), or \"*\" for all of its tools")
        return {server: tools for server, tools in v.items() if tools}  # a server left without rules goes

    @field_validator("skills")
    @classmethod
    def _skills(cls, v: list[str]) -> list[str]:
        for name in v:
            if name != ALL_SKILLS and (err := skill_name_error(name)):
                raise ValueError(f"{name!r}: a shared skill's name ({err}), or \"*\" for all of them")
        return v


class Browser(_Model):
    """valet.browser (shared/browser.py): the browser.yaml profiles its tools may use, and the agent's own site
    rules, which beat the profile's. Rules are read at every call: changing them needs no restart."""
    profiles: list[str] = []
    rules: SiteRules = {}           # site pattern -> kind -> allow | ask | deny

    @field_validator("profiles")
    @classmethod
    def _profiles(cls, v: list[str]) -> list[str]:
        for i, name in enumerate(v):
            if err := profile_name_error(name):
                raise ValueError(f"{name!r}: a browser profile's name ({err})")
            if name in v[:i]:
                raise ValueError(f"{name} is listed twice")
        return v


# A vault entry's name: one to three parts separated by "/" (github/alice), lowercase, at most 64 characters.
VAULT_NAME_RE = re.compile(r"^(?=.{1,64}$)[a-z0-9][a-z0-9._-]*(/[a-z0-9][a-z0-9._-]*){0,2}$")


def vault_name_error(name: object) -> str | None:
    if not isinstance(name, str) or not VAULT_NAME_RE.match(name):
        return ("a vault entry's name, like github/alice: lowercase letters, digits, '.', '_' and '-', up to three "
                "parts separated by '/', at most 64 characters")
    return None


def vault_env(entry: str) -> str:
    """The env var a vault entry's password gets when the definition doesn't name one: github/alice ->
    GITHUB_ALICE_PASSWORD. One that wouldn't be a usable name (it starts with a digit or '_', like an IP site's
    0/alice, or with a reserved prefix, like docker/ci) gets VAULT_ in front: VAULT_0_ALICE_PASSWORD."""
    name = re.sub(r"[^A-Z0-9]", "_", entry.upper()) + "_PASSWORD"
    return name if name[0].isalpha() and not name.startswith(RESERVED_PREFIXES) else "VAULT_" + name


class VaultSecret(_Model):
    vault: str                      # vault entry, e.g. aws/alice: its password becomes an env var
    env: str | None = None          # env var name inside the computer; None = derived from the entry (vault_env)

    @field_validator("vault")
    @classmethod
    def _vault(cls, v: str) -> str:
        if err := vault_name_error(v):
            raise ValueError(err)
        return v

    @field_validator("env")
    @classmethod
    def _env(cls, v: str | None) -> str | None:
        if v is not None and (err := secret_name_error(v)):
            raise ValueError(err)
        return v

    @property
    def env_name(self) -> str:
        return self.env or vault_env(self.vault)


TRIGGER_PROMPT_LIMIT = 8000        # characters of a trigger's own prompt (a webhook adds what it was sent)


class Trigger(_Model):
    """What wakes the agent with a prompt (README, "Always-on"): a schedule, or a webhook Valet serves at
    POST /hooks/<agent>/<webhook>. Either prompts the agent's main session (one per agent, made on first use), or a
    new session each time."""
    schedule: str | None = None     # a cron expression (shared/cron.py), in the control plane's zone (VALET_TZ)
    webhook: str | None = None      # its name: lowercase letters, digits and dashes, at most 31
    prompt: str | None = None       # what it says; required for a schedule, a webhook adds the request's body
    session: Literal["main", "new"] = "main"

    @field_validator("schedule")
    @classmethod
    def _schedule(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if err := cron.check(v):
            raise ValueError(err)
        return " ".join(v.split())

    @field_validator("webhook")
    @classmethod
    def _webhook(cls, v: str | None) -> str | None:
        if v is not None and not NAME_RE.match(v):
            raise ValueError("a webhook's name is lowercase letters, digits and dashes, starting with a letter or "
                             "digit, at most 31")
        return v

    @field_validator("prompt")
    @classmethod
    def _prompt(cls, v: str | None) -> str | None:
        if v is not None and len(v) > TRIGGER_PROMPT_LIMIT:
            raise ValueError(f"at most {TRIGGER_PROMPT_LIMIT} characters")
        return v

    @model_validator(mode="after")
    def _one_source(self):
        if (self.schedule is None) == (self.webhook is None):
            raise ValueError("a trigger has exactly one of schedule or webhook")
        if self.schedule is not None and not (self.prompt or "").strip():
            raise ValueError("a schedule needs a prompt: what the agent is asked each time")
        return self

    @property
    def key(self) -> str:
        """Its identity in the trigger runs and the scheduler's state: `schedule:<expression>` or
        `webhook:<name>` (an edited schedule is a new trigger)."""
        return f"schedule:{self.schedule}" if self.schedule is not None else f"webhook:{self.webhook}"


class AgentDefinition(_Model):
    name: str
    display_name: str | None = None # how the apps show it ("Scout"); None = the name
    emoji: str | None = None        # shown next to it
    harness: str = "claude"         # claude | deepseek | codex | gemini | opencode | <any ACP agent>
    model: str | None = None
    provider: str | None = None
    runner: str | None = None       # which runner hosts it; None = any connected runner (M0 has one)
    computer: Computer = Field(default_factory=Computer)
    tools: Tools = Field(default_factory=Tools)
    browser: Browser = Field(default_factory=Browser)
    secrets: list[str | VaultSecret] = []  # names of Valet's secrets (env vars of the same name), or vault entries
    permissions: Literal["locked", "ask", "trusted", "unrestricted"] = "ask"
    directives: list[str] = [PERSONA_FILE]
    memory: bool = False            # YAML `on`/`off` parse as booleans
    triggers: list[Trigger] = []

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not NAME_RE.match(v):
            raise ValueError("lowercase letters, digits and dashes, starting with a letter or digit, at most 31")
        if v == OWNER_NAME:
            raise ValueError(f"{OWNER_NAME} stands for the owner in Valet (the shared space, memory's history): pick "
                             "another name")
        return v

    @field_validator("secrets")
    @classmethod
    def _secrets(cls, v: list) -> list:
        seen = set()
        for s in v:
            name = s if isinstance(s, str) else s.env_name
            if err := secret_name_error(name):
                if isinstance(s, VaultSecret) and s.env is None:
                    raise ValueError(f"vault {s.vault}: its env var would be {name}, which can't be used ({err}); "
                                     "give it an env name")
                raise ValueError(f"{name}: {err}")
            if name in seen:
                raise ValueError(f"{name} is listed twice")
            seen.add(name)
        return v

    @field_validator("triggers")
    @classmethod
    def _triggers(cls, v: list[Trigger]) -> list[Trigger]:
        seen: set[str] = set()
        for t in v:
            if t.key in seen:
                what = f"the webhook {t.webhook}" if t.webhook else f"the schedule {t.schedule!r}"
                raise ValueError(f"{what} is listed twice" + ("" if t.webhook else ": put its prompts in one"))
            seen.add(t.key)
        return v

    def webhook(self, name: str) -> Trigger | None:
        return next((t for t in self.triggers if t.webhook == name), None)

    @field_validator("directives")
    @classmethod
    def _directives(cls, v: list[str]) -> list[str]:
        if err := directives_error(v):
            raise ValueError(err)
        return v

    def secret_names(self) -> list[str]:
        """The secrets listed by name (Valet's own secrets), in order. Vault entries aren't included."""
        return [s for s in self.secrets if isinstance(s, str)]

    def vault_secrets(self) -> list[VaultSecret]:
        """The vault entries listed in secrets, in order."""
        return [s for s in self.secrets if isinstance(s, VaultSecret)]

    def gateway_servers(self) -> list[str]:
        """The MCP servers this agent reaches through Valet's gateway, in order: tools.mcp's, each once, then
        valet.memory when it has memory. Empty: it gets no gateway at all (no token, no `valet` MCP server)."""
        return [*dict.fromkeys(self.tools.mcp), *([MEMORY_SERVER] if self.memory else [])]

    @field_validator("display_name", "emoji")
    @classmethod
    def _label(cls, v: str | None, info) -> str | None:
        if v is None:
            return None
        v = v.strip()
        limit = 40 if info.field_name == "display_name" else 16
        # Control characters (tabs, newlines, NUL, escapes) and Unicode line/paragraph separators all break "one
        # line" somewhere: in a list row, a terminal, a commit message. Emoji joiners and selectors are format (Cf)
        # and mark (Mn) characters, not these.
        if any(unicodedata.category(c) in ("Cc", "Zl", "Zp") for c in v) or len(v) > limit:
            raise ValueError(f"one line, at most {limit} characters")
        if info.field_name == "emoji" and " " in v:
            raise ValueError("one emoji, no spaces")
        return v or None


def parse_agent(text: str, dir_name: str | None = None) -> AgentDefinition:
    """agent.yaml text -> definition. dir_name, when given, must equal `name` (the directory is the identity)."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise DefinitionError(f"agent.yaml is not valid YAML: {e}") from None
    if not isinstance(data, dict):
        raise DefinitionError("agent.yaml must be a mapping")
    try:
        d = AgentDefinition.model_validate(data)
    except ValidationError as e:
        msgs = "; ".join(f"{'.'.join(map(str, err['loc'])) or '(root)'}: {err['msg']}" for err in e.errors())
        raise DefinitionError(f"agent.yaml: {msgs}") from None
    if dir_name is not None and d.name != dir_name:
        raise DefinitionError(f"agent.yaml: name {d.name!r} must match its directory {dir_name!r}")
    return d


def agent_dir(root: Path, name: str) -> Path:
    if not NAME_RE.match(name):
        raise DefinitionError(f"invalid agent name {name!r}")
    return root / AGENTS_DIR / name


def list_agents(root: Path) -> list[str]:
    """Names of agent directories that have an agent.yaml, sorted. Doesn't validate them."""
    base = root / AGENTS_DIR
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir() if NAME_RE.match(p.name) and (p / AGENT_FILE).is_file())


def read_files(root: Path, name: str, directives: list[str] = ()) -> dict[str, str]:
    """The text the runner needs for the agent's directives (shared/directives.py layer_file keys): its persona.md,
    and the repo's owner.md and authority/<preset>.md when `directives` lists them and they exist. The generated
    environment layer isn't here: the control plane writes it at each start."""
    p = agent_dir(root, name) / PERSONA_FILE
    return {**({PERSONA_FILE: p.read_text()} if p.is_file() else {}), **read_layers(root, directives)}


def load_agent(root: Path, name: str) -> tuple[AgentDefinition, dict[str, str]]:
    """Read and validate agents/<name>/: (definition, files). Raises DefinitionError or FileNotFoundError."""
    d = agent_dir(root, name)
    definition = parse_agent((d / AGENT_FILE).read_text(), name)
    return definition, read_files(root, name, definition.directives)


# Always written by to_yaml, default or not, so a file says what kind of agent it is at a glance.
_ALWAYS = ("name", "harness", "permissions")


class _Dumper(yaml.SafeDumper):
    """safe_dump, except that a string with NEL (U+0085) is double-quoted: PyYAML writes it raw inside single
    quotes, where YAML reads it as a line break and folds it into a space."""


_Dumper.add_representer(str, lambda dumper, s: dumper.represent_scalar(
    "tag:yaml.org,2002:str", s, style='"' if "\x85" in s else None))


def to_yaml(d: AgentDefinition) -> str:
    """The definition as agent.yaml: field order of the model, defaults left out except name, harness,
    computer.level and permissions. parse_agent(to_yaml(d)) == d."""
    lean = d.model_dump(mode="json", exclude_defaults=True)
    out: dict = {}
    for key in AgentDefinition.model_fields:
        if key == "computer":
            out[key] = {"level": d.computer.level, **lean.get(key, {})}
        elif key in lean:
            out[key] = lean[key]
        elif key in _ALWAYS:
            out[key] = getattr(d, key)
    return yaml.dump(out, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=1000)


def validation_errors(e: ValidationError) -> list[dict[str, str]]:
    """pydantic errors -> [{"field": "computer.mounts.0.host", "msg": "..."}] for the settings form."""
    return [{"field": ".".join(map(str, err["loc"])), "msg": err["msg"].removeprefix("Value error, ")}
            for err in e.errors()]


TEMPLATE = """\
name: {name}
harness: claude
computer:
  level: container
permissions: ask
directives: [persona.md]
"""

PERSONA_TEMPLATE = "You are {name}, one of {owner}'s agents. Be brief and direct.\n"
