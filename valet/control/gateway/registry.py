"""The MCP server registry: `mcp.yaml` at the root of the definitions repo, versioned like everything else.

```yaml
servers:
  github:                                   # a remote server, over streamable HTTP
    description: GitHub issues and pull requests
    url: https://api.githubcopilot.com/mcp/
    headers:
      Authorization: Bearer ${env:GITHUB_TOKEN}
  files:                                    # a stdio server the gateway runs itself
    command: npx
    args: [-y, "@modelcontextprotocol/server-filesystem", /srv/shared]
    env:
      LOG_LEVEL: info
```

An agent gets the servers its agent.yaml lists in `tools.mcp`, plus Valet's own (`valet.agents`, ...), which need
no entry here. Names starting with `valet` are reserved for those.

Credentials stay in the control plane: the url, header and env values may reference `${secret:NAME}`, one of Valet's
named secrets (control/secrets.py; set with `valet-control set-secret` or PUT /api/secrets/NAME), or `${env:NAME}`,
a variable of the control plane's own environment. They're resolved when the gateway connects (a secret is
decrypted then, and not kept anywhere but that connection), never shown by the API, and the agent never sees them.
`resolve()` takes the lookups by scheme (the gateway's: `{"env": os.environ.get, "secret": store.get}`)."""
import hashlib
import json
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from valet.shared.definition import secret_name_error

REGISTRY_FILE = "mcp.yaml"
# Registry names: they become tool-name prefixes (`<server>__<tool>`), so no dots and no double underscores.
SERVER_RE = re.compile(r"^(?!valet)[a-z0-9](?:[a-z0-9-]|_(?!_)){0,30}$")
HEADER_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")
ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
REF_RE = re.compile(r"\$\{([a-z]+):([A-Za-z0-9_.-]+)\}")
SCHEMES = ("env", "secret")

EXAMPLE = """\
# MCP servers Valet's gateway can give agents. An agent gets the ones its agent.yaml lists in tools.mcp
# (plus Valet's own, like valet.agents, which need no entry here). Values may use ${secret:NAME} (one of Valet's
# secrets) or ${env:NAME} (a variable of the control plane's environment), so credentials stay out of this file and
# away from the agents.
servers: {}
"""

Lookup = Callable[[str], str | None]


class RegistryError(ValueError):
    """mcp.yaml doesn't parse or validate, or a reference can't be resolved; the message is shown as is."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ServerSpec(_Model):
    description: str | None = None
    url: str | None = None                      # remote: streamable HTTP endpoint
    headers: dict[str, str] = {}
    command: str | None = None                  # local: a stdio server the gateway runs on the control plane's host
    args: list[str] = []
    env: dict[str, str] = {}

    @model_validator(mode="after")
    def _one_kind(self):
        if (self.url is None) == (self.command is None):
            raise ValueError("a server has exactly one of url (remote) or command (stdio)")
        if self.url is not None and (self.args or self.env):
            raise ValueError("args and env are for stdio servers (command)")
        if self.command is not None and self.headers:
            raise ValueError("headers are for remote servers (url)")
        return self

    @field_validator("url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        if v is not None:
            if not re.match(r"^https?://[^\s/]+", v) or any(c in v for c in " \r\n\0"):
                raise ValueError("an http(s) URL")
            _check_refs(v)
        return v

    @field_validator("command")
    @classmethod
    def _command(cls, v: str | None) -> str | None:
        if v is not None and (not v.strip() or v.startswith("-") or "\0" in v):
            raise ValueError("a program name or path")
        return v

    @field_validator("headers")
    @classmethod
    def _headers(cls, v: dict[str, str]) -> dict[str, str]:
        for name, value in v.items():
            if not HEADER_RE.match(name) or any(c in value for c in "\r\n\0"):
                raise ValueError(f"header {name!r}: a token name and a one-line value")
            _check_refs(value)
        return v

    @field_validator("env")
    @classmethod
    def _env(cls, v: dict[str, str]) -> dict[str, str]:
        for name, value in v.items():
            if not ENV_RE.match(name) or "\0" in value:
                raise ValueError(f"env {name!r}: a variable name")
            _check_refs(value)
        return v

    @property
    def kind(self) -> str:
        return "http" if self.url is not None else "stdio"


def _check_refs(value: str) -> None:
    for scheme, name in REF_RE.findall(value):
        if scheme not in SCHEMES:
            raise ValueError(f"unknown reference ${{{scheme}:...}}: use ${{secret:NAME}} or ${{env:NAME}}")
        if scheme == "secret" and (err := secret_name_error(name)):
            raise ValueError(f"${{secret:{name}}}: {err}")


class Registry(_Model):
    servers: dict[str, ServerSpec] = Field(default_factory=dict)

    @field_validator("servers")
    @classmethod
    def _names(cls, v: dict[str, ServerSpec]) -> dict[str, ServerSpec]:
        for name in v:
            if not SERVER_RE.match(name):
                raise ValueError(f"server name {name!r}: lowercase letters, digits, '-' and single '_', at most 31, "
                                 "not starting with 'valet' (reserved for Valet's own servers)")
        return v


def parse(text: str) -> Registry:
    """mcp.yaml text -> Registry. An empty file is an empty registry."""
    try:
        data = yaml.safe_load(text) if text.strip() else {}
    except yaml.YAMLError as e:
        raise RegistryError(f"mcp.yaml is not valid YAML: {e}") from None
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise RegistryError("mcp.yaml must be a mapping with `servers:`")
    try:
        return Registry.model_validate(data)
    except ValidationError as e:
        msgs = "; ".join(f"{'.'.join(map(str, err['loc'])) or '(root)'}: {err['msg'].removeprefix('Value error, ')}"
                         for err in e.errors())
        raise RegistryError(f"mcp.yaml: {msgs}") from None


def load(root: Path) -> Registry:
    """The registry in the definitions repo at `root` (empty when there's no mcp.yaml)."""
    p = root / REGISTRY_FILE
    return parse(p.read_text()) if p.is_file() else Registry()


def resolve(value: str, lookups: Mapping[str, Lookup], used: list[str] | None = None) -> str:
    """Substitute `${scheme:name}` references; each value substituted is appended to `used` (what to redact from
    messages). RegistryError names the missing one (never its value)."""
    def sub(m: re.Match) -> str:
        scheme, name = m.group(1), m.group(2)
        lookup = lookups.get(scheme)
        if lookup is None:
            raise RegistryError(f"${{{scheme}:{name}}}: unknown reference")
        found = lookup(name)
        if found is None:
            where = "the control plane's environment" if scheme == "env" else "Valet's secrets"
            raise RegistryError(f"${{{scheme}:{name}}}: {name} isn't set in {where}")
        if used is not None:
            used.append(found)
        return found
    return REF_RE.sub(sub, value)


def references(spec: ServerSpec) -> list[tuple[str, str]]:
    """Every (scheme, name) the spec's url, header and env values reference, in order, without resolving them."""
    values = [spec.url or "", *spec.headers.values(), *spec.env.values()]
    return [(scheme, name) for v in values for scheme, name in REF_RE.findall(v)]


def resolved(spec: ServerSpec, lookups: Mapping[str, Lookup], used: list[str] | None = None) -> ServerSpec:
    """The spec with every url, header and env value resolved: what the gateway connects with (never shown).
    `used`, if given, collects the substituted values (resolve)."""
    return spec.model_copy(update={
        "url": resolve(spec.url, lookups, used) if spec.url is not None else None,
        "headers": {k: resolve(v, lookups, used) for k, v in spec.headers.items()},
        "env": {k: resolve(v, lookups, used) for k, v in spec.env.items()},
    })


def fingerprint(spec: ServerSpec, refs: Mapping[str, Any] | None = None) -> str:
    """Changes when anything the connection depends on does: the entry as written, and `refs`, what its references
    stand for now (the gateway's: env values, and secrets by updated_at, so none is decrypted to compare)."""
    what = {"spec": spec.model_dump(mode="json"), "refs": refs or {}}
    return hashlib.sha256(json.dumps(what, sort_keys=True).encode()).hexdigest()


def public_view(name: str, spec: ServerSpec) -> dict:
    """What the API shows of a registry entry: no header or env values, only their names, and the names of the
    secrets it references. The url is shown as written, so a credential in it should be a reference."""
    return {"name": name, "kind": spec.kind, "builtin": False, "description": spec.description,
            "url": spec.url, "command": spec.command, "args": list(spec.args),
            "headers": sorted(spec.headers), "env": sorted(spec.env),
            "secrets": sorted({n for scheme, n in references(spec) if scheme == "secret"})}
