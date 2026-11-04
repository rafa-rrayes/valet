import Foundation

// The control plane's JSON shapes (docs/API.md, "Shapes"). Decoding is lenient: only identifiers are required,
// every other field is read with `get` (a missing or mistyped value becomes nil or a default), closed sets are
// OpenEnums, and lists drop elements that don't decode. Fields the apps don't model are kept in `extra`, so the
// shapes the M3/M4 servers grow tonight decode today.

func missing(_ key: String, in decoder: Decoder) -> DecodingError {
    .keyNotFound(AnyKey(key), .init(codingPath: decoder.codingPath, debugDescription: "\(key) is required"))
}

public struct Me: Decodable, Sendable, Equatable {
    public var owner: Bool
    public var setup: Bool
    public var api: Int?
    public var version: String?

    public init(owner: Bool, setup: Bool, api: Int? = 1, version: String? = nil) {
        self.owner = owner
        self.setup = setup
        self.api = api
        self.version = version
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        owner = c.get("owner") ?? false
        setup = c.get("setup") ?? false
        api = c.get("api")
        version = c.get("version")
    }
}

struct LoginResult: Decodable, Sendable {
    let token: String?
    let expiresIn: Double?

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        token = c.get("token")
        expiresIn = c.get("expires_in")
    }
}

// MARK: runners

public struct Runner: Decodable, Identifiable, Hashable, Sendable {
    public var name: String
    public var connected: Bool
    public var lastSeen: Double?
    public var version: String?
    public var harnesses: [String]

    public var id: String { name }

    public init(name: String, connected: Bool, lastSeen: Double? = nil, version: String? = nil,
                harnesses: [String] = []) {
        self.name = name
        self.connected = connected
        self.lastSeen = lastSeen
        self.version = version
        self.harnesses = harnesses
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        guard let name: String = c.get("name") else { throw missing("name", in: decoder) }
        self.name = name
        connected = c.get("connected") ?? false
        lastSeen = c.get("last_seen")
        version = c.get("version")
        harnesses = c.list("harnesses")
    }
}

// MARK: agents

/// Agent.status: what the agent is doing, as the list shows it.
public struct AgentStatus: OpenEnum {
    public let rawValue: String
    public init(rawValue: String) { self.rawValue = rawValue }
    public static let starting: Self = "starting", idle: Self = "idle", working: Self = "working",
                      waiting: Self = "waiting", stopped: Self = "stopped", failed: Self = "failed"
}

public struct Agent: Decodable, Identifiable, Hashable, Sendable {
    public var name: String
    public var displayName: String?
    public var emoji: String?
    public var harness: String?
    public var model: String?
    public var level: String?
    public var permissions: String?
    public var definitionError: String?
    public var runner: String?
    public var connected: Bool
    public var desired: String
    public var state: String
    public var status: AgentStatus
    public var error: String?
    public var restartNeeded: Bool
    public var missingSecrets: [String]
    /// M4: what it's doing right now (a line of text, or an object with one), how many prompts wait, and when
    /// it wakes next. Kept as JSON: the shape is the M4 track's.
    public var activity: JSONValue?
    public var queued: JSONValue?
    public var nextTrigger: JSONValue?
    /// Everything else the server sent (AgentDetail's files and definition are left out).
    public var extra: [String: JSONValue]

    public var id: String { name }

    static let known: Set<String> = [
        "name", "display_name", "emoji", "harness", "model", "level", "permissions", "definition_error", "runner",
        "connected", "desired", "state", "status", "error", "restart_needed", "missing_secrets", "activity",
        "queued", "next_trigger", "capabilities", "files", "definition", "commit",
    ]

    public init(name: String, displayName: String? = nil, emoji: String? = nil, harness: String? = "claude",
                model: String? = nil, level: String? = "host", permissions: String? = "ask",
                definitionError: String? = nil, runner: String? = nil, connected: Bool = false,
                desired: String = "stopped", state: String = "stopped", status: AgentStatus = .stopped,
                error: String? = nil, restartNeeded: Bool = false, missingSecrets: [String] = [],
                activity: JSONValue? = nil, queued: JSONValue? = nil, nextTrigger: JSONValue? = nil,
                extra: [String: JSONValue] = [:]) {
        self.name = name
        self.displayName = displayName
        self.emoji = emoji
        self.harness = harness
        self.model = model
        self.level = level
        self.permissions = permissions
        self.definitionError = definitionError
        self.runner = runner
        self.connected = connected
        self.desired = desired
        self.state = state
        self.status = status
        self.error = error
        self.restartNeeded = restartNeeded
        self.missingSecrets = missingSecrets
        self.activity = activity
        self.queued = queued
        self.nextTrigger = nextTrigger
        self.extra = extra
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        guard let name: String = c.get("name") else { throw missing("name", in: decoder) }
        self.name = name
        displayName = c.get("display_name")
        emoji = c.get("emoji")
        harness = c.get("harness")
        model = c.get("model")
        level = c.get("level")
        permissions = c.get("permissions")
        definitionError = c.get("definition_error")
        runner = c.get("runner")
        connected = c.get("connected") ?? false
        desired = c.get("desired") ?? "stopped"
        state = c.get("state") ?? "stopped"
        status = c.get("status") ?? AgentStatus(rawValue: state)
        error = c.get("error")
        restartNeeded = c.get("restart_needed") ?? false
        missingSecrets = c.list("missing_secrets")
        activity = c.get("activity").flatMap { (v: JSONValue) in v.isNull ? nil : v }
        queued = c.get("queued").flatMap { (v: JSONValue) in v.isNull ? nil : v }
        nextTrigger = c.get("next_trigger").flatMap { (v: JSONValue) in v.isNull ? nil : v }
        extra = c.rest(excluding: Self.known)
    }

    /// How to show it: the display name, else the name.
    public var title: String {
        if let d = displayName?.trimmingCharacters(in: .whitespaces), !d.isEmpty { return d }
        return name
    }

    /// Running, or on its way: a start would restart it.
    public var isUp: Bool { desired == "running" && (state == "running" || state == "starting") }

    /// It should run, but its runner isn't connected (the web UI's "runner offline").
    public var runnerOffline: Bool { desired == "running" && !connected && state != "failed" }

    /// What the agent is doing now, as one line (triggers.js's activityText): only while it works or waits on
    /// "Thinking…" and "Writing…" for the harness's two words, anything else as it is (M4's `activity`: a
    /// string, or an object with a text field).
    public var activityText: String? {
        guard status == .working || status == .waiting, let a = activity else { return nil }
        var text: String?
        if let s = a.string {
            text = s
        } else {
            for key in ["text", "summary", "title", "label", "message", "detail", "tool"] {
                if let s = a[key]?.text, !s.isEmpty { text = s; break }
            }
        }
        switch text {
        case nil, "": return nil
        case "thinking": return "Thinking…"
        case "writing": return "Writing…"
        default: return text
        }
    }

    /// "Needs GITHUB_TOKEN and github/alice's password": the secrets it can't start without, as the owner reads them.
    public var needsLine: String? {
        missingSecrets.isEmpty ? nil : "Needs " + andList(missingSecrets.map(neededName))
    }

    /// How many prompts wait in its sessions' queues (M4's `queued`: a count, or a list of them).
    public var queuedCount: Int {
        if let n = queued?.int { return n }
        if let a = queued?.array { return a.count }
        if let o = queued?.object { return o.values.reduce(0) { $0 + ($1.int ?? $1.array?.count ?? 0) } }
        return 0
    }

    /// When a trigger wakes it next (M4's `next_trigger`: unix seconds, or an object with `at` and a name).
    public var nextTriggerInfo: (at: Date?, name: String?)? {
        guard let t = nextTrigger else { return nil }
        if let at = t.double { return (Date(timeIntervalSince1970: at), nil) }
        if let s = t.string { return (nil, s) }
        let at = (t["at"] ?? t["next"] ?? t["ts"] ?? t["next_at"])?.double.map { Date(timeIntervalSince1970: $0) }
        let name = (t["label"] ?? t["name"] ?? t["title"] ?? t["trigger"] ?? t["kind"])?.text
        if at == nil && name == nil { return nil }
        return (at, name)
    }

    /// Its main session's id, once it has one (M4's `main_session`).
    public var mainSession: String? { extra["main_session"]?.string }

    /// "claude · host · claude-sonnet-5-5 · on mac"
    public var meta: String {
        [harness, level, model, runner.map { "on " + $0 }].compactMap { $0 }.filter { !$0.isEmpty }
            .joined(separator: " · ")
    }
}

/// A name `missing_secrets` lists, for the owner (app.js's neededName): GITHUB_TOKEN, or "github/alice's password" for
/// "vault:github/alice".
public func neededName(_ n: String) -> String {
    n.hasPrefix("vault:") ? "\(n.dropFirst("vault:".count))'s password" : n
}

/// "a", "a and b", "a, b and c".
public func andList(_ items: [String]) -> String {
    guard items.count > 1 else { return items.first ?? "" }
    return items.dropLast().joined(separator: ", ") + " and " + items[items.count - 1]
}

// MARK: sessions and events

public struct SessionState: OpenEnum {
    public let rawValue: String
    public init(rawValue: String) { self.rawValue = rawValue }
    public static let idle: Self = "idle", working: Self = "working"
}

public struct Session: Decodable, Identifiable, Hashable, Sendable {
    public var agent: String
    public var id: String
    public var state: SessionState
    public var title: String?
    public var turnId: String?
    public var createdAt: Double
    public var updatedAt: Double
    public var extra: [String: JSONValue]

    static let known: Set<String> = ["agent", "id", "state", "title", "turn_id", "created_at", "updated_at"]

    public init(agent: String, id: String, state: SessionState = .idle, title: String? = nil, turnId: String? = nil,
                createdAt: Double = 0, updatedAt: Double = 0, extra: [String: JSONValue] = [:]) {
        self.agent = agent
        self.id = id
        self.state = state
        self.title = title
        self.turnId = turnId
        self.createdAt = createdAt
        self.updatedAt = updatedAt
        self.extra = extra
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        guard let agent: String = c.get("agent") else { throw missing("agent", in: decoder) }
        guard let id: String = c.get("id") else { throw missing("id", in: decoder) }
        self.agent = agent
        self.id = id
        state = c.get("state") ?? .idle
        title = c.get("title")
        turnId = c.get("turn_id")
        createdAt = c.get("created_at") ?? 0
        updatedAt = c.get("updated_at") ?? createdAt
        extra = c.rest(excluding: Self.known)
    }

    public var displayTitle: String {
        if let t = title?.trimmingCharacters(in: .whitespacesAndNewlines), !t.isEmpty { return t }
        return "New session"
    }

    public var isWorking: Bool { state == .working }

    /// The agent's main session: the one conversation it keeps, where triggers and wake-ups go (M4).
    public var isMain: Bool { extra["main"]?.bool ?? false }

    /// How many prompts wait for its turn (M4's `queued`, else its `queue`'s length).
    public var queued: Int { extra["queued"]?.int ?? extra["queue"]?.array?.count ?? 0 }
}

/// One stored event (valet/shared/events.py): an ACP session/update, normalized, or one of Valet's own kinds.
public struct Event: Decodable, Identifiable, Hashable, Sendable {
    public var id: Int
    public var agent: String
    public var sessionId: String
    public var turnId: String?
    public var ts: Double
    public var kind: String
    public var data: [String: JSONValue]
    public var extra: [String: JSONValue]

    static let known: Set<String> = ["id", "agent", "session_id", "turn_id", "ts", "kind", "data"]

    public init(id: Int, agent: String = "a", sessionId: String = "s", turnId: String? = nil, ts: Double = 0,
                kind: String, data: [String: JSONValue] = [:], extra: [String: JSONValue] = [:]) {
        self.id = id
        self.agent = agent
        self.sessionId = sessionId
        self.turnId = turnId
        self.ts = ts
        self.kind = kind
        self.data = data
        self.extra = extra
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        guard let id: Int = c.get("id") else { throw missing("id", in: decoder) }
        self.id = id
        agent = c.get("agent") ?? ""
        sessionId = c.get("session_id") ?? ""
        turnId = c.get("turn_id")
        ts = c.get("ts") ?? 0
        kind = c.get("kind") ?? ""
        data = c.get("data", as: JSONValue.self)?.object ?? [:]
        extra = c.rest(excluding: Self.known)
    }

    /// Who sent a user_message, as a label (M4: "Schedule · Every day at 08:00", "Webhook deploy", "From scout"...),
    /// and "Sent again" on a Retry; nil for the owner's own.
    public var source: String? {
        Self.sourceLabel(data["source"] ?? extra["source"], label: (data["label"] ?? extra["label"])?.string,
                         retry: !(data["retry_of"]?.isNull ?? true))
    }

    /// The turn this user_message sends again (Retry), if it does.
    public var retryOf: String? { data["retry_of"]?.string }

    /// The line over a prompt (app.js's sourceLine): where it came from unless the owner typed it, then "Sent again"
    /// on a Retry. M4's sources are "owner", "schedule", "wakeup", "answer", "webhook:<name>" and "agent:<name>",
    /// with a label the server picked; without one, the same label the server would give.
    public static func sourceLabel(_ source: JSONValue?, label: String?, retry: Bool = false) -> String? {
        let parts = [origin(source, label: label), retry ? "Sent again" : nil].compactMap { $0 }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }

    private static func origin(_ source: JSONValue?, label: String?) -> String? {
        guard let source, !source.isNull else { return nil }
        let raw = source.text ?? (source["kind"] ?? source["type"])?.text ?? ""
        guard raw != "owner" else { return nil }
        let parts = raw.split(separator: ":", maxSplits: 1).map(String.init)
        let label = label.flatMap { $0.isEmpty ? nil : $0 }
        if parts.first == "schedule", let label { return "Schedule · \(label)" }
        if let label { return label }
        guard !raw.isEmpty else { return nil }
        let name = parts.count > 1 ? parts[1] : (source["name"]?.text ?? "")
        switch parts[0] {
        case "schedule": return name.isEmpty ? "Schedule" : "Schedule \(name)"
        case "wakeup": return "Wake-up"
        case "answer": return "Your answer"
        case "webhook": return name.isEmpty ? "Webhook" : "Webhook \(name)"
        case "agent": return name.isEmpty ? "Another agent" : "From \(name)"
        default: return raw
        }
    }
}

public struct EventPage: Decodable, Sendable {
    public var events: [Event]
    public var more: Bool

    public init(events: [Event], more: Bool) {
        self.events = events
        self.more = more
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        events = c.list("events")
        more = c.get("more") ?? false
    }
}

// MARK: inbox

public struct InboxKind: OpenEnum {
    public let rawValue: String
    public init(rawValue: String) { self.rawValue = rawValue }
    /// The harness asks (through its runner); the MCP gateway asks before a call; a browser action (M3); the
    /// agent's own message or question to the owner (M4, valet.owner).
    public static let permission: Self = "permission", tool: Self = "tool", browser: Self = "browser",
                      message: Self = "message", question: Self = "question"
}

public struct InboxStatus: OpenEnum {
    public let rawValue: String
    public init(rawValue: String) { self.rawValue = rawValue }
    public static let pending: Self = "pending", answered: Self = "answered", expired: Self = "expired"
}

public struct OptionKind: OpenEnum {
    public let rawValue: String
    public init(rawValue: String) { self.rawValue = rawValue }
    public static let allowOnce: Self = "allow_once", allowAlways: Self = "allow_always",
                      rejectOnce: Self = "reject_once", rejectAlways: Self = "reject_always"

    public var allows: Bool { rawValue.hasPrefix("allow") }
    public var rejects: Bool { rawValue.hasPrefix("reject") }
}

/// An ACP PermissionOption, and the gateway's and browser's asks:
/// allow_once, allow_hour, allow_always, reject_once.
public struct PermissionOption: Codable, Identifiable, Hashable, Sendable {
    public var optionId: String
    public var name: String
    public var kind: OptionKind

    public var id: String { optionId }

    public static let allowHourID = "allow_hour"

    /// The option drawn prominent: the first that allows (app.js's optionClass), so the safest yes stands out.
    public static func primaryID(in options: [PermissionOption]) -> String? {
        options.first { $0.kind.allows }?.optionId
    }

    public init(optionId: String, name: String, kind: OptionKind = "") {
        self.optionId = optionId
        self.name = name
        self.kind = kind
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        guard let id: String = c.get("optionId") ?? c.get("option_id") else { throw missing("optionId", in: decoder) }
        optionId = id
        name = c.get("name") ?? id
        kind = c.get("kind") ?? ""
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: AnyKey.self)
        try c.encode(optionId, forKey: AnyKey("optionId"))
        try c.encode(name, forKey: AnyKey("name"))
        try c.encode(kind, forKey: AnyKey("kind"))
    }
}

/// What a gateway ask is about: the MCP server, the tool and the arguments of the call.
public struct ToolAsk: Decodable, Hashable, Sendable {
    public var server: String
    public var name: String
    public var title: String?
    public var description: String?
    public var arguments: JSONValue?
    public var annotations: JSONValue?

    public init(server: String, name: String, title: String? = nil, description: String? = nil,
                arguments: JSONValue? = nil, annotations: JSONValue? = nil) {
        self.server = server
        self.name = name
        self.title = title
        self.description = description
        self.arguments = arguments
        self.annotations = annotations
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        server = c.get("server") ?? ""
        name = c.get("name") ?? ""
        title = c.get("title")
        description = c.get("description")
        arguments = c.get("arguments")
        annotations = c.get("annotations")
    }

    /// The tool as people read it: its title, else its name.
    public var label: String { [title, name].compactMap { $0 }.first { !$0.isEmpty } ?? "a tool" }
}

public struct InboxItem: Decodable, Identifiable, Hashable, Sendable {
    public var id: String
    public var kind: InboxKind
    public var agent: String
    public var sessionId: String?
    public var turnId: String?
    public var status: InboxStatus
    public var title: String
    /// The ACP ToolCallUpdate the ask is about ({} if none).
    public var toolCall: [String: JSONValue]
    public var options: [PermissionOption]
    public var tool: ToolAsk?
    /// kind "browser": the action asked about, or the take-over (M3). Also left in `extra`.
    public var browser: BrowserAsk?
    public var answer: JSONValue?
    public var createdAt: Double
    public var answeredAt: Double?
    /// Fields a kind adds (a browser ask's site and screenshot, a message's text...).
    public var extra: [String: JSONValue]

    static let known: Set<String> = ["id", "kind", "agent", "session_id", "turn_id", "status", "title", "tool_call",
                                     "options", "tool", "answer", "created_at", "answered_at"]

    public init(id: String, kind: InboxKind = .permission, agent: String = "a", sessionId: String? = nil,
                turnId: String? = nil, status: InboxStatus = .pending, title: String = "Permission request",
                toolCall: [String: JSONValue] = [:], options: [PermissionOption] = [], tool: ToolAsk? = nil,
                browser: BrowserAsk? = nil, answer: JSONValue? = nil, createdAt: Double = 0,
                answeredAt: Double? = nil, extra: [String: JSONValue] = [:]) {
        self.id = id
        self.kind = kind
        self.agent = agent
        self.sessionId = sessionId
        self.turnId = turnId
        self.status = status
        self.title = title
        self.toolCall = toolCall
        self.options = options
        self.tool = tool
        self.browser = browser
        self.answer = answer
        self.createdAt = createdAt
        self.answeredAt = answeredAt
        self.extra = extra
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        guard let id: String = c.get("id") ?? c.get("id", as: Int.self).map(String.init) else {
            throw missing("id", in: decoder)
        }
        self.id = id
        kind = c.get("kind") ?? .permission
        agent = c.get("agent") ?? ""
        sessionId = c.get("session_id")
        turnId = c.get("turn_id")
        status = c.get("status") ?? .pending
        title = c.get("title") ?? ""
        toolCall = c.get("tool_call", as: JSONValue.self)?.object ?? [:]
        options = c.list("options")
        tool = c.get("tool")
        browser = c.get("browser", as: JSONValue.self).flatMap { $0.object == nil ? nil : $0.decode(BrowserAsk.self) }
        answer = c.get("answer").flatMap { (v: JSONValue) in v.isNull ? nil : v }
        createdAt = c.get("created_at") ?? 0
        answeredAt = c.get("answered_at")
        extra = c.rest(excluding: Self.known)
    }

    public var isPending: Bool { status == .pending }

    /// A gateway ask (kind tool, or a tool object, or a `gateway-` tool call).
    public var isToolAsk: Bool {
        kind == .tool || tool != nil || (toolCall["toolCallId"]?.string ?? "").hasPrefix("gateway-")
    }

    /// Answered with text rather than an option (`{"text": ...}`).
    public var takesText: Bool { kind == .question }

    /// valet.owner's message or question.
    public var isOwnerAsk: Bool { kind == .message || kind == .question }

    /// What a message or question says, whole (its `text`, else the title: the first line).
    public var ownerText: String {
        if let t = extra["text"]?.string, !t.isEmpty { return t }
        return bodyText ?? title
    }

    /// Where it stands, as the conversation's asks say it.
    public var state: PermissionState { PermissionState(status: status, outcome: answer) }

    /// The options to offer, in order: every one an item carries, except that a harness's permission shows its
    /// allow_once and reject_once options when it has them (as the web UI does; "always" in a harness's own
    /// terms would bypass Valet's rules).
    public var shownOptions: [PermissionOption] {
        kind == .permission && !isToolAsk ? Self.harnessChoices(options) : options
    }

    public static func harnessChoices(_ options: [PermissionOption]) -> [PermissionOption] {
        let two = options.filter { $0.kind == .allowOnce || $0.kind == .rejectOnce }
        return two.isEmpty ? options : two
    }

    /// The text of a message or question (the agent's words): a field the kind adds, else the title.
    public var bodyText: String? {
        for key in ["text", "body", "message", "question", "detail", "description"] {
            if let s = extra[key]?.string, !s.isEmpty, s != title { return s }
            if let s = extra[key]?["text"]?.string, !s.isEmpty, s != title { return s }
        }
        return nil
    }

    public var created: Date { Date(timeIntervalSince1970: createdAt) }

    /// The option it was answered with, if any ({"outcome": "selected", "optionId"}).
    public var answeredOptionId: String? { answer?["optionId"]?.string ?? answer?["option_id"]?.string }
}

// MARK: settings catalog

public struct Catalog: Decodable, Sendable {
    public struct Model: Decodable, Hashable, Sendable {
        public var id: String
        public var name: String
        public var note: String?

        public init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: AnyKey.self)
            guard let id: String = c.get("id") else { throw missing("id", in: decoder) }
            self.id = id
            name = c.get("name") ?? id
            note = c.get("note")
        }
    }

    public struct Harness: Decodable, Hashable, Sendable {
        public var id: String
        public var name: String
        public var by: String?
        public var key: String?
        public var models: [Model]
        /// true: a connected runner can run it; false: runners are connected and none can; nil: no runner.
        public var ready: Bool?

        public init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: AnyKey.self)
            guard let id: String = c.get("id") else { throw missing("id", in: decoder) }
            self.id = id
            name = c.get("name") ?? id
            by = c.get("by")
            key = c.get("key")
            models = c.list("models")
            ready = c.get("ready")
        }
    }

    /// A permission level, computer level or network choice: an id, a name and a sentence.
    public struct Choice: Decodable, Hashable, Sendable {
        public var id: String
        public var name: String
        public var summary: String
        public var host: String?

        public init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: AnyKey.self)
            guard let id: String = c.get("id") else { throw missing("id", in: decoder) }
            self.id = id
            name = c.get("name") ?? id
            summary = c.get("summary") ?? ""
            host = c.get("host")
        }
    }

    public struct Template: Decodable, Hashable, Sendable {
        public var id: String
        public var name: String
        public var emoji: String?
        public var definition: JSONValue
        public var persona: String

        public init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: AnyKey.self)
            guard let id: String = c.get("id") else { throw missing("id", in: decoder) }
            self.id = id
            name = c.get("name") ?? id
            emoji = c.get("emoji")
            definition = c.get("definition") ?? [:]
            persona = c.get("persona") ?? ""
        }
    }

    public var harnesses: [Harness]
    public var permissions: [Choice]
    public var levels: [Choice]
    public var networks: [Choice]
    public var templates: [Template]
    /// The authority preset suggested for each permission level.
    public var authority: [String: String]

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        harnesses = c.list("harnesses")
        permissions = c.list("permissions")
        levels = c.list("levels")
        networks = c.list("networks")
        templates = c.list("templates")
        authority = c.get("authority") ?? [:]
    }
}

// MARK: push devices

public struct Device: Decodable, Identifiable, Hashable, Sendable {
    public var id: FlexibleID
    public var platform: String
    public var name: String?
    public var token: String?
    public var createdAt: Double?
    public var lastSeen: Double?

    public init(id: String, platform: String, name: String?, token: String? = nil, createdAt: Double? = nil) {
        self.id = FlexibleID(id)
        self.platform = platform
        self.name = name
        self.token = token
        self.createdAt = createdAt
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        guard let id: FlexibleID = c.get("id") else { throw missing("id", in: decoder) }
        self.id = id
        platform = c.get("platform") ?? ""
        name = c.get("name")
        token = c.get("token")
        createdAt = c.get("created_at")
        lastSeen = c.get("last_seen") ?? c.get("last_push") ?? c.get("updated_at")
    }
}

/// GET /api/devices: a list, or an object holding one (`{"devices": [...]}`).
struct DeviceList: Decodable {
    let devices: [Device]

    init(from decoder: Decoder) throws {
        if let list = try? decoder.singleValueContainer().decode([Lenient<Device>].self) {
            devices = list.compactMap(\.value)
        } else {
            devices = try decoder.container(keyedBy: AnyKey.self).list("devices")
        }
    }
}

// MARK: prompts

/// POST .../prompt: the turn it started, or (M4) the queue entry it waits in while a turn runs.
public struct PromptResult: Decodable, Sendable, Equatable {
    public var turnId: String?
    public var queued: String?

    public init(turnId: String? = nil, queued: String? = nil) {
        self.turnId = turnId
        self.queued = queued
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        turnId = c.get("turn_id")
        let q: JSONValue? = c.get("queued")
        queued = q.flatMap { v in
            if v.isNull || v == .bool(false) { return nil }
            return v.bool == true ? "queued" : (v.text ?? v["id"]?.text ?? "queued")
        }
    }
}
