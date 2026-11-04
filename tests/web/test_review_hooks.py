"""Review of m4's web UI hooks (app.js's addAskView, addSettingsSection, addRoute and the screens that use them):
offline tests that fail on the merged code and show a bug each. Run like the other web tests (node, the stand-in DOM
and the fake API of tests/web/settings.js). Skipped without node."""
import copy
import posixpath

from tests.web.test_web_browser import at, browser_put, ok, settings_page
from tests.web.test_web_settings import DEFINITION, agent, detail, pytestmark  # noqa: F401
from tests.web.test_web_triggers import inbox, item


def test_a_question_answer_being_typed_in_the_inbox_survives_another_item_arriving():
    """The inbox redraws every card on any `inbox` (or `hello`) frame, so the box a question's answer is typed in is
    made again, empty: Alice types "after lunch" under scout's question, another agent's item arrives (or the socket
    reconnects), and what he typed is gone."""
    other = item("message", id="I2", agent="alpha", text="Deploy done.", title="Deploy done.")
    r = inbox([item("question")], [{"input": "answer-I1", "value": "after lunch"},
                                   {"frame": {"type": "inbox", "item": other}}])
    assert "Deploy done." in r["text"]                                # the new item is there
    assert r["controls"]["answer-I1"]["value"] == "after lunch"       # and the answer being typed still is


def test_an_always_allow_reaches_settings_when_the_rule_lands_after_the_answer():
    """Settings › Browser looks again (syncRules) at the first `agent` frame after an "Always allow" answer, then
    forgets it was waiting. But that first frame is the answer route's own (api.py: emit_agent right after
    record_answer), sent before the browser service, woken by the answer, writes the rule (service.always: a git
    commit in a thread, then its own emit_agent). A look that lands before the commit sees no new rule; the frame
    that follows the commit is ignored; Save then PUTs the whole definition without the rule, taking it back.
    (app.js's Tools section has the same `scoped` pattern for tools.rules, SettingsView.frame.)"""
    after = copy.deepcopy(DEFINITION)
    after["browser"]["rules"] = {"github.com": {"commit": "allow"}}
    answered = {"id": "b1", "kind": "browser", "agent": "scout", "status": "answered",
                "answer": {"outcome": "selected", "optionId": "allow_always"}}
    r = settings_page([{"button": "Add a site", "within": "browser-sec"},
                       {"input": "agent-site-site:0", "value": "gitlab.com"},
                       {"change": "agent-site-read:0", "value": "deny"},
                       {"frame": {"type": "inbox", "item": answered}},
                       {"frame": {"type": "agent", "agent": agent()}},     # the answer route's frame
                       {"frame": {"type": "agent", "agent": agent()}},     # the one after the rule's commit
                       {"button": "Save"}],
                      # GETs of the agent: the page's load, then Tools' and Browser's looks at the first frame (both
                      # before the commit), then any look after it
                      **{"GET /api/agents/scout": [ok(detail()), ok(detail()), ok(detail()), ok(detail(definition=after))],
                         "PUT /api/agents/scout/definition": ok(detail(definition=after))})
    assert browser_put(r)["browser"]["rules"] == {"gitlab.com": {"read": "deny"}, "github.com": {"commit": "allow"}}


def test_a_vault_address_cant_reach_outside_the_vault():
    """#/vault/<entry> takes whatever the address says as the entry's name (no VAULT_RE check, unlike memory's and
    the shared space's screens), and vaultPath() keeps "..": a link to #/vault/../skills/review loads
    /api/vault/../skills/review, which the browser resolves to /api/skills/review, and the screen's "Delete login"
    sends DELETE there: the shared skill goes. An agent can put that link (https://<valet>/#/vault/../skills/review)
    in a message or a shared note."""
    r = at("#/vault/../skills/review")
    paths = [(q["method"], posixpath.normpath(q["path"].split("?")[0])) for q in r["requests"]]
    assert ("GET", "/api/skills/review") not in paths            # it asked the skills route, not the vault
    assert all(p.startswith(("/api/vault", "/api/browser")) for _, p in paths)
