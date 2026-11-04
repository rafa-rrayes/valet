"""The owner's name (VALET_OWNER_NAME, shared/owner.py): "the owner" unless set, in every text Valet writes for
agents and for people, rendered where the text is produced; no placeholder is ever left in one."""
import dataclasses
import itertools
import json
import re

import pytest
from fastapi.testclient import TestClient

from valet.control import directives, memory
from valet.control.__main__ import set_password
from valet.control.app import create_app
from valet.control.gateway import INSTRUCTIONS
from valet.control.prompts import source_label
from valet.shared import catalog, owner
from valet.shared.config import ControlConfig
from valet.shared.definition import AgentDefinition
from valet.shared.owner import say, say_all

PASSWORD = "correct horse battery"
PLACEHOLDER = re.compile(r"\{[Oo]wner\}")
YAML = ("name: scout\nharness: claude\ncomputer:\n  level: host\npermissions: ask\n"
        "tools:\n  mcp: [valet.agents, valet.browser, valet.owner, valet.shared]\nmemory: true\n"
        "directives: [owner, authority/full, persona.md, environment, memory]\n")


def app_client(cfg: ControlConfig, name: str | None):
    cfg = dataclasses.replace(cfg, owner_name=name) if name is not None else cfg
    set_password(cfg, PASSWORD)
    client = TestClient(create_app(cfg))
    client.__enter__()
    assert client.post("/api/login", json={"password": PASSWORD}).status_code == 200
    return client


@pytest.fixture(params=[None, "Rafa"], ids=["default", "Rafa"])
def named(request, cfg):
    client = app_client(cfg, request.param)
    yield client, request.param or "the owner"
    client.__exit__(None, None, None)


def tools_text(client) -> str:
    """Everything Valet's own servers say about themselves: their descriptions and every tool's, with its schema."""
    gateway = client.app.state.control.gateway
    out = [s["description"] or "" for s in gateway.servers()["servers"] if s["builtin"]]
    for up in gateway.builtins.values():
        out += [t.model_dump_json(by_alias=True) for t in client.portal.call(up.list_tools)]
    return "\n".join(out)


# ---------- the setting ----------
def test_the_setting_is_read_from_the_environment():
    assert ControlConfig.from_env({}).owner_name == "the owner"
    assert ControlConfig.from_env({"VALET_OWNER_NAME": ""}).owner_name == "the owner"
    assert ControlConfig.from_env({"VALET_OWNER_NAME": "  "}).owner_name == "the owner"
    assert ControlConfig.from_env({"VALET_OWNER_NAME": " Rafa "}).owner_name == "Rafa"


def test_say_fills_both_placeholders():
    text = "{Owner} has authorized you; ask {owner} first, and read {owner}'s notes. {Owner}'s call. {other}"
    assert say(text, "the owner") == ("The owner has authorized you; ask the owner first, and read the owner's "
                                      "notes. The owner's call. {other}")
    assert say(text, "Rafa") == "Rafa has authorized you; ask Rafa first, and read Rafa's notes. Rafa's call. {other}"
    owner.configure("Rafa")
    assert say("{Owner} and {owner}") == "Rafa and Rafa" and owner.name() == "Rafa"
    owner.configure("")
    assert say("{Owner} and {owner}") == "The owner and the owner"
    assert say_all({"a": ["{owner}", 3, {"b": "{Owner}"}]}, "Ann") == {"a": ["Ann", 3, {"b": "Ann"}]}


# ---------- the texts ----------
def test_presets_and_the_environment_say_the_name(named):
    client, who = named
    assert client.post("/api/agents", json={"name": "scout", "agent_yaml": YAML, "persona": "You are Scout.\n"}
                       ).status_code == 201
    # Stored with placeholders (the repo is the owner's to edit), rendered at every start and in the preview.
    assert "{owner}" in client.get("/api/authority/full").json()["text"]
    p = client.get("/api/agents/scout/directives").json()
    layers = {x["name"]: x["text"] for x in p["layers"]}
    assert f"You work for {who}, on {who}'s machines and accounts." in layers["authority/full"]
    assert f"{who[0].upper() + who[1:]} has authorized you" in layers["authority/full"]
    assert f"one of {who}'s agents in Valet" in layers["environment"]
    assert f"With valet.owner you reach {who}" in layers["environment"]
    assert f"Waits for {who}'s approval" in layers["environment"]
    assert f"what you learn about {who} and the work" in layers["memory"]
    assert not PLACEHOLDER.search(p["text"])
    if who == "Rafa":
        assert "the owner" not in layers["authority/full"].lower().replace("the owner's", "")


def test_tool_descriptions_say_the_name(named):
    client, who = named
    text = tools_text(client)
    assert not PLACEHOLDER.search(text)
    assert f"Message {who}" in text and f"{who}'s inbox" in text and f"{who[0].upper() + who[1:]}'s browser" in text
    assert f"Your shared space with {who}" in text
    if who == "Rafa":
        assert "the owner" not in text.lower().replace("owner (", "").replace("owner,", "")   # `by: owner` stays


def test_refusals_and_labels_say_the_name(named):
    client, who = named
    assert source_label("answer") == f"{who[0].upper() + who[1:]}'s answer"
    template = client.get("/api/catalog").json()["templates"][0]
    assert f"one of {who}'s agents" in template["persona"]
    r = client.post("/api/agents", json={"name": "forge", "template": "coder"})
    assert r.status_code == 201 and f"one of {who}'s agents" in r.json()["files"]["persona.md"]
    r = client.post("/api/agents", json={"name": "plain"})            # the default persona
    assert f"one of {who}'s agents" in r.json()["files"]["persona.md"]


# ---------- nothing is left unrendered ----------
def test_no_placeholder_leaks():
    for name in directives.shipped_presets():
        assert not PLACEHOLDER.search(say(directives.shipped_presets()[name], "Rafa")), name
    assert not PLACEHOLDER.search(json.dumps(say_all(catalog.TEMPLATES)))
    assert not PLACEHOLDER.search(say(INSTRUCTIONS)) and not PLACEHOLDER.search(say(memory.PREAMBLE))
    for permissions, harness, level, extra in itertools.product(
            ("locked", "ask", "trusted", "unrestricted"), ("claude", "deepseek"), ("host", "container"),
            ({}, {"memory": True, "tools": {"mcp": ["valet.browser", "valet.owner", "valet.shared", "valet.agents"],
                                            "rules": {"valet.memory": {"delete": "deny"}}},
                  "secrets": ["A_KEY"], "browser": {"profiles": ["work"]}})):
        d = AgentDefinition.model_validate({"name": "scout", "harness": harness, "permissions": permissions,
                                            "computer": {"level": level}, **extra})
        for who in ("the owner", "Rafa"):
            assert not PLACEHOLDER.search(say(directives.environment_text(d), who))
        assert not PLACEHOLDER.search(directives.environment_text(d))      # rendered already, by the configured name


def test_every_text_the_code_holds_is_a_known_placeholder():
    """`{owner}` and `{Owner}` are the only spellings: a typo like `{owner }` or `{OWNER}` would reach an agent as is."""
    import pathlib
    root = pathlib.Path(directives.__file__).parent
    bad = [f"{p.relative_to(root)}: {m}" for p in itertools.chain(root.rglob("*.py"), root.rglob("*.md"))
           for m in re.findall(r"\{\{?\s*[Oo][Ww][Nn][Ee][Rr]\s*\}\}?", p.read_text())
           if m.strip("{}") not in ("owner", "Owner")]
    assert not bad
