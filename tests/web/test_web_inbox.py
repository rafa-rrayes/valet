"""The web UI's side of the inbox's scoped answers and notifications: every option an item carries (the gateway's
once, an hour, always, reject) in the inbox and the session, a harness's own request as before, the answer in
words; Settings › Tools's rules (tools.rules) and live hour grants; the Notifications screen. Run by their own JS
under node with a stand-in DOM and a fake API (tests/web/settings.js, render.js). Skipped without node."""
import copy
import re
import time

from tests.web.test_web_render import render, tool_cards
from tests.web.test_web_secrets_tools import TOOLS, definition, html_of, page, sent, tool_item
from tests.web.test_web_settings import agent, detail, pytestmark, run  # noqa: F401

NOW = time.time()
FOUR = [{"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"},
        {"optionId": "allow_hour", "name": "Allow for an hour", "kind": "allow_once"},
        {"optionId": "allow_always", "name": "Always allow", "kind": "allow_always"},
        {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"}]
CLAUDE = [{"optionId": "allow_always", "name": "Always Allow", "kind": "allow_always"},
          {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
          {"optionId": "reject", "name": "Reject", "kind": "reject_once"}]


def buttons(html: str) -> list[tuple[str, str]]:
    """(class, text) of each button."""
    return [(m.group(1) or "", m.group(2))
            for m in re.finditer(r'<button(?: class="([^"]*)")?[^>]*>([^<]*)</button>', html)]


def inbox(items, actions=(), routes=None) -> dict:
    r = {"GET /api/inbox": {"status": 200, "body": items}}
    r.update(routes or {})
    (result,) = run({"view": "inbox", "agents": [agent()], "inbox": items, "actions": list(actions), "routes": r})
    return result


# ---------- every option an item carries ----------

def test_a_gateway_ask_offers_all_four_answers():
    item = tool_item(options=FOUR)
    r = inbox([item])
    assert buttons(r["html"]) == [("primary", "Allow once"), ("", "Allow for an hour"), ("", "Always allow"),
                                  ("danger", "Reject")]
    answered = tool_item(options=FOUR, status="answered", answer={"outcome": "selected", "optionId": "allow_hour"})
    r = inbox([item], [{"button": "Allow for an hour"}],
              {"POST /api/inbox/i1/answer": {"status": 200, "body": answered}})
    assert sent(r) == [("POST", "/api/inbox/i1/answer", {"option_id": "allow_hour"})]
    assert "Nothing is waiting on you." in r["text"]                   # answered: off the list


def test_a_harness_request_keeps_once_and_reject():
    perm = {"id": "p1", "kind": "permission", "agent": "scout", "session_id": "s1", "turn_id": "t1",
            "status": "pending", "title": "Write hello.txt", "created_at": NOW - 5, "answered_at": None,
            "answer": None, "tool_call": {"toolCallId": "w1", "title": "Write hello.txt"}, "options": CLAUDE}
    assert [t for _, t in buttons(inbox([perm])["html"])] == ["Allow", "Reject"]
    # Nothing but "always" offered: shown as it is.
    only = {**perm, "options": CLAUDE[:1]}
    assert buttons(inbox([only])["html"]) == [("primary", "Always Allow")]


def test_other_kinds_show_what_they_carry():
    """An item of a kind Valet adds later (a browser step, a question) is drawn from its options, whatever they are."""
    item = {"id": "b1", "kind": "browser", "agent": "scout", "session_id": None, "turn_id": None,
            "status": "pending", "title": "Sign in to example.com", "created_at": NOW - 5, "answered_at": None,
            "answer": None, "tool_call": {"toolCallId": "browser-b1", "title": "Sign in to example.com"},
            "options": [{"optionId": "open", "name": "Open the browser", "kind": "allow_once"},
                        {"optionId": "later", "name": "Not now", "kind": "reject_once"},
                        {"optionId": "done", "name": "Done", "kind": "allow_always"}]}
    r = inbox([item])
    assert buttons(r["html"]) == [("primary", "Open the browser"), ("danger", "Not now"), ("", "Done")]
    assert "Sign in to example.com" in r["text"]


# ---------- in the session ----------

GATEWAY_TOOL = {"server": "github", "name": "create_issue", "title": "Create issue", "description": None,
                "arguments": {"title": "Bug"}, "annotations": None}


def ev(i, kind, data):
    return {"id": i, "session_id": "s1", "turn_id": "t1", "kind": kind, "data": data, "created_at": 1_700_000_000 + i}


def gateway_ask(kind=True):
    data = {"requestId": "g1", "toolCall": {"toolCallId": "gateway-g1", "title": "github: Create issue",
                                            "kind": "other", "rawInput": {"title": "Bug"}},
            "options": FOUR, "tool": GATEWAY_TOOL}
    if kind:
        data["kind"] = "tool"
    return ev(1, "permission_request", data)


def answered(option):
    return ev(2, "permission_answer", {"requestId": "g1", "status": "answered",
                                       "outcome": {"outcome": "selected", "optionId": option}})


def test_the_session_offers_them_too_and_says_which_was_given():
    asking, older = render({"events": [gateway_ask()], "inbox": [{"id": "g1", "kind": "tool", "status": "pending",
                                                                   "answer": None}]},
                           {"events": [gateway_ask(kind=False)], "inbox": [{"id": "g1", "status": "pending",
                                                                            "answer": None}]})
    assert [t for _, t in buttons(asking["html"])] == ["Allow once", "Allow for an hour", "Always allow", "Reject"]
    assert [t for _, t in buttons(older["html"])] == ["Allow once", "Allow for an hour", "Always allow", "Reject"]
    words = render(*({"events": [gateway_ask(), answered(o)]}
                     for o in ("allow_once", "allow_hour", "allow_always", "reject_once")))
    assert [re.search(r'<div class="result">([^<]*)</div>', r["html"]).group(1) for r in words] == [
        "Allowed", "Allowed for an hour", "Always allowed", "Rejected"]
    # Without its options (an answer the history kept, its request long gone), the scoped ones still read right.
    (bare,) = render({"events": [{**gateway_ask(), "data": {**gateway_ask()["data"], "options": []}},
                                 answered("allow_hour")]})
    assert '<div class="result">Allowed for an hour</div>' in bare["html"]


def test_a_harness_request_in_the_session_is_as_before():
    events = [ev(1, "tool_call", {"toolCallId": "w1", "title": "Write a.txt", "kind": "edit", "status": "pending"}),
              ev(2, "permission_request", {"requestId": "R1", "toolCall": {"toolCallId": "w1"}, "options": CLAUDE})]
    asking, always = render({"events": events, "inbox": [{"id": "R1", "kind": "permission", "status": "pending",
                                                          "answer": None}]},
                            {"events": events + [ev(3, "permission_answer", {
                                "requestId": "R1", "status": "answered",
                                "outcome": {"outcome": "selected", "optionId": "allow_always"}})]})
    (card,) = tool_cards(asking)
    assert buttons(card) == [("primary", "Allow"), ("danger", "Reject")]
    (card,) = tool_cards(always)
    assert '<span class="badge allowed">Always allowed</span>' in card


def test_a_session_item_of_another_kind_shows_all_its_options():
    data = {"requestId": "b1", "toolCall": {"toolCallId": "browser-b1", "title": "Sign in"},
            "options": [{"optionId": "a", "name": "Allow", "kind": "allow_once"},
                        {"optionId": "b", "name": "Allow always", "kind": "allow_always"},
                        {"optionId": "r", "name": "Reject", "kind": "reject_once"}]}
    by_event, by_item = render(
        {"events": [ev(1, "permission_request", {**data, "kind": "browser"})],
         "inbox": [{"id": "b1", "status": "pending", "answer": None}]},
        {"events": [ev(1, "permission_request", data)],
         "inbox": [{"id": "b1", "kind": "browser", "status": "pending", "answer": None}]})
    assert [t for _, t in buttons(by_event["html"])] == ["Allow", "Allow always", "Reject"]
    assert [t for _, t in buttons(by_item["html"])] == ["Allow", "Allow always", "Reject"]


# ---------- settings › tools: rules and grants ----------

def ruled_tool(name, title, decision, source, level):
    return {"name": f"github__{name}", "tool": name, "title": title, "description": f"{title}, on GitHub.",
            "annotations": None, "decision": decision, "source": source, "level_decision": level}


RTOOLS = {**TOOLS, "servers": [{"server": "github", "ok": True, "error": None, "tools": [
    ruled_tool("get_issue", "Get issue", "allow", "level", "allow"),
    ruled_tool("create_issue", "Create issue", "allow", "rule", "ask"),
    ruled_tool("delete_repo", "Delete repository", "ask", "level", "ask")]}]}
RULES = {"github": {"create_issue": "allow"}}


def ruled(rules=RULES, mcp=("github",)):
    return detail(definition(tools={"mcp": list(mcp), "skills": ["*"], "rules": copy.deepcopy(rules)}))


def grant(gid, tool, minutes=40):
    return {"id": gid, "agent": "scout", "server": "github", "tool": tool, "expires_at": NOW + minutes * 60,
            "created_at": NOW - 60}


GRANTS = "GET /api/gateway/grants?agent=scout"
PUT_FAILS = {"PUT /api/agents/scout/definition": {"status": 500, "body": {"detail": "x"}}}


def test_each_tool_shows_its_rule():
    r = page(d=ruled(), tools=RTOOLS)
    assert r["controls"]["rule:github:create_issue"]["value"] == "allow"
    assert r["controls"]["rule:github:get_issue"]["value"] == ""       # none: as its level says
    assert r["controls"]["rule:github:*"]["value"] == ""
    assert "As its level says (asks you)" in html_of(r, "github__delete_repo")
    assert '<span class="badge runs">runs</span>' in html_of(r, "github__create_issue")
    assert "3 tools: 2 run, 1 ask you" in html_of(r, "github")
    assert "Other rules" not in r["text"] and "Unsaved changes" not in r["text"]


def test_picking_a_rule_shows_its_effect_and_saves_it():
    r = page([{"change": "rule:github:delete_repo", "value": "deny"},
              {"change": "rule:github:create_issue", "value": ""},       # back to its level: the rule goes
              {"button": "Save"}], d=ruled(), tools=RTOOLS, routes=PUT_FAILS)
    assert '<span class="badge blocked">blocked</span>' in html_of(r, "github__delete_repo")
    assert '<span class="badge asks">asks you</span>' in html_of(r, "github__create_issue")
    assert "3 tools: 1 run, 1 ask you, 1 blocked" in html_of(r, "github")
    ((_, _, body),) = sent(r, "PUT")
    assert body["definition"]["tools"] == {"mcp": ["github"], "skills": ["*"], "rules": {"github": {"delete_repo": "deny"}}}


def test_a_rule_taken_off_and_put_back_is_no_change():
    r = page([{"change": "rule:github:create_issue", "value": ""}, {"change": "rule:github:create_issue",
                                                                     "value": "allow"}], d=ruled(), tools=RTOOLS)
    assert "Unsaved changes" not in r["text"]
    # Every rule gone: the server goes too, and the definition is as it was without them.
    r = page([{"change": "rule:github:create_issue", "value": ""}, {"button": "Save"}], d=ruled(), tools=RTOOLS,
             routes=PUT_FAILS)
    ((_, _, body),) = sent(r, "PUT")
    assert body["definition"]["tools"]["rules"] == {}


def test_a_rule_for_all_its_tools():
    r = page([{"change": "rule:github:*", "value": "deny"}], d=ruled(), tools=RTOOLS)
    assert '<span class="badge blocked">blocked</span>' in html_of(r, "github__get_issue")
    assert '<span class="badge runs">runs</span>' in html_of(r, "github__create_issue")    # its own rule wins
    assert "Same as all its tools (blocked)" in html_of(r, "github__delete_repo")
    r = page([{"change": "rule:github:*", "value": "ask"}, {"button": "Save"}], d=ruled(), tools=RTOOLS,
             routes=PUT_FAILS)
    ((_, _, body),) = sent(r, "PUT")
    assert body["definition"]["tools"]["rules"] == {"github": {"create_issue": "allow", "*": "ask"}}


def test_rules_the_lists_dont_show_are_listed_apart():
    rules = {"github": {"create_issue": "allow", "gone_tool": "deny"}, "notes": {"*": "ask"}}
    r = page([{"button": "Remove", "row": "rule:notes/*"}, {"button": "Save"}], d=ruled(rules), tools=RTOOLS,
             routes=PUT_FAILS)
    assert "Other rules" in r["text"]
    assert "github › gone_tool" in html_of(r, "rule:github/gone_tool")
    assert '<span class="badge blocked">blocked</span>' in html_of(r, "rule:github/gone_tool")
    assert "rule:notes/*" not in r["rows"]
    ((_, _, body),) = sent(r, "PUT")
    assert body["definition"]["tools"]["rules"] == {"github": {"create_issue": "allow", "gone_tool": "deny"}}
    shown = page(d=ruled(rules), tools=RTOOLS)
    assert "notes › all its tools" in html_of(shown, "rule:notes/*")


def test_live_grants_show_and_revoke():
    grants = [grant(7, "delete_repo")]
    r = page(d=ruled(), tools=RTOOLS, routes={GRANTS: [{"status": 200, "body": grants}]})
    repo = html_of(r, "github__delete_repo")
    assert '<span class="badge runs">runs</span>' in repo and re.search(r"Allowed for an hour, until \d", repo)
    assert "3 tools: 3 run" in html_of(r, "github")
    assert "github › delete_repo" in html_of(r, "grant:7") and ">Revoke</button>" in html_of(r, "grant:7")
    # A grant never beats a block.
    blocked = page([{"change": "rule:github:delete_repo", "value": "deny"}], d=ruled(), tools=RTOOLS,
                   routes={GRANTS: {"status": 200, "body": grants}})
    assert '<span class="badge blocked">blocked</span>' in html_of(blocked, "github__delete_repo")
    # Revoked: gone from the list, and the tool asks again.
    r = page([{"button": "Revoke", "row": "grant:7"}], d=ruled(), tools=RTOOLS,
             routes={GRANTS: [{"status": 200, "body": grants}, {"status": 200, "body": []}],
                     "DELETE /api/gateway/grants/7": {"status": 200, "body": {"ok": True}}})
    assert sent(r) == [("DELETE", "/api/gateway/grants/7", None)]
    assert "grant:7" not in r["rows"] and "Allowed for an hour" not in r["text"]
    assert '<span class="badge asks">asks you</span>' in html_of(r, "github__delete_repo")


ALWAYS = {"type": "inbox", "item": tool_item(options=FOUR, status="answered",
                                             answer={"outcome": "selected", "optionId": "allow_always"})}
AGENT_FRAME = {"type": "agent", "agent": agent()}


def test_an_always_answer_reaches_an_open_settings_page():
    """"Always allow" in the inbox writes a rule; the settings page takes it, so saving the page keeps it."""
    after = ruled({"github": {"create_issue": "allow", "delete_repo": "allow"}})
    r = page([{"frame": ALWAYS}, {"frame": AGENT_FRAME}], d=ruled(), tools=RTOOLS,
             routes={"GET /api/agents/scout": [{"status": 200, "body": ruled()}, {"status": 200, "body": after}]})
    assert r["controls"]["rule:github:delete_repo"]["value"] == "allow"
    assert "Unsaved changes" not in r["text"]
    # With edits of its own: the new rule joins them, and they stay.
    r = page([{"input": "display_name", "value": "Scouty"}, {"change": "rule:github:get_issue", "value": "deny"},
              {"frame": ALWAYS}, {"frame": AGENT_FRAME}, {"button": "Save"}], d=ruled(), tools=RTOOLS,
             routes={"GET /api/agents/scout": [{"status": 200, "body": ruled()}, {"status": 200, "body": after}],
                     **PUT_FAILS})
    ((_, _, body),) = sent(r, "PUT")
    assert body["definition"]["display_name"] == "Scouty"
    assert body["definition"]["tools"]["rules"] == {"github": {"create_issue": "allow", "delete_repo": "allow",
                                                               "get_issue": "deny"}}
    # An agent frame alone (a status change) fetches nothing.
    quiet = page([{"frame": AGENT_FRAME}], d=ruled(), tools=RTOOLS)
    assert [q["path"] for q in quiet["requests"]].count("/api/agents/scout") == 1


# ---------- the notifications screen ----------

HOOK = "https://hooks.slack.com/services/T0/B0/secret"
SETTINGS = {"webhook_url": HOOK, "push": {"configured": True, "error": None, "topic": "br.com.rrayes.valet",
                                          "sandbox": False, "devices": 2}}


def devices_now() -> list[dict]:
    """Two devices, timed from now: "2h ago", "2m ago" and "1h ago" hold however slow the suite is."""
    now = time.time()
    return [{"id": 1, "platform": "ios", "name": "Alice's iPhone", "created_at": now - 7230, "last_push": now - 130},
            {"id": 2, "platform": "macos", "name": "Mac", "created_at": now - 3630, "last_push": None}]


DEVICES = devices_now()


def notifications(actions=(), routes=None, settings=SETTINGS, devices=None) -> dict:
    r = {"GET /api/notifications": {"status": 200, "body": settings},
         "GET /api/devices": {"status": 200, "body": devices if devices is not None else devices_now()}}
    r.update(routes or {})
    (result,) = run({"view": "route", "hash": "#/notifications", "routes": r, "actions": list(actions),
                     "agents": [agent()]})
    return result


def test_the_agents_list_links_to_it():
    (r,) = run({"view": "agents", "agents": [agent()]})
    assert 'href="#/notifications"' in r["html"] and "Notifications" in r["text"]


def test_the_screen_shows_the_webhook_and_the_devices():
    r = notifications()
    assert r["viewClass"] == "notifications"
    assert r["controls"]["webhook_url"]["value"] == HOOK
    assert "Push is on, through Apple's production service for br.com.rrayes.valet." in r["text"]
    phone = html_of(r, "device:1")
    assert "Alice's iPhone" in phone and "iPhone or iPad · added 2h ago · last notified 2m ago" in phone
    assert "Mac · added 1h ago · not notified yet" in html_of(r, "device:2")
    off = notifications(settings={"webhook_url": None, "push": {"configured": False, "error": None,
                                                                "topic": "x", "sandbox": False, "devices": 0}},
                        devices=[])
    assert off["controls"]["webhook_url"]["value"] == ""
    assert "Push isn't set up" in off["text"] and "VALET_APNS_KEY_FILE" in off["text"]
    assert "No devices yet." in off["text"]
    bad = notifications(settings={**SETTINGS, "push": {**SETTINGS["push"], "configured": False,
                                                       "error": "VALET_APNS_KEY_FILE: not a P-256 .p8 key"}})
    assert "Push is off: VALET_APNS_KEY_FILE: not a P-256 .p8 key" in bad["text"]


def test_saving_the_webhook():
    new = "https://discord.com/api/webhooks/1/abc"
    r = notifications([{"input": "webhook_url", "value": f"  {new} "}, {"button": "Save"}],
                      {"PUT /api/notifications": {"status": 200, "body": {**SETTINGS, "webhook_url": new}}})
    assert sent(r) == [("PUT", "/api/notifications", {"webhook_url": new})]
    assert r["controls"]["webhook_url"]["value"] == new and "Saved." in r["seen"]
    cleared = notifications([{"input": "webhook_url", "value": ""}, {"button": "Save"}],
                            {"PUT /api/notifications": {"status": 200, "body": {**SETTINGS, "webhook_url": None}}})
    assert sent(cleared) == [("PUT", "/api/notifications", {"webhook_url": None})]
    assert "Removed: no webhook." in cleared["seen"]
    msg = "webhook_url: an http(s) URL"
    refused = notifications([{"input": "webhook_url", "value": "ftp://x"}, {"button": "Save"}],
                            {"PUT /api/notifications": {"status": 422, "body": {"detail": msg}}})
    assert refused["errors"] == {"webhook_url": msg}
    assert refused["controls"]["webhook_url"]["value"] == "ftp://x"     # kept for fixing


def test_send_test_says_how_each_went():
    r = notifications([{"button": "Send test"}], {"POST /api/notifications/test": {"status": 200, "body": {
        "webhook": {"ok": True, "error": None}, "push": {"sent": 1, "failed": 0, "removed": 1, "error": None}}}})
    assert sent(r) == [("POST", "/api/notifications/test", None)]
    assert "Webhook: sent." in r["seen"]
    assert "Push: sent to 1 device, 1 no longer registered, removed." in r["seen"]
    bad = notifications([{"button": "Send test"}], {"POST /api/notifications/test": {"status": 200, "body": {
        "webhook": {"ok": False, "error": "hooks.slack.com answered 404"}, "push": None}}})
    assert "Webhook: hooks.slack.com answered 404" in bad["seen"] and "Push: not set up." in bad["seen"]
    none = notifications([{"button": "Send test"}], {"POST /api/notifications/test": {
        "status": 422, "body": {"detail": "Set a webhook URL, or configure push (VALET_APNS_*), first."}}})
    assert "Set a webhook URL, or configure push (VALET_APNS_*), first." in none["seen"]
    # A URL typed and not saved is saved first.
    new = "https://example.com/hook"
    typed = notifications([{"input": "webhook_url", "value": new}, {"button": "Send test"}],
                          {"PUT /api/notifications": {"status": 200, "body": {**SETTINGS, "webhook_url": new}},
                           "POST /api/notifications/test": {"status": 200, "body": {"webhook": {"ok": True,
                                                                                               "error": None},
                                                                                   "push": None}}})
    assert sent(typed) == [("PUT", "/api/notifications", {"webhook_url": new}),
                           ("POST", "/api/notifications/test", None)]


def test_removing_a_device():
    r = notifications([{"button": "Remove", "row": "device:2"}],
                      {"DELETE /api/devices/2": {"status": 200, "body": {"ok": True}},
                       "GET /api/devices": [{"status": 200, "body": DEVICES}, {"status": 200, "body": DEVICES[:1]}]})
    assert sent(r) == [("DELETE", "/api/devices/2", None)]
    assert "device:2" not in r["rows"] and "device:1" in r["rows"]


def test_leaving_with_a_url_not_saved_says_so_once():
    r = notifications([{"input": "webhook_url", "value": "https://example.com/new"}, {"go": "#/"}])
    assert r["hash"] == "#/notifications"
    assert r["errors"] == {"webhook_url": "This URL isn't saved: Save it, or leave again to drop it."}
    r = notifications([{"input": "webhook_url", "value": "https://example.com/new"}, {"go": "#/"}, {"go": "#/"}])
    assert r["hash"] == "#/" and r["viewClass"] != "notifications"
