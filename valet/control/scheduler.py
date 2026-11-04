"""Triggers that come due: schedules (`triggers: [{schedule: "0 8 * * *", prompt: ...}]` in agent.yaml) and the
wake-ups agents set for themselves (valet.owner), plus the record of every trigger run (table trigger_runs, which
webhooks write too, through fire()).

The rules (README, "Always-on"):
- A trigger fires only for an agent whose desired state is running. A run that comes due while the agent can't take
  a prompt (starting, failed, its runner offline) waits for it, up to the grace window (VALET_TRIGGER_GRACE, 10
  minutes); after that it's recorded as missed, with the reason. A stopped agent's runs are recorded as skipped.
- After downtime (the control plane's or the agent's) only the latest run that came due counts: it fires if it's
  within the grace window; the ones before it are recorded once, as missed. So a schedule catches up at most once.
- Every run handled is a trigger_runs row: started, queued (a turn was running), skipped, missed or failed.
  Consecutive identical skips and misses fold into one row with a count, and an agent keeps its latest RUNS_KEPT.
- At most once: the run is recorded (and the schedule's state moved past it) before it's delivered, so a crash in
  between loses that run (the row says so) rather than sending it twice.

Schedules are read in VALET_TZ, else the control plane's own zone (shared/cron.py). The loop wakes when the next
run is due, at least every MAX_SLEEP, and whenever an agent changes (poke())."""
import asyncio
import contextlib
import logging
import os
from datetime import UTC, datetime
from typing import Any

from valet.shared import cron
from valet.shared.definition import AGENT_FILE, AgentDefinition, DefinitionError, Trigger, agent_dir, parse_agent

from .prompts import MAIN, NEW, Delivery, DeliveryError, header

log = logging.getLogger(__name__)

MAX_SLEEP = 30.0            # seconds the loop sleeps at most
MIN_SLEEP = 0.05
RUNS_KEPT = 500             # trigger_runs rows kept per agent
CAP = 1000                  # missed runs counted one by one after downtime; more is "more than CAP"
LATE = 60.0                 # seconds late after which a run's prompt says when it was due


def iso(ts: float, tz) -> str:
    return datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d %H:%M")


class Scheduler:
    def __init__(self, control: Any) -> None:
        self.control = control
        self.tz = cron.zone(control.cfg.timezone or None)
        self._wake: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._defs: dict[str, tuple[tuple[int, int], AgentDefinition | None, str | None]] = {}
        self._known: dict[str, list[Trigger]] = {}  # each agent's schedules from its last valid agent.yaml
        self._ticking = asyncio.Lock()              # one tick at a time: a run is handled once

    @property
    def db(self):
        return self.control.db

    @property
    def clock(self):
        return self.control.clock

    @property
    def prompts(self):
        return self.control.prompts

    @property
    def grace(self) -> float:
        return self.control.cfg.trigger_grace

    # ---------- the loop ----------

    async def start(self) -> None:
        """For the app's lifespan: runs left `firing` by a crash are settled, then the loop starts."""
        now = self.clock.now()
        self.db.execute("UPDATE trigger_runs SET outcome = 'failed', error = 'Valet stopped while delivering it' "
                        "WHERE outcome = 'firing'")
        self.db.execute("UPDATE wakeups SET status = 'skipped', done_at = ? WHERE status = 'firing'", (now,))
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="valet-scheduler")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    def poke(self) -> None:
        """Look again now (an agent's definition or state changed). Safe from any thread."""
        if self._loop is None or self._wake is None or self._loop.is_closed():
            return
        try:
            if asyncio.get_running_loop() is self._loop:
                self._wake.set()
                return
        except RuntimeError:
            pass
        self._loop.call_soon_threadsafe(self._wake.set)

    async def _run(self) -> None:
        while True:
            self._wake.clear()
            try:
                upcoming = await self.tick()
            except Exception:
                log.exception("scheduler: a tick failed")
                upcoming = None
            delay = MAX_SLEEP if upcoming is None else min(MAX_SLEEP, max(MIN_SLEEP, upcoming - self.clock.now()))
            await self.clock.sleep(delay, self._wake)

    async def tick(self) -> float | None:
        """Handle every run due by now; return when the next one is due (None: nothing scheduled)."""
        async with self._ticking:
            return await self._tick()

    @contextlib.asynccontextmanager
    async def paused(self):
        """No tick runs inside: a deleted agent's triggers and wake-ups go with none of them firing halfway."""
        async with self._ticking:
            yield

    def forget(self, agent: str) -> None:
        """A deleted agent: its definition and schedules as last read go (an agent made again with its name starts
        afresh)."""
        self._defs.pop(agent, None)
        self._known.pop(agent, None)

    async def _tick(self) -> float | None:
        now = self.clock.now()
        upcoming: list[float] = []
        for agent in self.control.defs.names():
            try:
                nxt = await self._agent(agent, now)
            except Exception:
                log.exception("scheduler: %s's schedules", agent)
                continue
            if nxt is not None:
                upcoming.append(nxt)
        nxt = await self._wakeups(now)
        if nxt is not None:
            upcoming.append(nxt)
        return min(upcoming) if upcoming else None

    # ---------- definitions ----------

    def definition(self, agent: str) -> tuple[AgentDefinition | None, str | None]:
        """(definition, None), or (None, why it doesn't validate). Re-read only when agent.yaml changed."""
        path = agent_dir(self.control.defs.root, agent) / AGENT_FILE
        try:
            st = os.stat(path)
        except OSError:
            self._defs.pop(agent, None)
            return None, None
        stamp = (st.st_mtime_ns, st.st_size)
        cached = self._defs.get(agent)
        if cached and cached[0] == stamp:
            return cached[1], cached[2]
        try:
            d, err = parse_agent(path.read_text(), agent), None
        except (DefinitionError, OSError) as e:
            d, err = None, str(e)
        self._defs[agent] = (stamp, d, err)
        return d, err

    def schedules(self, agent: str) -> tuple[list[Trigger], str | None]:
        """Its schedules, and why they can't fire if its agent.yaml doesn't validate now (the schedules are then
        the ones it had when it last did, so their runs are recorded as skipped instead of forgotten)."""
        d, err = self.definition(agent)
        if d is not None:
            self._known[agent] = [t for t in d.triggers if t.schedule]
            return self._known[agent], None
        if err is None:
            self._known.pop(agent, None)
            return [], None
        return self._known.get(agent, []), f"its agent.yaml doesn't validate ({err})"

    # ---------- schedules ----------

    async def _agent(self, agent: str, now: float) -> float | None:
        triggers, invalid = self.schedules(agent)
        keys = [t.key for t in triggers]
        if invalid is None:     # forget schedules no longer in the definition (re-added later, they start afresh)
            marks = ",".join("?" * len(keys))
            self.db.execute(f"DELETE FROM trigger_state WHERE agent = ? AND trigger NOT IN ({marks})" if keys
                            else "DELETE FROM trigger_state WHERE agent = ?", (agent, *keys))
            d, _ = self.definition(agent)   # and the keys of webhooks it no longer has (re-added, one gets a new key)
            self.control.webhooks.forget(agent, [t.webhook for t in (d.triggers if d else []) if t.webhook])
        upcoming = []
        for t in triggers:
            nxt = await self._schedule(agent, t, now, invalid)
            if nxt is not None:
                upcoming.append(nxt)
        return min(upcoming) if upcoming else None

    def _due(self, c: cron.Cron, last: float, now: float) -> tuple[int, float | None, float | None]:
        """Runs of `c` after `last` up to `now`: (how many came before the latest, the last of those, the latest)."""
        n, prev, latest, t = 0, None, None, last
        while (nx := c.next_after(_dt(t), self.tz)) is not None and (ts := nx.timestamp()) <= now:
            n, prev, latest, t = n + 1, latest, ts, ts
            if n >= CAP:                    # long downtime: jump to the grace window for the latest
                t = max(t, now - self.grace - 60)
                while (nx := c.next_after(_dt(t), self.tz)) is not None and (ts := nx.timestamp()) <= now:
                    prev, latest, t = latest, ts, ts
                return CAP, prev, latest
        return max(0, n - 1), prev, latest

    async def _schedule(self, agent: str, t: Trigger, now: float, invalid: str | None) -> float | None:
        c = cron.parse(t.schedule)
        state = self.db.one("SELECT last FROM trigger_state WHERE agent = ? AND trigger = ?", (agent, t.key))
        if state is None:                   # new: its first run is the next one from now
            self.db.execute("INSERT OR IGNORE INTO trigger_state (agent, trigger, last) VALUES (?, ?, ?)",
                            (agent, t.key, now))
            nx = c.next_after(_dt(now), self.tz)
            return nx.timestamp() if nx else None
        before, prev, latest = self._due(c, state["last"], now)
        label = c.describe()
        if latest is not None:
            if before:
                count = f"more than {CAP - 1}" if before >= CAP else str(before)
                self.record(agent, t.key, "schedule", label, "missed", prev, count=before,
                            error=f"{count} earlier run{'s' if before != 1 else ''} came due while Valet or the agent "
                                  "was down; only the latest one catches up")
                self._set_last(agent, t.key, latest - 1)
            if not await self._handle(agent, t, c, label, latest, now, invalid):
                return now + 5.0            # waiting for the agent: look again soon (a poke comes sooner)
        nx = c.next_after(_dt(max(now, latest or 0)), self.tz)
        return nx.timestamp() if nx else None

    def _set_last(self, agent: str, key: str, last: float) -> None:
        self.db.execute("UPDATE trigger_state SET last = ? WHERE agent = ? AND trigger = ?", (last, agent, key))

    async def _handle(self, agent: str, t: Trigger, c: cron.Cron, label: str, due: float, now: float,
                      invalid: str | None) -> bool:
        """A schedule's run that is due. False while it waits for the agent (it stays due)."""
        reason = invalid or self.skip_reason(agent)
        if reason is not None:
            self._set_last(agent, t.key, due)
            self.record(agent, t.key, "schedule", label, "skipped", due, error=reason)
            return True
        offline = self.prompts.runner_of(agent) is None
        if now - due <= self.grace:
            if offline:
                return False
        else:                               # missed even if the runner is up (_late)
            self._set_last(agent, t.key, due)
            self.record(agent, t.key, "schedule", label, "missed", due, error=self._late(agent, offline))
            return True
        when = ""
        if now - due > LATE:
            when = f" It was due at {iso(due, self.tz)}; Valet or you couldn't take it then."
        text = header(f'a scheduled prompt: {label.lower()} ("{t.schedule}").{when}', t.prompt or "")
        run = self.record(agent, t.key, "schedule", label, "firing", due)
        self._set_last(agent, t.key, due)
        await self.fire(run, agent, t.session, text, "schedule", label, title=f"{label}: {t.prompt}")
        return True

    def _late(self, agent: str, offline: bool) -> str:
        """Why a run past the grace window is missed. With its runner up, Valet itself was down or asleep that long
        (the host slept, the loop stalled) and the runner was back before the scheduler looked: still missed."""
        why = self.prompts.why_not(agent) if offline else "Valet was down or asleep"
        return f"{why} until the grace window ({self.grace / 60:g} min) ran out"

    def skip_reason(self, agent: str) -> str | None:
        """Why a trigger never fires for this agent now (it's stopped), else None."""
        row = self.db.one("SELECT desired FROM agents WHERE name = ?", (agent,))
        return None if row and row["desired"] == "running" else f"{agent} is stopped"

    # ---------- wake-ups ----------

    async def _wakeups(self, now: float) -> float | None:
        for w in self.db.query("SELECT * FROM wakeups WHERE status = 'pending' AND due_at <= ? ORDER BY due_at, id",
                               (now,)):
            try:
                await self._wakeup(w, now)
            except Exception:
                log.exception("scheduler: wake-up %s", w["id"])
        row = self.db.one("SELECT MIN(due_at) AS at FROM wakeups WHERE status = 'pending' AND due_at > ?", (now,))
        waiting = self.db.one("SELECT 1 FROM wakeups WHERE status = 'pending' AND due_at <= ? LIMIT 1", (now,))
        nxt = row["at"] if row and row["at"] is not None else None
        return now + 5.0 if waiting else nxt

    async def _wakeup(self, w: dict, now: float) -> None:
        agent, key, label = w["agent"], f"wakeup:{w['id']}", "Wake-up"
        if not self.control.defs.exists(agent):
            self._wakeup_done(w, "skipped", None)
            return
        reason, outcome = self.skip_reason(agent), "skipped"
        if reason is None:
            offline = self.prompts.runner_of(agent) is None
            if now - w["due_at"] <= self.grace:
                if offline:
                    return
            else:
                reason, outcome = self._late(agent, offline), "missed"
        if reason is not None:
            run = self.record(agent, key, "wakeup", label, outcome, w["due_at"], error=reason, fold=False)
            self._wakeup_done(w, "skipped", run)
            return
        run = self.record(agent, key, "wakeup", label, "firing", w["due_at"], fold=False)
        self.db.execute("UPDATE wakeups SET status = 'firing', run_id = ? WHERE id = ?", (run, w["id"]))
        late = f" (it was due at {iso(w['due_at'], self.tz)})" if now - w["due_at"] > LATE else ""
        set_at = iso(w["created_at"], self.tz)
        text = header(f"the wake-up you scheduled at {set_at} for {iso(w['due_at'], self.tz)}{late}.", w["prompt"])
        target = w["session_id"] if w["session_id"] and self.db.one(
            "SELECT 1 FROM sessions WHERE agent = ? AND id = ?", (agent, w["session_id"])) else MAIN
        await self.fire(run, agent, target, text, "wakeup", label, title=f"Wake-up: {w['prompt']}")
        self._wakeup_done(w, "fired", run)

    def _wakeup_done(self, w: dict, status: str, run: int | None) -> None:
        self.db.execute("UPDATE wakeups SET status = ?, done_at = ?, run_id = COALESCE(?, run_id) WHERE id = ?",
                        (status, self.clock.now(), run, w["id"]))

    # ---------- firing and the record ----------

    async def fire(self, run: int, agent: str, target: str, text: str, source: str, label: str,
                   title: str | None = None) -> dict:
        """Deliver a trigger's prompt (target: MAIN, NEW or a session id) and complete its run row (made `firing`
        by record()). Returns the row."""
        target = NEW if target == "new" else MAIN if target == "main" else target
        try:
            d: Delivery = await self.prompts.deliver(agent, target, text, source, label, title=title)
        except DeliveryError as e:
            outcome = "skipped" if e.status == 503 else "failed"
            self.db.execute("UPDATE trigger_runs SET outcome = ?, error = ?, fired_at = ? WHERE id = ?",
                            (outcome, str(e), self.clock.now(), run))
        else:
            self.db.execute("UPDATE trigger_runs SET outcome = ?, error = ?, session_id = ?, turn_id = ?, "
                            "queue_id = ?, fired_at = ? WHERE id = ?",
                            (d.outcome, d.error, d.session_id, d.turn_id, d.queue_id, self.clock.now(), run))
        await self.control.hub.emit_agent(agent)
        return self.run_view(self.db.one("SELECT * FROM trigger_runs WHERE id = ?", (run,)))

    def record(self, agent: str, trigger: str, source: str, label: str | None, outcome: str,
               scheduled_for: float | None, *, error: str | None = None, count: int = 1, fold: bool = True) -> int:
        """A trigger_runs row; a skip or miss just like the agent's previous row for that trigger adds to its count
        instead. Returns the row's id."""
        now = self.clock.now()
        if fold and outcome in ("skipped", "missed"):
            prev = self.db.one("SELECT id, outcome, error FROM trigger_runs WHERE agent = ? AND trigger = ? "
                               "ORDER BY id DESC LIMIT 1", (agent, trigger))
            if prev and prev["outcome"] == outcome and prev["error"] == error:
                self.db.execute("UPDATE trigger_runs SET count = count + ?, fired_at = ?, scheduled_for = ? "
                                "WHERE id = ?", (count, now, scheduled_for, prev["id"]))
                return prev["id"]
        cur = self.db.execute(
            "INSERT INTO trigger_runs (agent, trigger, source, label, scheduled_for, fired_at, outcome, error, count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (agent, trigger, source, label, scheduled_for, now, outcome, error,
                                                   count))
        self.db.execute("DELETE FROM trigger_runs WHERE agent = ? AND id <= (SELECT id FROM trigger_runs "
                        "WHERE agent = ? ORDER BY id DESC LIMIT 1 OFFSET ?)", (agent, agent, RUNS_KEPT))
        if outcome != "firing":
            log.info("trigger %s %s: %s%s", agent, trigger, outcome, f" ({error})" if error else "")
        return cur.lastrowid

    @staticmethod
    def run_view(row: dict) -> dict:
        """A TriggerRun (docs/API.md)."""
        return {k: row[k] for k in ("id", "agent", "trigger", "source", "label", "scheduled_for", "fired_at", "outcome",
                                    "session_id", "turn_id", "queue_id", "error", "count")}

    # ---------- for the views ----------

    def next_trigger(self, agent: str, d: AgentDefinition | None) -> dict | None:
        """Agent.next_trigger: the soonest of its schedules' next runs and its pending wake-ups."""
        now = self.clock.now()
        best: dict | None = None
        for t in (d.triggers if d else []):
            if not t.schedule:
                continue
            c = cron.parse(t.schedule)
            nx = c.next_after(_dt(now), self.tz)
            if nx and (best is None or nx.timestamp() < best["at"]):
                best = {"at": nx.timestamp(), "kind": "schedule", "trigger": t.key, "label": c.describe()}
        w = self.db.one("SELECT * FROM wakeups WHERE agent = ? AND status = 'pending' ORDER BY due_at, id LIMIT 1",
                        (agent,))
        if w and (best is None or w["due_at"] < best["at"]):
            best = {"at": w["due_at"], "kind": "wakeup", "trigger": f"wakeup:{w['id']}", "label": "Wake-up"}
        return best

    def upcoming(self, expr: str, count: int = 3) -> list[float]:
        return [t.timestamp() for t in cron.parse(expr).upcoming(_dt(self.clock.now()), self.tz, count)]


def _dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, UTC)
