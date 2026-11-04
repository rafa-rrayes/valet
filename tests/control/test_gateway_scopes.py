"""The gateway's whole decision (policy.decide_tool: rules, grants, the level) as a table, and what an answer's scope
leaves behind (scopes.py): the ask's options, hour grants, the "always" rule written into agent.yaml."""
import subprocess
import time

import pytest

from valet.control.db import Database
from valet.control.definitions import DefinitionsRepo
from valet.control.gateway import scopes
from valet.control.gateway.policy import decide, decide_tool
from valet.shared.definition import DefinitionError, parse_agent

READ = {"readOnlyHint": True}
ADDITIVE = {"readOnlyHint": False, "destructiveHint": False}
DESTRUCTIVE = None              # unannotated: destructive, per the MCP spec


# (level, annotations, the server's rules, granted) -> (decision, source), for a tool called "t".
TABLE = [
    # no rules: the level, as before (policy.decide)
    ("locked", READ, None, False, ("allow", "level")),
    ("locked", DESTRUCTIVE, None, False, ("deny", "level")),
    ("ask", ADDITIVE, None, False, ("ask", "level")),
    ("trusted", ADDITIVE, None, False, ("allow", "level")),
    ("trusted", DESTRUCTIVE, None, False, ("ask", "level")),
    ("unrestricted", DESTRUCTIVE, None, False, ("allow", "level")),
    # the tool's own rule beats the level, both ways
    ("locked", DESTRUCTIVE, {"t": "allow"}, False, ("allow", "rule")),      # listed even at locked
    ("locked", DESTRUCTIVE, {"t": "ask"}, False, ("ask", "rule")),
    ("unrestricted", READ, {"t": "deny"}, False, ("deny", "rule")),         # hidden even when unrestricted
    ("unrestricted", READ, {"t": "ask"}, False, ("ask", "rule")),
    # the server's "*", unless the tool has its own
    ("ask", ADDITIVE, {"*": "allow"}, False, ("allow", "rule")),
    ("ask", READ, {"*": "deny"}, False, ("deny", "rule")),
    ("ask", READ, {"*": "deny", "t": "allow"}, False, ("allow", "rule")),
    ("trusted", ADDITIVE, {"*": "allow", "t": "ask"}, False, ("ask", "rule")),
    ("ask", ADDITIVE, {"other": "deny"}, False, ("ask", "level")),          # another tool's rule
    # a live hour grant turns an ask into an allow, wherever the ask came from; never a deny
    ("ask", ADDITIVE, None, True, ("allow", "grant")),
    ("trusted", DESTRUCTIVE, None, True, ("allow", "grant")),
    ("unrestricted", READ, {"t": "ask"}, True, ("allow", "grant")),
    ("ask", READ, {"*": "ask"}, True, ("allow", "grant")),
    ("locked", DESTRUCTIVE, None, True, ("deny", "level")),
    ("ask", ADDITIVE, {"t": "deny"}, True, ("deny", "rule")),
    ("ask", READ, None, True, ("allow", "level")),                          # allowed anyway: the level says so
    ("ask", ADDITIVE, {"t": "allow"}, True, ("allow", "rule")),
]


@pytest.mark.parametrize("level,annotations,rules,granted,expected", TABLE)
def test_decision_table(level, annotations, rules, granted, expected):
    assert decide_tool(level, annotations, rules, "t", granted) == expected


def test_without_rules_or_grants_it_is_the_level():
    for level in ("locked", "ask", "trusted", "unrestricted", "bogus"):
        for ann in (READ, ADDITIVE, DESTRUCTIVE, {}):
            assert decide_tool(level, ann, None, "t") == (decide(level, ann), "level")
            assert decide_tool(level, ann, {}, "t") == (decide(level, ann), "level")


def test_ask_options_follow_the_contract():
    assert scopes.options("create_issue") == [
        {"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"},
        {"optionId": "allow_hour", "name": "Allow for an hour", "kind": "allow_once"},
        {"optionId": "allow_always", "name": "Always allow", "kind": "allow_always"},
        {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"}]
    # A tool whose name a rule can't hold (MCP only says SHOULD) gets no "always".
    assert [o["optionId"] for o in scopes.options("do it/now")] == ["allow_once", "allow_hour", "reject_once"]
    opts = scopes.options("x")
    opts[0]["name"] = "changed"
    assert scopes.OPTIONS[0]["name"] == "Allow once"           # copies


def test_grants(tmp_path):
    db = Database(tmp_path / "v.db")
    g = scopes.grant(db, "scout", "github", "create_issue")
    assert g["expires_at"] - g["created_at"] == pytest.approx(scopes.HOUR)
    assert scopes.granted(db, "scout", "github") == {"create_issue"}
    assert scopes.granted(db, "scout", "notes") == set() and scopes.granted(db, "coder", "github") == set()
    # A new grant for the same tool renews it (one row, same id).
    time.sleep(0.01)
    again = scopes.grant(db, "scout", "github", "create_issue")
    assert again["id"] == g["id"] and again["expires_at"] > g["expires_at"]
    other = scopes.grant(db, "coder", "notes", "write", seconds=0.05)
    assert [x["id"] for x in scopes.live(db)] == [other["id"], g["id"]]       # soonest to expire first
    assert [x["agent"] for x in scopes.live(db, "scout")] == ["scout"]
    time.sleep(0.06)
    assert scopes.granted(db, "coder", "notes") == set()                      # expired
    assert [x["id"] for x in scopes.live(db)] == [g["id"]]
    assert db.one("SELECT COUNT(*) AS n FROM tool_grants")["n"] == 1         # and cleaned up
    assert scopes.revoke(db, other["id"]) is None
    assert scopes.revoke(db, g["id"])["tool"] == "create_issue"
    assert scopes.live(db) == [] and scopes.granted(db, "scout", "github") == set()


AGENT = """\
name: scout
# a comment the rewrite drops, as a settings save does
harness: claude
permissions: ask
tools:
  mcp: [github]
"""


def git(root, *args) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout.strip()


def test_always_allow_writes_a_rule_in_one_commit(tmp_path):
    defs = DefinitionsRepo(tmp_path / "defs")
    defs.init()
    defs.create("scout", AGENT)
    commit = scopes.always_allow(defs, "scout", "github", "create_issue")
    assert commit == git(defs.root, "rev-parse", "HEAD")
    assert git(defs.root, "log", "-1", "--format=%s") == "Inbox: always allow github create_issue for scout"
    assert git(defs.root, "show", "--stat", "--format=", "HEAD").splitlines()[0].strip().startswith(
        "agents/scout/agent.yaml")
    d = defs.definition("scout")
    assert d.tools.rules == {"github": {"create_issue": "allow"}}
    assert d.tools.mcp == ["github"] and d.permissions == "ask"
    text = (defs.root / "agents/scout/agent.yaml").read_text()
    assert "rules:\n    github:\n      create_issue: allow" in text and "# a comment" not in text
    assert parse_agent(text, "scout") == d
    # Again: nothing to do, no commit. Another tool joins the server's rules; a deny on it is replaced.
    assert scopes.always_allow(defs, "scout", "github", "create_issue") is None
    defs.write("scout", {"agent.yaml": text.replace("create_issue: allow", "create_issue: allow\n      "
                                                    "delete_repo: deny")}, "edit")
    scopes.always_allow(defs, "scout", "github", "delete_repo")
    assert defs.definition("scout").tools.rules == {"github": {"create_issue": "allow", "delete_repo": "allow"}}


def test_always_allow_leaves_a_broken_definition_alone(tmp_path):
    defs = DefinitionsRepo(tmp_path / "defs")
    defs.init()
    defs.create("scout", AGENT)
    (defs.root / "agents/scout/agent.yaml").write_text("name: scout\nbogus: 1\n")
    head = git(defs.root, "rev-parse", "HEAD")
    with pytest.raises(DefinitionError):
        scopes.always_allow(defs, "scout", "github", "create_issue")
    assert git(defs.root, "rev-parse", "HEAD") == head
    assert (defs.root / "agents/scout/agent.yaml").read_text() == "name: scout\nbogus: 1\n"
