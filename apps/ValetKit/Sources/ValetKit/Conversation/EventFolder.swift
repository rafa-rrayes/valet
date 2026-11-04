import Foundation

/// Folds a session's events, in id order, into what its conversation shows. The web UI's Renderer (app.js) as a
/// pure value: chunks of one message are joined, tool calls merge their updates, a permission request shows on
/// its tool's entry (or on its own), a plan replaces the last one, a turn's end closes its tools. Applying an
/// event twice, or one older than the last applied, does nothing, so history and live frames can overlap.
public struct EventFolder: Sendable {
    public private(set) var items: [ConversationItem] = []
    /// The highest event id applied.
    public private(set) var cursor = 0
    /// The harness's latest non-empty session_info title.
    public private(set) var title: String?
    /// Turns that have started (a user_message) and not ended (turn_end) in what's been applied.
    public private(set) var openTurns: Set<String> = []

    private var index: [String: Int] = [:]            // item id -> position in items
    private var streaming: (role: MessageEntry.Role, turn: String?, id: String)?
    private var running: [String: String?] = [:]      // tool id -> its turn, while that turn hasn't ended
    private var permissionAt: [String: String] = [:]  // requestId -> the item id it lives on
    private var endAt: [String: String] = [:]         // turn id -> its turn-end item's id
    private var retried: Set<String> = []             // turns a later prompt sends again

    public init() {}

    public init(_ events: [Event]) { apply(events) }

    public mutating func apply(_ events: [Event]) {
        for e in events { apply(e) }
    }

    public mutating func apply(_ ev: Event) {
        guard ev.id > cursor else { return }
        cursor = ev.id
        let d = ev.data
        switch ev.kind {
        case "user_message": message(ev, .user)
        case "agent_message": message(ev, .agent)
        case "agent_thought": message(ev, .thought)
        case "tool_call": tool(ev, update: false)
        case "tool_call_update": tool(ev, update: true)
        case "plan":
            if let old = index["plan"] { remove(at: old) }
            let steps = (d["entries"]?.array ?? []).map {
                PlanEntry.Step(content: $0["content"]?.string ?? "", status: $0["status"]?.string ?? "pending",
                               priority: $0["priority"]?.string)
            }
            add(.plan(PlanEntry(id: "plan", steps: steps)))
        case "permission_request": permission(ev)
        case "permission_answer":
            guard let rid = d["requestId"]?.string, let at = permissionAt[rid], let i = index[at] else { break }
            let answer = PermissionAnswer(status: InboxStatus(rawValue: d["status"]?.string ?? "answered"),
                                          outcome: d["outcome"].flatMap { $0.isNull ? nil : $0 })
            switch items[i] {
            case .tool(var t): t.permission?.answer = answer; items[i] = .tool(t)
            case .permission(var p): p.answer = answer; items[i] = .permission(p)
            default: break
            }
        case "session_info":
            if let t = d["title"]?.string?.trimmingCharacters(in: .whitespacesAndNewlines), !t.isEmpty { title = t }
        case "turn_end": turnEnd(ev)
        default: break      // kinds this client doesn't show (commands, mode, usage...), and any newer ones
        }
    }

    // MARK: kinds

    private mutating func message(_ ev: Event, _ role: MessageEntry.Role) {
        let attachment = MessageAttachment(block: ev.data["content"], agent: ev.agent)
        let piece = attachment == nil ? blockText(ev.data["content"]) : ""
        if role == .user, let turn = ev.turnId { openTurns.insert(turn) }
        if role == .user, let earlier = ev.retryOf, retried.insert(earlier).inserted, let at = endAt[earlier],
           let i = index[at], case .turnEnd(var e) = items[i] {
            e.retried = true
            items[i] = .turnEnd(e)
        }
        if role == .user, let h = ev.data["handoff"]?.object {
            // A hand-off: the earlier conversation folded, then the owner's note on its own (app.js's Renderer.message).
            let source = h["source"].flatMap { $0.text ?? $0["title"]?.text ?? $0["kind"]?.text }
            add(.handoff(HandoffEntry(id: "h:\(ev.id)", turnId: ev.turnId,
                                      source: source.flatMap { $0.isEmpty ? nil : $0 }, text: piece)))
            if let note = h["note"]?.string?.trimmingCharacters(in: .whitespacesAndNewlines), !note.isEmpty {
                add(.message(MessageEntry(id: "m:\(ev.id)", role: .user, turnId: ev.turnId, text: note,
                                          source: ev.source)))
            }
            return
        }
        if let s = streaming, s.role == role, s.turn == ev.turnId, let i = index[s.id],
           case .message(var m) = items[i] {
            m.text += piece
            if let attachment { m.attachments.append(attachment) }
            items[i] = .message(m)
            return
        }
        let id = "m:\(ev.id)"
        add(.message(MessageEntry(id: id, role: role, turnId: ev.turnId, text: piece,
                                  source: role == .user ? ev.source : nil,
                                  attachments: attachment.map { [$0] } ?? [])))
        streaming = (role, ev.turnId, id)
    }

    private mutating func tool(_ ev: Event, update: Bool) {
        guard let callId = ev.data["toolCallId"]?.string else { return }
        let key = "t:" + callId
        var t: ToolEntry
        if let i = index[key], case .tool(let existing) = items[i] {
            t = existing
        } else {
            t = ToolEntry(id: callId, turnId: ev.turnId, data: [:])
            add(.tool(t))
            running[callId] = ev.turnId
        }
        for (k, v) in ev.data where !v.isNull && !(update && k == "toolCallId") { t.data[k] = v }
        if let i = index[key] { items[i] = .tool(t) }
    }

    private mutating func permission(_ ev: Event) {
        let d = ev.data
        guard let rid = d["requestId"]?.string else { return }
        let tc = d["toolCall"]?.object ?? [:]
        let entry = PermissionEntry(requestId: rid, agent: ev.agent, toolCall: tc,
                                    options: (d["options"]?.array ?? []).compactMap { $0.decode(PermissionOption.self) },
                                    tool: d["tool"].flatMap { $0.isNull ? nil : $0.decode(ToolAsk.self) },
                                    kind: d["kind"]?.string,
                                    browser: d["browser"].flatMap { $0.object == nil ? nil : $0.decode(BrowserAsk.self) },
                                    text: d["text"]?.string,
                                    answer: nil)
        if let callId = tc["toolCallId"]?.string, let i = index["t:" + callId], case .tool(var t) = items[i] {
            // The request's ToolCallUpdate is the tool's latest state (but its status stays the tool's own).
            for (k, v) in tc where !v.isNull && k != "toolCallId" && k != "status" { t.data[k] = v }
            t.permission = entry
            items[i] = .tool(t)
            permissionAt[rid] = "t:" + callId
            return
        }
        let item = ConversationItem.permission(entry)
        add(item)
        permissionAt[rid] = item.id
    }

    private mutating func turnEnd(_ ev: Event) {
        streaming = nil
        if let turn = ev.turnId { openTurns.remove(turn) }
        for (callId, turn) in running where turn == ev.turnId {
            running[callId] = nil
            if let i = index["t:" + callId], case .tool(var t) = items[i] {
                t.ended = true
                items[i] = .tool(t)
            }
        }
        if let (text, bad) = TurnEndEntry.text(stopReason: ev.data["stopReason"]?.string,
                                               error: ev.data["error"]?.string) {
            var entry = TurnEndEntry(id: "e:\(ev.id)", turnId: ev.turnId, text: text, bad: bad)
            entry.retryable = !(ev.data["error"]?.string ?? "").isEmpty
            if let turn = ev.turnId {
                entry.retried = retried.contains(turn)
                endAt[turn] = entry.id
            }
            add(.turnEnd(entry))
        }
    }

    // MARK: items

    private mutating func add(_ item: ConversationItem) {
        index[item.id] = items.count
        items.append(item)
        streaming = nil
    }

    private mutating func remove(at i: Int) {
        index[items[i].id] = nil
        items.remove(at: i)
        for j in i..<items.count { index[items[j].id] = j }
    }

    /// The item with that id (a tool entry is "t:<toolCallId>").
    public func item(_ id: String) -> ConversationItem? { index[id].map { items[$0] } }
}
