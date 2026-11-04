"""The gateway's pure parts: the per-tool rule (policy.decide) and the registry (mcp.yaml)."""
import pytest

from valet.control.gateway.policy import decide
from valet.control.gateway.registry import RegistryError, parse, public_view, references, resolve, resolved

READ = {"readOnlyHint": True}
ADDITIVE = {"readOnlyHint": False, "destructiveHint": False}
DESTRUCTIVE = {"destructiveHint": True}
NONE = None                 # a tool without annotations: per the MCP spec, not read-only and destructive
EMPTY = {}

# DESIGN.md, "Permissions", gateway column.
TABLE = {
    #               read     additive  destructive  unannotated
    "locked":       ("allow", "deny",  "deny",      "deny"),
    "ask":          ("allow", "ask",   "ask",       "ask"),
    "trusted":      ("allow", "allow", "ask",       "ask"),
    "unrestricted": ("allow", "allow", "allow",     "allow"),
}


@pytest.mark.parametrize("level", TABLE)
def test_decision_table(level):
    got = tuple(decide(level, a) for a in (READ, ADDITIVE, DESTRUCTIVE, NONE))
    assert got == TABLE[level]
    assert decide(level, EMPTY) == decide(level, NONE)


def test_hints_are_read_strictly():
    # Only an explicit true makes a tool read-only, only an explicit false makes it additive.
    assert decide("locked", {"readOnlyHint": "yes"}) == "deny"
    assert decide("trusted", {"destructiveHint": None}) == "ask"
    # readOnlyHint wins over a stray destructiveHint (the spec: destructive is meaningful only when not read-only).
    assert decide("ask", {"readOnlyHint": True, "destructiveHint": True}) == "allow"
    assert decide("bogus", ADDITIVE) == "ask"


GOOD = """\
servers:
  github:
    description: GitHub
    url: https://example.com/mcp?key=${env:GH_KEY}
    headers:
      Authorization: Bearer ${env:GH_TOKEN}
  files:
    command: npx
    args: [-y, server-files, /srv]
    env:
      LOG: info
      TOKEN: ${env:FILES_TOKEN}
"""


def test_registry_parses():
    reg = parse(GOOD)
    assert set(reg.servers) == {"github", "files"}
    assert reg.servers["github"].kind == "http" and reg.servers["files"].kind == "stdio"
    assert parse("").servers == {} and parse("servers: {}\n").servers == {}
    view = public_view("github", reg.servers["github"])
    assert view["headers"] == ["Authorization"] and "GH_TOKEN" not in str(view["headers"])
    assert public_view("files", reg.servers["files"])["env"] == ["LOG", "TOKEN"]


@pytest.mark.parametrize("text,msg", [
    ("servers: [a]", "servers"),
    ("servers:\n  x: {}\n", "exactly one of url"),
    ("servers:\n  x: {url: http://a, command: b}\n", "exactly one of url"),
    ("servers:\n  x: {url: ftp://a}\n", "http(s) URL"),
    ("servers:\n  x: {url: http://a, env: {A: b}}\n", "stdio servers"),
    ("servers:\n  x: {command: a, headers: {A: b}}\n", "remote servers"),
    ("servers:\n  x: {command: -rf}\n", "program"),
    ("servers:\n  valet-x: {command: a}\n", "reserved"),
    ("servers:\n  a__b: {command: a}\n", "single '_'"),
    ("servers:\n  Big: {command: a}\n", "lowercase"),
    ("servers:\n  x: {url: 'http://a', headers: {'Bad Name': v}}\n", "header"),
    ("servers:\n  x: {command: a, env: {A: '${vault:x}'}}\n", "unknown reference"),
    ("servers:\n  x: {command: a, env: {A: '${secret:lower}'}}\n", "an env var name"),
    ("servers:\n  x: {command: a, env: {A: '${secret:VALET_KEY}'}}\n", "reserved"),
    ("servers:\n  x: {command: a, bogus: 1}\n", "Extra inputs"),
    ("[", "not valid YAML"),
])
def test_registry_rejects(text, msg):
    with pytest.raises(RegistryError) as e:
        parse(text)
    assert msg in str(e.value), str(e.value)


def test_references_resolve_in_the_control_plane():
    env = {"GH_TOKEN": "t0k", "GH_KEY": "k", "FILES_TOKEN": "f"}
    reg = parse(GOOD)
    gh = resolved(reg.servers["github"], {"env": env.get})
    assert gh.headers == {"Authorization": "Bearer t0k"} and gh.url == "https://example.com/mcp?key=k"
    assert resolved(reg.servers["files"], {"env": env.get}).env == {"LOG": "info", "TOKEN": "f"}
    with pytest.raises(RegistryError, match="GH_TOKEN isn't set in the control plane's environment"):
        resolved(reg.servers["github"], {"env": {"GH_KEY": "k"}.get})
    # Valet's named secrets: one more lookup, same format (the gateway's is the secrets store).
    assert resolve("${secret:GH}", {"secret": {"GH": "s"}.get}) == "s"
    with pytest.raises(RegistryError, match="GH isn't set in Valet's secrets"):
        resolve("${secret:GH}", {"secret": {}.get})
    with pytest.raises(RegistryError, match="unknown reference"):
        resolve("${secret:GH}", {"env": env.get})
    assert resolve("plain $HOME ${not a ref}", {}) == "plain $HOME ${not a ref}"
    # Every value substituted is collected, for redacting messages.
    used = []
    resolved(reg.servers["github"], {"env": env.get}, used)
    assert sorted(used) == ["k", "t0k"]


def test_references_are_listed_without_resolving():
    reg = parse(GOOD + """\
  vault:
    url: https://vault.example.com/mcp
    headers:
      Authorization: Bearer ${secret:VAULT_TOKEN}
      X-Org: ${secret:ORG_ID}-${env:ORG}
""")
    assert references(reg.servers["vault"]) == [("secret", "VAULT_TOKEN"), ("secret", "ORG_ID"), ("env", "ORG")]
    assert public_view("vault", reg.servers["vault"])["secrets"] == ["ORG_ID", "VAULT_TOKEN"]
    assert public_view("github", reg.servers["github"])["secrets"] == []


def test_upstream_messages_redact_the_credentials():
    from valet.control.gateway.upstream import _describe
    e = ExceptionGroup("wrapped", [ValueError("401 for https://x.example/mcp?key=t0k&org=0rg-1 and Bearer t0k")])
    assert _describe(e, ["0rg-1", "t0k"]) == "401 for https://x.example/mcp?key=[redacted]&org=[redacted] and " \
                                              "Bearer [redacted]"
    long = ValueError("x" * 299 + "s3cret")
    assert "s3c" not in _describe(long, ["s3cret"])      # redacted before it's cut short
