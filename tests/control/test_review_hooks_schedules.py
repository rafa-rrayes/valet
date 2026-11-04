"""Review of m4 (triggers): webhooks, schedules and the scheduler's catch-up. Each test fails on the code as merged
and shows one bug; its docstring says what goes wrong."""
import threading
import time

import pytest
from fastapi.testclient import TestClient

from tests.control.always_on import (
    agent_yaml, answer_new, answer_prompt, control, end_turn, runs, start, text_of, ts, wait,
)
from tests.control.conftest import PASSWORD
from valet.control import webhooks
from valet.control.__main__ import set_password
from valet.control.app import create_app
from valet.control.clock import ManualClock
from valet.shared.config import ControlConfig

T0 = ts(2026, 11, 2, 7, 59, 30)        # a Monday
DAILY = '  - schedule: "0 8 * * *"\n    prompt: Morning report.\n'
HOURLY = '  - schedule: "0 * * * *"\n    prompt: Hourly.\n'
HOOKS = ("  - webhook: gh\n    prompt: Review what was pushed.\n"
         "  - webhook: alerts\n    session: new\n")


@pytest.fixture
def clock():
    return ManualClock(T0)


@pytest.fixture
def cfg(tmp_path):
    return ControlConfig(data=tmp_path / "data", definitions=tmp_path / "data" / "definitions", rpc_timeout=5.0,
                         timezone="UTC", trigger_grace=600.0)


@pytest.fixture
def client(cfg, clock):
    set_password(cfg, PASSWORD)
    with TestClient(create_app(cfg, clock=clock)) as c:
        assert c.post("/api/login", json={"password": PASSWORD}).status_code == 200
        yield c


@pytest.fixture
def anon(client):
    other = TestClient(client.app)
    other.portal = client.portal
    return other


def seen(client, name="scout", n=1):
    db = control(client).db
    wait(lambda: db.one("SELECT COUNT(*) AS n FROM trigger_state WHERE agent = ?", (name,))["n"] >= n,
         what="the scheduler to see the schedule")


def key_of(client, hook="gh", name="scout"):
    triggers = client.get(f"/api/agents/{name}/triggers").json()["triggers"]
    return next(t for t in triggers if t["webhook"] == hook)["key"]


# ---------- the scheduler ----------

def test_a_run_hours_past_the_grace_window_is_missed_even_if_the_runner_is_up(client, connect, rpc, clock):
    """scheduler.py's rule: after downtime the latest due run "fires if it's within the grace window". _handle only
    looks at the grace window when the runner is offline: if the agent's runner is connected at the first tick after
    a gap (the host slept, the loop stalled), a run four hours late is delivered anyway ("It was due at 08:00")."""
    with connect() as runner:
        start(client, rpc, runner, triggers=DAILY)
        seen(client)
        clock.t = ts(2026, 11, 2, 12, 0)       # 08:00 is four hours (24 grace windows) ago; the runner is up
        wait(lambda: runs(client), what="the 08:00 run handled")
        run = runs(client)[0]
        assert run["scheduled_for"] == ts(2026, 11, 2, 8)
        assert run["outcome"] == "missed", run   # as merged: "firing" (a session.new went to the runner)


def test_a_wakeup_hours_past_the_grace_window_is_missed_even_if_the_runner_is_up(client, connect, rpc, clock):
    """Wake-ups follow the same rules (docs/API.md): one four hours late isn't delivered because the runner is up."""
    with connect() as runner:
        start(client, rpc, runner)
        control(client).db.execute("INSERT INTO wakeups (agent, prompt, due_at, created_at) VALUES (?, ?, ?, ?)",
                                   ("scout", "Check the build.", ts(2026, 11, 2, 8), T0))
        clock.t = ts(2026, 11, 2, 12, 0)
        client.portal.call(control(client).scheduler.tick)
        [run] = runs(client)
        assert run["outcome"] == "missed", run   # as merged: "firing" (a session.new went to the runner)
        assert run["error"] == "Valet was down or asleep until the grace window (10 min) ran out"
        [w] = client.get("/api/agents/scout/wakeups", params={"status": "all"}).json()["wakeups"]
        assert w["status"] == "skipped" and w["run_id"] == run["id"]


def test_the_missed_row_after_downtime_names_a_run_that_was_due(client, clock):
    """After downtime the earlier runs are folded into one `missed` row whose scheduled_for is `latest - 1`: for an
    hourly schedule down from 07:59:30 to 11:03 it says 10:59:59, a time no run was due (the last missed one is
    10:00). The API calls scheduled_for "the run it was"."""
    client.post("/api/agents", json={"name": "scout", "agent_yaml": agent_yaml(triggers=HOURLY)})
    seen(client)
    clock.t = ts(2026, 11, 2, 11, 3)
    client.portal.call(control(client).scheduler.tick)
    missed = next(r for r in runs(client) if r["outcome"] == "missed")
    assert missed["count"] == 3
    assert missed["scheduled_for"] == ts(2026, 11, 2, 10), missed["scheduled_for"]   # as merged: 10:59:59


def test_a_schedule_that_makes_the_main_session_takes_the_prompts_waiting_for_one(client, connect, rpc, clock, pool):
    """defer_to_main queues a prompt with session_id NULL ("for the main session it gets when it next runs") when
    the agent is up but session/new fails. Only pump_agent (the agent reporting `running`, or a lost main session)
    attaches those rows. When a schedule (deliver -> main_session) then makes the main session, they're left out:
    after the schedule's turn ends, Alice's answer is still waiting, and stays so until the agent restarts."""
    with connect() as runner:
        start(client, rpc, runner, triggers=DAILY)
        seen(client)
        c = control(client)
        fut = pool.submit(client.portal.call, c.prompts.defer_to_main, "scout", "Valet: Alice's answer.\n\nOrange",
                          "answer")
        runner.reply(runner.recv("session.new"), error="the harness is busy")   # no main session for now
        assert fut.result(5) is None            # queued for the main session to come
        clock.t = ts(2026, 11, 2, 8, 0, 2)      # the 08:00 schedule makes the main session
        answer_new(runner, "M1")
        first = answer_prompt(runner)           # as merged: the schedule's; Alice's answer isn't queued in M1
        end_turn(runner, first)
        wait(lambda: c.db.one("SELECT COUNT(*) AS n FROM prompt_queue")["n"] == 0,
             what="Alice's answer to go to the main session the schedule made")
        second = answer_prompt(runner)
        assert {text_of(first).rsplit("\n\n", 1)[1], text_of(second).rsplit("\n\n", 1)[1]} == \
            {"Morning report.", "Orange"}


# ---------- webhook keys ----------

def test_a_removed_then_re_added_webhook_gets_a_new_key(client, anon):
    """webhooks.forget ("re-added later, one gets a new key") only runs inside GET /api/agents/<name>/triggers. A
    webhook removed and added back through the file (PUT .../files/agent.yaml, a hand edit, git) without that GET
    in between keeps its old key: a key the owner meant to retire by removing the webhook opens it again. (The fix:
    the scheduler, which looks at every agent.yaml that changes, forgets the keys as it forgets schedules' state.)"""
    client.post("/api/agents", json={"name": "scout", "agent_yaml": agent_yaml(triggers=HOOKS)})
    old = key_of(client)
    assert client.put("/api/agents/scout/files/agent.yaml",
                      json={"text": agent_yaml(triggers="  - webhook: alerts\n")}).status_code == 200
    w = control(client).webhooks
    wait(lambda: w.key("scout", "gh") is None, what="gh's key forgotten")   # as merged: kept until a GET .../triggers
    assert w.key("scout", "alerts") is not None
    assert client.put("/api/agents/scout/files/agent.yaml",
                      json={"text": agent_yaml(triggers=HOOKS)}).status_code == 200
    r = anon.post("/hooks/scout/gh", headers={"Authorization": f"Bearer {old}"})
    assert r.status_code == 404, r.json()      # as merged: 503 "scout is stopped", i.e. the old key got through


def test_two_first_looks_at_a_webhook_agree_on_its_key(client, monkeypatch):
    """Webhooks.ensure is `key() or rotate()`, not atomic: two GET .../triggers at once on a webhook with no key yet
    (the agent page and Settings open together; GET runs in the threadpool) each make one, the second overwrites the
    first, and one response shows a key that never works."""
    client.post("/api/agents", json={"name": "scout", "agent_yaml": agent_yaml(triggers=HOOKS)})
    w = control(client).webhooks
    real_key = webhooks.Webhooks.key

    def key(self, agent, name):
        found = real_key(self, agent, name)
        if found is None and name == "gh" and threading.current_thread().name.startswith("look"):
            time.sleep(0.3)                     # "no key yet": the other look comes in before this one makes one
        return found

    monkeypatch.setattr(webhooks.Webhooks, "key", key)
    got: dict[str, str] = {}
    looks = [threading.Thread(target=lambda n=n: got.__setitem__(n, w.ensure("scout", "gh")), name=f"look{n}")
             for n in range(2)]
    for t in looks:
        t.start()
    for t in looks:
        t.join(10)
    monkeypatch.setattr(webhooks.Webhooks, "key", real_key)
    stored = w.key("scout", "gh")
    assert got[0] == stored and got[1] == stored, "one caller was handed a key that doesn't open the webhook"


# ---------- webhook limits ----------

def test_failed_addresses_are_forgotten_once_their_window_has_passed():
    """Webhooks.fails is a Window keyed by the caller's address, and a key's deque is dropped only when that same
    key is looked at again with nothing left in it. Every address that fails once stays in memory for good, so
    unauthenticated calls from ever-new addresses (an IPv6 /64 has 2^64) grow the control plane without bound."""
    now = [0.0]
    fails = webhooks.Window(webhooks.FAIL_LIMIT, webhooks.FAIL_WINDOW, lambda: now[0])
    for i in range(1000):                       # what the hook route does for each failed call
        ip = f"2001:db8::{i:x}"
        assert fails.full(ip) is None
        fails.add(ip)
    now[0] += webhooks.FAIL_WINDOW + 1          # every one of those failures has aged out
    assert fails.full("198.51.100.7") is None
    fails.add("198.51.100.7")
    assert len(fails._seen) <= 1, f"{len(fails._seen)} addresses still held"


# ---------- the body the agent gets ----------

def test_deeply_nested_json_is_shown_not_a_500():
    """render() catches only ValueError around json.loads; a body nested deeper than the decoder's recursion limit
    (200 KB of `[`/`]`, well under BODY_LIMIT) raises RecursionError, so the hook route answers 500 for a call
    with a good key instead of placing the prompt (with the body shown as text, as for any JSON that doesn't parse)."""
    body = b"[" * 100_000 + b"]" * 100_000
    text = webhooks.render("application/json", body)
    assert text.startswith("[[[[") and text.endswith("more characters)")
