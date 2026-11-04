"""The settings page's API: the catalog, saving the form, new agents from a template or a copy, history and
restore, and a running agent saying it needs a restart."""
import subprocess
import time

import pytest

from valet.control.__main__ import add_runner
from valet.shared import catalog
from valet.shared.owner import say, say_all
from valet.shared.definition import AgentDefinition, Computer, parse_agent, to_yaml

HOST_YAML = "name: scout\nharness: claude\nmodel: m1\ncomputer:\n  level: host\n"
MINIMAL_YAML = "name: scout\nharness: claude\ncomputer:\n  level: container\npermissions: ask\n"
TEMPLATE_DIRECTIVES = "directives:\n- owner\n- authority/standard\n- persona.md\n- environment\n"   # at `ask`


def _wait(predicate, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not reached")


def git(cfg, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cfg.definitions), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def create(client, name: str = "scout", **body) -> dict:
    r = client.post("/api/agents", json={"name": name, **body})
    assert r.status_code == 201, r.text
    return r.json()


def save(client, definition: dict, persona: str | None = None, name: str = "scout"):
    return client.put(f"/api/agents/{name}/definition", json={"definition": definition, "persona": persona})


# ---------- catalog ----------
def test_catalog_lists_the_forms_choices(client):
    cat = client.get("/api/catalog").json()
    assert [h["id"] for h in cat["harnesses"]] == ["claude", "deepseek"]
    assert cat["harnesses"][0]["models"] == catalog.HARNESSES[0]["models"]
    assert (cat["permissions"], cat["levels"], cat["networks"], cat["templates"]) == (
        catalog.PERMISSIONS, catalog.LEVELS, catalog.NETWORKS, say_all(catalog.TEMPLATES))
    client.cookies.clear()
    assert client.get("/api/catalog").status_code == 401


def test_catalog_only_offers_what_the_definition_accepts():
    """Permission levels are the model's, in its order; computer levels all of the model's, and networks all but
    `none` (which means `gateway`); templates validate, and their personas open the way _renamed expects."""
    levels = AgentDefinition.model_fields["permissions"].annotation.__args__
    assert [p["id"] for p in catalog.PERMISSIONS] == list(levels)
    assert {lv["id"] for lv in catalog.LEVELS} == set(Computer.model_fields["level"].annotation.__args__)
    networks = set(Computer.model_fields["network"].annotation.__args__)
    assert {n["id"] for n in catalog.NETWORKS} == networks - {"none"}
    assert len({t["id"] for t in catalog.TEMPLATES}) == len(catalog.TEMPLATES)
    for t in catalog.TEMPLATES:
        AgentDefinition.model_validate({"name": "x", **t["definition"], "emoji": t["emoji"]})
        assert t["persona"].startswith("You are {who}")
    assert catalog.template("blank")["definition"] == {} and catalog.template("nope") is None


def ready(client) -> dict:
    return {h["id"]: h["ready"] for h in client.get("/api/catalog").json()["harnesses"]}


def test_harness_ready_follows_what_connected_runners_say(client, connect, cfg):
    assert ready(client) == {"claude": None, "deepseek": None}          # no runner: nobody can tell
    with connect(harnesses=["claude"]):
        assert ready(client) == {"claude": True, "deepseek": False}
        assert client.get("/api/runners").json()[0]["harnesses"] == ["claude"]
        with connect(token=add_runner(cfg, "r2"), harnesses=["deepseek", "mine"]):
            assert ready(client) == {"claude": True, "deepseek": True}  # any connected runner will do
            assert [r["harnesses"] for r in client.get("/api/runners").json()] == [["deepseek", "mine"], ["claude"]]
        assert ready(client) == {"claude": True, "deepseek": False}
    assert ready(client) == {"claude": None, "deepseek": None}
    assert client.get("/api/runners").json()[0]["harnesses"] == []
    with connect():         # a runner from before the hello listed harnesses
        assert ready(client) == {"claude": False, "deepseek": False}


def test_runner_frame_carries_its_harnesses(client, connect):
    with client.websocket_connect("/ws/client") as live:
        live.receive_json()
        with connect(harnesses=["claude", "deepseek"]):
            frame = live.receive_json()
            while frame["type"] != "runner":
                frame = live.receive_json()
            assert frame["runner"]["harnesses"] == ["claude", "deepseek"] and frame["runner"]["connected"]


# ---------- saving the form ----------
def test_save_writes_the_whole_definition_and_the_persona_in_one_commit(client, cfg):
    before = create(client, template="assistant", definition={"display_name": "Scout"})
    d = {**before["definition"], "model": "claude-sonnet-5-5", "permissions": "trusted"}
    r = save(client, d, "You are Scout. Be terse.\n")
    assert r.status_code == 200, r.text
    a = r.json()
    assert a["definition"] == d
    assert (a["display_name"], a["emoji"], a["model"], a["permissions"]) == ("Scout", "🧭", "claude-sonnet-5-5",
                                                                              "trusted")
    assert a["files"] == {"agent.yaml": to_yaml(AgentDefinition.model_validate(d)),
                          "persona.md": "You are Scout. Be terse.\n"}
    assert a["commit"] == git(cfg, "rev-parse", "HEAD") and git(cfg, "rev-list", "--count",
                                                                 f"{before['commit']}..HEAD") == "1"
    assert git(cfg, "log", "-1", "--format=%s") == "scout settings: model, permissions, persona"
    assert git(cfg, "show", "--name-only", "--format=", "HEAD").split() == ["agents/scout/agent.yaml",
                                                                           "agents/scout/persona.md"]

    # Whole object: what the form leaves out goes back to its default. No persona: persona.md stays.
    r = save(client, {"model": "m2"})
    assert r.json()["definition"] == AgentDefinition(name="scout", model="m2").model_dump(mode="json")
    assert r.json()["files"]["persona.md"] == "You are Scout. Be terse.\n"
    assert git(cfg, "log", "-1", "--format=%s") == ("scout settings: display_name, emoji, model, permissions, "
                                                    "directives")

    # Saving what's there commits nothing.
    head = r.json()["commit"]
    assert save(client, r.json()["definition"], "You are Scout. Be terse.\n").json()["commit"] == head


def test_save_reports_an_error_per_field_and_writes_nothing(client):
    before = create(client)
    bad = {"emoji": "two words", "computer": {"level": "host", "mounts": [{"host": "relative", "path": "/data"}]},
           "colour": "red"}
    r = save(client, bad, "Changed.\n")
    assert r.status_code == 422
    errors = {e["field"]: e["msg"] for e in r.json()["errors"]}
    assert errors == {"emoji": "one emoji, no spaces", "computer.mounts.0.host": "an absolute path without ':' or ','",
                      "colour": "Extra inputs are not permitted"}
    assert "computer.mounts.0.host: an absolute path without ':' or ','" in r.json()["detail"]
    after = client.get("/api/agents/scout").json()
    assert (after["files"], after["commit"]) == (before["files"], before["commit"])


def test_save_refuses_a_rename(client):
    create(client)
    r = save(client, {"name": "other"})
    assert r.status_code == 422 and r.json()["detail"] == "an agent can't be renamed; copy it instead"
    assert client.get("/api/agents/other").status_code == 404
    assert save(client, {"name": "scout", "model": "m"}).status_code == 200       # its own name is fine
    assert save(client, {}, name="ghost").status_code == 404


def test_save_replaces_a_hand_written_file_comments_and_all(client, cfg):
    create(client)
    client.put("/api/agents/scout/files/agent.yaml", json={"text": "# my notes\nname: scout   # me\nmodel: m1\n"})
    d = client.get("/api/agents/scout").json()["definition"]
    a = save(client, d).json()
    assert a["files"]["agent.yaml"] == "name: scout\nharness: claude\nmodel: m1\ncomputer:\n  level: container\n" \
                                      "permissions: ask\n"
    assert git(cfg, "log", "-1", "--format=%s") == "scout settings"       # same definition, new text


def test_save_repairs_an_invalid_file_without_calling_the_persona_changed(client, cfg):
    create(client, persona="Hi.\n")
    (cfg.definitions / "agents/scout/agent.yaml").write_text("name: scout\nharness: [\n")
    assert client.get("/api/agents/scout").json()["definition_error"]
    r = save(client, {"model": "m"}, "Hi.\n")
    assert r.status_code == 200 and r.json()["definition_error"] is None
    assert git(cfg, "log", "-1", "--format=%s") == "scout settings"


# ---------- new agents ----------
def test_new_agent_from_a_template(client):
    a = create(client, "forge", template="coder",
               definition={"display_name": "Forge", "harness": "deepseek", "model": "deepseek-v4-pro"})
    assert (a["display_name"], a["emoji"], a["harness"], a["model"], a["permissions"]) == (
        "Forge", "🛠️", "deepseek", "deepseek-v4-pro", "trusted")
    assert a["files"]["persona.md"] == say(catalog.template("coder")["persona"].replace("{who}", "Forge"))
    assert a["files"]["agent.yaml"] == to_yaml(parse_agent(a["files"]["agent.yaml"]))       # written by to_yaml
    # A template's agent is told about the owner, its standing authority (for its level), its persona and environment.
    assert a["definition"]["directives"] == ["owner", "authority/full", "persona.md", "environment"]

    # Without a display name the persona addresses it by name; `definition` alone means the blank template.
    b = create(client, "plain", definition={})
    assert b["files"] == {"agent.yaml": MINIMAL_YAML.replace("scout", "plain") + TEMPLATE_DIRECTIVES,
                          "persona.md": "You are plain, one of the owner's agents.\n"}
    assert b["emoji"] is None and b["display_name"] is None

    # A persona sent along wins over the template's; the template still gives the rest.
    c = create(client, "terse", template="researcher", persona="Be terse.\n")
    assert c["files"]["persona.md"] == "Be terse.\n" and c["emoji"] == "🔎"


def test_new_agent_refusals(client, cfg):
    r = client.post("/api/agents", json={"name": "x", "template": "nope"})
    assert r.status_code == 422 and r.json()["detail"] == "no template named nope"
    r = client.post("/api/agents", json={"name": "x", "template": "coder", "definition": {"computer": {"level": "vm"}}})
    assert r.status_code == 422 and [e["field"] for e in r.json()["errors"]] == ["computer.level"]
    r = client.post("/api/agents", json={"name": "x", "copy_from": "ghost"})
    assert r.status_code == 404 and r.json()["detail"] == "no agent named ghost"
    create(client, "broken")
    (cfg.definitions / "agents/broken/agent.yaml").write_text("name: broken\nnope: 1\n")
    r = client.post("/api/agents", json={"name": "x", "copy_from": "broken"})
    assert r.status_code == 422 and r.json()["detail"].startswith("broken: agent.yaml: nope")
    assert [a["name"] for a in client.get("/api/agents").json()] == ["broken"]


def test_new_agent_with_a_taken_name_is_a_conflict_whatever_else_it_says(client):
    create(client)
    for body in ({"template": "nope"}, {"definition": {"computer": {"level": "vm"}}}, {"copy_from": "ghost"},
                 {"agent_yaml": "name: other\n"}, {}):
        r = client.post("/api/agents", json={"name": "scout", **body})
        assert r.status_code == 409 and r.json()["detail"] == "agent scout already exists", body


def test_copy_takes_the_settings_and_persona_but_not_the_name(client):
    src = create(client, "forge", template="coder", definition={
        "display_name": "Forge", "model": "claude-opus-5-5",
        "computer": {"level": "container", "mounts": [{"host": "/srv/code", "path": "/code"}]}})
    persona = src["files"]["persona.md"]
    assert persona.startswith("You are Forge, one of the owner's agents")

    a = create(client, "forge-2", copy_from="forge", template="nope")      # the template isn't used: no 422
    assert a["definition"] == {**src["definition"], "name": "forge-2", "display_name": None}
    assert a["emoji"] == "🛠️"
    # The copy keeps the persona's text but doesn't introduce itself as the original.
    assert a["files"]["persona.md"] == persona.replace("You are Forge,", "You are forge-2,", 1)

    b = create(client, "anvil", copy_from="forge", definition={"display_name": "Anvil", "model": "claude-sonnet-5-5"})
    assert b["model"] == "claude-sonnet-5-5" and b["definition"]["computer"] == src["definition"]["computer"]
    assert b["files"]["persona.md"] == persona.replace("You are Forge,", "You are Anvil,", 1)

    # Top-level fields replace the original's: a computer given replaces its whole computer.
    c = create(client, "local", copy_from="forge", definition={"computer": {"level": "host"}})
    assert c["definition"]["computer"]["mounts"] == [] and c["level"] == "host"

    d = create(client, "mine", copy_from="forge", persona="Mine.\n")
    assert d["files"]["persona.md"] == "Mine.\n"


@pytest.mark.parametrize("display_name,persona,copied", [
    # an opening written before the display name was set still names the original
    ("Forge", "You are forge, an engineer.\n", "You are copy, an engineer.\n"),
    # only a whole name, matched literally
    ("Forge", "You are Forgetful.\n", "You are Forgetful.\n"),
    ("C.3PO", "You are C.3PO: polite.\n", "You are copy: polite.\n"),
    ("C.3PO", "You are CX3PO.\n", "You are CX3PO.\n"),
    # anything else is the owner's own text
    ("Forge", "Be terse. You are Forge.\n", "Be terse. You are Forge.\n"),
    (None, "You are forge.\n", "You are copy.\n"),
])
def test_copy_renames_only_the_personas_opening(client, display_name, persona, copied):
    create(client, "forge", definition={"display_name": display_name}, persona=persona)
    assert create(client, "copy", copy_from="forge")["files"]["persona.md"] == copied


# ---------- history and restore ----------
def test_history_is_the_agents_own_commits_newest_first(client):
    first = create(client, persona="One.\n")
    save(client, {"model": "m2"}, "Two.\n")
    create(client, "scout-2")               # a name sharing the prefix: not scout's history
    client.put("/api/agents/scout/files/persona.md", json={"text": "Three.\n"})
    h = client.get("/api/agents/scout/history").json()
    assert [e["message"] for e in h] == ["Edit scout/persona.md", "scout settings: model, persona",
                                         "Create agent scout"]
    assert h[-1]["commit"] == first["commit"] and len(h[0]["commit"]) == 40
    assert all(isinstance(e["at"], int) and abs(e["at"] - time.time()) < 300 for e in h)
    assert [e["message"] for e in client.get("/api/agents/scout-2/history").json()] == ["Create agent scout-2"]
    assert client.get("/api/agents/ghost/history").status_code == 404


def test_restore_writes_the_old_version_as_a_new_commit(client, connect, rpc):
    first = create(client, persona="One.\n")
    save(client, {"model": "m2", "permissions": "trusted"}, "Two.\n")
    h = client.get("/api/agents/scout/history").json()
    r = client.post("/api/agents/scout/restore", json={"commit": first["commit"][:7]})
    assert r.status_code == 200, r.text
    a = r.json()
    assert a["files"] == first["files"] and a["model"] is None and a["permissions"] == "ask"
    h2 = client.get("/api/agents/scout/history").json()
    assert h2[0]["message"] == f"Restore scout to {first['commit'][:7]}" and h2[0]["commit"] == a["commit"]
    assert h2[1:] == h          # history is kept, not rewritten


def test_restore_refusals(client, cfg):
    create(client)

    def restore(commit, name="scout"):
        return client.post(f"/api/agents/{name}/restore", json={"commit": commit})
    head = client.get("/api/agents/scout").json()["commit"]
    assert restore("0" * 40).status_code == 404                                      # no such commit
    initial = git(cfg, "rev-list", "--max-parents=0", "HEAD")                        # before scout existed
    r = restore(initial)
    assert r.status_code == 404 and r.json()["detail"] == f"scout has no definition at {initial[:7]}"
    for bad in ("HEAD", "main", "--output=/tmp/x", "abc12", "ABCDEF1", "a" * 41, f"{head}:agents"):
        r = restore(bad)
        assert r.status_code == 422 and r.json()["detail"] == "not a commit hash", bad
    assert restore(head, name="ghost").status_code == 404
    assert client.get("/api/agents/scout").json()["commit"] == head


def test_restore_of_a_version_that_no_longer_validates_is_refused(client, cfg):
    """The repo is git: a commit made outside Valet can hold anything. Restoring it is a 422, nothing written."""
    create(client)
    (cfg.definitions / "agents/scout/agent.yaml").write_text("name: scout\nnope: 1\n")
    git(cfg, "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", "commit", "-qam", "by hand")
    bad = git(cfg, "rev-parse", "HEAD")
    client.put("/api/agents/scout/files/agent.yaml", json={"text": MINIMAL_YAML})
    r = client.post("/api/agents/scout/restore", json={"commit": bad})
    assert r.status_code == 422 and "nope" in r.json()["detail"]
    assert client.get("/api/agents/scout").json()["files"]["agent.yaml"] == MINIMAL_YAML


# ---------- restart awareness ----------
def test_running_agent_says_when_a_save_needs_a_restart(client, connect, rpc):
    def agent():
        return client.get("/api/agents/scout").json()

    with client.websocket_connect("/ws/client") as live, connect() as runner:
        live.receive_json()
        create(client, agent_yaml=HOST_YAML)
        assert agent()["restart_needed"] is False                       # stopped
        rpc(runner, client.post, "/api/agents/scout/start", expect="agent.start")
        runner.status("scout", "running")
        _wait(lambda: agent()["state"] == "running")
        assert agent()["restart_needed"] is False
        d = agent()["definition"]

        save(client, {**d, "display_name": "Scout", "emoji": "🧭"})    # how the apps show it: no restart
        assert agent()["restart_needed"] is False
        a = save(client, {**d, "model": "m2"}).json()
        assert a["restart_needed"] is True
        frame = live.receive_json()
        while not (frame["type"] == "agent" and frame["agent"]["restart_needed"]):
            frame = live.receive_json()

        _, cmd = rpc(runner, client.post, "/api/agents/scout/start", expect="agent.start")     # restart
        assert cmd["definition"]["model"] == "m2"
        assert agent()["restart_needed"] is False
        client.put("/api/agents/scout/files/persona.md", json={"text": "New persona.\n"})      # files count too
        assert agent()["restart_needed"] is True

        rpc(runner, client.post, "/api/agents/scout/stop", expect="agent.stop")
        runner.status("scout", "stopped")
        _wait(lambda: agent()["state"] == "stopped")
        assert agent()["restart_needed"] is False                       # nothing running to restart
