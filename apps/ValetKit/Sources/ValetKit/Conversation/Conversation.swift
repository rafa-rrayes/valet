import Foundation

// What a session's conversation shows, built from its events by `EventFolder` (docs/API.md, "Rendering events").

/// The text of an ACP ContentBlock (app.js's blockText): text as it is, other blocks as a short placeholder.
public func blockText(_ c: JSONValue?) -> String {
    guard let c else { return "" }
    switch c["type"]?.string {
    case "text": return c["text"]?.string ?? ""
    case "image": return "[image]"
    case "audio": return "[audio]"
    case "resource_link": return "[\(c["name"]?.string ?? c["uri"]?.string ?? "link")]"
    case "resource":
        let r = c["resource"]
        return r?["text"]?.string ?? r?["uri"]?.string.map { "[\($0)]" } ?? ""
    default: return ""
    }
}

public struct MessageEntry: Hashable, Sendable {
    public enum Role: String, Sendable { case user, agent, thought }
    public var id: String
    public var role: Role
    public var turnId: String?
    public var text: String
    /// Who sent a user message, when it wasn't the owner (M4: a schedule, a webhook, another agent).
    public var source: String?
    /// Pictures and files that came with it, shown after its text (not in it).
    public var attachments: [MessageAttachment] = []
}

/// A picture or a file in a message (docs/API.md, "Rendering events"): the owner's attachments (an image to GET by
/// its `uri`; a file put in the agent's computer, as a `file://` link), or an agent's image (its bytes).
public struct MessageAttachment: Hashable, Sendable {
    public enum Kind: Hashable, Sendable {
        /// `uri`: a path on the server to GET with the token; `data`: base64 bytes.
        case image(uri: String?, data: String?)
        /// `path`: where it is in the agent's computer (a `file://` link); else the link as it came.
        case file(path: String?, uri: String)
    }

    public var kind: Kind
    public var name: String
    public var mime: String?
    public var size: Int?
    /// Whose computer a file is in.
    public var agent: String

    /// An `image` or `resource_link` content block; nil for anything else.
    public init?(block: JSONValue?, agent: String) {
        guard let b = block else { return nil }
        self.agent = agent
        mime = b["mimeType"]?.string
        size = b["size"]?.int
        switch b["type"]?.string {
        case "image":
            let uri = b["uri"]?.string, data = b["data"]?.string
            guard uri != nil || data != nil else { return nil }
            kind = .image(uri: uri, data: data)
            name = b["name"]?.string ?? "Image"
        case "resource_link":
            guard let uri = b["uri"]?.string else { return nil }
            let path = uri.hasPrefix("file:///") ? String(uri.dropFirst("file://".count)) : nil
            kind = .file(path: path, uri: uri)
            name = b["name"]?.string ?? path.map(FilePaths.name) ?? uri
        default:
            return nil
        }
    }
}

/// One piece of a tool call's `content`.
public enum ToolContent: Hashable, Sendable {
    case diff(path: String, oldText: String?, newText: String)
    /// A ContentBlock; text is Markdown (Claude fences command output as ```console).
    case block(JSONValue)
    case terminal(String)

    init?(_ v: JSONValue) {
        switch v["type"]?.string {
        case "diff":
            self = .diff(path: v["path"]?.string ?? "", oldText: v["oldText"]?.string,
                         newText: v["newText"]?.string ?? "")
        case "content": self = .block(v["content"] ?? .null)
        case "terminal": self = .terminal(v["terminalId"]?.string ?? "")
        default: return nil
        }
    }
}

/// A permission request in the conversation, and how it was answered (permission_answer), if it was.
public struct PermissionEntry: Hashable, Sendable {
    public var requestId: String
    public var agent: String
    public var toolCall: [String: JSONValue]
    public var options: [PermissionOption]
    /// A gateway ask's server and tool (its `tool` object), when the event carries one.
    public var tool: ToolAsk?
    /// The event's `kind` when it isn't the harness's own ask: "tool" (the gateway), "browser" (valet.browser).
    public var kind: String? = nil
    /// A browser ask's action or take-over (its `browser` object).
    public var browser: BrowserAsk? = nil
    /// valet.owner's message or question: what the agent says or asks (Markdown).
    public var text: String? = nil
    public var answer: PermissionAnswer?

    /// valet.owner's message or question to the owner (not a permission at all).
    public var isOwnerAsk: Bool { kind == "message" || kind == "question" }

    /// A gateway ask: no tool entry of its own (`gateway-<id>`), or a `tool` object.
    public var isToolAsk: Bool {
        kind == "tool" || tool != nil || (toolCall["toolCallId"]?.string ?? "").hasPrefix("gateway-")
    }

    /// A browser ask (`browser-<id>`, kind browser, or a `browser` object).
    public var isBrowserAsk: Bool {
        kind == "browser" || browser != nil || (toolCall["toolCallId"]?.string ?? "").hasPrefix("browser-")
    }

    /// The harness's own permission request (not one of Valet's asks).
    var isHarnessAsk: Bool { (kind == nil || kind == "permission") && !isToolAsk && !isBrowserAsk }

    public var title: String {
        toolCall["title"]?.string.flatMap { $0.isEmpty ? nil : $0 } ?? "Permission request"
    }

    /// The options to offer (InboxItem.shownOptions): every one, but a harness's permission keeps to
    /// allow_once and reject_once when it has them.
    public func shownOptions(item: InboxItem?) -> [PermissionOption] {
        let opts = (item?.options.isEmpty == false ? item?.options : nil) ?? options
        let harness = item.map { $0.kind == .permission && !$0.isToolAsk } ?? isHarnessAsk
        return harness ? InboxItem.harnessChoices(opts) : opts
    }

    /// Where it stands: its answer event, else its inbox item, else closed (the inbox no longer has it pending).
    public func state(item: InboxItem?) -> PermissionState {
        if let a = answer { return PermissionState(status: a.status, outcome: a.outcome) }
        if let item { return PermissionState(status: item.status, outcome: item.answer) }
        return PermissionState(status: "closed", outcome: nil)
    }
}

public struct PermissionAnswer: Hashable, Sendable {
    public var status: InboxStatus
    public var outcome: JSONValue?
}

public struct PermissionState: Hashable, Sendable {
    public var status: InboxStatus
    public var outcome: JSONValue?

    public var isPending: Bool { status == .pending }

    public enum Tone: Sendable { case allowed, rejected, expired, answered }

    /// Once it's settled: what it came to (app.js's answerOf), nil while pending or if nobody knows. `owner`:
    /// valet.owner's message or question, which reads as triggers.js's ownerResult ("Dismissed", "You answered:
    /// Yes, tonight · after 6").
    public func result(options: [PermissionOption], owner: Bool = false) -> (tone: Tone, text: String)? {
        if owner { return ownerResult(options: options) }
        if status == .expired { return (.expired, "Expired") }
        guard status == .answered else { return nil }
        guard let o = outcome, o["outcome"]?.string != "cancelled" else { return (.expired, "Cancelled") }
        let chosen = o["optionId"]?.string ?? o["option_id"]?.string
        if chosen == nil, let text = o["text"]?.string { return (.answered, text) }
        let opt = options.first { $0.optionId == chosen }
        if chosen == PermissionOption.allowHourID { return (.allowed, "Allowed for an hour") }
        if opt?.kind == .allowAlways { return (.allowed, "Always allowed") }
        if opt?.kind.allows == true { return (.allowed, "Allowed") }
        if opt?.kind.rejects == true { return (.rejected, "Rejected") }
        return (.answered, opt?.name ?? chosen ?? "Answered")
    }

    private func ownerResult(options: [PermissionOption]) -> (tone: Tone, text: String)? {
        if status == .expired { return (.expired, "Expired") }
        if status == .pending { return nil }
        guard status == .answered else { return (.expired, "Closed") }
        let o = outcome
        let chosen = o?["optionId"]?.string ?? o?["option_id"]?.string
        if chosen == "dismiss" { return (.expired, "Dismissed") }
        let words = o?["text"]?.string.flatMap { $0.isEmpty ? nil : $0 }
        let parts = [options.first { $0.optionId == chosen }?.name, words].compactMap { $0 }
        return (.answered, parts.isEmpty ? "Answered" : "You answered: " + parts.joined(separator: " · "))
    }
}

/// A tool call: its ACP ToolCall fields merged from tool_call, its updates and its permission request.
public struct ToolEntry: Hashable, Sendable {
    public var id: String
    public var turnId: String?
    public var data: [String: JSONValue]
    /// The permission request about this call, if there was one.
    public var permission: PermissionEntry?
    /// Its turn ended (it's shown closed even if the harness never said it finished).
    public var ended = false

    public var title: String { data["title"]?.string.flatMap { $0.isEmpty ? nil : $0 } ?? "Tool call" }

    /// DeepSeek Harness titles a call with the bare tool name ("bash") and describes it in rawInput.description:
    /// the title, then that.
    public var displayName: String {
        let desc = data["rawInput"]?["description"]?.string ?? ""
        return desc.isEmpty || desc == title ? title : "\(title) · \(desc)"
    }

    /// ACP ToolKind (read, edit, delete, move, search, execute, think, fetch, switch_mode, other).
    public var kind: String? { data["kind"]?.string }

    /// pending, in_progress, completed or failed.
    public var status: String { data["status"]?.string ?? "pending" }

    public var command: String? { data["rawInput"]?["command"]?.string }

    public var content: [ToolContent] { (data["content"]?.array ?? []).compactMap(ToolContent.init) }

    public var locations: [String] {
        (data["locations"]?.array ?? []).compactMap { l in
            guard let p = l["path"]?.string else { return nil }
            return l["line"]?.int.map { "\(p):\($0)" } ?? p
        }
    }

    public var rawInput: JSONValue? { data["rawInput"].flatMap { $0.isEmpty ? nil : $0 } }
    public var rawOutput: JSONValue? { data["rawOutput"].flatMap { $0.isEmpty ? nil : $0 } }

    public var isDone: Bool { status == "completed" || status == "failed" }

    /// Open while it runs or asks, closed once it's done or its turn ended (app.js's drawTool).
    public func isOpen(asking: Bool) -> Bool { asking || (!ended && !isDone) }
}

public struct PlanEntry: Hashable, Sendable {
    public struct Step: Hashable, Sendable {
        public var content: String
        /// pending, in_progress or completed.
        public var status: String
        public var priority: String?
    }

    public var id: String
    public var steps: [Step]
}

/// A turn that ended in a way worth saying: an error, a cancel, a refusal, a limit.
public struct TurnEndEntry: Hashable, Sendable {
    public var id: String
    public var turnId: String?
    public var text: String
    public var bad: Bool
    /// It ended with an error (failed or interrupted), so its prompt can be sent again (M4's Retry).
    public var retryable = false
    /// A later prompt says it sends this turn again: no Retry any more.
    public var retried = false

    /// Retry is offered.
    public var offersRetry: Bool { retryable && !retried && turnId != nil }

    /// The line for a turn_end, or nil for an ordinary end (end_turn and no error).
    public static func text(stopReason: String?, error: String?) -> (String, Bool)? {
        if let error, !error.isEmpty { return (error == "interrupted" ? "Interrupted" : "Error: \(error)", true) }
        switch stopReason {
        case "cancelled": return ("Cancelled", false)
        case "refusal": return ("The agent refused", true)
        case "max_tokens": return ("Stopped: output limit reached", false)
        case "max_turn_requests": return ("Stopped: too many steps", false)
        default: return nil
        }
    }
}

/// A hand-off's first message (`user_message` with `data.handoff`): the condensed earlier conversation, which the
/// conversation folds away under "Handed off: <source>". The owner's note follows it as a message of its own.
public struct HandoffEntry: Hashable, Sendable {
    public var id: String
    public var turnId: String?
    /// Where it came from ("Claude Code session in ~/code/x", "scout: Fix the build"), if it says.
    public var source: String?
    /// The whole text the agent got.
    public var text: String

    /// "Handed off: <source>", or "Handed off: an earlier conversation".
    public var label: String { "Handed off: " + (source ?? "an earlier conversation") }
}

public enum ConversationItem: Identifiable, Hashable, Sendable {
    case message(MessageEntry)
    case tool(ToolEntry)
    /// A permission request with no tool entry to sit on (a gateway ask, or one the harness sent first).
    case permission(PermissionEntry)
    case plan(PlanEntry)
    case turnEnd(TurnEndEntry)
    /// A conversation handed off from elsewhere: folded away, under where it came from.
    case handoff(HandoffEntry)

    public var id: String {
        switch self {
        case .message(let m): return m.id
        case .tool(let t): return "t:" + t.id
        case .permission(let p): return "p:" + p.requestId
        case .plan(let p): return p.id
        case .turnEnd(let e): return e.id
        case .handoff(let h): return h.id
        }
    }
}
