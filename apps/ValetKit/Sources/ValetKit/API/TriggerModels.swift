import Foundation

// Always-on (docs/API.md, "Triggers"): an agent's schedules and webhooks, the wake-ups it set itself, and the
// record of each run.

/// One run of a trigger or a wake-up: what happened to it.
public struct TriggerRun: Decodable, Identifiable, Hashable, Sendable {
    public var id: Int
    public var agent: String
    /// Trigger.trigger, or "wakeup:<id>".
    public var trigger: String
    public var source: String
    public var label: String
    /// When it was due (nil: a webhook call).
    public var scheduledFor: Double?
    /// When it was handled.
    public var firedAt: Double?
    /// firing, started, queued, skipped, missed or failed.
    public var outcome: String
    public var sessionId: String?
    public var turnId: String?
    public var error: String?
    /// Identical skipped or missed runs in a row, folded into this one.
    public var count: Int

    public init(id: Int, agent: String = "", trigger: String = "", source: String = "", label: String = "",
                scheduledFor: Double? = nil, firedAt: Double? = nil, outcome: String = "started",
                sessionId: String? = nil, turnId: String? = nil, error: String? = nil, count: Int = 1) {
        self.id = id
        self.agent = agent
        self.trigger = trigger
        self.source = source
        self.label = label
        self.scheduledFor = scheduledFor
        self.firedAt = firedAt
        self.outcome = outcome
        self.sessionId = sessionId
        self.turnId = turnId
        self.error = error
        self.count = count
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        guard let id: Int = c.get("id") else { throw missing("id", in: decoder) }
        self.init(id: id, agent: c.get("agent") ?? "", trigger: c.get("trigger") ?? "", source: c.get("source") ?? "",
                  label: c.get("label") ?? "", scheduledFor: c.get("scheduled_for"), firedAt: c.get("fired_at"),
                  outcome: c.get("outcome") ?? "", sessionId: c.get("session_id"), turnId: c.get("turn_id"),
                  error: c.get("error"), count: c.get("count") ?? 1)
    }

    /// When it was due, else when it was handled.
    public var when: Double? { scheduledFor ?? firedAt }

    /// The outcome in a word (the web UI's).
    public var outcomeWord: String {
        switch outcome {
        case "started": return "ran"
        case "firing": return "sending"
        default: return outcome
        }
    }

    /// When, as a reader wants it ("today 08:00"); "" when the server didn't say.
    public var whenText: String { when.map { ValetKit.whenText($0) } ?? "" }

    /// "Last: ran today 08:00", "Last: skipped yesterday 08:00 (the agent was stopped)".
    public var lastLine: String {
        ["Last: \(outcomeWord)", whenText.isEmpty ? nil : whenText, error.map { "(\($0))" }]
            .compactMap { $0 }.joined(separator: " ")
    }

    /// Whether it went badly (skipped, missed, failed).
    public var bad: Bool { ["skipped", "missed", "failed"].contains(outcome) }
}

/// A schedule or a webhook in the saved agent.yaml, with what the scheduler knows of it.
public struct AgentTrigger: Decodable, Identifiable, Hashable, Sendable {
    /// "schedule:<expression>" or "webhook:<name>".
    public var trigger: String
    public var kind: String
    public var schedule: String?
    public var webhook: String?
    public var prompt: String?
    /// "main" or "new".
    public var session: String
    /// "Weekdays at 08:00"; a webhook: "When POST /hooks/... is called".
    public var description: String
    public var lastRun: TriggerRun?
    /// A schedule's next runs.
    public var next: [Double]
    /// A webhook's address, key (vhk_...), why there's none, and when it was made.
    public var url: String?
    public var key: String?
    public var keyError: String?
    public var keyCreatedAt: Double?

    public var id: String { trigger }

    public init(trigger: String, kind: String = "schedule", schedule: String? = nil, webhook: String? = nil,
                prompt: String? = nil, session: String = "main", description: String = "", lastRun: TriggerRun? = nil,
                next: [Double] = [], url: String? = nil, key: String? = nil, keyError: String? = nil,
                keyCreatedAt: Double? = nil) {
        self.trigger = trigger
        self.kind = kind
        self.schedule = schedule
        self.webhook = webhook
        self.prompt = prompt
        self.session = session
        self.description = description
        self.lastRun = lastRun
        self.next = next
        self.url = url
        self.key = key
        self.keyError = keyError
        self.keyCreatedAt = keyCreatedAt
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        let schedule: String? = c.get("schedule")
        let webhook: String? = c.get("webhook")
        let id: String = c.get("trigger") ?? (schedule.map { "schedule:\($0)" } ?? webhook.map { "webhook:\($0)" } ?? "")
        self.init(trigger: id, kind: c.get("kind") ?? (webhook != nil ? "webhook" : "schedule"), schedule: schedule,
                  webhook: webhook, prompt: c.get("prompt"), session: c.get("session") ?? "main",
                  description: c.get("description") ?? "", lastRun: c.get("last_run"), next: c.list("next"),
                  url: c.get("url"), key: c.get("key"), keyError: c.get("key_error"),
                  keyCreatedAt: c.get("key_created_at"))
    }

    public var isWebhook: Bool { kind == "webhook" }
}

/// A prompt the agent set for itself, later (valet.owner's schedule_wakeup).
public struct WakeUp: Decodable, Identifiable, Hashable, Sendable {
    public var id: Int
    public var agent: String
    /// Where it comes (nil: the main session).
    public var sessionId: String?
    public var prompt: String
    public var dueAt: Double
    /// pending, firing, fired, skipped or cancelled.
    public var status: String

    public init(id: Int, agent: String = "", sessionId: String? = nil, prompt: String = "", dueAt: Double = 0,
                status: String = "pending") {
        self.id = id
        self.agent = agent
        self.sessionId = sessionId
        self.prompt = prompt
        self.dueAt = dueAt
        self.status = status
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        guard let id: Int = c.get("id") else { throw missing("id", in: decoder) }
        self.init(id: id, agent: c.get("agent") ?? "", sessionId: c.get("session_id"), prompt: c.get("prompt") ?? "",
                  dueAt: c.get("due_at") ?? 0, status: c.get("status") ?? "pending")
    }
}

/// Its soonest schedule run or wake-up (Agent.next_trigger).
public struct NextTrigger: Decodable, Hashable, Sendable {
    public var at: Double
    /// schedule or wakeup.
    public var kind: String
    public var trigger: String
    public var label: String

    public init(at: Double, kind: String = "schedule", trigger: String = "", label: String = "") {
        self.at = at
        self.kind = kind
        self.trigger = trigger
        self.label = label
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        guard let at: Double = c.get("at") else { throw missing("at", in: decoder) }
        self.init(at: at, kind: c.get("kind") ?? "schedule", trigger: c.get("trigger") ?? "", label: c.get("label") ?? "")
    }
}

/// GET /api/agents/{name}/triggers.
public struct AgentTriggers: Decodable, Sendable {
    public var timezone: String
    public var triggers: [AgentTrigger]
    public var wakeups: [WakeUp]
    public var next: NextTrigger?
    /// agent.yaml doesn't validate, so no triggers.
    public var error: String?

    public init(timezone: String = "", triggers: [AgentTrigger] = [], wakeups: [WakeUp] = [], next: NextTrigger? = nil,
                error: String? = nil) {
        self.timezone = timezone
        self.triggers = triggers
        self.wakeups = wakeups
        self.next = next
        self.error = error
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        self.init(timezone: c.get("timezone") ?? "", triggers: c.list("triggers"), wakeups: c.list("wakeups"),
                  next: c.get("next_trigger"), error: c.get("error"))
    }
}

struct TriggerRunPage: Decodable {
    var runs: [TriggerRun]
    var more: Bool

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        runs = c.list("runs")
        more = c.get("more") ?? false
    }
}

/// What a schedule means (GET /api/triggers/preview).
public struct SchedulePreview: Decodable, Hashable, Sendable {
    public var description: String
    public var next: [Double]

    public init(description: String, next: [Double] = []) {
        self.description = description
        self.next = next
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        self.init(description: c.get("description") ?? "", next: c.list("next"))
    }
}

/// A webhook's new key.
public struct WebhookKey: Decodable, Sendable {
    public var url: String
    public var key: String
    public var createdAt: Double?

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        url = c.get("url") ?? ""
        key = c.get("key") ?? ""
        createdAt = c.get("key_created_at")
    }
}

/// A moment as a reader wants it, in this device's time: "today 08:00", "tomorrow 08:00", "Mon 08:00", "3 Nov 08:00"
/// (triggers.js's whenOf).
public func whenText(_ ts: Double, now: Date = Date(), calendar: Calendar = .current) -> String {
    let d = Date(timeIntervalSince1970: ts)
    let hm = calendar.dateComponents([.hour, .minute], from: d)
    let clock = String(format: "%02d:%02d", hm.hour ?? 0, hm.minute ?? 0)
    let days = calendar.dateComponents([.day], from: calendar.startOfDay(for: now), to: calendar.startOfDay(for: d)).day ?? 0
    switch days {
    case 0: return "today \(clock)"
    case 1: return "tomorrow \(clock)"
    case -1: return "yesterday \(clock)"
    case 2..<7: return d.formatted(.dateTime.weekday(.abbreviated)) + " \(clock)"
    default: return d.formatted(.dateTime.day().month(.abbreviated)) + " \(clock)"
    }
}

// MARK: agent.yaml's triggers, in the settings form

/// agent.yaml's `triggers` are edited in place, as JSON (as triggers.js does): each is a schedule (`schedule` set) or
/// a webhook (`webhook` set), with its `prompt` and `session` (main or new); fields this app doesn't know stay.
extension DefinitionDraft {
    public var triggers: [JSONValue] { self["triggers"]?.array ?? [] }

    public static func isSchedule(_ t: JSONValue) -> Bool { !(t["schedule"]?.isNull ?? true) }

    /// Sets one field of the trigger at `i` (nil: null, as the web UI does for an emptied prompt).
    public mutating func setTrigger(_ i: Int, _ key: String, _ value: JSONValue?) {
        var list = triggers
        guard list.indices.contains(i), var o = list[i].object else { return }
        o[key] = value ?? .null
        list[i] = .object(o)
        set("triggers", .array(list))
    }

    /// A schedule for every morning, its prompt to write.
    public mutating func addSchedule() {
        set("triggers", .array(triggers + [["schedule": "0 8 * * *", "webhook": .null, "prompt": "", "session": "main"]]))
    }

    /// A webhook named hook, hook-2, ... (the first free one).
    public mutating func addWebhook() {
        let names = Set(triggers.compactMap { $0["webhook"]?.string })
        var n = 1
        while names.contains(n == 1 ? "hook" : "hook-\(n)") { n += 1 }
        let name = n == 1 ? "hook" : "hook-\(n)"
        set("triggers", .array(triggers + [["schedule": .null, "webhook": .string(name), "prompt": .null,
                                            "session": "main"]]))
    }

    public mutating func removeTrigger(_ i: Int) {
        var list = triggers
        guard list.indices.contains(i) else { return }
        list.remove(at: i)
        set("triggers", .array(list))
    }
}

/// The presets under a schedule (triggers.js's).
public let schedulePresets: [(label: String, schedule: String)] = [
    ("Every morning", "0 8 * * *"), ("Weekdays at 9", "0 9 * * 1-5"), ("Every hour", "0 * * * *"),
    ("Every 15 minutes", "*/15 * * * *"), ("Mondays at 9", "0 9 * * 1"),
]

/// "Schedules follow Europe/Lisbon time", and that the times shown are this device's when its zone is another.
public func zoneNote(_ zone: String, here: String = TimeZone.current.identifier) -> String {
    guard !zone.isEmpty else { return "" }
    return "Schedules follow \(zone) time" + (here == zone ? "." : "; the times here are this device's.")
}
