"""tools.rules in the definition: what's valid, the YAML it's written as, and staying out of the restart
fingerprint."""
import hashlib
import json

import pytest
from pydantic import ValidationError

from valet.control import views
from valet.shared.definition import AgentDefinition, DefinitionError, parse_agent, to_yaml


def agent(**tools) -> AgentDefinition:
    return AgentDefinition.model_validate({"name": "scout", "tools": tools})


def test_rules_parse_and_round_trip():
    text = """\
name: scout
tools:
  mcp: [github, valet.agents]
  rules:
    github: {create_issue: allow, delete_repo: deny, "*": ask}
    valet.agents: {list_agents: deny}
"""
    d = parse_agent(text, "scout")
    assert d.tools.rules == {"github": {"create_issue": "allow", "delete_repo": "deny", "*": "ask"},
                             "valet.agents": {"list_agents": "deny"}}
    out = to_yaml(d)
    assert "'*': ask" in out                      # quoted: a bare * is a YAML alias
    assert parse_agent(out, "scout") == d
    # Tool names MCP allows: letters, digits, _, - and ., up to 128.
    d = agent(rules={"notes": {"Get.Note-v2_x": "allow", "x" * 128: "ask"}})
    assert parse_agent(to_yaml(d), "scout") == d


def test_tool_names_that_yaml_would_read_as_something_else_survive():
    d = agent(rules={"notes": {"on": "allow", "null": "deny", "1": "ask", "true": "allow"}})
    assert parse_agent(to_yaml(d), "scout") == d


def test_no_rules_writes_nothing():
    assert "rules" not in to_yaml(agent(mcp=["github"]))
    # A server left with no rules goes, so an emptied rule set leaves the file as it was.
    assert agent(rules={"github": {}}).tools.rules == {}
    assert to_yaml(agent(mcp=["github"], rules={"github": {}})) == to_yaml(agent(mcp=["github"]))


@pytest.mark.parametrize("rules,msg", [
    ({"Bad": {"x": "allow"}}, "a server name"),
    ({"a__b": {"x": "allow"}}, "a server name"),
    ({"github": {"has space": "allow"}}, "a tool's name"),
    ({"github": {"x" * 129: "allow"}}, "a tool's name"),
    ({"github": {"": "allow"}}, "a tool's name"),
    ({"github": {"a/b": "allow"}}, "a tool's name"),
    ({"github": {"x": "always"}}, "allow"),
    ({"github": {"x": True}}, "allow"),
    ({"github": ["x"]}, "dictionary"),
])
def test_bad_rules_are_refused(rules, msg):
    with pytest.raises(ValidationError) as e:
        agent(rules=rules)
    assert msg in str(e.value), str(e.value)


def test_a_yaml_boolean_key_is_refused_not_guessed():
    with pytest.raises(DefinitionError, match="tools.rules"):
        parse_agent("name: scout\ntools:\n  rules:\n    github: {on: allow}\n", "scout")


def test_rules_stay_out_of_the_fingerprint():
    """A rule applies at the next call, so it never makes an agent need a restart, and an agent without rules
    fingerprints exactly as it did before rules existed (no running agent turns restart_needed on the upgrade)."""
    plain = agent(mcp=["github"])
    ruled = agent(mcp=["github"], rules={"github": {"create_issue": "allow"}})
    files = {"persona.md": "x"}
    assert views.fingerprint(plain, files) == views.fingerprint(ruled, files)
    before = plain.model_dump(mode="json", exclude={"display_name", "emoji"})
    del before["tools"]["rules"]                 # the model as it was in M2 (browser.rules came with M3 too)
    del before["browser"]["rules"]
    del before["computer"]["workdir"]          # left out while unset (M2 leftovers)
    old = hashlib.sha256(json.dumps({"definition": before, "files": files}, sort_keys=True).encode()).hexdigest()[:16]
    assert views.fingerprint(plain, files) == old
    # Everything else in tools still counts.
    assert views.fingerprint(agent(mcp=["notes"]), files) != views.fingerprint(plain, files)
