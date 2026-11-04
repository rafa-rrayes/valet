"""The web UI's directives: the settings page's "What it knows" (the layers an agent is told, its standing authority
and a preview of all of it) and #/directives (the owner profile and the authority presets). Run by their own JS
under node with a stand-in DOM and a fake API (tests/web/settings.js). Skipped without node."""
import copy

from tests.web.test_web_settings import CATALOG, DEFINITION, agent, detail, pytestmark, run  # noqa: F401
from valet.shared import catalog

CAT = {**CATALOG, "authority": catalog.AUTHORITY}
LEVEL = {p["id"]: p["name"] for p in catalog.PERMISSIONS}

OWNER_TEXT = "# About Alice\n\n- Lives in São Paulo; his cat is Bartolomeu.\n"
OWNER = {"text": OWNER_TEXT, "commit": "abc", "used_by": ["scout"]}
PRESETS = [
    {"name": "full", "text": "# Standing authority: full trust\n\nAlice trusts you with his computer.\n", "used_by": []},
    {"name": "standard", "text": "# Standing authority: standard\n\nDo the work without asking.\n", "used_by": []},
    {"name": "strict", "text": "# Standing authority: strict\n\nDo what Alice asks, and only that.\n",
     "used_by": ["scout"]},
]
PREVIEW = {"agent": "scout", "harness": "claude", "error": None,
           "layers": [{"name": "owner", "file": "owner.md", "state": "ok", "text": OWNER["text"]},
                      {"name": "persona.md", "file": "persona.md", "state": "ok", "text": "You are Scout.\n"},
                      {"name": "environment", "file": "environment.md", "state": "ok", "text": "# Your environment\n"},
                      {"name": "memory", "file": "memory.md", "state": "off", "text": ""}],
           "text": OWNER_TEXT + "\nYou are Scout.\n\n# Your environment"}


def definition(**over) -> dict:
    d = copy.deepcopy(DEFINITION)
    d.update(over)
    return d


def page(actions=(), d=None, routes=None, owner=OWNER, presets=PRESETS) -> dict:
    """scout's settings page with the owner profile and the presets answered."""
    r = {"GET /api/agents/scout": {"status": 200, "body": d or detail()},
         "GET /api/catalog": {"status": 200, "body": CAT},
         "GET /api/agents/scout/history": {"status": 200, "body": []},
         "GET /api/owner": {"status": 200, "body": owner},
         "GET /api/authority": {"status": 200, "body": presets},
         "POST /api/agents/scout/directives/preview": {"status": 200, "body": PREVIEW}}
    r.update(routes or {})
    (result,) = run({"view": "settings", "name": "scout", "routes": r, "actions": list(actions), "agents": [agent()]})
    return result


def saved(result) -> list[str]:
    (body,) = [q["body"] for q in result["requests"] if q["method"] == "PUT"]
    return body["definition"]["directives"]


def previews(result) -> list[dict]:
    return [q["body"] for q in result["requests"] if q["path"].endswith("/directives/preview")]


# ---------- What it knows ----------

def test_what_it_knows_shows_the_saved_layers():
    r = page()                                       # scout: directives [persona.md, memory]
    c = r["controls"]
    assert c["directive:owner"]["checked"] is False and c["directive:environment"]["checked"] is False
    assert "authority:-" in r["chosen"]              # no standing authority
    assert {"authority:full", "authority:standard", "authority:strict"}.isdisjoint(r["chosen"])
    assert "Lives in São Paulo; his cat is Bartolomeu." in r["rows"]["directive:owner"]   # the profile's gist
    assert "Do what Alice asks, and only that." in r["text"]                                 # each preset's gist
    assert "Suggested for " + LEVEL["ask"] in r["text"]                                     # ask -> standard
    at = [r["html"].index(f'data-choice="authority:{n}"') for n in ("-", "strict", "standard", "full")]
    assert at == sorted(at)                          # from the most careful to the most trusting
    assert "Full trust" in r["text"]                 # titled by the preset's own heading
    assert "directive:persona.md" not in r["rows"]   # persona.md is listed: no warning
    assert not previews(r)                           # the preview loads when it's opened


def test_layers_go_in_their_place_and_save_with_the_definition():
    d = definition(directives=["owner", "authority/full", "persona.md", "environment", "memory"])
    r = page([{"check": "directive:owner", "checked": True}, {"check": "directive:environment", "checked": True},
              {"click": "authority:full"}, {"button": "Save"}],
             routes={"PUT /api/agents/scout/definition": {"status": 200, "body": detail(d)}})
    assert saved(r) == ["owner", "authority/full", "persona.md", "environment", "memory"]
    assert "authority:full" in r["chosen"] and r["errors"] == {}


def test_changing_the_preset_keeps_its_place_and_none_drops_it():
    d = detail(definition(directives=["persona.md", "authority/strict", "owner"]))   # an odd order, kept
    put = {"PUT /api/agents/scout/definition": {"status": 500, "body": {"detail": "x"}}}
    r = page([{"click": "authority:full"}, {"button": "Save"}], d=d, routes=put)
    assert saved(r) == ["persona.md", "authority/full", "owner"]
    r = page([{"click": "authority:-"}, {"check": "directive:owner", "checked": False}, {"button": "Save"}], d=d,
             routes=put)
    assert saved(r) == ["persona.md"]


def test_suggested_preset_follows_the_permission_level():
    r = page([{"click": "permissions:trusted"}])
    assert "Suggested for " + LEVEL["trusted"] in r["text"]       # trusted -> full
    assert "Suggested for " + LEVEL["ask"] not in r["text"]


def test_a_missing_preset_is_shown_and_its_save_error_lands_by_the_choices():
    d = detail(definition(directives=["authority/gone", "persona.md"]))
    msg = "authority/gone: there's no such authority preset"
    err = {"detail": "invalid", "errors": [{"field": "directives", "msg": msg}]}
    r = page([{"input": "display_name", "value": "Scout 2"}, {"button": "Save"}], d=d,
             routes={"PUT /api/agents/scout/definition": {"status": 422, "body": err}})
    assert "authority:gone" in r["chosen"]
    assert "There's no such preset any more: pick another." in r["text"]
    assert r["errors"]["directives"] == msg


def test_a_definition_without_its_persona_says_so():
    d = detail(definition(directives=["owner"]))
    r = page([{"button": "Tell it"}, {"button": "Save"}], d=d,
             routes={"PUT /api/agents/scout/definition": {"status": 500, "body": {"detail": "x"}}})
    assert saved(r) == ["owner", "persona.md"]
    assert "directive:persona.md" not in r["rows"]


def test_owner_profile_not_written_yet():
    r = page(owner={"text": "", "commit": "abc", "used_by": []})
    assert "You haven't written it yet" in r["rows"]["directive:owner"]
    assert "Write about you ›" in r["text"]


def test_owner_and_presets_that_fail_to_load_leave_the_rest_of_the_page():
    r = page(routes={"GET /api/owner": {"status": 500, "body": {"detail": "disk on fire"}},
                     "GET /api/authority": {"status": 500, "body": {"detail": "disk on fire"}}})
    assert "disk on fire" in r["text"] and r["controls"]["display_name"]["value"] == "Scout"
    assert "directive:owner" in r["controls"]       # the switches still work


def test_preview_sends_the_draft_and_shows_the_assembled_text():
    r = page([{"input": "persona", "value": "You are Scout, a scout.\n"},
              {"check": "directive:owner", "checked": True}, {"open": "preview-det"}])
    (body,) = previews(r)
    assert body["persona"] == "You are Scout, a scout.\n"
    assert body["definition"]["directives"] == ["owner", "persona.md", "memory"]
    assert body["definition"]["computer"] == DEFINITION["computer"]       # the whole draft
    assert "his cat is Bartolomeu" in r["rows"]["preview"] and "# Your environment" in r["rows"]["preview"]
    assert "Added to Claude Code's own system prompt" in r["text"]
    assert "Memory · off" in r["text"]          # its directives list the layer, but `memory` is off
    assert "You changed something since" not in r["seen"]


def test_preview_goes_stale_on_a_change_and_refreshes():
    r = page([{"open": "preview-det"}, {"check": "directive:environment", "checked": True}])
    assert len(previews(r)) == 1
    assert "You changed something since: Refresh to see it." in r["seen"]
    r = page([{"open": "preview-det"}, {"check": "directive:environment", "checked": True}, {"button": "Refresh"}])
    first, second = previews(r)
    assert "environment" not in first["definition"]["directives"]
    assert second["definition"]["directives"] == ["persona.md", "environment", "memory"]
    assert "You changed something since" not in r["seen"]


def test_preview_for_deepseek_and_with_an_error():
    gone = {"name": "authority/gone", "file": "authority/gone.md", "state": "missing", "text": ""}
    p = {**PREVIEW, "harness": "deepseek", "error": "authority/gone: there's no such authority preset",
         "layers": [gone], "text": ""}
    r = page([{"open": "preview-det"}],
             routes={"POST /api/agents/scout/directives/preview": {"status": 200, "body": p}})
    assert "authority/gone: there's no such authority preset" in r["text"]
    assert "Authority: Gone · missing" in r["text"]
    assert "Nothing: it gets only its harness's own instructions." in r["rows"]["preview"]


# ---------- #/directives ----------

def screen(actions=(), routes=None, agents=None) -> dict:
    r = {"GET /api/owner": {"status": 200, "body": OWNER}, "GET /api/authority": {"status": 200, "body": PRESETS}}
    r.update(routes or {})
    (result,) = run({"view": "directives", "routes": r, "actions": list(actions),
                     "agents": agents if agents is not None else [agent()]})
    return result


def test_screen_shows_the_owner_profile_and_the_presets():
    r = screen()
    assert r["controls"]["owner"]["value"] == OWNER["text"]
    assert r["controls"]["preset:full"]["value"] == PRESETS[0]["text"]
    assert "Included by Scout" in r["text"]
    assert "Used by Scout" in r["rows"]["preset:strict"] and "No agent uses it" in r["rows"]["preset:full"]
    assert "Delete" in r["rows"]["preset:full"] and "Delete" not in r["rows"]["preset:strict"]   # in use: kept


def test_saving_the_owner_profile_says_who_needs_a_restart():
    text = "# About Alice\n\nHis cat is Bartolomeu.\n"
    r = screen([{"input": "owner", "value": text}, {"button": "Save"}],
               routes={"PUT /api/owner": {"status": 200, "body": {**OWNER, "text": text}}})
    assert {"method": "PUT", "path": "/api/owner", "body": {"text": text}} in r["requests"]
    assert "Saved. Scout needs a restart to use it." in r["text"]
    stopped = screen([{"input": "owner", "value": text}, {"button": "Save"}],
                     routes={"PUT /api/owner": {"status": 200, "body": {**OWNER, "text": text}}},
                     agents=[agent(desired="stopped", state="stopped")])
    assert "Saved. Scout reads it at its next start." in stopped["text"]


def test_saving_a_preset():
    text = "# Standing authority: strict\n\nAsk before anything.\n"
    r = screen([{"input": "preset:strict", "value": text}, {"button": "Save", "row": "preset:strict"}],
               routes={"PUT /api/authority/strict": {"status": 200, "body": {**PRESETS[2], "text": text}}})
    assert {"method": "PUT", "path": "/api/authority/strict", "body": {"text": text}} in r["requests"]
    assert "Saved. Scout needs a restart to use it." in r["rows"]["preset:strict"]
    bad = screen([{"input": "preset:full", "value": "x"}, {"button": "Save", "row": "preset:full"}],
                 routes={"PUT /api/authority/full": {"status": 422, "body": {"detail": "too long"}}})
    assert bad["errors"]["preset:full"] == "too long"


def test_a_new_preset():
    made = {"name": "github-only", "text": "# Standing authority: Github only\n\n", "used_by": []}
    r = screen([{"button": "New preset"}, {"input": "preset-new", "value": "GitHub only"}, {"button": "Create"}],
               routes={"PUT /api/authority/github-only": {"status": 200, "body": made},
                       "GET /api/authority": [{"status": 200, "body": PRESETS},
                                              {"status": 200, "body": [*PRESETS, made]}]})
    assert {"method": "PUT", "path": "/api/authority/github-only",
            "body": {"text": "# Standing authority: Github only\n\n"}} in r["requests"]
    assert r["controls"]["preset:github-only"]["value"] == made["text"]
    taken = screen([{"button": "New preset"}, {"input": "preset-new", "value": "Full"}, {"button": "Create"}])
    assert taken["errors"]["preset-new"] == "There's already a preset called full."
    assert not [q for q in taken["requests"] if q["method"] == "PUT"]


def test_deleting_an_unused_preset_asks_first():
    r = screen([{"button": "Delete", "row": "preset:full"}])
    assert "Delete Full trust? It stays in the definitions' history." in r["seen"]
    assert not [q for q in r["requests"] if q["method"] == "DELETE"]
    r = screen([{"button": "Delete", "row": "preset:full"}, {"button": "Delete", "within": "confirm"}],
               routes={"DELETE /api/authority/full": {"status": 200, "body": {"ok": True}},
                       "GET /api/authority": [{"status": 200, "body": PRESETS},
                                              {"status": 200, "body": PRESETS[1:]}]})
    assert {"method": "DELETE", "path": "/api/authority/full", "body": None} in r["requests"]
    assert "preset:full" not in r["rows"]


def test_the_agent_list_links_to_the_screen():
    (r,) = run({"view": "agents", "routes": {}, "agents": [agent()], "actions": []})
    assert 'href="#/directives"' in r["html"] and "About you &amp; authority" in r["html"]
    (r,) = run({"view": "route", "hash": "#/directives", "routes": {"GET /api/owner": {"status": 200, "body": OWNER},
                "GET /api/authority": {"status": 200, "body": PRESETS}}, "agents": [], "actions": []})
    assert r["viewClass"] == "directives"
