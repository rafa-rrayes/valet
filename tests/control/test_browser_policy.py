"""The browser's decision (control/browser/policy.py): the baseline table, the precedence of the agent's rules over
the profile's over the level, hour grants, URL schemes, and how a click is classified."""
import pytest

from valet.control.browser.policy import BASELINE, classify_click, decide

URL = "https://github.com/alice/valet"
KINDS = ("read", "interact", "commit", "login")

# DESIGN.md, "Permissions", the browser column.
TABLE = {
    #               read     interact  commit  login
    "locked":       ("allow", "deny",  "deny",  "deny"),
    "ask":          ("allow", "ask",   "ask",   "ask"),
    "trusted":      ("allow", "allow", "ask",   "allow"),
    "unrestricted": ("allow", "allow", "allow", "allow"),
}


@pytest.mark.parametrize("level", TABLE)
def test_baseline_table(level):
    got = tuple(decide(level, {}, {}, k, URL).mode for k in KINDS)
    assert got == TABLE[level]
    assert tuple(BASELINE[level][k] for k in KINDS) == TABLE[level]
    assert decide(level, {}, {}, "commit", URL).reason == f"your permission level, {level}"


def test_an_unknown_level_is_ask():
    v = decide("bogus", {}, {}, "commit", URL)
    assert (v.mode, v.reason) == ("ask", "your permission level, ask")
    assert decide("bogus", {}, {}, "read", URL).mode == "allow"


def test_the_agents_rule_beats_the_profiles_beats_the_level():
    agent = {"github.com": {"commit": "allow"}, "*.google.com": {"read": "deny"}}
    profile = {"github.com": {"commit": "deny", "interact": "deny"}, "*": {"login": "allow"},
               "github.com/alice": {"read": "ask"}}
    v = decide("locked", agent, profile, "commit", URL)
    assert (v.mode, v.reason) == ("allow", "your rule for github.com")                  # allow beats locked
    v = decide("unrestricted", agent, profile, "interact", URL)
    assert (v.mode, v.reason) == ("deny", "the profile's rule for github.com")          # deny beats unrestricted
    v = decide("ask", agent, profile, "read", URL)
    assert (v.mode, v.reason) == ("ask", "the profile's rule for github.com/alice")      # even a read can ask
    v = decide("unrestricted", agent, profile, "read", "https://mail.google.com/")
    assert (v.mode, v.reason) == ("deny", "your rule for *.google.com")
    v = decide("ask", agent, profile, "login", "https://example.com/")
    assert (v.mode, v.reason) == ("allow", "the profile's rule for *")
    # The agent's rule wins even when the profile's pattern is more specific: precedence is by owner, then
    # specificity within each.
    v = decide("ask", {"*": {"read": "allow"}}, profile, "read", URL)
    assert (v.mode, v.reason) == ("allow", "your rule for *")
    # A rule that doesn't name the kind falls through.
    v = decide("trusted", agent, {}, "login", URL)
    assert (v.mode, v.reason) == ("allow", "your permission level, trusted")


def test_hour_grants_turn_an_ask_into_an_allow():
    grants = {("github.com", "commit")}
    v = decide("ask", {}, {}, "commit", URL, grants)
    assert (v.mode, v.reason) == ("allow", "The owner allowed it for an hour")
    assert decide("ask", {}, {}, "interact", URL, grants).mode == "ask"                 # per kind
    assert decide("ask", {}, {}, "commit", "https://gist.github.com/", grants).mode == "ask"    # per host
    # A grant never overrides a deny.
    assert decide("locked", {}, {}, "commit", URL, grants).mode == "deny"
    assert decide("ask", {"github.com": {"commit": "deny"}}, {}, "commit", URL, grants).mode == "deny"
    # And an ask from a rule is granted the same way.
    assert decide("unrestricted", {"github.com": {"commit": "ask"}}, {}, "commit", URL, grants).mode == "allow"


@pytest.mark.parametrize("url,mode", [
    ("about:blank", "allow"),
    ("about:blank#x", "allow"),
    ("about:blankx", "deny"),
    ("about:config", "deny"),
    ("chrome://settings", "deny"),
    ("file:///etc/passwd", "deny"),
    ("javascript:alert(1)", "deny"),
    ("data:text/html,hi", "deny"),
    ("view-source:https://github.com/", "deny"),
    ("ftp://example.com/", "deny"),
    ("https://", "deny"),
    ("HTTP://GitHub.com/", "allow"),
])
def test_only_web_pages(url, mode):
    v = decide("unrestricted", {"*": {"read": "allow"}}, {}, "read", url)
    assert v.mode == mode, (url, v)
    if mode == "deny":
        assert v.reason == "only http(s) pages are allowed"


@pytest.mark.parametrize("name,el_type,submit,kind", [
    ("Next page", "", False, "interact"),
    ("Issues", "", False, "interact"),
    ("Settings", "button", False, "interact"),
    ("Delete repository", "", False, "commit"),
    ("Save", "", False, "commit"),
    ("Pay now", "", False, "commit"),
    ("Place order", "", False, "commit"),
    ("Sign out", "", False, "commit"),
    ("Log out", "", False, "commit"),
    ("Logout", "", False, "commit"),
    ("SEND", "", False, "commit"),
    ("Excluir conta", "", False, "commit"),
    ("Guardar cambios", "", False, "commit"),
    ("Saved items", "", False, "interact"),        # whole words only
    ("Go", "submit", False, "commit"),
    ("Go", "SUBMIT", False, "commit"),
    ("Continue", "", True, "commit"),               # a button that submits a form
    ("", "", False, "interact"),
])
def test_click_classification(name, el_type, submit, kind):
    assert classify_click(name, el_type, submit) == kind
