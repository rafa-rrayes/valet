"""The web UI's browser screens (browser.js): the Browser tab (profiles, a new one, grants, the activity log,
browser.yaml), a profile's screen (title, note, site rules, tabs, saved logins, delete), Settings › Browser, and
browser asks in the inbox and the session. Run by their own JS under node with a stand-in DOM and a fake API
(tests/web/settings.js, render.js). Skipped without node."""
import copy
import re
import time

from tests.web.test_web_inbox import buttons, ev, inbox
from tests.web.test_web_render import render
from tests.web.test_web_secrets_tools import html_of, page, sent
from tests.web.test_web_settings import DEFINITION, agent, detail, pytestmark, run  # noqa: F401

NOW = time.time()
WORK = {"name": "work", "title": "Work logins", "note": "Company accounts.",
        "sites": {"github.com": {"commit": "ask", "read": "allow"}}, "used_by": ["scout"], "vault": 1, "running": True}
HOME = {"name": "home", "title": None, "note": None, "sites": {}, "used_by": [], "vault": 0, "running": False}
RUNNING = [{"profile": "work", "tabs": 2, "started_at": NOW - 600, "idle": 5, "busy": False, "viewers": 0}]
GRANT = {"id": 7, "agent": "scout", "profile": "work", "host": "github.com", "kind": "commit",
         "expires_at": NOW + 3000, "created_at": NOW - 600}
ACTION = {"id": 41, "ts": NOW - 60, "agent": "scout", "profile": "work", "action": "click", "kind": "commit",
          "host": "github.com", "target": "Merge pull request", "decision": "ask", "outcome": "ok",
          "note": "Alice allowed it once", "shot": "0123456789abcdef.jpg"}
MINE = {**ACTION, "id": 40, "agent": None, "action": "live_navigate", "kind": None, "target": "https://github.com/",
        "decision": None, "note": None, "shot": None}
LOGIN = {"name": "github/alice", "profile": "work", "site": "github.com", "label": "Work", "username": "alice@x.com",
         "has_password": True, "has_totp": False, "created_at": NOW - 9000, "updated_at": NOW - 9000, "used_at": None,
         "used_by": []}
TAB = {"tab": "t1", "url": "https://github.com/pulls", "title": "Pull requests"}


def ok(body, status=200):
    return {"status": status, "body": body}


def browser_routes(**over) -> dict:
    r = {"GET /api/browser/profiles": ok({"profiles": [HOME, WORK], "error": None}),
         "GET /api/browser/running": ok(RUNNING),
         "GET /api/browser/grants": ok([GRANT]),
         "GET /api/browser/audit?limit=30": ok({"actions": [ACTION, MINE], "more": True}),
         "GET /api/browser/profiles/work": ok(WORK),
         "GET /api/browser/profiles/work/tabs": ok([TAB]),
         "GET /api/vault?profile=work": ok([LOGIN]),
         "GET /api/browser/audit?limit=30&profile=work": ok({"actions": [ACTION], "more": False})}
    r.update(over)
    return r


def at(hash_, actions=(), agents=None, **routes) -> dict:
    (result,) = run({"view": "route", "hash": hash_, "routes": browser_routes(**routes), "actions": list(actions),
                     "agents": agents if agents is not None else [agent()]})
    return result


# ---------- #/browser ----------

def test_the_browser_tab_lists_profiles_and_whether_they_run():
    r = at("#/browser")
    work, home = html_of(r, "profile:work"), html_of(r, "profile:home")
    assert "Work logins" in work and '<span class="badge running">running</span>' in work
    assert "2 tabs · used by Scout · 1 saved login · 1 site rule" in work
    assert 'href="#/browser/work/live"' in work and 'href="#/browser/work"' in work and ">Stop</button>" in work
    assert '<span class="badge ">stopped</span>' in home and "not running · no agent uses it" in home
    assert ">Stop</button>" not in home
    assert 'href="#/vault"' in r["html"]                                 # the vault is a card away
    assert r["viewClass"] == "browser"


def test_stop_and_a_yaml_error():
    r = at("#/browser", [{"button": "Stop", "row": "profile:work"}],
           **{"POST /api/browser/profiles/work/stop": ok({"ok": True, "was_running": True})})
    assert sent(r) == [("POST", "/api/browser/profiles/work/stop", None)]
    assert [q["path"] for q in r["requests"]].count("/api/browser/profiles") == 2      # looked again
    bad = at("#/browser", **{"GET /api/browser/profiles": ok({"profiles": [], "error": "profiles.x: bad name"})})
    assert "browser.yaml has an error" in bad["text"] and "profiles.x: bad name" in bad["text"]
    assert "No profiles yet" not in bad["text"]


def test_a_new_profile_is_named_from_its_title():
    made = {**HOME, "name": "alices-logins", "title": "Alice's logins"}
    r = at("#/browser", [{"button": "New profile"}, {"input": "profile-title", "value": "Alice's logins"},
                         {"button": "Create"}],
           **{"POST /api/browser/profiles": ok(made, 201), "GET /api/browser/profiles/alices-logins": ok(made)})
    assert sent(r)[0] == ("POST", "/api/browser/profiles", {"name": "alices-logins", "title": "Alice's logins"})
    assert r["hash"] == "#/browser/alices-logins"
    bad = at("#/browser", [{"button": "New profile"}, {"input": "profile-name", "value": "Not OK"},
                           {"button": "Create"}])
    assert sent(bad) == [] and "Lowercase letters, digits and dashes" in bad["errors"]["profile-new"]
    taken = at("#/browser", [{"button": "New profile"}, {"input": "profile-title", "value": "Work"}, {"button": "Create"}],
               **{"POST /api/browser/profiles": ok({"detail": "there's already a profile named work"}, 409)})
    assert taken["errors"]["profile-new"] == "there's already a profile named work" and taken["hash"] == "#/browser"


def test_hour_grants_are_listed_with_revoke():
    r = at("#/browser", [{"button": "Revoke", "row": "bgrant:7"}],
           **{"DELETE /api/browser/grants/7": ok({"ok": True})})
    g = html_of(r, "bgrant:7")
    assert "Scout" in g and "commit on " in g and "github.com" in g and "work · until" in g
    assert sent(r) == [("DELETE", "/api/browser/grants/7", None)]
    assert [q["path"] for q in r["requests"]].count("/api/browser/grants") == 2
    none = at("#/browser", **{"GET /api/browser/grants": ok([])})
    assert "Allowed for an hour" not in none["seen"]


def test_the_activity_log_shows_screenshots_pages_back_and_filters():
    r = at("#/browser", [{"button": "Older"}, {"change": "audit-agent", "value": "scout"}],
           **{"GET /api/browser/audit?limit=30&before=40": ok({"actions": [{**ACTION, "id": 12}], "more": False}),
              "GET /api/browser/audit?limit=30&agent=scout": ok({"actions": [ACTION], "more": False})})
    paths = [q["path"] for q in r["requests"]]
    assert "/api/browser/audit?limit=30&before=40" in paths and "/api/browser/audit?limit=30&agent=scout" in paths
    a = html_of(r, "audit:41")
    assert "<strong>Scout</strong> clicked" in a and "Merge pull request" in a and "github.com · work · Commit" in a
    assert "asked you · Alice allowed it once" in a and '<span class="badge completed">done</span>' in a
    assert '<img class="shot" src="/api/browser/shots/0123456789abcdef.jpg"' in a
    first = at("#/browser")
    assert "<strong>You</strong> went to" in html_of(first, "audit:40")      # the owner's own, from the live view
    assert ">Older</button>" in first["html"]


def test_browser_yaml_is_edited_as_text():
    text = "profiles:\n  work: {title: Work logins}\n"
    r = at("#/browser", [{"open": "browser-yaml"}, {"input": "file:browser.yaml", "value": text + "  home: {}\n"},
                         {"button": "Save file"}],
           **{"GET /api/browser/yaml": ok({"text": text, "commit": "abc"}),
              "PUT /api/browser/yaml": ok({"commit": "0123456789", "profiles": [WORK], "error": None})})
    assert sent(r) == [("PUT", "/api/browser/yaml", {"text": text + "  home: {}\n"})]
    assert "Saved (0123456)" in r["text"]
    bad = at("#/browser", [{"open": "browser-yaml"}, {"button": "Save file"}],
             **{"GET /api/browser/yaml": ok({"text": text, "commit": "abc"}),
                "PUT /api/browser/yaml": ok({"detail": "profiles.work.sites: bad pattern"}, 422)})
    assert bad["errors"]["browser-yaml"] == "profiles.work.sites: bad pattern"
    assert bad["controls"]["file:browser.yaml"]["value"] == text


# ---------- #/browser/<profile> ----------

def test_a_profile_screen_shows_its_title_note_and_rules():
    r = at("#/browser/work")
    c = r["controls"]
    assert c["title"]["value"] == "Work logins" and c["note"]["value"] == "Company accounts."
    assert c["site-site:0"]["value"] == "github.com"
    assert (c["site-read:0"]["value"], c["site-interact:0"]["value"], c["site-commit:0"]["value"],
            c["site-login:0"]["value"]) == ("allow", "", "ask", "")
    assert "Used by Scout · 1 saved login" in r["text"]
    assert 'href="#/browser/work/live/t1"' in html_of(r, "tab:t1") and "Pull requests" in html_of(r, "tab:t1")
    assert 'href="#/vault/github/alice"' in html_of(r, "login:github/alice")
    assert "Stop its browser" in r["text"]
    assert sent(r) == []


def test_editing_a_profile_saves_it_in_one_put():
    saved = {**WORK, "title": "Work", "sites": {"github.com": {"commit": "ask", "read": "allow", "login": "deny"},
                                                  "admin.shopify.com/store": {"commit": "deny"}}}
    r = at("#/browser/work", [
        {"input": "title", "value": "Work"},
        {"change": "site-login:0", "value": "deny"},
        {"button": "Add a site"},
        {"input": "site-site:1", "value": "https://Admin.Shopify.com/store/"}, {"blur": "site-site:1"},
        {"change": "site-commit:1", "value": "deny"},
        {"button": "Add a site"},                                       # a row left empty isn't a rule
        {"button": "Save"}], **{"PUT /api/browser/profiles/work": ok(saved)})
    (put,) = sent(r)
    assert put[:2] == ("PUT", "/api/browser/profiles/work")
    assert put[2] == {"title": "Work", "note": "Company accounts.", "sites": saved["sites"]}
    assert list(put[2]["sites"]["github.com"]) == ["commit", "read", "login"]          # the order it came in
    assert "Saved. Agents apply its rules at their next action." in r["seen"]


def test_setting_a_rule_back_is_no_change_and_half_rows_say_why():
    r = at("#/browser/work", [{"change": "site-read:0", "value": ""}, {"change": "site-read:0", "value": "allow"}])
    assert "Unsaved changes" not in r["seen"]
    r = at("#/browser/work", [{"button": "Add a site"}, {"change": "site-commit:1", "value": "allow"}])
    assert "Type the site it's for." in r["seen"]
    r = at("#/browser/work", [{"button": "Add a site"}, {"input": "site-site:1", "value": "gitlab.com"}])
    assert "Pick at least one" in r["seen"]
    r = at("#/browser/work", [{"button": "Add a site"}, {"input": "site-site:1", "value": "github.com"},
                              {"change": "site-read:1", "value": "deny"}])
    assert "This site is listed twice: only the first counts." in r["seen"]


def test_leaving_with_edits_asks_first():
    r = at("#/browser/work", [{"input": "note", "value": "Changed."}, {"go": "#/browser"}])
    assert r["hash"] == "#/browser/work" and "You have unsaved changes" in r["seen"]
    assert "Discard & leave" in r["seen"]
    gone = at("#/browser/work", [{"input": "note", "value": "Changed."}, {"go": "#/browser"},
                                 {"button": "Discard & leave"}])
    assert gone["hash"] == "#/browser" and gone["viewClass"] == "browser"


def test_open_a_tab_goes_to_its_live_view():
    r = at("#/browser/work", [{"input": "open-url", "value": "news.ycombinator.com"}, {"button": "Open"}],
           **{"POST /api/browser/profiles/work/tabs": ok({"tab": "t2", "url": "https://news.ycombinator.com/",
                                                          "title": ""}, 201)})
    assert sent(r)[0] == ("POST", "/api/browser/profiles/work/tabs", {"url": "news.ycombinator.com"})
    assert r["hash"] == "#/browser/work/live/t2"


def test_deleting_a_profile_asks_whether_to_wipe_its_logins():
    r = at("#/browser/work", [{"button": "Delete profile"}, {"check": "wipe", "checked": True},
                              {"button": "Delete and wipe"}],
           **{"DELETE /api/browser/profiles/work?wipe=true": ok({"ok": True})})
    assert sent(r) == [("DELETE", "/api/browser/profiles/work?wipe=true", None)] and r["hash"] == "#/browser"
    kept = at("#/browser/work", [{"button": "Delete profile"}, {"button": "Delete"}],
              **{"DELETE /api/browser/profiles/work?wipe=false": ok({"detail": "scout uses it"}, 409)})
    assert sent(kept) == [("DELETE", "/api/browser/profiles/work?wipe=false", None)]
    assert kept["errors"]["delete"] == "scout uses it" and kept["hash"] == "#/browser/work"
    assert "First take it from Scout's settings and delete its 1 saved login" in kept["seen"]
    assert "Also wipe its logins" in kept["seen"]


def test_a_profile_that_is_not_there():
    r = at("#/browser/nope", **{"GET /api/browser/profiles/nope": ok({"detail": "not found"}, 404)})
    assert "There's no browser profile named nope." in r["text"] and "Delete profile" not in r["text"]


def test_new_login_from_a_profile_opens_the_vault_for_it():
    r = at("#/browser/work", [{"button": "New login"}], **{"GET /api/vault": ok([LOGIN])})
    assert r["hash"] == "#/vault" and r["controls"]["vault-profile"]["value"] == "work"


# ---------- Settings › Browser ----------

def settings_page(actions=(), d=None, **routes) -> dict:
    r = {"GET /api/browser/profiles": ok({"profiles": [HOME, WORK], "error": None}),
         "GET /api/browser/grants?agent=scout": ok([GRANT])}
    r.update(routes)
    return page(actions, d=d, routes=r)


def browser_put(r) -> dict:
    (put,) = [b for m, p, b in sent(r) if m == "PUT"]
    return put["definition"]


def test_settings_show_the_profiles_it_uses_and_its_grants():
    r = settings_page()
    c = r["controls"]
    assert c["browser-profile:work"]["checked"] is True and c["browser-profile:home"]["checked"] is False
    assert "also used by" not in html_of(r, "browser-profile:work")
    assert "1 site rule · 1 saved login" in html_of(r, "browser-profile:work")
    assert "Its browser tools (valet.browser, in Tools) are off" in html_of(r, "browser-off")   # mcp: [github]
    assert "commit on " in html_of(r, "bgrant:7") and "Scout" not in html_of(r, "bgrant:7")     # its own only


def test_picking_a_profile_turns_the_browser_tools_on_and_dropping_the_last_turns_them_off():
    d = detail(definition={**DEFINITION, "tools": {**DEFINITION["tools"], "mcp": ["github", "valet.browser"]}})
    r = settings_page([{"check": "browser-profile:home", "checked": True}, {"button": "Save"}], d=d,
                      **{"PUT /api/agents/scout/definition": ok(d)})
    sent_def = browser_put(r)
    assert sent_def["browser"]["profiles"] == ["work", "home"]
    assert sent_def["tools"]["mcp"] == ["github", "valet.browser"]
    r = settings_page([{"check": "browser-profile:work", "checked": False}, {"button": "Save"}], d=d,
                      **{"PUT /api/agents/scout/definition": ok(d)})
    sent_def = browser_put(r)
    assert sent_def["browser"]["profiles"] == [] and sent_def["tools"]["mcp"] == ["github"]
    # Off then on again: no change, valet.browser back where it was.
    r = settings_page([{"check": "browser-profile:work", "checked": False},
                       {"check": "browser-profile:work", "checked": True}], d=d)
    assert "Unsaved changes" not in r["seen"]
    # Without the tools, a picked profile turns them on.
    r = settings_page([{"check": "browser-profile:home", "checked": True}, {"button": "Save"}],
                      **{"PUT /api/agents/scout/definition": ok(detail())})
    assert browser_put(r)["tools"]["mcp"] == ["github", "valet.browser"]


def test_its_own_site_rules_save_with_the_page():
    r = settings_page([{"button": "Add a site", "within": "browser-sec"},
                       {"input": "agent-site-site:0", "value": "github.com"},
                       {"change": "agent-site-commit:0", "value": "allow"}, {"button": "Save"}],
                      **{"PUT /api/agents/scout/definition": ok(detail())})
    assert browser_put(r)["browser"] == {"profiles": ["work"], "rules": {"github.com": {"commit": "allow"}}}


def test_an_always_allow_from_elsewhere_comes_into_the_form():
    """An "Always allow" answered in the inbox wrote a rule into agent.yaml: the form takes it, so a save from here
    doesn't take it back; the form's own edit of another site stays."""
    after = copy.deepcopy(DEFINITION)
    after["browser"]["rules"] = {"github.com": {"commit": "allow"}}
    item = {"id": "b1", "kind": "browser", "agent": "scout", "status": "answered",
            "answer": {"outcome": "selected", "optionId": "allow_always"}}
    r = settings_page([{"button": "Add a site", "within": "browser-sec"},
                       {"input": "agent-site-site:0", "value": "gitlab.com"},
                       {"change": "agent-site-read:0", "value": "deny"},
                       {"frame": {"type": "inbox", "item": item}},
                       {"frame": {"type": "agent", "agent": agent()}},
                       {"button": "Save"}],
                      **{"GET /api/agents/scout": [ok(detail()), ok(detail(definition=after))],
                         "PUT /api/agents/scout/definition": ok(detail(definition=after))})
    assert browser_put(r)["browser"]["rules"] == {"gitlab.com": {"read": "deny"}, "github.com": {"commit": "allow"}}


def test_tools_says_the_browser_decides_its_own_calls():
    servers = {"servers": [{"name": "valet.browser", "kind": "builtin", "builtin": True, "description": "The browser",
                            "agents": ["scout"], "connected": True}], "error": None}
    d = detail(definition={**DEFINITION, "tools": {**DEFINITION["tools"], "mcp": ["valet.browser"]}})
    click = {"name": "valet.browser__click", "tool": "click", "title": "Click", "description": None,
             "annotations": None, "decision": "ask"}
    tools = {"agent": "scout", "permissions": "ask", "url": "", "servers": [
        {"server": "valet.browser", "ok": True, "error": None, "own_rules": True, "tools": [click]}]}
    r = page([{"open": "toolset"}], d=d, servers=servers, tools=tools,
             routes={"GET /api/browser/profiles": ok({"profiles": [WORK], "error": None})})
    assert "Valet decides each call to it by its own rules: the site rules in Browser, below" in r["text"]
    row = html_of(r, "valet.browser__click")
    assert '<span class="badge ">its rules</span>' in row
    assert re.findall(r"<option[^>]*>([^<]*)</option>", row) == ["Its own rules decide", "Block"]   # only Block


# ---------- asks: the inbox, the session ----------

ASK = {"profile": "work", "tab": "t1", "url": "https://github.com/alice/valet/pull/3", "host": "github.com",
       "kind": "commit", "summary": "click “Merge pull request”",
       "detail": {"element": {"tag": "button", "type": "submit", "name": "Merge pull request"}},
       "shot": "00112233445566ff.jpg"}
FOUR = [{"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"},
        {"optionId": "allow_hour", "name": "Allow for an hour", "kind": "allow_once"},
        {"optionId": "allow_always", "name": "Always allow", "kind": "allow_always"},
        {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"}]
HUMAN = [{"optionId": "done", "name": "Done", "kind": "allow_once"},
         {"optionId": "cant", "name": "I can't", "kind": "reject_once"}]


def browser_item(browser=None, options=None, **over) -> dict:
    item = {"id": "b1", "kind": "browser", "agent": "scout", "session_id": "s1", "turn_id": "t1", "status": "pending",
            "title": "Scout wants to click “Merge pull request” on github.com", "created_at": NOW - 5,
            "answered_at": None, "answer": None, "tool_call": {"toolCallId": "browser-b1", "title": "click"},
            "options": options or FOUR, "browser": browser or ASK}
    item.update(over)
    return item


def test_a_browser_ask_in_the_inbox_shows_the_page_the_site_and_every_option():
    card = html_of(inbox([browser_item()]), "ask:b1")
    assert "Scout wants to " in card and "click “Merge pull request”" in card
    assert "github.com" in card and "Commit · profile work" in card
    assert "https://github.com/alice/valet/pull/3" in card and "On button (submit) &quot;Merge pull request&quot;" in card
    assert '<img class="shot" src="/api/browser/shots/00112233445566ff.jpg"' in card
    assert 'href="#/browser/work/live/t1"' in card and "Look at it live" in card
    assert [t for _, t in buttons(card)] == ["Allow once", "Allow for an hour", "Always allow", "Reject"]
    r = inbox([browser_item()], [{"button": "Allow for an hour"}],
              {"POST /api/inbox/b1/answer": ok(browser_item(status="answered"))})
    assert sent(r) == [("POST", "/api/inbox/b1/answer", {"option_id": "allow_hour"})]


def test_what_it_would_type_and_which_login():
    typed = {**ASK, "kind": "interact", "summary": "type into “Comment”",
             "detail": {"text": "LGTM", "chars": 4, "submit": True, "field": "Comment"}}
    card = html_of(inbox([browser_item(typed)]), "ask:b1")
    assert "What it would type, then submit:" in card and '<pre class="typed">LGTM</pre>' in card
    login = {**ASK, "kind": "login", "summary": "sign in", "detail": {"entry": "github/alice", "site": "github.com"}}
    card = html_of(inbox([browser_item(login)]), "ask:b1")
    assert "With the saved login github/alice (for github.com)" in card and "Sign in · profile work" in card


def test_request_human_offers_the_live_view_then_done_or_cant():
    human = {**ASK, "kind": "human", "summary": "take over", "detail": {"reason": "solve the captcha"}, "shot": None}
    card = html_of(inbox([browser_item(human, HUMAN)]), "ask:b1")
    assert "Scout needs you to take over" in card and ": solve the captcha" in card
    assert 'href="#/browser/work/live/t1"' in card and "Open the live view" in card
    assert buttons(card) == [("primary", "Done"), ("danger", "I can't")]
    r = inbox([browser_item(human, HUMAN)], [{"button": "Done"}],
              {"POST /api/inbox/b1/answer": ok(browser_item(human, HUMAN, status="answered"))})
    assert sent(r) == [("POST", "/api/inbox/b1/answer", {"option_id": "done"})]


def test_the_session_draws_the_same_card_and_the_answer():
    data = {"requestId": "b1", "toolCall": {"toolCallId": "browser-b1", "title": "click"}, "options": FOUR,
            "kind": "browser", "browser": ASK}
    asking, done = render(
        {"events": [ev(1, "permission_request", data)], "inbox": [{"id": "b1", "status": "pending", "answer": None}]},
        {"events": [ev(1, "permission_request", {**data, "browser": {**ASK, "kind": "human", "detail": {}},
                                                 "options": HUMAN}),
                    ev(2, "permission_answer", {"requestId": "b1", "status": "answered",
                                                "outcome": {"outcome": "selected", "optionId": "cant"}})]})
    assert "wants to " in asking["html"] and "/api/browser/shots/00112233445566ff.jpg" in asking["html"]
    assert [t for _, t in buttons(asking["html"])] == ["Allow once", "Allow for an hour", "Always allow", "Reject"]
    assert re.search(r'<div class="result">You said you couldn.t</div>', done["html"])
    assert "<button" not in done["html"]
