import Foundation

// What agents share (docs/API.md): what they're told (the owner profile, the authority presets, an agent's
// directives), the skills library and an agent's own skills, the gateway's servers and its log of tool calls, and
// the browser's activity log. Lenient like the rest of the models.

// MARK: what agents are told

/// owner.md: who the owner is and how they work, for the agents whose directives include `owner`.
public struct OwnerProfile: Decodable, Sendable, Equatable {
    public var text: String
    public var commit: String
    public var usedBy: [String]

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        text = c.get("text") ?? ""
        commit = c.get("commit") ?? ""
        usedBy = c.list("used_by")
    }
}

/// authority/<name>.md: what an agent may do without asking first.
public struct AuthorityPreset: Decodable, Identifiable, Hashable, Sendable {
    public var name: String
    public var text: String
    public var usedBy: [String]

    public var id: String { name }

    public init(name: String, text: String, usedBy: [String] = []) {
        self.name = name
        self.text = text
        self.usedBy = usedBy
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        guard let name: String = c.get("name") else { throw missing("name", in: decoder) }
        self.name = name
        text = c.get("text") ?? ""
        usedBy = c.list("used_by")
    }

    /// "Github only" for github-only.
    public static func title(_ name: String) -> String { name.replacingOccurrences(of: "-", with: " ").capitalizedFirst }

    /// What its "# Standing authority: …" heading calls it, else its name (app.js's presetLabel).
    public var label: String {
        for line in text.split(separator: "\n") {
            let l = line.trimmingCharacters(in: .whitespaces)
            guard l.hasPrefix("#") else { continue }
            let h = l.drop { $0 == "#" }.trimmingCharacters(in: .whitespaces)
            if h.lowercased().hasPrefix("standing authority:") {
                let rest = h.dropFirst("standing authority:".count).trimmingCharacters(in: .whitespaces)
                if !rest.isEmpty { return Self.title(rest) }
            }
        }
        return Self.title(name)
    }

    /// Valet's own, from the most careful to the most trusting, then the owner's, by name.
    public static func sorted(_ names: [String]) -> [String] {
        let order = ["strict", "standard", "full"]
        return names.sorted { a, b in
            let ra = order.firstIndex(of: a) ?? order.count, rb = order.firstIndex(of: b) ?? order.count
            return ra != rb ? ra < rb : a < b
        }
    }
}

/// A text's gist: its first line that isn't a heading, on one line (app.js's gist).
public func gist(_ text: String, _ n: Int = 140) -> String {
    let line = text.split(separator: "\n").map { $0.trimmingCharacters(in: .whitespaces) }
        .first { !$0.isEmpty && !$0.hasPrefix("#") } ?? ""
    var s = line.replacingOccurrences(of: #"^[-*]\s+"#, with: "", options: .regularExpression)
    if s.hasSuffix(":") { s = String(s.dropLast()) + "…" }
    return s.count > n ? String(s.prefix(n - 1)) + "…" : s
}

/// What an agent would be told if it started now: each layer, and the text they make.
public struct Directives: Decodable, Sendable {
    public struct Layer: Decodable, Identifiable, Hashable, Sendable {
        public var name: String
        public var file: String?
        /// ok, empty, missing (no such preset), off (memory while memory is off).
        public var state: String
        public var text: String

        public var id: String { name }

        public init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: AnyKey.self)
            guard let name: String = c.get("name") else { throw missing("name", in: decoder) }
            self.name = name
            file = c.get("file")
            state = c.get("state") ?? "ok"
            text = c.get("text") ?? ""
        }

        /// "About you", "Authority: Standard", "Personality"... and its state when it isn't ok.
        public var label: String {
            let base: String
            if name.hasPrefix("authority/") {
                base = "Authority: " + AuthorityPreset.title(String(name.dropFirst("authority/".count)))
            } else {
                base = ["owner": "About you", "persona.md": "Personality", "environment": "Environment",
                        "memory": "Memory"][name] ?? name
            }
            return state == "ok" ? base : base + " · " + state
        }
    }

    public var agent: String
    public var harness: String
    public var layers: [Layer]
    public var text: String
    public var error: String?

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        agent = c.get("agent") ?? ""
        harness = c.get("harness") ?? ""
        layers = c.list("layers")
        text = c.get("text") ?? ""
        error = c.get("error")
    }

    /// Where the harness gets it.
    public var whereText: String {
        switch harness {
        case "claude": return "Added to Claude Code's own system prompt, in every session."
        case "deepseek": return "Written to its AGENTS.md, which DeepSeek Harness reads in every session."
        default: return "Given to its harness in every session."
        }
    }
}

// MARK: skills

/// A skill in the library, or an agent's own (with `agent` and its text once fetched whole).
public struct SkillInfo: Decodable, Identifiable, Hashable, Sendable {
    public var name: String
    public var description: String?
    public var files: [String]
    public var size: Int
    public var error: String?
    public var usedBy: [String]
    /// An agent's own: a shared skill has the same name, and this one is used instead.
    public var replacesShared: Bool

    public var id: String { name }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        guard let name: String = c.get("name") else { throw missing("name", in: decoder) }
        self.name = name
        description = c.get("description")
        files = c.list("files")
        size = c.get("size") ?? 0
        error = c.get("error")
        usedBy = c.list("used_by")
        replacesShared = c.get("replaces_shared") ?? false
    }

    /// Files besides SKILL.md.
    public var moreFiles: Int { files.filter { $0 != Skill.file }.count }
}

public struct Skill: Decodable, Sendable {
    public static let file = "SKILL.md"

    public var info: SkillInfo
    public var agent: String?
    public var instructions: String
    /// SKILL.md's other frontmatter (license, allowed-tools...), kept on edits.
    public var extra: [String: JSONValue]
    /// Every file's text.
    public var content: [String: String]
    public var commit: String

    public init(from decoder: Decoder) throws {
        info = try SkillInfo(from: decoder)
        let c = try decoder.container(keyedBy: AnyKey.self)
        agent = c.get("agent")
        instructions = c.get("instructions") ?? ""
        extra = c.get("extra", as: JSONValue.self)?.object ?? [:]
        content = (c.get("content", as: JSONValue.self)?.object ?? [:]).compactMapValues(\.string)
        commit = c.get("commit") ?? ""
    }

    /// SKILL.md can't be read as name / description / instructions: it's edited as a file instead.
    public var raw: Bool { content[Self.file] != nil && info.description == nil }

    /// `^[a-z0-9]+(-[a-z0-9]+)*$`, at most 64 characters.
    public static func isValidName(_ s: String) -> Bool {
        s.count <= 64 && s.range(of: #"^[a-z0-9]+(-[a-z0-9]+)*$"#, options: .regularExpression) != nil
    }

    /// As typed -> a skill name: trimmed, lowercase, spaces and underscores to dashes (app.js's skillName).
    public static func name(from text: String) -> String {
        text.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
            .replacingOccurrences(of: #"[\s_]+"#, with: "-", options: .regularExpression)
    }

    /// A file's relative path: letters, digits, `. _ -` and `/`, no hidden parts.
    public static func isValidPath(_ p: String) -> Bool {
        p.range(of: #"^[A-Za-z0-9_][A-Za-z0-9._-]*(/[A-Za-z0-9_][A-Za-z0-9._-]*)*$"#, options: .regularExpression) != nil
    }

    /// The PUT body for what changed: description and instructions (unless SKILL.md is edited as a file), and
    /// each file written (its text) or removed (null).
    public static func editBody(raw: Bool, saved: SkillDraft, draft: SkillDraft) -> JSONValue {
        var out: [String: JSONValue] = [:]
        if !raw && draft.description != saved.description {
            out["description"] = .string(draft.description.trimmingCharacters(in: .whitespacesAndNewlines))
        }
        if !raw && draft.instructions != saved.instructions { out["instructions"] = .string(draft.instructions) }
        var files: [String: JSONValue] = [:]
        for (p, t) in draft.files where saved.files[p] != t { files[p] = .string(t) }
        for p in saved.files.keys where draft.files[p] == nil { files[p] = .null }
        if !files.isEmpty { out["files"] = .object(files) }
        return .object(out)
    }
}

/// A skill as its screen edits it.
public struct SkillDraft: Equatable, Sendable {
    public var description = ""
    public var instructions = ""
    /// Path -> text; SKILL.md only when it's edited as a file.
    public var files: [String: String] = [:]

    public init(description: String = "", instructions: String = "", files: [String: String] = [:]) {
        self.description = description
        self.instructions = instructions
        self.files = files
    }

    public init(_ s: Skill) {
        description = s.info.description ?? ""
        instructions = s.instructions
        files = s.raw ? s.content : s.content.filter { $0.key != Skill.file }
    }
}

/// What an agent gets: its own skills, the shared ones its definition picks, and what's left out.
public struct AgentSkills: Decodable, Sendable {
    public var agent: String
    public var all: Bool
    public var own: [SkillInfo]
    public var missing: [String]
    public var problems: [String]

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        agent = c.get("agent") ?? ""
        all = c.get("all") ?? false
        own = c.list("own")
        missing = c.list("missing")
        problems = c.list("problems")
    }
}

// MARK: the gateway

/// A server's details beyond what GatewayServer models: where it is and what it signs in with (names only).
public struct GatewayServerDetail: Decodable, Identifiable, Hashable, Sendable {
    public var server: GatewayServer
    public var url: String?
    public var headers: [String]
    public var command: String?
    public var args: [String]
    public var env: [String]
    public var secrets: [String]

    public var id: String { server.name }

    public init(from decoder: Decoder) throws {
        server = try GatewayServer(from: decoder)
        let c = try decoder.container(keyedBy: AnyKey.self)
        url = c.get("url")
        headers = c.list("headers")
        command = c.get("command")
        args = c.list("args")
        env = c.list("env")
        secrets = c.list("secrets")
    }

    /// Its address, or its command line.
    public var whereText: String? {
        switch server.kind {
        case "http": return url
        case "stdio": return ([command].compactMap { $0 } + args).joined(separator: " ")
        default: return nil
        }
    }

    public var kindText: String {
        switch server.kind {
        case "builtin": return "Built into Valet"
        case "http": return "Remote (HTTP)"
        case "stdio": return "A command on the control plane's machine"
        default: return server.kind
        }
    }
}

struct GatewayServerDetails: Decodable {
    var servers: [GatewayServerDetail]
    var error: String?

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        servers = c.list("servers")
        error = c.get("error")
    }
}

/// One call through the gateway, as its log keeps it.
public struct ToolCallRecord: Decodable, Identifiable, Hashable, Sendable {
    public var id: Int
    public var agent: String
    public var server: String
    public var tool: String
    public var arguments: JSONValue?
    public var decision: String?
    public var outcome: String
    public var error: String?
    public var startedAt: Double
    public var duration: Double?

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        guard let id: Int = c.get("id") else { throw missing("id", in: decoder) }
        self.id = id
        agent = c.get("agent") ?? ""
        server = c.get("server") ?? ""
        tool = c.get("tool") ?? ""
        arguments = c.get("arguments").flatMap { (v: JSONValue) in v.isNull || v.isEmpty ? nil : v }
        decision = c.get("decision")
        outcome = c.get("outcome") ?? ""
        error = c.get("error")
        startedAt = c.get("started_at") ?? 0
        duration = c.get("duration")
    }

    /// Its outcome in words, and how it reads (app.js's OUTCOME).
    public var outcomeLabel: (text: String, tone: OutcomeTone) {
        switch outcome {
        case "pending": return decision == "ask" ? ("waiting on you", .waiting) : ("running", .working)
        case "ok": return ("done", .good)
        case "error": return ("error", .bad)
        case "denied": return ("blocked", .bad)
        case "rejected": return ("rejected", .bad)
        case "expired": return ("no answer", .quiet)
        case "failed": return ("failed", .bad)
        case "unknown": return ("unknown tool", .quiet)
        case "cancelled": return ("cancelled", .quiet)
        case "interrupted": return ("interrupted", .quiet)
        default: return (outcome, .quiet)
        }
    }
}

public enum OutcomeTone: Sendable { case good, bad, waiting, working, quiet }

struct ToolCallPage: Decodable {
    var calls: [ToolCallRecord]
    var more: Bool

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        calls = c.list("calls")
        more = c.get("more") ?? false
    }
}

/// "350 ms", "2.5 s", "3 min".
public func durationText(_ s: Double?) -> String? {
    guard let s else { return nil }
    if s < 1 { return "\(Int((s * 1000).rounded())) ms" }
    if s < 60 { return String(format: "%.1f s", s) }
    return "\(Int((s / 60).rounded())) min"
}

// MARK: the browser's activity

/// One thing done in a profile's Chrome: an agent's action, or the owner's in the live view.
public struct BrowserAction: Decodable, Identifiable, Hashable, Sendable {
    public var id: Int
    public var ts: Double
    /// nil: the owner.
    public var agent: String?
    public var profile: String
    public var action: String
    public var kind: String?
    public var host: String?
    public var target: String?
    public var decision: String?
    public var outcome: String
    public var note: String?
    public var shot: String?

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        guard let id: Int = c.get("id") else { throw missing("id", in: decoder) }
        self.id = id
        ts = c.get("ts") ?? 0
        agent = c.get("agent")
        profile = c.get("profile") ?? ""
        action = c.get("action") ?? ""
        kind = c.get("kind")
        host = c.get("host")
        target = c.get("target")
        decision = c.get("decision")
        outcome = c.get("outcome") ?? ""
        note = c.get("note")
        shot = c.get("shot")
    }

    /// What was done, after who did it: "opened a tab", "went to"... (browser.js's AUDIT_ACTION).
    public var actionText: String {
        switch action {
        case "open_tab", "live_open": return "opened a tab"
        case "navigate", "live_navigate": return "went to"
        case "snapshot": return "read the page"
        case "screenshot": return "took a screenshot"
        case "click": return "clicked"
        case "type_text": return "typed into"
        case "select_option": return "chose in"
        case "press_key": return "pressed"
        case "scroll": return "scrolled"
        case "back": return "went back"
        case "close_tab": return "closed a tab"
        case "login": return "signed in"
        case "request_human": return "asked you to take over"
        case "live_view": return "opened the live view"
        default: return action.replacingOccurrences(of: "_", with: " ")
        }
    }

    /// How it ended, in a word, and its colour (browser.js's AUDIT_OUTCOME).
    public var outcomeLabel: (text: String, tone: OutcomeTone) {
        switch outcome {
        case "ok": return ("done", .good)
        case "failed": return ("failed", .bad)
        case "denied": return ("blocked", .bad)
        case "rejected": return ("rejected", .bad)
        case "expired": return ("no answer", .quiet)
        case "cancelled": return ("cancelled", .quiet)
        default: return (outcome, .quiet)
        }
    }

    /// "Read" for a kind, as a browser ask names it.
    public var kindText: String? {
        switch kind {
        case "read": return "Read"
        case "interact": return "Interact"
        case "commit": return "Commit"
        case "login": return "Sign in"
        case "human": return "Take over"
        default: return kind
        }
    }
}

struct BrowserActionPage: Decodable {
    var actions: [BrowserAction]
    var more: Bool

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        actions = c.list("actions")
        more = c.get("more") ?? false
    }
}
