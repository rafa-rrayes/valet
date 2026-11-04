"""The browser's rules as files (shared/browser.py, definition.py's `browser` and vault secrets): site patterns and
their matching, browser.yaml, an agent's browser.rules, and vault entry names."""
import pytest

from valet.shared import browser as b
from valet.shared.browser import BrowserError, best_rule, matches, normalize_pattern, parse, specificity, to_yaml
from valet.shared.definition import AgentDefinition, DefinitionError, parse_agent as parse_agent_yaml, to_yaml as agent_yaml
from valet.shared.definition import RESERVED_PREFIXES, VaultSecret, vault_env, vault_name_error

BASE = "name: scout\nharness: claude\n"


# ---------- site patterns ----------

@pytest.mark.parametrize("given,stored", [
    ("github.com", "github.com"),
    ("GitHub.com", "github.com"),
    ("https://github.com/", "github.com"),
    ("http://admin.shopify.com/store/", "admin.shopify.com/store"),
    ("*.google.com", "*.google.com"),
    ("*", "*"),
    ("  dash.cloudflare.com  ", "dash.cloudflare.com"),
    ("my_host-1.example", "my_host-1.example"),
    ("docs.*.example.com/a/b", "docs.*.example.com/a/b"),
])
def test_patterns_normalize(given, stored):
    assert normalize_pattern(given) == stored


@pytest.mark.parametrize("bad", ["", "/", "github.com:443", "a b.com", "a.com/x?y=1", "a.com#top", "a..com",
                                 ".a.com", "x" * 201, "ex%ample.com", 42])
def test_bad_patterns(bad):
    with pytest.raises(ValueError):
        normalize_pattern(bad)


@pytest.mark.parametrize("pattern,url,hit", [
    ("github.com", "https://github.com/home", True),
    ("github.com", "https://gist.github.com/", False),
    ("github.com", "https://GitHub.com./x", True),             # case and a trailing dot don't matter
    ("*.google.com", "https://google.com/", True),             # *.domain covers the domain itself
    ("*.google.com", "https://mail.google.com/u/0", True),
    ("*.google.com", "https://a.b.google.com/", True),
    ("*.google.com", "https://evilgoogle.com/", False),
    ("*.google.com", "https://google.com.evil.net/", False),
    ("mail.*.com", "https://mail.google.com/", True),
    ("mail.?oogle.com", "https://mail.google.com/", True),
    ("*", "https://anything.example/", True),
    ("*", "about:blank", False),                               # no host
    ("admin.shopify.com/store", "https://admin.shopify.com/store/home/orders", True),
    ("admin.shopify.com/store", "https://admin.shopify.com/Store", True),      # path prefixes ignore case
    ("admin.shopify.com/store", "https://admin.shopify.com/settings", False),
    ("admin.shopify.com/store", "https://admin.shopify.com/", False),
    ("github.com", "http://github.com:8080/x", True),           # ports aren't part of a pattern
    ("github.com", "not a url", False),
    ("not a pattern!", "https://github.com/", False),
])
def test_matching(pattern, url, hit):
    assert matches(pattern, url) is hit


def test_the_most_specific_rule_wins():
    rules = {"*": {"read": "allow", "commit": "deny"},
             "*.google.com": {"commit": "ask"},
             "mail.google.com": {"commit": "allow"},
             "mail.google.com/settings": {"commit": "deny"},
             "m?il.google.com": {"interact": "deny"}}
    assert best_rule(rules, "https://mail.google.com/u/0", "commit") == ("mail.google.com", "allow")
    assert best_rule(rules, "https://mail.google.com/settings/x", "commit") == ("mail.google.com/settings", "deny")
    assert best_rule(rules, "https://docs.google.com/", "commit") == ("*.google.com", "ask")
    assert best_rule(rules, "https://example.com/", "commit") == ("*", "deny")
    # A rule names only the kinds it sets: the others fall through to a broader pattern (or to nothing).
    assert best_rule(rules, "https://mail.google.com/", "read") == ("*", "allow")
    assert best_rule(rules, "https://mail.google.com/", "interact") == ("m?il.google.com", "deny")
    assert best_rule(rules, "https://mail.google.com/", "login") is None
    assert best_rule({}, "https://x.com/", "read") is None
    # Longer path, then fewer wildcards, then longer host.
    assert specificity("a.com/x/y") > specificity("*.a.com/x") > specificity("a.com")
    assert specificity("mail.google.com") > specificity("*.google.com") > specificity("*")
    assert specificity("mail.google.com") > specificity("google.com")


# ---------- browser.yaml ----------

GOOD = """\
profiles:
  home:
    title: Home logins
    note: Personal accounts.
    sites:
      GitHub.com: {commit: allow}
      "https://*.google.com/": {read: allow, login: ask}
      admin.shopify.com/store: {commit: deny, interact: ask}
  work: {}
  empty:
"""


def test_browser_yaml_parses_and_round_trips():
    f = parse(GOOD)
    assert list(f.profiles) == ["home", "work", "empty"]
    home = f.profiles["home"]
    assert home.title == "Home logins" and home.note == "Personal accounts."
    assert home.sites == {"github.com": {"commit": "allow"}, "*.google.com": {"read": "allow", "login": "ask"},
                          "admin.shopify.com/store": {"commit": "deny", "interact": "ask"}}
    assert f.profiles["work"].sites == {} and f.profiles["empty"].title is None
    text = to_yaml(f)
    assert text.startswith("profiles:\n  empty: {}\n  home:\n")       # by name; empty fields left out
    assert parse(text) == f and to_yaml(parse(text)) == text
    assert parse("").profiles == {} and parse("profiles:\n").profiles == {} and parse("# nothing\n").profiles == {}


def test_titles_with_odd_characters_round_trip():
    f = parse("profiles:\n  a:\n    title: x\n")
    f.profiles["a"].note = "line one\x85line two"
    assert parse(to_yaml(f)) == f


@pytest.mark.parametrize("text,msg", [
    ("[", "not valid YAML"),
    ("- a\n", "a mapping with `profiles`"),
    ("profiles: [a]\n", "profiles"),
    ("profiles:\n  Alice: {}\n", "'Alice': lowercase letters"),
    ("profiles:\n  -a: {}\n", "lowercase letters"),
    (f"profiles:\n  {'a' * 32}: {{}}\n", "at most 31"),
    ("profiles:\n  a: {sites: {github.com: {read: maybe}}}\n", "read"),
    ("profiles:\n  a: {sites: {github.com: {click: allow}}}\n", "click"),
    ("profiles:\n  a: {sites: {github.com: {}}}\n", "sets at least one of read, interact, commit, login"),
    ("profiles:\n  a: {sites: {'github.com:443': {read: allow}}}\n", "no port"),
    ("profiles:\n  a: {sites: {GitHub.com: {read: allow}, github.com/: {commit: ask}}}\n", "listed twice"),
    ("profiles:\n  a: {title: \"two\\nlines\"}\n", "one line"),
    (f"profiles:\n  a: {{title: {'x' * 61}}}\n", "at most 60"),
    (f"profiles:\n  a: {{note: {'x' * 2001}}}\n", "at most 2000"),
    ("profiles:\n  a: {colour: red}\n", "Extra inputs"),
    ("profiles: {}\nother: 1\n", "Extra inputs"),
])
def test_browser_yaml_rejects(text, msg):
    with pytest.raises(BrowserError) as e:
        parse(text)
    assert str(e.value).startswith("browser.yaml") and msg in str(e.value), str(e.value)


def test_profile_names():
    for good in ("home", "a", "work-2", "0x", "a" * 31):
        assert b.profile_name_error(good) is None
    for bad in ("", "Alice", "-a", "a_b", "a.b", "a" * 32, None, 3):
        assert b.profile_name_error(bad)


# ---------- agent.yaml: browser ----------

def test_agent_browser_round_trips():
    d = parse_agent_yaml(BASE + "browser:\n  profiles: [home, work]\n  rules:\n    https://GitHub.com/: {commit: allow}\n"
                                "    '*.google.com': {read: deny}\n")
    assert d.browser.profiles == ["home", "work"]
    assert d.browser.rules == {"github.com": {"commit": "allow"}, "*.google.com": {"read": "deny"}}
    assert parse_agent_yaml(agent_yaml(d)) == d
    plain = parse_agent_yaml(BASE)
    assert plain.browser.profiles == [] and plain.browser.rules == {}
    assert "browser" not in agent_yaml(plain)
    only_profiles = parse_agent_yaml(BASE + "browser:\n  profiles: [home]\n")
    assert "rules" not in agent_yaml(only_profiles) and parse_agent_yaml(agent_yaml(only_profiles)) == only_profiles


@pytest.mark.parametrize("extra,msg", [
    ("browser:\n  profiles: [Alice]\n", "lowercase letters"),
    ("browser:\n  profiles: [home, home]\n", "twice"),
    ("browser:\n  rules: {github.com: {commit: sure}}\n", "commit"),
    ("browser:\n  rules: {github.com: {}}\n", "sets at least one"),
    ("browser:\n  rules: {'a b': {read: allow}}\n", "a site like"),
    ("browser:\n  colour: red\n", "Extra inputs"),
])
def test_agent_browser_rejects(extra, msg):
    with pytest.raises(DefinitionError) as e:
        parse_agent_yaml(BASE + extra)
    assert msg in str(e.value), str(e.value)


# ---------- vault entries in secrets ----------

def test_vault_names_and_env_vars():
    for good in ("github", "github/home", "aws/home/prod", "a.b_c-d/e", "0/1"):
        assert vault_name_error(good) is None, good
    for bad in ("", "GitHub", "/a", "a/", "a//b", "a/b/c/d", "-a", "a/-b", "a b", "x" * 65, None):
        assert vault_name_error(bad), bad
    assert vault_env("github/home") == "GITHUB_HOME_PASSWORD"
    assert vault_env("aws.prod/ci-bot") == "AWS_PROD_CI_BOT_PASSWORD"
    assert vault_env("0/home") == "VAULT_0_HOME_PASSWORD"          # an IP site's suggested name
    assert vault_env("_x/home") == "VAULT__X_HOME_PASSWORD"
    assert vault_env("docker/ci") == "VAULT_DOCKER_CI_PASSWORD"    # DOCKER_ is reserved
    assert VaultSecret(vault="0/home").env_name == "VAULT_0_HOME_PASSWORD"

    d = parse_agent_yaml(BASE + "secrets:\n  - GITHUB_TOKEN\n  - vault: github/home\n  - {vault: aws/home, env: AWS_PW}\n")
    assert [s.env_name for s in d.vault_secrets()] == ["GITHUB_HOME_PASSWORD", "AWS_PW"]
    assert parse_agent_yaml(agent_yaml(d)) == d
    assert parse_agent_yaml(BASE + "secrets:\n  - vault: 0/x\n").vault_secrets()[0].env_name == "VAULT_0_X_PASSWORD"
    long = "a" * 60     # a fine entry name, but 69 characters as an env var
    with pytest.raises(DefinitionError, match=f"vault {long}: its env var would be {long.upper()}_PASSWORD, which can't be"):
        parse_agent_yaml(BASE + f"secrets:\n  - vault: {long}\n")
    assert parse_agent_yaml(BASE + "secrets:\n  - {vault: 0/x, env: ZERO_PW}\n").vault_secrets()[0].env_name == "ZERO_PW"
    with pytest.raises(DefinitionError, match="GITHUB_HOME_PASSWORD"):
        parse_agent_yaml(BASE + "secrets:\n  - GITHUB_HOME_PASSWORD\n  - vault: github/home\n")
    with pytest.raises(DefinitionError, match="vault entry"):
        parse_agent_yaml(BASE + "secrets:\n  - vault: GitHub\n")


def test_definition_model_takes_browser_dicts():
    d = AgentDefinition.model_validate({"name": "scout", "harness": "claude",
                                        "browser": {"profiles": ["home"], "rules": {"X.com": {"login": "ask"}}}})
    assert d.browser.rules == {"x.com": {"login": "ask"}}
