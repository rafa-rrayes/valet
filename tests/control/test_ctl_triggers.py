"""Schedules and wake-ups (control/scheduler.py) on a ManualClock in UTC, with the test playing the runner: when a
run fires, where it goes, what's recorded when it can't, catch-up after downtime, and the routes around them."""
import pytest
from fastapi.testclient import TestClient

from tests.control.always_on import (
    agent_yaml, answer_new, answer_prompt, control, end_turn, fake_runner, hand_edit, runs, start, text_of, ts, wait,
)
from tests.control.conftest import PASSWORD
from valet.control.__main__ import add_runner, set_password
from valet.control.app import create_app
from valet.control.clock import ManualClock
from valet.control.scheduler import Scheduler
from valet.shared import cron
from valet.shared.config import ControlConfig

T0 = ts(2026, 11, 2, 7, 59, 30)        # a Monday
DAILY = '  - schedule: "0 8 * * *"\n    prompt: Morning report.\n'
MINUTELY = '  - schedule: "* * * * *"\n    prompt: Tick.\n'


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


def seen(client, name="scout", n=1):
    """Wait until the scheduler has looked at the agent's schedules (its first run counts from then)."""
    db = control(client).db
    wait(lambda: db.one("SELECT COUNT(*) AS n FROM trigger_state WHERE agent = ?", (name,))["n"] >= n,
         what="the scheduler to see the schedule")


# ---------- firing ----------

def test_a_schedule_prompts_the_main_session(client, connect, rpc, clock):
    with connect() as runner:
        start(client, rpc, runner, triggers=DAILY)
        seen(client)
        a = client.get("/api/agents/scout").json()
        assert a["next_trigger"] == {"at": ts(2026, 11, 2, 8), "kind": "schedule", "trigger": "schedule:0 8 * * *",
                                     "label": "Every day at 08:00"}
        assert a["main_session"] is None and a["queued"] == 0

        clock.t = ts(2026, 11, 2, 8, 0, 2)
        answer_new(runner, "M1")                # the first prompt for the main session makes it
        cmd = answer_prompt(runner)
        assert cmd["session_id"] == "M1"
        assert text_of(cmd) == 'Valet: a scheduled prompt: every day at 08:00 ("0 8 * * *").\n\nMorning report.'
        wait(lambda: runs(client) and runs(client)[0]["outcome"] == "started", what="the run recorded")
        [run] = runs(client)
        assert run == {**run, "agent": "scout", "trigger": "schedule:0 8 * * *", "source": "schedule",
                       "label": "Every day at 08:00", "scheduled_for": ts(2026, 11, 2, 8), "session_id": "M1",
                       "turn_id": cmd["turn_id"], "queue_id": None, "error": None, "count": 1}
        s = client.get("/api/agents/scout/sessions/M1").json()
        assert s["main"] is True and s["state"] == "working" and s["title"] == "Every day at 08:00: Morning report."
        a = client.get("/api/agents/scout").json()
        assert a["main_session"] == "M1" and a["next_trigger"]["at"] == ts(2026, 11, 3, 8)
        [ev] = client.get("/api/agents/scout/sessions/M1/events").json()["events"]
        assert ev["kind"] == "user_message" and ev["data"]["source"] == "schedule"
        assert ev["data"]["label"] == "Every day at 08:00" and ev["data"]["content"]["text"] == text_of(cmd)
        turn = control(client).db.one("SELECT * FROM turns WHERE id = ?", (cmd["turn_id"],))
        assert turn["source"] == "schedule" and turn["prompt"] == text_of(cmd)

        end_turn(runner, cmd)
        wait(lambda: client.get("/api/agents/scout/sessions/M1").json()["state"] == "idle")
        clock.t = ts(2026, 11, 3, 8, 0, 1)      # the next day: the same main session, no session/new
        cmd = answer_prompt(runner)
        assert cmd["session_id"] == "M1"
        wait(lambda: len(runs(client)) == 2)


def test_a_run_due_while_a_turn_runs_is_queued(client, connect, rpc, clock):
    with connect() as runner:
        start(client, rpc, runner, triggers=MINUTELY)
        seen(client)
        clock.t = ts(2026, 11, 2, 8, 0, 1)
        answer_new(runner, "M1")
        first = answer_prompt(runner)
        wait(lambda: runs(client) and runs(client)[0]["outcome"] == "started")
        clock.t = ts(2026, 11, 2, 8, 1, 1)
        wait(lambda: len(runs(client)) == 2, what="the second run")
        run = runs(client)[0]
        assert run["outcome"] == "queued" and run["queue_id"] and run["session_id"] == "M1" and run["turn_id"] is None
        s = client.get("/api/agents/scout/sessions/M1").json()
        assert s["queued"] == 1 and s["queue"][0]["source"] == "schedule" and s["queue"][0]["id"] == run["queue_id"]
        assert client.get("/api/agents/scout").json()["queued"] == 1
        end_turn(runner, first)
        second = answer_prompt(runner)         # sent as soon as the turn ends
        assert second["session_id"] == "M1" and text_of(second).endswith("\n\nTick.")
        assert client.get("/api/agents/scout").json()["queued"] == 0


def test_a_new_session_trigger_makes_one_each_time(client, connect, rpc, clock):
    with connect() as runner:
        start(client, rpc, runner, triggers='  - schedule: "*/30 * * * *"\n    prompt: Look.\n    session: new\n')
        seen(client)
        clock.t = ts(2026, 11, 2, 8, 0, 1)
        answer_new(runner, "N1")
        assert answer_prompt(runner)["session_id"] == "N1"
        clock.t = ts(2026, 11, 2, 8, 30, 1)
        answer_new(runner, "N2")
        assert answer_prompt(runner)["session_id"] == "N2"
        wait(lambda: len(runs(client)) == 2)
        assert client.get("/api/agents/scout").json()["main_session"] is None


def test_a_new_schedule_starts_from_now(client, connect, rpc, clock):
    """No catch-up for runs before Valet first saw the schedule."""
    clock.t = ts(2026, 11, 2, 8, 30)
    with connect() as runner:
        start(client, rpc, runner, triggers=DAILY)
        seen(client)
        assert control(client).db.one("SELECT last FROM trigger_state")["last"] == ts(2026, 11, 2, 8, 30)
        assert client.get("/api/agents/scout").json()["next_trigger"]["at"] == ts(2026, 11, 3, 8)
        assert runs(client) == []


# ---------- when it can't fire ----------

def test_a_stopped_agents_runs_are_skipped_and_folded(client, clock):
    client.post("/api/agents", json={"name": "scout", "agent_yaml": agent_yaml(triggers=MINUTELY)})
    seen(client)
    for minute in range(3):
        clock.t = ts(2026, 11, 2, 8, minute, 1)
        wait(lambda: runs(client) and runs(client)[0]["count"] == minute + 1, what=f"skip {minute + 1}")
    [run] = runs(client)
    assert run["outcome"] == "skipped" and run["error"] == "scout is stopped" and run["count"] == 3
    assert run["scheduled_for"] == ts(2026, 11, 2, 8, 2)


def test_a_run_waits_for_a_runner_within_the_grace_window(client, connect, rpc, clock):
    with connect() as runner:
        start(client, rpc, runner, triggers=DAILY)
        seen(client)
    # The runner is gone; the agent is still meant to run.
    clock.t = ts(2026, 11, 2, 8, 0, 1)
    sched = control(client).scheduler
    client.portal.call(sched.tick)
    assert runs(client) == []                   # waiting for it
    clock.t = ts(2026, 11, 2, 8, 5)
    with connect(agents=[{"agent": "scout", "state": "running"}]) as runner:
        answer_new(runner, "M1")
        cmd = answer_prompt(runner)
        assert text_of(cmd).startswith("Valet: a scheduled prompt: every day at 08:00 (\"0 8 * * *\"). It was due "
                                       "at 2026-11-02 08:00; Valet or you couldn't take it then.\n\nMorning report.")
        wait(lambda: runs(client) and runs(client)[0]["outcome"] == "started")


def test_a_run_past_the_grace_window_is_missed(client, connect, rpc, clock):
    with connect() as runner:
        start(client, rpc, runner, triggers=DAILY)
        seen(client)
    clock.t = ts(2026, 11, 2, 8, 10, 1)        # 10 minutes and a second late
    client.portal.call(control(client).scheduler.tick)
    [run] = runs(client)
    assert run["outcome"] == "missed" and run["scheduled_for"] == ts(2026, 11, 2, 8)
    assert run["error"] == "scout's runner rr is offline until the grace window (10 min) ran out"


def test_after_downtime_only_the_latest_run_catches_up(cfg, rpc, clock):
    """Valet is down from 07:59:30 to 11:03: the 08:00, 09:00 and 10:00 runs are missed (one row), 11:00 still
    fires (it's within the grace window)."""
    set_password(cfg, PASSWORD)
    token = add_runner(cfg, "rr")
    hourly = '  - schedule: "0 * * * *"\n    prompt: Hourly.\n'
    with TestClient(create_app(cfg, clock=clock)) as first:
        assert first.post("/api/login", json={"password": PASSWORD}).status_code == 200
        with fake_runner(first, token) as runner:
            start(first, rpc, runner, triggers=hourly)
            seen(first)
    clock.t = ts(2026, 11, 2, 11, 3)
    with TestClient(create_app(cfg, clock=clock)) as again:
        assert again.post("/api/login", json={"password": PASSWORD}).status_code == 200
        with fake_runner(again, token, agents=[{"agent": "scout", "state": "running"}]) as runner:
            answer_new(runner, "M1")
            cmd = answer_prompt(runner)
            assert "It was due at 2026-11-02 11:00" in text_of(cmd)
            wait(lambda: len(runs(again)) == 2 and runs(again)[0]["outcome"] == "started")
            started, missed = runs(again)
            assert started["scheduled_for"] == ts(2026, 11, 2, 11)
            assert missed["outcome"] == "missed" and missed["count"] == 3
            assert missed["error"] == ("3 earlier runs came due while Valet or the agent was down; only the latest "
                                       "one catches up")


def test_a_run_left_firing_by_a_crash_is_settled_at_startup(cfg, clock):
    set_password(cfg, PASSWORD)
    with TestClient(create_app(cfg, clock=clock)) as first:
        s = control(first).scheduler
        run = s.record("scout", "webhook:h", "webhook:h", "Webhook h", "firing", None)
    with TestClient(create_app(cfg, clock=clock)) as again:
        assert again.post("/api/login", json={"password": PASSWORD}).status_code == 200
        row = control(again).db.one("SELECT * FROM trigger_runs WHERE id = ?", (run,))
        assert row["outcome"] == "failed" and row["error"] == "Valet stopped while delivering it"


def test_a_definition_that_stops_validating_skips_its_runs(client, connect, rpc, clock):
    with connect() as runner:
        start(client, rpc, runner, triggers=MINUTELY)
        seen(client)
        hand_edit(client, agent_yaml(triggers=MINUTELY) + "nonsense: true\n")
        clock.t = ts(2026, 11, 2, 8, 0, 1)
        wait(lambda: runs(client), what="a run")
        [run] = runs(client)
        assert run["outcome"] == "skipped" and run["error"].startswith("its agent.yaml doesn't validate")
        # Fixed again: the next run fires.
        assert client.put("/api/agents/scout/files/agent.yaml",
                          json={"text": agent_yaml(triggers=MINUTELY)}).status_code == 200
        clock.t = ts(2026, 11, 2, 8, 1, 1)
        answer_new(runner, "M1")
        answer_prompt(runner)
        wait(lambda: runs(client)[0]["outcome"] == "started")


def test_a_removed_schedule_is_forgotten(client, connect, rpc, clock):
    with connect() as runner:
        start(client, rpc, runner, triggers=DAILY)
        seen(client)
        assert client.put("/api/agents/scout/files/agent.yaml", json={"text": agent_yaml()}).status_code == 200
        db = control(client).db
        wait(lambda: db.one("SELECT COUNT(*) AS n FROM trigger_state")["n"] == 0, what="state dropped")
        assert client.get("/api/agents/scout").json()["next_trigger"] is None


# ---------- routes ----------

def test_triggers_route_describes_each_trigger(client, clock):
    yaml = agent_yaml(triggers=DAILY + '  - webhook: gh\n    prompt: A push.\n    session: new\n')
    client.post("/api/agents", json={"name": "scout", "agent_yaml": yaml})
    body = client.get("/api/agents/scout/triggers").json()
    assert body["timezone"] == "UTC" and body["error"] is None and body["wakeups"] == []
    sched, hook = body["triggers"]
    assert sched == {"trigger": "schedule:0 8 * * *", "kind": "schedule", "schedule": "0 8 * * *", "webhook": None,
                     "prompt": "Morning report.", "session": "main", "last_run": None,
                     "description": "Every day at 08:00",
                     "next": [ts(2026, 11, 2, 8), ts(2026, 11, 3, 8), ts(2026, 11, 4, 8)]}
    assert hook["kind"] == "webhook" and hook["session"] == "new" and hook["url"].endswith("/hooks/scout/gh")
    assert hook["key"].startswith("vhk_") and hook["key_error"] is None and hook["key_created_at"] == T0
    assert client.get("/api/agents/scout/triggers").json()["triggers"][1]["key"] == hook["key"]   # made once
    assert client.get("/api/agents/ghost/triggers").status_code == 404

    hand_edit(client, yaml + "nonsense: true\n")
    broken = client.get("/api/agents/scout/triggers").json()
    assert broken["triggers"] == [] and broken["error"]


def test_preview_route(client):
    r = client.get("/api/triggers/preview", params={"schedule": " 30  9 * *   1-5 ", "count": 2})
    assert r.json() == {"schedule": "30 9 * * 1-5", "description": "Weekdays at 09:30",
                        "next": [ts(2026, 11, 2, 9, 30), ts(2026, 11, 3, 9, 30)], "timezone": "UTC"}
    r = client.get("/api/triggers/preview", params={"schedule": "61 * * * *"})
    assert r.status_code == 422 and "minute" in r.json()["detail"]
    assert client.get("/api/triggers/preview", params={"schedule": "* * * * *", "count": 99}).status_code == 422
    assert client.get("/api/triggers/preview").status_code == 422


def test_trigger_runs_route_pages(client, clock):
    client.post("/api/agents", json={"name": "scout", "agent_yaml": agent_yaml()})
    s = control(client).scheduler
    ids = [s.record("scout", f"webhook:h{i}", f"webhook:h{i}", f"Webhook h{i}", "skipped", None, error="x")
           for i in range(5)]
    page = client.get("/api/agents/scout/trigger-runs", params={"limit": 2}).json()
    assert [r["id"] for r in page["runs"]] == ids[:2:-1] and page["more"] is True
    rest = client.get("/api/agents/scout/trigger-runs", params={"before": ids[3]}).json()
    assert [r["id"] for r in rest["runs"]] == ids[2::-1] and rest["more"] is False
    one = client.get("/api/agents/scout/trigger-runs", params={"trigger": "webhook:h1"}).json()["runs"]
    assert [r["id"] for r in one] == [ids[1]]
    assert client.get("/api/agents/ghost/trigger-runs").status_code == 404


def test_trigger_runs_are_capped_per_agent(client, monkeypatch):
    from valet.control import scheduler
    monkeypatch.setattr(scheduler, "RUNS_KEPT", 3)
    client.post("/api/agents", json={"name": "scout", "agent_yaml": agent_yaml()})
    s = control(client).scheduler
    ids = [s.record("scout", "webhook:h", "webhook:h", "W", "started", None) for _ in range(5)]
    assert [r["id"] for r in runs(client)] == ids[:1:-1]


# ---------- wake-ups ----------

def call(client, pool, agent, tool, args):
    """A valet.owner/valet.agents call through the gateway, as the agent would make it, in another thread (it may
    wait on the test's runner)."""
    c = control(client)
    return pool.submit(client.portal.call, c.gateway.call, agent, tool, args)


def test_a_wakeup_fires_in_the_session_that_set_it(client, connect, rpc, clock, pool):
    from tests.control.always_on import session
    with connect() as runner:
        start(client, rpc, runner)
        session(client, rpc, runner, "S1")
        r, cmd = rpc(runner, client.post, "/api/agents/scout/sessions/S1/prompt", json={"text": "remind me"},
                     expect="session.prompt")
        result = call(client, pool, "scout", "valet_owner__schedule_wakeup",
                      {"prompt": "Check the build.", "in_minutes": 30}).result(5)
        assert not result.is_error, result
        assert result.structured_content == {"id": 1, "at": "2026-11-02T08:29+00:00", "prompt": "Check the build.",
                                             "session": "this session"}
        a = client.get("/api/agents/scout").json()
        assert a["next_trigger"] == {"at": T0 + 1800, "kind": "wakeup", "trigger": "wakeup:1", "label": "Wake-up"}
        end_turn(runner, cmd)
        wait(lambda: client.get("/api/agents/scout/sessions/S1").json()["state"] == "idle")

        clock.t = T0 + 1800
        cmd = answer_prompt(runner)
        assert cmd["session_id"] == "S1"
        assert text_of(cmd) == ("Valet: the wake-up you scheduled at 2026-11-02 07:59 for 2026-11-02 08:29.\n\n"
                                "Check the build.")
        wait(lambda: client.get("/api/agents/scout/wakeups", params={"status": "all"}).json()["wakeups"][0]["status"]
             == "fired")
        [run] = runs(client)
        assert run["trigger"] == "wakeup:1" and run["source"] == "wakeup" and run["outcome"] == "started"
        assert client.get("/api/agents/scout/wakeups").json() == {"wakeups": []}


def test_wakeup_validation_and_cancel(client, connect, rpc, clock, pool):
    with connect() as runner:
        start(client, rpc, runner)
        bad = [({"prompt": "x"}, "exactly one of"), ({"prompt": "x", "in_minutes": 0}, "greater than 0"),
               ({"prompt": "x", "at": "yesterday"}, "isn't an ISO-8601 time"),
               ({"prompt": "x", "at": "2026-11-01T09:00"}, "is in the past"),
               ({"prompt": "x", "in_minutes": 10 ** 7}, "at most a year ahead"),
               ({"prompt": " ", "in_minutes": 5}, "needs `prompt`")]
        for args, says in bad:
            result = call(client, pool, "scout", "valet_owner__schedule_wakeup", args).result(5)
            assert result.is_error and says in result.content[0].text, (args, result)
        ok = call(client, pool, "scout", "valet_owner__schedule_wakeup",
                  {"prompt": "Later.", "at": "2026-11-02T10:00"}).result(5)
        assert ok.structured_content["session"] == "main"            # no turn running: the main session
        listed = call(client, pool, "scout", "valet_owner__wakeups", {}).result(5).structured_content
        assert listed == {"timezone": "UTC", "wakeups": [{"id": 1, "at": "2026-11-02T10:00+00:00",
                                                          "prompt": "Later.", "session": "main"}]}
        assert call(client, pool, "scout", "valet_owner__cancel_wakeup", {"id": 9}).result(5).is_error
        assert not call(client, pool, "scout", "valet_owner__cancel_wakeup", {"id": 1}).result(5).is_error
        assert call(client, pool, "scout", "valet_owner__wakeups", {}).result(5).structured_content["wakeups"] == []

        call(client, pool, "scout", "valet_owner__schedule_wakeup", {"prompt": "Again.", "in_minutes": 5}).result(5)
        assert client.delete("/api/agents/scout/wakeups/2").json()["status"] == "cancelled"
        assert client.delete("/api/agents/scout/wakeups/2").status_code == 409
        assert client.delete("/api/agents/scout/wakeups/99").status_code == 404
        clock.t = T0 + 600
        client.portal.call(control(client).scheduler.tick)
        assert runs(client) == []


def test_a_stopped_agents_wakeup_is_skipped(client, connect, rpc, clock, pool):
    with connect() as runner:
        start(client, rpc, runner)
        call(client, pool, "scout", "valet_owner__schedule_wakeup", {"prompt": "Later.", "in_minutes": 1}).result(5)
        r, _ = rpc(runner, client.post, "/api/agents/scout/stop", expect="agent.stop")
        clock.t = T0 + 61
        wait(lambda: runs(client), what="the wake-up handled")
        [run] = runs(client)
        assert run["outcome"] == "skipped" and run["error"] == "scout is stopped"
        [w] = client.get("/api/agents/scout/wakeups", params={"status": "all"}).json()["wakeups"]
        assert w["status"] == "skipped" and w["run_id"] == run["id"]


# ---------- the scheduler's arithmetic ----------

def test_due_counts_runs_and_caps_long_downtime(cfg):
    class C:
        pass
    c = C()
    c.cfg = cfg
    s = Scheduler(c)
    hourly = cron.parse("0 * * * *")
    assert s._due(hourly, ts(2026, 11, 2, 7, 59), ts(2026, 11, 2, 7, 59, 59)) == (0, None, None)
    assert s._due(hourly, ts(2026, 11, 2, 7, 59), ts(2026, 11, 2, 8)) == (0, None, ts(2026, 11, 2, 8))
    assert s._due(hourly, ts(2026, 11, 2, 7, 59), ts(2026, 11, 2, 10, 30)) == (
        2, ts(2026, 11, 2, 9), ts(2026, 11, 2, 10))
    minutely = cron.parse("* * * * *")
    before, prev, latest = s._due(minutely, ts(2026, 1, 1), ts(2026, 11, 2, 8, 0, 30))
    assert before == 1000 and prev == ts(2026, 11, 2, 7, 59) and latest == ts(2026, 11, 2, 8)
