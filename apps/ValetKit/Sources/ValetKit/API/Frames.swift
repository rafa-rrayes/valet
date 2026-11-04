import Foundation

/// The client socket's snapshot, sent first on every connection.
public struct Hello: Sendable, Equatable {
    public var agents: [Agent]
    public var runners: [Runner]
    public var inbox: [InboxItem]
    public var lastEventId: Int

    public init(agents: [Agent] = [], runners: [Runner] = [], inbox: [InboxItem] = [], lastEventId: Int = 0) {
        self.agents = agents
        self.runners = runners
        self.inbox = inbox
        self.lastEventId = lastEventId
    }
}

/// One frame of `GET /ws/client` (docs/API.md, "Live updates"). A frame of a type this app doesn't know, or whose
/// payload doesn't decode, is `.unknown`: kept, never an error.
public enum Frame: Sendable, Equatable {
    case hello(Hello)
    case event(Event)
    case agent(Agent)
    /// The agent was deleted: its sessions and inbox items went with it.
    case agentDeleted(String)
    case session(Session)
    case inbox(InboxItem)
    case runner(Runner)
    /// A memory file written or deleted (M4).
    case memory(MemoryChange)
    /// The shared space: an item added or changed, one deleted, items seen (M4).
    case shared(SharedChange)
    case unknown(type: String, JSONValue)

    public var type: String {
        switch self {
        case .hello: return "hello"
        case .event: return "event"
        case .agent: return "agent"
        case .agentDeleted: return "agent_deleted"
        case .session: return "session"
        case .inbox: return "inbox"
        case .runner: return "runner"
        case .memory: return "memory"
        case .shared: return "shared"
        case .unknown(let t, _): return t
        }
    }

    /// Decodes one text frame. nil only if it isn't a JSON object at all.
    public static func decode(_ text: String) -> Frame? { decode(Data(text.utf8)) }

    public static func decode(_ data: Data) -> Frame? {
        if let wire = try? JSONDecoder().decode(Wire.self, from: data), let frame = wire.frame { return frame }
        guard let raw = try? JSONDecoder().decode(JSONValue.self, from: data), raw.object != nil else { return nil }
        return .unknown(type: raw["type"]?.string ?? "", raw)
    }

    private struct Wire: Decodable {
        let frame: Frame?

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: AnyKey.self)
            switch c.get("type", as: String.self) ?? "" {
            case "hello":
                frame = .hello(Hello(agents: c.list("agents"), runners: c.list("runners"), inbox: c.list("inbox"),
                                     lastEventId: c.get("last_event_id") ?? 0))
            case "event": frame = c.get("event", as: Event.self).map(Frame.event)
            case "agent": frame = c.get("agent", as: Agent.self).map(Frame.agent)
            case "agent_deleted": frame = c.get("agent", as: String.self).map(Frame.agentDeleted)
            case "session": frame = c.get("session", as: Session.self).map(Frame.session)
            case "inbox": frame = c.get("item", as: InboxItem.self).map(Frame.inbox)
            case "runner": frame = c.get("runner", as: Runner.self).map(Frame.runner)
            case "memory":
                guard let agent: String = c.get("agent"), let name: String = c.get("name") else { frame = nil; break }
                frame = .memory(MemoryChange(agent: agent, name: name, file: c.get("file")))
            case "shared":
                let seen: JSONValue? = c.get("seen")
                let change = SharedChange(item: c.get("item"), deleted: c.get("deleted"),
                                          seen: seen?.array?.compactMap(\.int), seenAll: seen?.string == "all",
                                          counts: c.get("unseen", as: Int.self) == nil ? nil
                                              : try? SharedCounts(from: decoder))
                frame = change.item == nil && change.deleted == nil && change.seen == nil && !change.seenAll
                    && change.counts == nil ? nil : .shared(change)
            default: frame = nil
            }
        }
    }
}
