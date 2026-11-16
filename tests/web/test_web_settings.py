"""The web UI's settings page, new-agent form and agent list, run by their own JS under node with a stand-in DOM and
a fake API (tests/web/settings.js). Skipped without node."""
import copy
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from valet.shared import catalog
from valet.shared.definition import AgentDefinition

HERE = Path(__file__).parent
STATIC = HERE.parent.parent / "valet" / "control" / "static"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node isn't installed")

CATALOG = {"harnesses": [{**h, "ready": h["id"] == "claude"} for h in catalog.HARNESSES],
           "permissions": catalog.PERMISSIONS, "levels": catalog.LEVELS, "networks": catalog.NETWORKS,
           "templates": catalog.TEMPLATES}

# Every field set, most of them ones the form doesn't show: a save must send them back untouched.
DEFINITION = AgentDefinition.model_validate({
    "name": "scout", "display_name": "Scout", "emoji": "🔎", "harness": "claude", "model": "claude-opus-5-5",
    "provider": "anthropic", "runner": "mac",
    "computer": {"level": "container", "image": None, "network": "open", "allowlist": ["example.com"],
                 "mounts": [{"host": "/Users/alex/notes", "path": "/work/notes", "readonly": True}], "user": "agent"},
    "tools": {"mcp": ["github"], "skills": ["*"]}, "browser": {"profiles": ["work"]},
    "secrets": ["GH_TOKEN", {"vault": "aws/alice", "env": "AWS_KEY"}], "permissions": "ask",
    "directives": ["persona.md", "memory"], "memory": True,
    "triggers": [{"schedule": "0 9 * * *", "prompt": "morning news"}],
}).model_dump(mode="json")

PERSONA = "You are Scout.\n"


def agent(**over) -> dict:
    a = {"name": "scout", "display_name": "Scout", "emoji": "🔎", "harness": "claude", "model": "claude-opus-5-5",
         "level": "container", "permissions": "ask", "definition_error": None, "runner": "mac", "connected": True,
         "desired": "running", "state": "running", "status": "idle", "error": None, "capabilities": None,
         "restart_needed": False}
    return {**a, **over}


def detail(definition=None, persona=PERSONA, **over) -> dict:
    d = definition or DEFINITION
    return {**agent(**over), "display_name": d["display_name"], "emoji": d["emoji"], "harness": d["harness"],
            "model": d["model"], "files": {"agent.yaml": "name: scout\n", "persona.md": persona}, "definition": d,
            "commit": "0123456789abcdef"}


def run(*jobs: dict) -> list[dict]:
    out = subprocess.run(["node", str(HERE / "settings.js"), str(STATIC)], input=json.dumps(jobs),
                         capture_output=True, text=True, timeout=60, check=False)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def settings(actions=(), d=None, put=None, routes=None, agents=None) -> dict:
    d = d or detail()
    r = {"GET /api/agents/scout": {"status": 200, "body": d}, "GET /api/catalog": {"status": 200, "body": CATALOG},
         "GET /api/agents/scout/history": {"status": 200, "body": []}}
    if put:
        r["PUT /api/agents/scout/definition"] = put
    r.update(routes or {})
    (result,) = run({"view": "settings", "name": "scout", "routes": r, "actions": list(actions),
                     "agents": agents if agents is not None else [agent()]})
    return result


def puts(result) -> list[dict]:
    return [q["body"] for q in result["requests"] if q["method"] == "PUT"]


# ---------- the form ----------

def test_form_is_built_from_the_definition():
    r = settings()
    c = r["controls"]
    assert c["display_name"]["value"] == "Scout" and c["emoji"]["value"] == "🔎"
    assert c["persona"]["value"] == PERSONA
    assert c["model"]["value"] == "claude-opus-5-5" and "model-other" not in c
    assert c["computer.mounts.0.host"]["value"] == "/Users/alex/notes"
    assert c["computer.mounts.0.path"]["value"] == "/work/notes"
    assert c["computer.mounts.0.readonly"]["checked"] is True
    assert c["computer.image"]["value"] == ""
    assert "computer.allowlist.0" not in c                 # open: the site list is kept, not shown
    assert set(r["chosen"]) == {"harness:claude", "level:container", "network:open", "permissions:ask",
                                "trigger-0-session:main"}
    assert "No key on the runner" in r["text"] and "Needs DEEPSEEK_API_KEY" in r["text"]   # deepseek isn't ready
    assert puts(r) == [] and r["errors"] == {}             # nothing to save, nothing wrong


def test_unknown_model_shows_as_other():
    d = copy.deepcopy(DEFINITION)
    d["model"] = "claude-experimental-9"
    c = settings(d=detail(d))["controls"]
    assert c["model"]["value"] == "__other__" and c["model-other"]["value"] == "claude-experimental-9"


# ---------- its computer: internet and no computer ----------

def test_internet_choices_in_plain_words():
    r = settings()
    for n in catalog.NETWORKS:
        assert n["name"] in r["text"] and n["summary"] in r["text"]
    edited = copy.deepcopy(DEFINITION)
    edited["computer"]["network"] = "gateway"
    r = settings([{"click": "network:gateway"}, {"button": "Save"}], put={"status": 200, "body": detail(edited)})
    assert puts(r) == [{"definition": edited}]
    assert "network:gateway" in r["chosen"] and "network:open" not in r["chosen"]
    assert "computer.allowlist.0" not in r["controls"]


def test_network_none_in_the_file_shows_as_its_model_only_and_isnt_a_change():
    d = copy.deepcopy(DEFINITION)
    d["computer"]["network"] = "none"
    r = settings(d=detail(d))
    assert "network:gateway" in r["chosen"] and "Unsaved changes" not in r["text"]


def test_the_site_list():
    """Picking "Only these sites" shows the list the file has; a pasted URL becomes its domain."""
    edited = copy.deepcopy(DEFINITION)
    edited["computer"].update(network="allowlist", allowlist=["api.github.com"])
    acts = [{"click": "network:allowlist"}, {"button": "Add a site"},
            {"input": "computer.allowlist.1", "value": " https://API.GitHub.com:443/repos?x=1 "},
            {"blur": "computer.allowlist.1"}]
    r = settings(acts)
    assert r["controls"]["computer.allowlist.0"]["value"] == "example.com"
    assert r["controls"]["computer.allowlist.1"]["value"] == "api.github.com"
    assert "A site includes its subdomains" in r["text"]
    r = settings(acts + [{"button": "Remove", "row": "computer.allowlist.0"}, {"button": "Save"}],
                 put={"status": 200, "body": detail(edited)})
    assert puts(r) == [{"definition": edited}]
    assert r["controls"]["computer.allowlist.0"]["value"] == "api.github.com" and "computer.allowlist.1" not in r["controls"]


def test_an_empty_site_list_says_what_it_reaches():
    d = copy.deepcopy(DEFINITION)
    d["computer"].update(network="allowlist", allowlist=[])
    r = settings(d=detail(d))
    assert "None yet: it reaches only its model and Valet's tools." in r["text"]


def test_site_errors_go_next_to_the_site():
    errors = [{"field": "computer.allowlist.1", "msg": "a domain like github.com (its subdomains come with it)"},
              {"field": "computer.allowlist", "msg": "example.com is listed twice"}]
    r = settings([{"click": "network:allowlist"}, {"button": "Add a site"},
                  {"input": "computer.allowlist.1", "value": "localhost"}, {"button": "Save"}],
                 put={"status": 422, "body": {"detail": "invalid", "errors": errors}})
    assert r["errors"] == {"computer.allowlist.1": "a domain like github.com (its subdomains come with it)",
                           "computer.allowlist": "example.com is listed twice",
                           "bar": "Some settings need fixing: see the notes in red."}
    r = settings([{"click": "network:allowlist"}, {"button": "Add a site"},
                  {"input": "computer.allowlist.1", "value": "localhost"}, {"button": "Save"},
                  {"input": "computer.allowlist.1", "value": "localhost.dev"}],
                 put={"status": 422, "body": {"detail": "invalid", "errors": errors}})
    assert "computer.allowlist.1" not in r["errors"]


@pytest.mark.parametrize("text, site", [
    ("github.com", "github.com"), ("  GitHub.COM ", "github.com"), ("https://api.github.com/x/y?z#w", "api.github.com"),
    ("http://user@example.org:8080", "example.org"), ("*.example.org", "example.org"), ("example.org.", "example.org"),
    ("example.org/path", "example.org"), ("", ""),
])
def test_site_of(text, site):
    (r,) = run({"call": "siteOf", "args": [text]})
    assert r["result"] == site


def test_no_computer():
    """It drops the shared folders and the internet choice; picking a computer again puts the folders back."""
    edited = copy.deepcopy(DEFINITION)
    edited["computer"].update(level="none", mounts=[])
    r = settings([{"click": "level:none"}, {"button": "Save"}], put={"status": 500, "body": {"detail": "x"}})
    (body,) = puts(r)
    assert body == {"definition": edited}
    assert not any(k.startswith(("computer.mounts", "computer.allowlist")) for k in r["controls"])
    assert not any(k.startswith("network:") for k in r["chosen"])
    assert "It has no files and runs no commands." in r["text"] and "computer.image" in r["controls"]
    none = next(l for l in catalog.LEVELS if l["id"] == "none")
    assert none["name"] in r["text"] and none["summary"] in r["text"]
    back = settings([{"click": "level:none"}, {"click": "level:container"}])
    assert back["controls"]["computer.mounts.0.host"]["value"] == "/Users/alex/notes"
    assert "Unsaved changes" not in back["text"]


def test_host_level_hides_container_settings_and_warns():
    r = settings([{"click": "level:host"}])
    assert not any(k.startswith("computer.mounts") or k == "computer.image" for k in r["controls"])
    assert not any(k.startswith("network:") for k in r["chosen"]) and "Full internet" not in r["text"]
    assert "Careful: on this machine it isn't in a container" in r["text"]
    host_note = next(p["host"] for p in catalog.PERMISSIONS if p["id"] == "trusted")
    assert host_note in r["text"]
    assert puts(r) == []


def test_save_sends_the_whole_definition_with_the_edits():
    """Only what the owner changed differs; tools, secrets, triggers and the rest go back as they came."""
    edited = copy.deepcopy(DEFINITION)
    edited.update(display_name="Scout Two", permissions="trusted", harness="deepseek", model="deepseek-v4-flash")
    edited["computer"]["mounts"][0]["readonly"] = False
    r = settings([{"input": "display_name", "value": "  Scout Two "}, {"click": "permissions:trusted"},
                  {"click": "harness:deepseek"}, {"change": "model", "value": "deepseek-v4-flash"},
                  {"check": "computer.mounts.0.readonly", "checked": False}, {"button": "Save"}],
                 put={"status": 200, "body": detail(edited)})
    (body,) = puts(r)
    assert body == {"definition": edited}                  # no persona: it didn't change
    assert r["errors"] == {} and r["controls"]["display_name"]["value"] == "Scout Two"
    assert r["agent"]["display_name"] == "Scout Two"       # S.agents has the saved agent
    assert "Saved." in r["text"] and "Unsaved changes" not in r["text"]


def test_switching_harness_resets_the_model_to_default():
    r = settings([{"click": "harness:deepseek"}, {"button": "Save"}], put={"status": 500, "body": {"detail": "x"}})
    (body,) = puts(r)
    assert body["definition"]["harness"] == "deepseek" and body["definition"]["model"] is None
    assert r["controls"]["model"]["value"] == ""


def test_persona_and_new_folder_are_sent():
    edited = copy.deepcopy(DEFINITION)
    edited["computer"]["mounts"].append({"host": "/srv/data", "path": "/work/data", "readonly": False})
    r = settings([{"input": "persona", "value": "You are Scout, a scout.\n"}, {"button": "Add a folder"},
                  {"input": "computer.mounts.1.host", "value": "/srv/data/"}, {"blur": "computer.mounts.1.host"},
                  {"input": "computer.mounts.1.host", "value": "/srv/data"}, {"button": "Save"}],
                 put={"status": 500, "body": {"detail": "boom"}})
    (body,) = puts(r)
    assert body == {"definition": edited, "persona": "You are Scout, a scout.\n"}   # the path inside was suggested
    assert r["errors"] == {"bar": "boom"} and "Unsaved changes" in r["text"]          # a failed save keeps the edits


def test_errors_go_next_to_their_fields():
    errors = [{"field": "computer.mounts.0.host", "msg": "an absolute path without ':' or ','"},
              {"field": "display_name", "msg": "one line, at most 40 characters"},
              {"field": "triggers.0", "msg": "a trigger has exactly one of schedule or webhook"}]
    r = settings([{"input": "computer.mounts.0.host", "value": "notes"}, {"button": "Save"}],
                 put={"status": 422, "body": {"detail": "invalid", "errors": errors}})
    assert r["errors"] == {"computer.mounts.0.host": "an absolute path without ':' or ','",
                           "display_name": "one line, at most 40 characters",
                           "triggers.0": "a trigger has exactly one of schedule or webhook",
                           "bar": "Some settings need fixing: see the notes in red."}
    assert "Unsaved changes" in r["text"]


def test_editing_a_field_clears_its_error():
    errors = [{"field": "computer.image", "msg": "a docker image reference like registry/name:tag"}]
    r = settings([{"input": "computer.image", "value": "Bad Image"}, {"button": "Save"},
                  {"input": "computer.image", "value": "ghcr.io/alice/agent:1"}],
                 put={"status": 422, "body": {"detail": "invalid", "errors": errors}})
    assert "computer.image" not in r["errors"]


@pytest.mark.parametrize("field, keys, expected", [
    ("computer.mounts.0.host", ["computer.mounts.0.host", "computer.mounts.0", "computer.mounts"], "computer.mounts.0.host"),
    ("computer.mounts.0.readonly", ["computer.mounts.0", "computer.mounts"], "computer.mounts.0"),
    ("computer.mounts", ["computer.mounts.0", "computer.mounts"], "computer.mounts"),
    ("computer.imagex", ["computer.image"], None),
    ("secrets.1.vault", ["display_name", "computer.image"], None),
])
def test_field_for(field, keys, expected):
    (r,) = run({"call": "fieldFor", "args": [field, keys]})
    assert r["result"] == expected


def test_template_replaces_the_persona_after_a_confirmation():
    coder = next(t for t in catalog.TEMPLATES if t["id"] == "coder")
    asked = settings([{"click": "persona:coder"}])
    assert asked["controls"]["persona"]["value"] == PERSONA            # not yet: there's text to lose
    assert "Replace your text with the Coder template?" in asked["text"]
    done = settings([{"click": "persona:coder"}, {"button": "Replace", "within": "confirm"}])
    assert done["controls"]["persona"]["value"] == coder["persona"].replace("{who}", "Scout")
    kept = settings([{"click": "persona:coder"}, {"button": "Cancel", "within": "confirm"}])
    assert kept["controls"]["persona"]["value"] == PERSONA and "Unsaved changes" not in kept["text"]


def test_agent_frames_dont_clobber_edits():
    r = settings([{"input": "display_name", "value": "Edited"},
                  {"frame": {"type": "agent", "agent": agent(status="working", restart_needed=True)}}])
    assert r["controls"]["display_name"]["value"] == "Edited"
    assert "working" in r["text"] and "Unsaved changes" in r["text"]


def test_save_and_restart_asks_first_while_it_works():
    routes = {"POST /api/agents/scout/start": {"status": 200, "body": agent(status="idle")}}
    edited = copy.deepcopy(DEFINITION)
    edited["permissions"] = "locked"
    acts = [{"click": "permissions:locked"}, {"frame": {"type": "agent", "agent": agent(status="working")}},
            {"button": "Save & restart"}]
    asked = settings(acts, put={"status": 200, "body": detail(edited)}, routes=routes)
    assert puts(asked) == [] and "restarting stops it" in asked["text"]
    done = settings(acts + [{"button": "Restart anyway", "within": "confirm"}],
                    put={"status": 200, "body": detail(edited)}, routes=routes)
    assert [(q["method"], q["path"]) for q in done["requests"] if q["method"] != "GET"] == [
        ("PUT", "/api/agents/scout/definition"), ("POST", "/api/agents/scout/start")]
    assert "Saved and restarted." in done["text"]


def test_restore_from_history_reloads_the_form():
    old = copy.deepcopy(DEFINITION)
    old["display_name"] = "Old Scout"
    history = [{"commit": "c" * 40, "message": "scout settings: display_name", "at": 1_800_000_000},
               {"commit": "b" * 40, "message": "Create scout", "at": 1_700_000_000}]
    r = settings([{"open": "advanced"}, {"button": "Restore"}, {"button": "Restore", "within": "confirm"}],
                 routes={"GET /api/agents/scout/history": {"status": 200, "body": history},
                         "POST /api/agents/scout/restore": {"status": 200, "body": detail(old)}})
    assert {"method": "POST", "path": "/api/agents/scout/restore", "body": {"commit": "b" * 40}} in r["requests"]
    assert r["controls"]["display_name"]["value"] == "Old Scout" and "Unsaved changes" not in r["text"]
    assert "scout settings: display_name" in r["text"] and "ccccccc" in r["text"]


def test_invalid_definition_shows_the_files():
    bad = {**detail(), "definition": None, "definition_error": "agent.yaml: unknown key colour"}
    r = settings(d=bad, agents=[agent(definition_error="agent.yaml: unknown key colour")])
    assert "agent.yaml has an error" in r["text"] and "unknown key colour" in r["text"]
    assert "display_name" not in r["controls"] and r["controls"]["file:agent.yaml"]["value"] == "name: scout\n"


# ---------- new agent ----------

@pytest.mark.parametrize("display, expected", [
    ("Scout", "scout"), ("  My   Agent! ", "my-agent"), ("Café Bot", "cafe-bot"), ("🔎 Research", "research"),
    ("Dr. Who 2", "dr-who-2"), ("--x--", "x"), ("snake_case", "snake-case"), ("!!!", ""), ("A" * 40, "a" * 31),
])
def test_id_is_derived_from_the_display_name(display, expected):
    (r,) = run({"call": "slugify", "args": [display]})
    assert r["result"] == expected


def new_agent(actions, routes=None, agents=None) -> dict:
    (result,) = run({"view": "new", "routes": {"GET /api/catalog": {"status": 200, "body": CATALOG}, **(routes or {})},
                     "agents": agents if agents is not None else [agent()], "catalog": CATALOG,
                     "actions": actions})
    return result


def created(name: str) -> dict:
    return {"POST /api/agents": {"status": 201, "body": detail(name=name)},
            f"POST /api/agents/{name}/start": {"status": 200, "body": agent(name=name)}}


def test_create_from_a_template():
    r = new_agent([{"input": "display_name", "value": "Ana Lise"}, {"click": "template:researcher"},
                   {"click": "harness:deepseek"}, {"change": "model", "value": "deepseek-v4-pro"},
                   {"button": "Create & start"}], created("ana-lise"))
    assert "ana-lise" in r["text"]                          # the derived id, shown under the name
    posts = [(q["path"], q["body"]) for q in r["requests"] if q["method"] == "POST"]
    assert posts == [("/api/agents", {"name": "ana-lise", "template": "researcher",
                                      "definition": {"display_name": "Ana Lise", "harness": "deepseek",
                                                     "model": "deepseek-v4-pro"}}),
                     ("/api/agents/ana-lise/start", None)]
    assert r["hash"] == "#/a/ana-lise" and r["flash"] is None


def test_create_defaults_to_the_first_template_and_a_ready_harness():
    r = new_agent([{"input": "display_name", "value": "Helper"}, {"button": "Create & start"}], created("helper"))
    body = next(q["body"] for q in r["requests"] if q["path"] == "/api/agents")
    assert body == {"name": "helper", "template": catalog.TEMPLATES[0]["id"],
                    "definition": {"display_name": "Helper", "harness": "claude", "model": None}}


def test_create_a_copy_with_an_edited_id():
    r = new_agent([{"input": "display_name", "value": "Scout"}, {"click": "copy"},
                   {"input": "name", "value": "scout-2"}, {"button": "Create & start"}], created("scout-2"))
    body = next(q["body"] for q in r["requests"] if q["path"] == "/api/agents")
    assert body == {"name": "scout-2", "copy_from": "scout",
                    "definition": {"display_name": "Scout", "harness": "claude", "model": "claude-opus-5-5"}}


def test_create_says_when_no_runner_is_connected():
    routes = {**created("helper"), "POST /api/agents/helper/start": {
        "status": 503, "body": {"detail": "no runner is connected; the agent starts when one connects"}}}
    r = new_agent([{"input": "display_name", "value": "Helper"}, {"button": "Create & start"}], routes)
    assert r["hash"] == "#/a/helper"
    assert r["flash"]["agent"] == "helper" and "starts when a runner connects" in r["flash"]["text"]


def test_create_refuses_a_taken_id_without_asking_the_server():
    r = new_agent([{"input": "display_name", "value": "Scout"}, {"button": "Create & start"}])
    assert r["errors"] == {"name": "There's already an agent with the ID scout."}
    assert not [q for q in r["requests"] if q["method"] == "POST"]


def test_create_shows_server_errors_by_field():
    routes = {"POST /api/agents": {"status": 422, "body": {"detail": "invalid", "errors": [
        {"field": "display_name", "msg": "one line, at most 40 characters"}, {"field": "secrets", "msg": "odd"}]}}}
    r = new_agent([{"input": "display_name", "value": "Helper"}, {"button": "Create & start"}], routes)
    assert r["errors"] == {"display_name": "one line, at most 40 characters", "form": "odd"}
    assert r["hash"] == ""


# ---------- the agent list ----------

def test_agent_cards_show_emoji_display_name_and_restart_hint():
    (r,) = run({"view": "agents", "agents": [agent(restart_needed=True), agent(name="plain", display_name=None,
                                                                               emoji=None)]})
    assert '<span class="emoji">🔎</span><span>Scout</span><span class="id">scout</span>' in r["html"]
    assert '<strong class="grow title"><span>plain</span></strong>' in r["html"]
    assert r["text"].count("Restart to apply saved changes") == 1


# ---------- leaving with unsaved edits ----------

SETTINGS = "#/a/scout/settings"


def routed(actions, put=None) -> dict:
    """The app's router on the settings page: {"go": hash} in actions follows a link (a hashchange)."""
    r = {"GET /api/agents/scout": {"status": 200, "body": detail()}, "GET /api/catalog": {"status": 200, "body": CATALOG},
         "GET /api/agents/scout/history": {"status": 200, "body": []},
         "GET /api/agents/scout/sessions": {"status": 200, "body": []}}
    if put:
        r["PUT /api/agents/scout/definition"] = put
    (result,) = run({"view": "route", "hash": SETTINGS, "name": "scout", "routes": r, "actions": actions,
                     "agents": [agent()]})
    return result


def test_leaving_with_unsaved_edits_asks_first():
    r = routed([{"input": "display_name", "value": "Edited"}, {"go": "#/"}])
    assert r["viewClass"] == "settings" and r["hash"] == SETTINGS      # still here, address put back
    assert r["replaced"] == ["/" + SETTINGS]
    assert "You have unsaved changes" in r["text"]
    assert re.findall(r'<div class="actions">(.*?)</div>', r["html"])[0].count("<button") == 3
    assert all(b in r["text"] for b in ("Save", "Discard & leave", "Stay"))
    assert r["controls"]["display_name"]["value"] == "Edited"


def test_stay_keeps_the_edits():
    r = routed([{"input": "display_name", "value": "Edited"}, {"go": "#/inbox"}, {"button": "Stay"}])
    assert r["viewClass"] == "settings" and r["hash"] == SETTINGS
    assert "You have unsaved changes" not in r["text"] and "Unsaved changes" in r["text"]
    assert r["controls"]["display_name"]["value"] == "Edited" and puts(r) == []


def test_discard_and_leave_goes_where_they_wanted():
    r = routed([{"input": "display_name", "value": "Edited"}, {"go": "#/a/scout"}, {"button": "Discard & leave"}])
    assert r["hash"] == "#/a/scout" and r["viewClass"] == "" and "Sessions" in r["text"]
    assert puts(r) == []


def test_save_then_leave():
    edited = copy.deepcopy(DEFINITION)
    edited["display_name"] = "Edited"
    r = routed([{"input": "display_name", "value": "Edited"}, {"go": "#/"}, {"button": "Save"}],
               put={"status": 200, "body": detail(edited)})
    assert puts(r) == [{"definition": edited}]
    assert r["hash"] == "#/" and "Runners" in r["text"]


def test_a_failed_save_stays_with_its_errors():
    errors = [{"field": "display_name", "msg": "one line, at most 40 characters"}]
    r = routed([{"input": "display_name", "value": "Edited"}, {"go": "#/"}, {"button": "Save"}],
               put={"status": 422, "body": {"detail": "invalid", "errors": errors}})
    assert r["hash"] == SETTINGS and r["viewClass"] == "settings"
    assert r["errors"]["display_name"] == "one line, at most 40 characters"
    assert "You have unsaved changes" in r["text"]


def test_a_clean_form_leaves_without_asking():
    r = routed([{"input": "display_name", "value": "Edited"}, {"input": "display_name", "value": "Scout"},
                {"go": "#/a/scout"}])
    assert r["hash"] == "#/a/scout" and r["viewClass"] == "" and r["replaced"] == []


# ---------- deleting the agent ----------

def deleting(actions, d=None, delete=None, a=None) -> dict:
    """The router on the settings page, DELETE /api/agents/scout (with or without ?volumes=1) answering `delete`."""
    ok = delete or {"status": 200, "body": {"ok": True, "volumes": [], "kept": None}}
    r = {"GET /api/agents/scout": {"status": 200, "body": d or detail()}, "GET /api/catalog": {"status": 200, "body": CATALOG},
         "GET /api/agents/scout/history": {"status": 200, "body": []},
         "GET /api/agents/scout/sessions": {"status": 200, "body": []},
         "DELETE /api/agents/scout": ok, "DELETE /api/agents/scout?volumes=1": ok}
    (result,) = run({"view": "route", "hash": SETTINGS, "name": "scout", "routes": r, "actions": actions,
                     "agents": [a or agent()]})
    return result


def deletes(result) -> list[str]:
    return [q["path"] for q in result["requests"] if q["method"] == "DELETE"]


def test_delete_a_container_agent_with_its_volumes():
    r = deleting([{"button": "Delete agent…"}])
    assert "Delete Scout? It stops, and Valet forgets it" in r["seen"]
    assert "sessions, inbox items, shared notes and files, queued messages, triggers and wake-ups go for good" \
        in r["seen"]
    assert "Also delete its volumes on mac" in r["seen"] and r["controls"]["delete-volumes"]["checked"] is False
    assert deletes(r) == []                                     # nothing until they confirm

    r = deleting([{"button": "Delete agent…"}, {"check": "delete-volumes", "checked": True},
                  {"button": "Delete with its volumes"}])
    assert deletes(r) == ["/api/agents/scout?volumes=1"]
    assert r["hash"] == "#/" and "Runners" in r["text"]         # the agents list

    r = deleting([{"button": "Delete agent…"}, {"button": "Delete"}])
    assert deletes(r) == ["/api/agents/scout"] and r["hash"] == "#/"
    r = deleting([{"button": "Delete agent…"}, {"button": "Cancel"}])
    assert deletes(r) == [] and "Delete agent…" in r["seen"] and "Valet forgets it" not in r["seen"]

    # Never placed on a runner (never ran): no volumes to offer.
    never = {"runner": None, "state": "stopped", "status": "stopped"}
    r = deleting([{"button": "Delete agent…"}], d=detail(**never), a=agent(**never))
    assert "Valet forgets it" in r["seen"] and "delete-volumes" not in r["controls"]


def test_delete_a_host_agent_keeps_its_folder_and_shows_a_refusal():
    host = copy.deepcopy(DEFINITION)
    host["computer"] = {**host["computer"], "level": "host", "workdir": "/Users/alex/code", "mounts": []}
    refused = {"status": 503, "body": {"detail": "runner mac is not connected: it has to be, to stop scout and "
                                                 "remove its files there"}}
    r = deleting([{"input": "display_name", "value": "Edited"}, {"button": "Delete agent…"}, {"button": "Delete"}],
                 d=detail(host), delete=refused)
    assert "Its working folder on mac stays: /Users/alex/code is yours, and Valet never deletes it." in r["seen"]
    assert "delete-volumes" not in r["controls"]
    assert deletes(r) == ["/api/agents/scout"] and r["hash"] == SETTINGS
    assert "runner mac is not connected" in r["seen"]
    assert r["controls"]["display_name"]["value"] == "Edited"   # the unsaved edits are still there


def test_unsaved_edits_dont_hold_a_deleted_agent():
    r = deleting([{"input": "display_name", "value": "Edited"}, {"button": "Delete agent…"}, {"button": "Delete"}])
    assert r["hash"] == "#/" and "You have unsaved changes" not in r["text"]


def test_a_deleted_agent_leaves_every_list():
    """Deleted elsewhere (another tab, an app): its frame takes it off the list and the inbox, and its open page goes
    to the agents list."""
    gone = {"type": "agent_deleted", "agent": "scout"}
    (r,) = run({"view": "route", "hash": "#/a/scout", "name": "scout", "agents": [agent(), agent(name="other", display_name="Other")],
                "routes": {"GET /api/agents/scout": {"status": 200, "body": detail()},
                           "GET /api/agents/scout/sessions": {"status": 200, "body": []}},
                "actions": [{"frame": gone}]})
    assert r["hash"] == "#/" and r["agent"] is None and "Runners" in r["text"]
    assert "Scout" not in r["seen"] and "Other" in r["seen"]
    item = {"id": "i1", "kind": "message", "agent": "scout", "session_id": None, "turn_id": None, "status": "pending",
            "title": "Hello from scout", "text": "Hello from scout", "options": [], "created_at": 1, "answer": None}
    kept = {**item, "id": "i2", "agent": "other", "title": "Hello from other", "text": "Hello from other"}
    (r,) = run({"view": "inbox", "agents": [agent(), agent(name="other", display_name="Other")], "inbox": [item, kept], "actions": []})
    assert "Hello from scout" in r["seen"]
    (r,) = run({"view": "inbox", "agents": [agent(), agent(name="other", display_name="Other")], "inbox": [item, kept],
                "actions": [{"frame": gone}]})
    assert "Hello from scout" not in r["seen"] and "Hello from other" in r["seen"]
