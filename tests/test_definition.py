"""agent.yaml parsing: the full DESIGN.md shape parses; mistakes come back as readable DefinitionErrors. And
writing it back: to_yaml, what the settings page saves."""
import pytest
import yaml
from pydantic import ValidationError

from valet.shared.definition import (
    PERSONA_TEMPLATE, TEMPLATE, AgentDefinition, DefinitionError, VaultSecret, list_agents, load_agent, parse_agent,
    read_files, to_yaml, validation_errors,
)

FULL = """\
name: scout
harness: claude
model: claude-opus-5-5
provider: anthropic
runner: myhost
computer:
  level: container
  image: valet/agent-full
  network: allowlist
  allowlist: [github.com]
  mounts:
    - {host: /srv/notes, path: /notes, readonly: true}
tools:
  mcp: [valet.shared, github]
  skills: ["*"]
browser:
  profiles: [alice]
secrets:
  - GITHUB_TOKEN
  - {vault: aws/alice, env: AWS_PROFILE}
permissions: trusted
directives: [owner, authority/full, persona.md, environment, memory]
memory: on
triggers:
  - {schedule: "0 8 * * *", prompt: "Morning brief"}
  - {webhook: deploy}
"""


def test_template_parses():
    d = parse_agent(TEMPLATE.format(name="scout"), "scout")
    assert d == AgentDefinition(name="scout")
    assert d.computer.level == "container" and d.permissions == "ask" and d.directives == ["persona.md"]
    assert "scout" in PERSONA_TEMPLATE.replace("{name}", "scout")


def test_full_shape():
    d = parse_agent(FULL, "scout")
    assert d.model == "claude-opus-5-5" and d.runner == "myhost"
    assert d.computer.mounts[0].readonly and d.computer.allowlist == ["github.com"]
    assert d.tools.skills == ["*"] and d.browser.profiles == ["alice"]
    assert d.secrets == ["GITHUB_TOKEN", VaultSecret(vault="aws/alice", env="AWS_PROFILE")]
    assert d.memory is True
    assert d.directives == ["owner", "authority/full", "persona.md", "environment", "memory"]
    assert d.triggers[0].schedule == "0 8 * * *" and d.triggers[1].webhook == "deploy"
    # It round-trips through the JSON the control plane sends in agent.start.
    assert AgentDefinition.model_validate(d.model_dump(mode="json")) == d


@pytest.mark.parametrize("text,needle", [
    ("name: [", "not valid YAML"),
    ("- a\n- b\n", "must be a mapping"),
    ("name: Scout\n", "name"),
    ("name: scout\ncolour: red\n", "colour"),
    ("name: scout\ncomputer:\n  level: vm\n", "computer.level"),
    ("name: scout\ntriggers:\n  - {prompt: hi}\n", "exactly one of schedule or webhook"),
    ("name: other\n", "must match its directory"),
    ("name: scout\ndirectives: [rules.md]\n", "'rules.md': one of owner, authority/<preset>, persona.md"),
    ("name: scout\ndirectives: [persona.md, owner, persona.md]\n", "persona.md is listed twice"),
    ("name: scout\ndirectives: [authority/full, authority/strict]\n", "one authority preset at most"),
    ("name: scout\ndirectives: [authority/../owner]\n", "a preset name is lowercase letters"),
    ("name: scout\ndirectives: [authority/]\n", "a preset name is lowercase letters"),
])
def test_errors(text, needle):
    with pytest.raises(DefinitionError) as e:
        parse_agent(text, "scout")
    assert needle in str(e.value)


def test_files(tmp_path):
    assert list_agents(tmp_path) == []
    d = tmp_path / "agents" / "scout"
    d.mkdir(parents=True)
    (d / "agent.yaml").write_text("name: scout\n")
    (tmp_path / "agents" / "empty").mkdir()
    assert list_agents(tmp_path) == ["scout"]
    assert read_files(tmp_path, "scout") == {}
    (d / "persona.md").write_text("Hi.\n")
    definition, files = load_agent(tmp_path, "scout")
    assert definition.name == "scout" and files == {"persona.md": "Hi.\n"}
    with pytest.raises(DefinitionError):
        load_agent(tmp_path, "../etc")


@pytest.mark.parametrize("computer", [
    {"mounts": [{"host": "valet-agent-mail-home", "path": "/mnt/x"}]},     # a named volume: another agent's home
    {"mounts": [{"host": "/srv:/x:rw", "path": "/mnt/x"}]},                # extra -v fields
    {"mounts": [{"host": "/srv", "path": "relative"}]},
    {"image": "--privileged"},
    {"image": "ubuntu latest"},
])
def test_boundary_fields_that_would_bend_docker_argv(computer):
    with pytest.raises(DefinitionError):
        parse_agent(yaml.safe_dump({"name": "scout", "computer": computer}))


def test_valid_images_and_mounts():
    d = parse_agent(yaml.safe_dump({"name": "scout", "computer": {
        "image": "localhost:5000/team/agent:1.2", "mounts": [{"host": "/srv/data", "path": "/data"}]}}))
    assert d.computer.image == "localhost:5000/team/agent:1.2"


# ---------- display name and emoji ----------

@pytest.mark.parametrize("field,value,stored", [
    ("display_name", "  Scout  ", "Scout"),
    ("display_name", "   ", None),                  # blank is no display name
    ("display_name", "x" * 40, "x" * 40),
    ("emoji", "🛠️", "🛠️"),                           # a variation selector
    ("emoji", "👩‍👩‍👧‍👦", "👩‍👩‍👧‍👦"),                   # zero-width joiners
    ("emoji", "🇧🇷", "🇧🇷"),
])
def test_labels(field, value, stored):
    assert getattr(AgentDefinition.model_validate({"name": "scout", field: value}), field) == stored


@pytest.mark.parametrize("field,value,msg", [
    ("display_name", "x" * 41, "one line, at most 40 characters"),
    ("display_name", "two\nlines", "one line, at most 40 characters"),
    ("display_name", "a\ttab", "one line, at most 40 characters"),
    ("display_name", "nul\x00", "one line, at most 40 characters"),
    ("display_name", "esc\x1b[31m", "one line, at most 40 characters"),
    ("display_name", "next\x85line", "one line, at most 40 characters"),
    ("display_name", "line\u2028sep", "one line, at most 40 characters"),
    ("emoji", "🧭 🛠️", "one emoji, no spaces"),
    ("emoji", "x" * 17, "one line, at most 16 characters"),
])
def test_label_errors(field, value, msg):
    with pytest.raises(ValidationError) as e:
        AgentDefinition.model_validate({"name": "scout", field: value})
    assert validation_errors(e.value) == [{"field": field, "msg": msg}]


def test_validation_errors_name_each_field_for_the_form():
    with pytest.raises(ValidationError) as e:
        AgentDefinition.model_validate({"name": "scout", "permissions": "maybe", "computer": {
            "mounts": [{"host": "/ok", "path": "/ok"}, {"host": "rel", "path": "/x"}]}})
    assert validation_errors(e.value) == [
        {"field": "computer.mounts.1.host", "msg": "an absolute path without ':' or ','"},
        {"field": "permissions", "msg": "Input should be 'locked', 'ask', 'trusted' or 'unrestricted'"}]


# ---------- to_yaml ----------

def test_to_yaml_leaves_defaults_out_but_says_what_kind_of_agent_it_is():
    assert to_yaml(AgentDefinition(name="scout")) == TEMPLATE.format(name="scout").replace(
        "directives: [persona.md]\n", "")
    d = AgentDefinition(name="scout", harness="deepseek", permissions="trusted", computer={"level": "host"},
                        directives=[])
    assert to_yaml(d) == ("name: scout\nharness: deepseek\ncomputer:\n  level: host\npermissions: trusted\n"
                          "directives: []\n")


def test_to_yaml_round_trips_the_full_shape_in_the_models_order():
    d = parse_agent(FULL, "scout")
    text = to_yaml(d)
    assert parse_agent(text, "scout") == d
    assert list(yaml.safe_load(text)) == ["name", "harness", "model", "provider", "runner", "computer", "tools",
                                          "browser", "secrets", "permissions", "directives", "memory", "triggers"]
    assert "#" not in text


def test_to_yaml_mounts():
    d = AgentDefinition.model_validate({"name": "scout", "computer": {"network": "none", "mounts": [
        {"host": "/srv/a", "path": "/a"}, {"host": "/srv/b", "path": "/b", "readonly": True}]}})
    assert to_yaml(d) == ("name: scout\nharness: claude\ncomputer:\n  level: container\n  network: none\n"
                          "  mounts:\n  - host: /srv/a\n    path: /a\n  - host: /srv/b\n    path: /b\n"
                          "    readonly: true\npermissions: ask\n")
    assert parse_agent(to_yaml(d)) == d


def test_to_yaml_writes_unicode_as_it_is():
    d = AgentDefinition(name="scout", display_name="Éclair 🧭", emoji="🛠️")
    text = to_yaml(d)
    assert text.startswith("name: scout\ndisplay_name: Éclair 🧭\nemoji: 🛠️\nharness: claude\n")
    assert parse_agent(text) == d


@pytest.mark.parametrize("value", [
    "yes", "No", "on", "null", "~", "1.0", "0x1F", "1e3", ".inf", "2026-11-01", "", "#1: the best", "- dash",
    "{x}", "[x]", "*star", "&anchor", "!tag", "@at", "%p", "`tick", "> fold", "| lit", "? key", "it's \"quoted\"",
    "multi\nline", "trailing\n", "\n\nleading", " pad ", "tab\tin", "cr\rlf", "nel\x85in", "nel\x85",
    "sep\u2028in", "nul\x00", "\ufeffbom", "émoji 🧭",
])
def test_to_yaml_round_trips_any_string(value):
    """Free-text fields go through YAML unchanged, whatever they look like (NEL needed double quotes)."""
    d = AgentDefinition.model_validate({"name": "scout", "model": value, "provider": value,
                                        "triggers": [{"webhook": "w", "prompt": value}]})
    assert parse_agent(to_yaml(d)) == d
