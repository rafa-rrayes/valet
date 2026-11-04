"""The browser's rules as files: `browser.yaml` at the definitions repo's root (the profiles and their site rules)
and an agent's own `browser.rules` in its agent.yaml. Both say, per site pattern, what each kind of action does:

```yaml
profiles:
  alice:
    title: Personal logins
    note: Personal accounts.
    sites:
      github.com: {commit: allow}
      "*.google.com": {read: allow, login: ask}
      admin.shopify.com/store: {commit: deny}
```

Kinds: `read` (open and read pages, scroll), `interact` (click links, type, choose options), `commit` (buttons
that save, send, pay, delete, publish; submitting a form), `login` (Valet fills saved credentials). Modes: `allow`,
`ask`, `deny`. A rule names only the kinds it sets.

Site patterns: a host glob (`dash.cloudflare.com`, `*.google.com`, `*`),
optionally followed by a path prefix (`admin.shopify.com/store`). `*.domain` matches the domain itself and every
subdomain; any other host part is an fnmatch glob. When several patterns match a URL, the most specific wins:
the longer path, then fewer wildcards, then the longer host."""
import re
import unicodedata
from fnmatch import fnmatch
from typing import Annotated, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import AfterValidator, BaseModel, ConfigDict, ValidationError, field_validator

PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
KINDS = ("read", "interact", "commit", "login")
MODES = ("allow", "ask", "deny")
BROWSER_FILE = "browser.yaml"
PATTERN_MAX = 200
# The host part of a pattern: labels of letters, digits, '-', '_' and the glob characters * and ?; `*.` in front.
_HOST_RE = re.compile(r"^(\*\.)?[a-z0-9_*?-]+(\.[a-z0-9_*?-]+)*$")

Kind = Literal["read", "interact", "commit", "login"]
Mode = Literal["allow", "ask", "deny"]
SiteRule = dict[Kind, Mode]


class BrowserError(ValueError):
    """browser.yaml that doesn't parse or validate; the message is shown to the owner as is."""


# ---------- site patterns ----------

def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def path_of(url: str) -> str:
    try:
        return urlsplit(url).path or "/"
    except ValueError:
        return "/"


def normalize_pattern(pattern: str) -> str:
    """What people type or paste -> the pattern as stored: lowercase, without a scheme or a trailing slash.
    ValueError if it isn't one."""
    if not isinstance(pattern, str):
        raise ValueError("a site pattern is text, like github.com or *.google.com")
    p = pattern.strip().lower()
    if "://" in p:
        p = p.split("://", 1)[1]
    p = p.rstrip("/")
    host, _, path = p.partition("/")
    if not p or len(p) > PATTERN_MAX or any(c.isspace() for c in p) or "#" in p or "?" in path:
        raise ValueError(f"{pattern!r}: a site like github.com, *.google.com or admin.shopify.com/store")
    if not _HOST_RE.match(host):
        raise ValueError(f"{pattern!r}: a site like github.com, *.google.com or admin.shopify.com/store (no port)")
    return p


def matches(pattern: str, url: str) -> bool:
    """Whether `url` falls under `pattern` (normalized or not)."""
    try:
        pattern = normalize_pattern(pattern)
    except ValueError:
        return False
    host_pat, _, path_pat = pattern.partition("/")
    host = host_of(url)
    if not host:
        return False
    if host_pat.startswith("*."):
        base = host_pat[2:]
        ok = host == base or host.endswith("." + base)
    else:
        ok = fnmatch(host, host_pat)
    return ok and (not path_pat or path_of(url).lower().startswith("/" + path_pat))


def specificity(pattern: str) -> tuple[int, int, int]:
    """More specific patterns sort higher: longer path, then fewer wildcards, then longer host."""
    host_pat, _, path_pat = pattern.strip().lower().partition("/")
    return len(path_pat), -(host_pat.count("*") + host_pat.count("?")), len(host_pat)


def best_rule(rules: dict[str, dict[str, str]], url: str, kind: str) -> tuple[str, str] | None:
    """(pattern, mode) of the most specific pattern in `rules` that matches `url` and sets `kind`, or None."""
    hits = [(p, r[kind]) for p, r in rules.items() if kind in r and matches(p, url)]
    if not hits:
        return None
    return max(hits, key=lambda h: specificity(h[0]))


def _site_rules(v: dict) -> dict:
    """Keys normalized (two that normalize alike are an error), each rule naming at least one kind."""
    out: dict[str, dict] = {}
    for key, rule in (v or {}).items():
        p = normalize_pattern(key)
        if p in out:
            raise ValueError(f"{p} is listed twice")
        if not rule:
            raise ValueError(f"{p}: a site rule sets at least one of {', '.join(KINDS)}")
        out[p] = dict(rule)
    return out


SiteRules = Annotated[dict[str, SiteRule], AfterValidator(_site_rules)]


def profile_name_error(name: object) -> str | None:
    if not isinstance(name, str) or not PROFILE_RE.match(name):
        return "lowercase letters, digits and dashes, starting with a letter or digit, at most 31"
    return None


# ---------- browser.yaml ----------

class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Profile(_Model):
    title: str | None = None        # how the apps show it ("Personal logins"); one line, at most 60 characters
    note: str | None = None         # what it's for, shown to the owner and to the agents that use it
    sites: SiteRules = {}

    @field_validator("title")
    @classmethod
    def _title(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if len(v) > 60 or any(unicodedata.category(c) in ("Cc", "Zl", "Zp") for c in v):
            raise ValueError("one line, at most 60 characters")
        return v or None

    @field_validator("note")
    @classmethod
    def _note(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if len(v) > 2000:
            raise ValueError("at most 2000 characters")
        return v.strip() or None


class BrowserFile(_Model):
    profiles: dict[str, Profile] = {}

    @field_validator("profiles", mode="before")
    @classmethod
    def _names(cls, v):
        if v is None:
            return {}
        if not isinstance(v, dict):
            return v
        for name in v:
            if err := profile_name_error(name):
                raise ValueError(f"{name!r}: {err}")
        return {k: (p if p is not None else {}) for k, p in v.items()}


def _errors(e: ValidationError) -> str:
    return "; ".join(f"{'.'.join(map(str, err['loc'])) or '(root)'}: {err['msg'].removeprefix('Value error, ')}"
                     for err in e.errors())


def parse(text: str) -> BrowserFile:
    """browser.yaml text ("" or missing: no profiles) -> its model. BrowserError if it doesn't validate."""
    try:
        data = yaml.safe_load(text) if text.strip() else {}
    except yaml.YAMLError as e:
        raise BrowserError(f"browser.yaml is not valid YAML: {e}") from None
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise BrowserError("browser.yaml must be a mapping with `profiles`")
    try:
        return BrowserFile.model_validate(data)
    except ValidationError as e:
        raise BrowserError(f"browser.yaml: {_errors(e)}") from None


class _Dumper(yaml.SafeDumper):
    """safe_dump, except that a string with NEL (U+0085) is double-quoted (as in definition.to_yaml)."""


_Dumper.add_representer(str, lambda dumper, s: dumper.represent_scalar(
    "tag:yaml.org,2002:str", s, style='"' if "\x85" in s else None))


def to_yaml(f: BrowserFile) -> str:
    """The canonical browser.yaml: profiles by name, empty fields left out. parse(to_yaml(f)) == f."""
    data = {"profiles": {name: p.model_dump(mode="json", exclude_defaults=True)
                         for name, p in sorted(f.profiles.items())}}
    return yaml.dump(data, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=1000)
