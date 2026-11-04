"""Always-on in the web UI (static/triggers.js and its hooks in app.js), run by their own JS under node with a
stand-in DOM and a fake API (tests/web/settings.js, render.js): Settings › Triggers, valet.owner's messages and
questions, the session's queue, source lines and Retry, and the agents list and page. Node runs in UTC, so times
read as this file computes them. Skipped without node."""
import copy
import json
import os
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.web.test_web_settings import CATALOG, DEFINITION, agent, detail

HERE = Path(__file__).parent
STATIC = HERE.parent.parent / "valet" / "control" / "static"
ENV = {**os.environ, "TZ": "UTC"}

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node isn't installed")


def node(script: str, jobs: list[dict]) -> list[dict]:
    out = subprocess.run(["node", str(HERE / script), str(STATIC)], input=json.dumps(jobs), capture_output=True,
                         text=True, timeout=60, check=False, env=ENV)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def run(job: dict) -> dict:
    return node("settings.js", [job])[0]


def requests(r: dict, method: str) -> list[dict]:
    return [q for q in r["requests"] if q["method"] == method]


def tomorrow_at(hour: int, minute: int = 0) -> int:
    """A timestamp tomorrow (UTC) at hour:minute, and what whenOf() calls it."""
    now = datetime.now(UTC)
    day = datetime(now.year, now.month, now.day, hour, minute, tzinfo=UTC).timestamp() + 86400
    return int(day)


# ---------- Settings › Triggers ----------

TZ = "America/Sao_Paulo"
DAILY = {"trigger": "schedule:0 9 * * *", "kind": "schedule", "schedule": "0 9 * * *", "webhook": None,
         "prompt": "morning news", "session": "main", "description": "Every day at 09:00",
         "next": [tomorrow_at(9), tomorrow_at(9) + 86400], "last_run": None}
KEY = "vhk_0123456789abcdefghijklmnopqrstuvwxyz"


def hook(name="deploy", **over) -> dict:
    return {"trigger": f"webhook:{name}", "kind": "webhook", "schedule": None, "webhook": name, "prompt": None,
            "session": "main", "url": f"http://valet.test/hooks/scout/{name}", "key": KEY, "key_error": None,
            "key_created_at": 1, "last_run": None, **over}


def info(*triggers) -> dict:
    return {"status": 200, "body": {"agent": "scout", "timezone": TZ, "triggers": list(triggers), "wakeups": [],
                                    "next_trigger": None, "error": None}}


def settings(actions=(), triggers=None, routes=None, put=None) -> dict:
    d = copy.deepcopy(DEFINITION)
    if triggers is not None:
        d["triggers"] = triggers
    r = {"GET /api/agents/scout": {"status": 200, "body": detail(d)},
         "GET /api/catalog": {"status": 200, "body": CATALOG},
         "GET /api/agents/scout/history": {"status": 200, "body": []},
         "GET /api/agents/scout/triggers": info(DAILY)}
    if put:
        r["PUT /api/agents/scout/definition"] = put
    r.update(routes or {})
    return run({"view": "settings", "name": "scout", "routes": r, "actions": list(actions), "agents": [agent()]})


def test_a_schedule_shows_what_it_means_and_when_it_runs_next():
    r = settings([{"wait": 350}])
    c = r["controls"]
    assert c["trigger-0-schedule"]["value"] == "0 9 * * *" and c["trigger-0-prompt"]["value"] == "morning news"
    assert "Every day at 09:00 · next tomorrow 09:00" in r["text"]
    assert f"Schedules follow {TZ} time; the times here are this device's." in r["text"]
    assert {"trigger-0-session:main"} <= set(r["chosen"])
    # The saved triggers' own description: no preview asked for, nothing saved, nothing wrong.
    assert [q["path"] for q in requests(r, "GET") if "preview" in q["path"]] == []
    assert requests(r, "PUT") == [] and r["errors"] == {}


def test_typing_a_schedule_previews_it_and_saving_sends_it():
    preview = {"status": 200, "body": {"schedule": "*/15 * * * *", "description": "Every 15 minutes",
                                       "next": [tomorrow_at(0, 15)], "timezone": TZ}}
    routes = {"GET /api/triggers/preview?schedule=*%2F15%20*%20*%20*%20*": preview}
    typed = [{"input": "trigger-0-schedule", "value": "*/15 * * * *"}, {"wait": 350},
             {"click": "trigger-0-session:new"}, {"input": "trigger-0-prompt", "value": "check the feed"}]
    r = settings(typed, routes=routes)
    assert "Every 15 minutes · next tomorrow 00:15" in r["text"]
    assert len([q for q in requests(r, "GET") if "preview" in q["path"]]) == 1    # asked once, kept
    r = settings(typed + [{"button": "Save"}], routes=routes, put={"status": 200, "body": detail()})
    (put,) = requests(r, "PUT")
    assert put["body"]["definition"]["triggers"] == [{"schedule": "*/15 * * * *", "webhook": None,
                                                      "prompt": "check the feed", "session": "new"}]


def test_a_schedule_that_isnt_one_says_why():
    bad = {"status": 422, "body": {"detail": "minute: 61 is out of 0-59"}}
    r = settings([{"input": "trigger-0-schedule", "value": "61 * * * *"}, {"wait": 350}],
                 routes={"GET /api/triggers/preview?schedule=61%20*%20*%20*%20*": bad})
    assert "minute: 61 is out of 0-59" in r["text"]


def test_presets_add_and_remove():
    r = settings([{"click": "trigger-0-preset:0 9 * * 1-5"}, {"button": "Add a webhook"}, {"button": "Add a webhook"},
                  {"button": "Add a schedule"}, {"button": "Remove", "row": "trigger-0"}, {"button": "Save"}],
                 put={"status": 200, "body": detail()})
    (put,) = requests(r, "PUT")
    assert put["body"]["definition"]["triggers"] == [
        {"schedule": None, "webhook": "hook", "prompt": None, "session": "main"},
        {"schedule": None, "webhook": "hook-2", "prompt": None, "session": "main"},
        {"schedule": "0 8 * * *", "webhook": None, "prompt": "", "session": "main"}]


def test_the_preset_chip_sets_the_schedule():
    r = settings([{"click": "trigger-0-preset:0 9 * * 1-5"}])
    assert r["controls"]["trigger-0-schedule"]["value"] == "0 9 * * 1-5"
    assert "trigger-0-preset:0 9 * * 1-5" in r["chosen"] and "Unsaved changes" in r["text"]


def test_a_new_webhook_gets_its_address_once_saved():
    r = settings([{"button": "Add a webhook"}])
    assert r["controls"]["trigger-1-webhook"]["value"] == "hook"
    assert "Save to get its address and key." in r["text"]


WEBHOOK = [{"schedule": None, "webhook": "deploy", "prompt": "A deploy finished.", "session": "main"}]


def test_a_saved_webhook_shows_its_address_and_hides_its_key():
    r = settings(triggers=WEBHOOK, routes={"GET /api/agents/scout/triggers": info(hook())})
    c = r["controls"]
    assert c["trigger-0-url"]["value"] == "http://valet.test/hooks/scout/deploy"
    assert c["trigger-0-key"]["value"] == "vhk_0123••••••••" and KEY not in r["text"]
    shown = settings([{"button": "Show"}], triggers=WEBHOOK, routes={"GET /api/agents/scout/triggers": info(hook())})
    assert shown["controls"]["trigger-0-key"]["value"] == KEY


def test_a_new_key_is_asked_for_then_shown():
    new = "vhk_" + "z" * 40
    routes = {"GET /api/agents/scout/triggers": info(hook()),
              "POST /api/agents/scout/webhooks/deploy/key": {"status": 200, "body": {"key": new, "key_created_at": 2}}}
    asked = settings([{"button": "New key"}], triggers=WEBHOOK, routes=routes)
    assert "The old one stops working at once" in asked["seen"] and requests(asked, "POST") == []
    r = settings([{"button": "New key"}, {"button": "New key", "within": "confirm"}], triggers=WEBHOOK, routes=routes)
    (post,) = requests(r, "POST")
    assert post["path"] == "/api/agents/scout/webhooks/deploy/key"
    assert r["controls"]["trigger-0-key"]["value"] == new


def test_a_trigger_error_shows_next_to_it():
    errors = [{"field": "triggers.0.schedule", "msg": "never fires"}]
    r = settings([{"input": "trigger-0-prompt", "value": "news"}, {"button": "Save"}],
                 put={"status": 422, "body": {"detail": "invalid", "errors": errors}})
    assert r["errors"]["triggers.0.schedule"] == "never fires"


def test_when_the_addresses_dont_load_the_form_still_works():
    r = settings(routes={"GET /api/agents/scout/triggers": {"status": 500, "body": {"detail": "boom"}}})
    assert "Couldn't load the webhooks' addresses: boom" in r["text"]
    assert r["controls"]["trigger-0-schedule"]["value"] == "0 9 * * *"


# ---------- valet.owner in the inbox ----------

def item(kind, status="pending", answer=None, **over) -> dict:
    dismiss = {"optionId": "dismiss", "name": "Dismiss", "kind": "reject_once"}
    options = [dismiss] if kind == "message" else [
        {"optionId": "option-1", "name": "Ship it", "kind": "allow_once"},
        {"optionId": "option-2", "name": "Wait", "kind": "allow_once"}, dismiss]
    i = {"id": "I1", "kind": kind, "agent": "scout", "session_id": "S1", "turn_id": "T1", "status": status,
         "title": "The build", "tool_call": {"title": "The build"}, "options": options, "tool": None,
         "text": "The build is **green**." if kind == "message" else "Ship the release?", "answer": answer,
         "created_at": time.time() - 60, "answered_at": None}
    return {**i, **over}


def inbox(items, actions=(), routes=None) -> dict:
    return run({"view": "inbox", "agents": [agent()], "inbox": items, "actions": list(actions),
                "routes": routes or {}})


def test_a_message_reads_as_the_agent_saying_it_and_can_be_dismissed():
    done = item("message", status="answered", answer={"outcome": "selected", "optionId": "dismiss"})
    r = inbox([item("message")])
    assert "Scout says" in r["text"] and "<strong>green</strong>" in r["html"]
    assert "Open session ›" in r["text"] and "Dismiss" in r["text"]
    r = inbox([item("message")], [{"button": "Dismiss"}],
              routes={"POST /api/inbox/I1/answer": {"status": 200, "body": done}})
    (post,) = requests(r, "POST")
    assert post == {"method": "POST", "path": "/api/inbox/I1/answer", "body": {"option_id": "dismiss"}}
    assert "Nothing is waiting on you." in r["text"]                   # answered, it leaves the inbox


def test_a_question_takes_a_choice_words_or_both():
    answered = item("question", status="answered", answer={"outcome": "selected", "optionId": "option-1",
                                                           "text": "after lunch"})
    routes = {"POST /api/inbox/I1/answer": {"status": 200, "body": answered}}
    r = inbox([item("question")])
    assert "Scout asks" in r["text"] and "Ship the release?" in r["text"]
    assert [b for b in ("Ship it", "Wait", "Dismiss", "Send") if f">{b}</button>" in r["html"]] == [
        "Ship it", "Wait", "Dismiss", "Send"]
    r = inbox([item("question")], [{"input": "answer-I1", "value": " after lunch "}, {"button": "Ship it"}],
              routes=routes)
    assert requests(r, "POST")[0]["body"] == {"option_id": "option-1", "text": "after lunch"}

    r = inbox([item("question")], [{"input": "answer-I1", "value": "not today"}, {"button": "Send"}], routes=routes)
    assert requests(r, "POST")[0]["body"] == {"text": "not today"}

    r = inbox([item("question")], [{"button": "Send"}], routes=routes)
    assert requests(r, "POST") == [] and "Write an answer, or pick one of the choices." in r["text"]


def test_a_failed_answer_says_why_and_can_be_tried_again():
    r = inbox([item("question")], [{"button": "Wait"}],
              routes={"POST /api/inbox/I1/answer": {"status": 500, "body": {"detail": "boom"}}})
    assert "boom" in r["text"] and r["controls"]["answer-I1"]["disabled"] is False


def asked(kind: str) -> list[dict]:
    """A valet.owner item in the conversation: its permission_request event."""
    i = item(kind)
    return [ev(1, "user_message", {"content": {"type": "text", "text": "go"}}),
            ev(2, "permission_request", {"requestId": "I1", "toolCall": i["tool_call"], "options": i["options"],
                                         "kind": kind, "text": i["text"]})]


def test_in_the_conversation_an_answered_item_shows_the_answer():
    def shown(kind, answer):
        (r,) = node("render.js", [{"events": asked(kind), "inbox": [item(kind, status="answered", answer=answer)]}])
        return r["html"]
    html = shown("question", {"outcome": "selected", "optionId": "option-1", "text": "after lunch"})
    assert "scout asks" in html and "You answered: Ship it · after lunch" in html and "<button" not in html
    assert "You answered: Friday" in shown("question", {"outcome": "text", "text": "Friday"})
    assert "Dismissed" in shown("message", {"outcome": "selected", "optionId": "dismiss"})
    (pending,) = node("render.js", [{"events": asked("question"), "inbox": [item("question")]}])
    assert "scout asks" in pending["html"] and ">Ship it</button>" in pending["html"]


# ---------- the session: its queue, where prompts came from, Retry ----------

def ev(id_, kind, data, turn="T1") -> dict:
    return {"id": id_, "agent": "scout", "session_id": "S1", "turn_id": turn, "ts": 0, "kind": kind, "data": data}


def session(**over) -> dict:
    s = {"agent": "scout", "id": "S1", "title": "Mornings", "state": "idle", "turn_id": None, "created_at": 1,
         "updated_at": 1, "main": True, "queued": 0, "queue": []}
    return {**s, **over}


BASE = "/api/agents/scout/sessions/S1"


def session_view(actions=(), s=None, events=(), routes=None) -> dict:
    r = {f"GET {BASE}": {"status": 200, "body": s or session()},
         f"GET {BASE}/events?after=0&limit=500": {"status": 200, "body": {"events": list(events), "more": False}}}
    r.update(routes or {})
    return run({"view": "session", "name": "scout", "session": "S1", "agents": [agent()], "routes": r,
                "actions": list(actions)})


def test_the_main_session_is_marked_and_its_queue_listed():
    queue = [{"id": 7, "session_id": "S1", "text": "What's new today?", "source": "schedule",
              "label": "Every day at 08:00", "retry_of": None, "created_at": 1},
             {"id": 8, "session_id": "S1", "text": "and the weather", "source": "owner", "label": None,
              "retry_of": None, "created_at": 2}]
    r = session_view(s=session(state="working", turn_id="T1", queued=2, queue=queue))
    assert "Main session" in r["seen"] and "2 prompts waiting" in r["seen"]
    assert "Every day at 08:00: What's new today?" in r["rows"]["queued-7"]
    assert "and the weather" in r["rows"]["queued-8"] and "owner" not in r["rows"]["queued-8"]
    # Not the main one, nothing waiting: neither shows.
    plain = session_view(s=session(main=False))
    assert "Main session" not in plain["seen"] and "waiting" not in plain["seen"]


def test_removing_a_queued_prompt():
    queue = [{"id": 7, "session_id": "S1", "text": "later", "source": "owner", "label": None, "retry_of": None,
              "created_at": 1}]
    r = session_view([{"button": "Remove", "row": "queued-7"}], s=session(state="working", queued=1, queue=queue),
                     routes={f"DELETE {BASE}/queue/7": {"status": 200, "body": {"removed": 7}}})
    assert [q["path"] for q in requests(r, "DELETE")] == [f"{BASE}/queue/7"]
    assert [q["path"] for q in requests(r, "GET")].count(BASE) == 2      # and the session is fetched again


def test_a_prompt_sent_while_it_works_is_queued():
    r = session_view([{"input": "prompt", "value": "and then this"}, {"submit": "prompt"}],
                     s=session(state="working", turn_id="T1"),
                     routes={f"POST {BASE}/prompt": {"status": 202, "body": {"queued": 9, "session_id": "S1"}}})
    (post,) = requests(r, "POST")
    assert post["body"] == {"text": "and then this"}
    assert "Queued: it goes when the agent finishes what it's doing." in r["seen"]
    assert r["controls"]["prompt"]["value"] == "" and r["errors"] == {}


def test_where_a_prompt_came_from_shows_over_it():
    events = [ev(1, "user_message", {"content": {"type": "text", "text": "Valet: a scheduled prompt"},
                                     "source": "schedule", "label": "Every day at 08:00"}),
              ev(2, "turn_end", {"stopReason": "end_turn", "error": None}),
              ev(3, "user_message", {"content": {"type": "text", "text": "hi"}, "source": "agent:alpha",
                                     "label": "From alpha"}, turn="T2"),
              ev(4, "user_message", {"content": {"type": "text", "text": "x"}, "source": "webhook:deploy",
                                     "label": "Webhook deploy"}, turn="T3"),
              ev(5, "user_message", {"content": {"type": "text", "text": "y"}, "source": "answer",
                                     "label": "Alice's answer"}, turn="T4"),
              ev(6, "user_message", {"content": {"type": "text", "text": "z"}, "source": "owner", "label": None},
                 turn="T5"),
              ev(7, "user_message", {"content": {"type": "text", "text": "z"}, "source": "owner", "label": None,
                                     "retry_of": "T5"}, turn="T6")]
    (r,) = node("render.js", [{"events": events}])
    froms = [e for e in r["elements"] if e["className"].startswith("from")]
    assert [(e["className"], e["attrs"]["data-source"]) for e in froms] == [
        ("from schedule", "schedule"), ("from agent", "agent:alpha"), ("from webhook", "webhook:deploy"),
        ("from answer", "answer"), ("from owner", "owner")]
    for text in ("Schedule · Every day at 08:00", "From alpha", "Webhook deploy", "Alice's answer", "Sent again"):
        assert f">{text}</div>" in r["html"]


def test_retry_on_a_turn_that_failed():
    events = [ev(1, "user_message", {"content": {"type": "text", "text": "hi"}, "source": "owner"}),
              ev(2, "turn_end", {"stopReason": None, "error": "interrupted: the runner went away"})]
    r = session_view([{"button": "Retry"}], events=events,
                     routes={f"POST {BASE}/turns/T1/retry": {"status": 202, "body": {"queued": 3, "session_id": "S1"}}})
    (post,) = requests(r, "POST")
    assert post["path"] == f"{BASE}/turns/T1/retry"
    assert "Queued: it goes when the agent finishes what it's doing." in r["seen"]

    refused = session_view([{"button": "Retry"}], events=events, routes={
        f"POST {BASE}/turns/T1/retry": {"status": 409, "body": {"detail": "it was already sent again"}}})
    assert "it was already sent again" in refused["seen"]
    # The retried turn's own prompt takes the button away.
    again = events + [ev(3, "user_message", {"content": {"type": "text", "text": "hi"}, "source": "owner",
                                             "retry_of": "T1"}, turn="T2")]
    gone = session_view(events=again)
    assert "data-retry" not in gone["html"] and "Sent again" in gone["seen"]
    # A turn that ended well has none.
    ok = session_view(events=[events[0], ev(2, "turn_end", {"stopReason": "end_turn", "error": None})])
    assert "data-retry" not in ok["html"]


# ---------- the agents list and page ----------

def test_the_list_says_what_it_does_whats_waiting_and_whats_next():
    nxt = {"at": tomorrow_at(8), "kind": "schedule", "trigger": "schedule:0 8 * * *", "label": "Every day at 08:00"}
    r = run({"view": "agents", "agents": [
        agent(status="working", activity="Reading notes.md", queued=2, next_trigger=nxt),
        agent(name="idle", display_name="Idle", next_trigger={**nxt, "kind": "wakeup", "label": None}),
        agent(name="off", display_name="Off", desired="stopped", state="stopped", status="stopped",
              next_trigger=nxt, queued=1),
        agent(name="quiet", display_name="Quiet")]})
    assert (">Reading notes.md</span> · <span>2 prompts waiting</span> · <span class=\"next\">Next: Every day at "
            "08:00, tomorrow 08:00</span>") in r["rows"]["always-on-scout"]
    assert "Next: a wake-up it set, tomorrow 08:00" in r["rows"]["always-on-idle"]
    assert "1 prompt waiting" in r["rows"]["always-on-off"] and "Next" not in r["rows"]["always-on-off"]
    assert "always-on-quiet" not in r["rows"]


def test_activity_shows_only_while_it_works():
    r = run({"view": "agents", "agents": [agent(status="working", activity="thinking"),
                                          agent(name="b", display_name="B", status="idle", activity="writing")]})
    assert "Thinking…" in r["rows"]["always-on-scout"] and "always-on-b" not in r["rows"]


def test_the_agent_page_lists_its_triggers_wakeups_and_recent_runs():
    runs = [{"id": 2, "agent": "scout", "trigger": "schedule:0 9 * * *", "kind": "schedule",
             "label": "Every day at 09:00", "scheduled_for": tomorrow_at(9) - 86400, "fired_at": None,
             "outcome": "missed", "error": "its runner was offline until the grace window (10 min) ran out",
             "count": 1, "session_id": None, "turn_id": None},
            {"id": 1, "agent": "scout", "trigger": "schedule:0 9 * * *", "kind": "schedule",
             "label": "Every day at 09:00", "scheduled_for": tomorrow_at(9) - 3 * 86400, "fired_at": None,
             "outcome": "skipped", "error": "scout is stopped", "count": 3, "session_id": None, "turn_id": None}]
    wake = {"id": 4, "agent": "scout", "session_id": "S1", "due_at": tomorrow_at(7, 30), "prompt": "Check the PR",
            "status": "pending", "created_at": 1}
    body = info(DAILY)["body"] | {"wakeups": [wake]}
    routes = {"GET /api/agents/scout": {"status": 200, "body": detail()},
              "GET /api/agents/scout/sessions": {"status": 200, "body": [
                  session(), session(id="S2", title="Other", main=False, queued=2)]},
              "GET /api/agents/scout/triggers": {"status": 200, "body": body},
              "GET /api/agents/scout/trigger-runs?limit=10": {"status": 200, "body": {"runs": runs, "more": False}}}
    r = run({"view": "agent", "name": "scout", "agents": [agent()], "routes": routes})
    assert "Every day at 09:00" in r["rows"]["trigger-schedule:0 9 * * *"]
    assert "next tomorrow 09:00 · in its main session" in r["rows"]["trigger-schedule:0 9 * * *"]
    assert "Wake-up tomorrow 07:30" in r["rows"]["wakeup-4"] and "Check the PR" in r["rows"]["wakeup-4"]
    assert "missed" in r["rows"]["run-2"] and "grace window" in r["rows"]["run-2"]
    assert "3 times, the last" in r["rows"]["run-1"] and "skipped" in r["rows"]["run-1"]
    assert f"Schedules follow {TZ} time" in r["seen"]
    # The sessions list marks the main one and counts what waits in the others.
    assert '<span class="badge main">main</span>' in r["html"] and "· 2 waiting" in r["text"]
    # Stopped, its triggers don't fire: no next run.
    stopped = agent(desired="stopped", state="stopped", status="stopped")
    r = run({"view": "agent", "name": "scout", "agents": [stopped], "routes": routes})
    assert "skipped while it's stopped · in its main session" in r["rows"]["trigger-schedule:0 9 * * *"]
    assert "next tomorrow" not in r["text"] and "Schedules follow" not in r["text"]


def test_the_agent_page_has_no_triggers_part_without_any():
    routes = {"GET /api/agents/scout": {"status": 200, "body": detail()},
              "GET /api/agents/scout/sessions": {"status": 200, "body": []},
              "GET /api/agents/scout/triggers": info(),
              "GET /api/agents/scout/trigger-runs?limit=10": {"status": 200, "body": {"runs": [], "more": False}}}
    r = run({"view": "agent", "name": "scout", "agents": [agent()], "routes": routes})
    assert "Triggers" not in r["seen"]
