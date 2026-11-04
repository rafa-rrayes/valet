import Foundation
import Testing
@testable import ValetKit

@MainActor
@Suite("Always-on: triggers, wake-ups, Retry and the queue")
struct TriggerTests {
    nonisolated static let triggers = #"""
    {"agent": "scout", "timezone": "Europe/Lisbon",
     "triggers": [
       {"trigger": "schedule:0 8 * * *", "kind": "schedule", "schedule": "0 8 * * *", "webhook": null,
        "prompt": "Check the news", "session": "main", "description": "Every day at 08:00",
        "last_run": {"id": 4, "agent": "scout", "trigger": "schedule:0 8 * * *", "source": "schedule",
                     "label": "Every day at 08:00", "scheduled_for": 1000.0, "fired_at": 1001.5, "outcome": "skipped",
                     "session_id": null, "turn_id": null, "queue_id": null, "error": "the agent was stopped", "count": 3},
        "next": [2000.0, 3000.0, 4000.0]},
       {"trigger": "webhook:deploy", "kind": "webhook", "schedule": null, "webhook": "deploy", "prompt": null,
        "session": "new", "description": "When POST /hooks/scout/deploy is called", "last_run": null,
        "url": "https://valet.example/hooks/scout/deploy", "key": "vhk_abcdefghijkl", "key_error": null,
        "key_created_at": 500.0, "later": true},
       {"kind": "schedule"}],
     "wakeups": [{"id": 9, "agent": "scout", "session_id": null, "prompt": "Look again", "due_at": 1500.0,
                  "status": "pending", "created_at": 900.0, "done_at": null, "run_id": null}, {"no": "id"}],
     "next_trigger": {"at": 1500.0, "kind": "wakeup", "trigger": "wakeup:9", "label": "Wake-up"},
     "error": null}
    """#

    func signedIn(_ stub: Stub) -> AppModel {
        let defaults = UserDefaults(suiteName: "valet-tests-\(UUID().uuidString)")!
        defaults.set("http://127.0.0.1:9", forKey: "server")
        return AppModel(store: MemoryTokenStore(["token": "vown_t"]), defaults: defaults, transport: FakeTransport(),
                        urlSession: stub.session)
    }

    // MARK: shapes

    @Test func theTriggersShape() throws {
        let t = try decode(AgentTriggers.self, Self.triggers)
        #expect(t.timezone == "Europe/Lisbon" && t.error == nil)
        #expect(t.triggers.map(\.id) == ["schedule:0 8 * * *", "webhook:deploy", ""])
        let s = t.triggers[0]
        #expect(!s.isWebhook && s.schedule == "0 8 * * *" && s.prompt == "Check the news" && s.session == "main")
        #expect(s.next == [2000, 3000, 4000] && s.description == "Every day at 08:00")
        let r = try #require(s.lastRun)
        #expect(r.outcome == "skipped" && r.bad && r.count == 3 && r.when == 1000 && r.error == "the agent was stopped")
        let h = t.triggers[1]
        #expect(h.isWebhook && h.webhook == "deploy" && h.session == "new" && h.prompt == nil)
        #expect(h.url == "https://valet.example/hooks/scout/deploy" && h.key == "vhk_abcdefghijkl" && h.keyCreatedAt == 500)
        #expect(t.wakeups.map(\.id) == [9])
        #expect(t.wakeups[0].sessionId == nil && t.wakeups[0].dueAt == 1500 && t.wakeups[0].status == "pending")
        #expect(t.next == NextTrigger(at: 1500, kind: "wakeup", trigger: "wakeup:9", label: "Wake-up"))
        #expect(try decode(AgentTriggers.self, #"{"triggers": [], "error": "triggers.0: bad"}"#).error == "triggers.0: bad")
    }

    @Test func runsAndTheirWords() throws {
        let run = try decode(TriggerRun.self, #"{"id": 1, "trigger": "webhook:deploy", "source": "webhook:deploy", "label": "Webhook deploy", "scheduled_for": null, "fired_at": 50.0, "outcome": "started", "session_id": "s1", "turn_id": "t1", "count": 1}"#)
        #expect(run.when == 50 && run.outcomeWord == "ran" && !run.bad && run.sessionId == "s1")
        #expect(TriggerRun(id: 2, outcome: "firing").outcomeWord == "sending")
        #expect(TriggerRun(id: 3, outcome: "missed").bad)
        #expect(TriggerRun(id: 4, outcome: "queued").lastLine == "Last: queued")
        let page = try decode(TriggerRunPage.self, #"{"runs": [{"id": 5, "outcome": "failed"}, {"outcome": "x"}], "more": true}"#)
        #expect(page.runs.map(\.id) == [5] && page.more)
    }

    @Test func whenAMomentIs() {
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = TimeZone.current
        let now = cal.date(from: DateComponents(year: 2026, month: 11, day: 2, hour: 12))!     // a Monday
        func at(_ day: Int, _ hour: Int) -> Double {
            cal.date(from: DateComponents(year: 2026, month: 11, day: day, hour: hour))!.timeIntervalSince1970
        }
        #expect(whenText(at(2, 8), now: now, calendar: cal) == "today 08:00")
        #expect(whenText(at(3, 8), now: now, calendar: cal) == "tomorrow 08:00")
        #expect(whenText(at(1, 20), now: now, calendar: cal) == "yesterday 20:00")
        #expect(whenText(at(5, 9), now: now, calendar: cal).hasSuffix(" 09:00"))
        #expect(whenText(at(5, 9), now: now, calendar: cal) != "today 09:00")
        #expect(zoneNote("Europe/Lisbon", here: "Europe/Lisbon") == "Schedules follow Europe/Lisbon time.")
        #expect(zoneNote("Europe/Lisbon", here: "America/Sao_Paulo")
                == "Schedules follow Europe/Lisbon time; the times here are this device's.")
    }

    @Test func theAgentAndTheSession() throws {
        let a = try decode(Agent.self, #"{"name": "scout", "desired": "running", "queued": 2, "main_session": "m1", "next_trigger": {"at": 1500.0, "kind": "wakeup", "trigger": "wakeup:9", "label": "Wake-up"}}"#)
        #expect(a.mainSession == "m1" && a.queuedCount == 2)
        #expect(a.nextTriggerLine?.hasPrefix("Next: a wake-up it set, ") == true)
        let b = try decode(Agent.self, #"{"name": "b", "next_trigger": {"at": 1500.0, "kind": "schedule", "label": "Every day at 08:00"}}"#)
        #expect(b.nextTriggerLine?.hasPrefix("Next: Every day at 08:00, ") == true)
        #expect(try decode(Agent.self, #"{"name": "c", "main_session": null}"#).mainSession == nil)
        #expect(try decode(Session.self, #"{"agent": "a", "id": "s", "main": true}"#).isMain)
        #expect(try !decode(Session.self, #"{"agent": "a", "id": "s"}"#).isMain)
    }

    @Test func whereAPromptCameFrom() {
        #expect(Event.sourceLabel("schedule", label: "Every day at 08:00") == "Schedule · Every day at 08:00")
        #expect(Event.sourceLabel("owner", label: "You", retry: true) == "Sent again")
        #expect(Event.sourceLabel("webhook:deploy", label: "Webhook deploy", retry: true) == "Webhook deploy · Sent again")
        #expect(Event.sourceLabel("owner", label: "You") == nil)
        let e = ev(3, "user_message", ["content": ["type": "text", "text": "x"], "source": "owner", "retry_of": "t1"])
        #expect(e.source == "Sent again" && e.retryOf == "t1")
    }

    // MARK: Retry

    @Test func retryIsOfferedUntilTheTurnIsSentAgain() {
        var f = EventFolder([
            ev(1, "user_message", turn: "t1", text("do it")),
            ev(2, "turn_end", turn: "t1", ["stopReason": "end_turn", "error": "fake failure"]),
            ev(3, "user_message", turn: "t2", text("again")),
            ev(4, "turn_end", turn: "t2", ["stopReason": "cancelled"]),
            ev(5, "user_message", turn: "t3", text("and")),
            ev(6, "turn_end", turn: "t3", ["error": "interrupted"]),
        ])
        #expect(ends(f).map(\.offersRetry) == [true, false, true])
        #expect(ends(f).map(\.text) == ["Error: fake failure", "Cancelled", "Interrupted"])
        f.apply([ev(7, "user_message", turn: "t4", ["content": ["type": "text", "text": "do it"], "source": "owner",
                                                    "retry_of": "t1"])])
        #expect(ends(f).map(\.offersRetry) == [false, false, true])
        #expect(f.items.compactMap { if case .message(let m) = $0, m.role == .user { m.source } else { nil } }.last == "Sent again")
        // A retry that arrives first (history in pieces) still hides the turn's Retry when its end is applied.
        let g = EventFolder([ev(1, "user_message", turn: "t9", ["content": ["type": "text", "text": "x"], "retry_of": "t8"]),
                             ev(2, "turn_end", turn: "t8", ["error": "boom"])])
        #expect(ends(g).map(\.offersRetry) == [false])
    }

    private func ends(_ f: EventFolder) -> [TurnEndEntry] {
        f.items.compactMap { if case .turnEnd(let e) = $0 { e } else { nil } }
    }

    // MARK: the triggers draft

    @Test func editingTriggersInPlace() throws {
        var d = DefinitionDraft(json(#"""
            {"name": "scout", "triggers": [{"schedule": "0 8 * * *", "prompt": "news", "session": "main", "jitter": 5}]}
            """#).object!)
        let saved = d
        d.setTrigger(0, "prompt", "news, please")
        #expect(d.triggers[0]["prompt"] == "news, please" && d.triggers[0]["jitter"] == 5)
        d.setTrigger(0, "prompt", "news")
        #expect(d.same(as: saved))
        d.addSchedule()
        d.addWebhook()
        d.addWebhook()
        #expect(d.triggers.count == 4)
        #expect(DefinitionDraft.isSchedule(d.triggers[1]) && d.triggers[1]["schedule"] == "0 8 * * *")
        #expect(d.triggers.compactMap { $0["webhook"]?.string } == ["hook", "hook-2"])
        #expect(!DefinitionDraft.isSchedule(d.triggers[2]) && d.triggers[2]["session"] == "main")
        d.removeTrigger(2)
        d.addWebhook()
        #expect(d.triggers.compactMap { $0["webhook"]?.string } == ["hook-2", "hook"])
        d.setTrigger(9, "prompt", "nowhere")
        #expect(d.triggers.count == 4)
        d.setTrigger(1, "prompt", nil)
        #expect(d.triggers[1]["prompt"] == .null)
        #expect(d["name"] == "scout")
    }

    @Test func triggerErrorsGoUnderTheirFields() {
        let keys = ["triggers", "triggers.0", "triggers.0.schedule", "triggers.0.prompt"]
        let e = FieldErrors(body: json(#"{"detail": "bad", "errors": [{"field": "triggers.0.schedule", "msg": "minute: 61 is out of range (0-59)"}, {"field": "triggers.0", "msg": "a schedule needs a prompt"}, {"field": "triggers.3.webhook", "msg": "taken"}]}"#), keys: keys)
        #expect(e["triggers.0.schedule"] == "minute: 61 is out of range (0-59)")
        #expect(e["triggers.0"] == "a schedule needs a prompt")
        #expect(e["triggers"] == "taken")
    }

    // MARK: routes

    @Test func theRoutes() async throws {
        let stub = Stub { r in
            switch (r.method, r.path) {
            case ("GET", "/api/agents/scout/triggers"): return (200, body(Self.triggers))
            case ("GET", "/api/agents/scout/trigger-runs"): return (200, body(#"{"runs": [{"id": 3, "outcome": "started"}], "more": false}"#))
            case ("GET", "/api/triggers/preview"):
                if r.query["schedule"] == "61 * * * *" { return (422, body(#"{"detail": "minute: 61 is out of range (0-59)"}"#)) }
                return (200, body(#"{"schedule": "0 9 * * 1-5", "description": "Weekdays at 09:00", "next": [1.0, 2.0], "timezone": "UTC"}"#))
            case ("POST", "/api/agents/scout/webhooks/deploy/key"):
                return (200, body(#"{"webhook": "deploy", "url": "https://x/hooks/scout/deploy", "key": "vhk_new", "key_created_at": 9.0}"#))
            case ("DELETE", "/api/agents/scout/wakeups/9"): return (200, body(#"{"id": 9, "status": "cancelled"}"#))
            case ("POST", "/api/agents/scout/sessions/s1/turns/t1/retry"): return (202, body(#"{"turn_id": "t2"}"#))
            case ("POST", "/api/agents/scout/sessions/s1/turns/t5/retry"): return (409, body(#"{"detail": "already retried"}"#))
            case ("DELETE", "/api/agents/scout/sessions/s1/queue/7"): return (200, body(#"{"ok": true}"#))
            default: return (404, body(#"{"detail": "no"}"#))
            }
        }
        let api = signedIn(stub).api
        #expect(try await api.triggers("scout").triggers.count == 3)
        let runs = try await api.triggerRuns("scout", before: 10, limit: 5)
        #expect(runs.runs.map(\.id) == [3] && !runs.more)
        #expect(stub.recorded[1].query == ["before": "10", "limit": "5"])
        let p = try await api.schedulePreview("0 9 * * 1-5")
        #expect(p.description == "Weekdays at 09:00" && p.next == [1, 2])
        #expect(stub.recorded[2].query == ["schedule": "0 9 * * 1-5", "count": "3"])
        await #expect(throws: APIError.self) { try await api.schedulePreview("61 * * * *") }
        let k = try await api.newWebhookKey("scout", hook: "deploy")
        #expect(k.key == "vhk_new" && k.createdAt == 9)
        try await api.cancelWakeup("scout", id: 9)
        #expect(try await api.retryTurn("scout", "s1", turn: "t1").turnId == "t2")
        try await api.removeQueued("scout", "s1", id: "7")
        #expect(stub.recorded.last?.method == "DELETE")
    }

    @Test func retryAndRemoveFromASession() async throws {
        let stub = Stub { r in
            switch (r.method, r.path) {
            case ("GET", "/api/agents/a/sessions/s"):
                return (200, body(#"{"agent": "a", "id": "s", "state": "working", "main": true, "queue": [{"id": 7, "text": "again", "source": "owner", "label": "You", "retry_of": "t1"}]}"#))
            case ("GET", "/api/agents/a/sessions/s/events"): return (200, body(#"{"events": [], "more": false}"#))
            case ("POST", "/api/agents/a/sessions/s/turns/t1/retry"): return (202, body(#"{"queued": 7, "session_id": "s"}"#))
            case ("POST", "/api/agents/a/sessions/s/turns/t2/retry"): return (409, body(#"{"detail": "already retried"}"#))
            case ("POST", "/api/agents/a/sessions/s/turns/t3/retry"): return (503, body(#"{"detail": "not running"}"#))
            case ("DELETE", "/api/agents/a/sessions/s/queue/7"): return (404, body(#"{"detail": "gone"}"#))
            default: return (404, body(#"{"detail": "no"}"#))
            }
        }
        let m = signedIn(stub)
        let s = SessionModel(agent: "a", id: "s", model: m)
        s.start()
        #expect(await eventually { s.loaded })
        #expect(s.session?.isMain == true)
        #expect(s.queued.map(\.source) == ["Sent again"] && s.queued[0].removable)
        #expect(await s.retry("t1") == nil)
        #expect(await s.retry("t2") == "It was sent again already, or there's nothing to send.")
        #expect(await s.retry("t3") == "The agent isn't running: start it, then retry.")
        await s.remove(s.queued[0])
        #expect(s.error == nil)     // a 404: it was sent meanwhile
        #expect(stub.recorded.contains { $0.method == "DELETE" && $0.path == "/api/agents/a/sessions/s/queue/7" })
        s.stop()
    }
}
