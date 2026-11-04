import Foundation

// An agent's definition and what its settings page reads beside it (docs/API.md, "Agents", "Secrets", "The MCP
// gateway", "Notifications", "Continuing and handing off").

/// DELETE /api/agents/{name}: the volumes it deleted, and the folder its runner kept (its work/ with files in it, or
/// its data folder holding something Valet didn't put there), if any.
public struct AgentDeleted: Decodable, Sendable, Equatable {
    public var volumes: [String]
    public var kept: String?

    public init(volumes: [String] = [], kept: String? = nil) {
        self.volumes = volumes
        self.kept = kept
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        volumes = c.get("volumes") ?? []
        kept = c.get("kept")
    }
}

/// GET /api/agents/{name}: the agent, its definition as JSON (nil when agent.yaml doesn't validate), its two files
/// and the repo's commit.
public struct AgentDetail: Decodable, Sendable {
    public var agent: Agent
    public var definition: [String: JSONValue]?
    public var files: [String: String]
    public var commit: String

    public init(agent: Agent, definition: [String: JSONValue]?, files: [String: String] = [:], commit: String = "") {
        self.agent = agent
        self.definition = definition
        self.files = files
        self.commit = commit
    }

    public init(from decoder: Decoder) throws {
        agent = try Agent(from: decoder)
        let c = try decoder.container(keyedBy: AnyKey.self)
        definition = c.get("definition", as: JSONValue.self)?.object
        files = c.get("files") ?? [:]
        commit = c.get("commit") ?? ""
    }

    public var agentYAML: String { files["agent.yaml"] ?? "" }
    public var persona: String { files["persona.md"] ?? "" }
}

/// A secret's name and whether it's set; never its value.
public struct Secret: Decodable, Identifiable, Hashable, Sendable {
    public var name: String
    public var set: Bool
    public var updatedAt: Double?
    public var usedBy: [String]
    public var servers: [String]

    public var id: String { name }

    public init(name: String, set: Bool, updatedAt: Double? = nil, usedBy: [String] = [], servers: [String] = []) {
        self.name = name
        self.set = set
        self.updatedAt = updatedAt
        self.usedBy = usedBy
        self.servers = servers
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        guard let name: String = c.get("name") else { throw missing("name", in: decoder) }
        self.name = name
        set = c.get("set") ?? false
        updatedAt = c.get("updated_at")
        usedBy = c.list("used_by")
        servers = c.list("servers")
    }

    /// `^[A-Z][A-Z0-9_]{0,63}$` (the server also refuses the names Valet and the harnesses set).
    public static func isValidName(_ s: String) -> Bool {
        s.range(of: #"^[A-Z][A-Z0-9_]{0,63}$"#, options: .regularExpression) != nil
    }

    /// As typed -> a secret's name: "github token" -> "GITHUB_TOKEN" (app.js's secretName).
    public static func name(from text: String) -> String {
        text.trimmingCharacters(in: .whitespacesAndNewlines).uppercased()
            .replacingOccurrences(of: #"[\s-]+"#, with: "_", options: .regularExpression)
    }
}

/// A server the MCP gateway offers: Valet's own, then mcp.yaml's.
public struct GatewayServer: Decodable, Identifiable, Hashable, Sendable {
    public var name: String
    public var kind: String
    public var builtin: Bool
    public var description: String?
    /// The setting that gives it instead of tools.mcp ("memory" for valet.memory).
    public var comesWith: String?
    public var agents: [String]
    public var connected: Bool

    public var id: String { name }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        guard let name: String = c.get("name") else { throw missing("name", in: decoder) }
        self.name = name
        kind = c.get("kind") ?? ""
        builtin = c.get("builtin") ?? false
        description = c.get("description")
        comesWith = c.get("comes_with")
        agents = c.list("agents")
        connected = c.get("connected") ?? false
    }
}

public struct GatewayServers: Decodable, Sendable {
    public var servers: [GatewayServer]
    /// mcp.yaml doesn't validate.
    public var error: String?

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        servers = c.list("servers")
        error = c.get("error")
    }
}

/// GET /api/agents/{name}/tools: what each tool a server offers does for this agent.
public struct AgentTools: Decodable, Sendable {
    public struct Tool: Decodable, Identifiable, Hashable, Sendable {
        public var name: String
        /// Its name on the server (what a rule names).
        public var tool: String
        public var title: String?
        public var description: String?
        public var decision: String
        public var source: String
        public var levelDecision: String?

        public var id: String { tool }
        public var label: String { [title, tool].compactMap { $0 }.first { !$0.isEmpty } ?? name }

        public init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: AnyKey.self)
            let tool: String? = c.get("tool") ?? c.get("name")
            guard let tool else { throw missing("tool", in: decoder) }
            self.tool = tool
            name = c.get("name") ?? tool
            title = c.get("title")
            description = c.get("description")
            decision = c.get("decision") ?? "ask"
            source = c.get("source") ?? "level"
            levelDecision = c.get("level_decision")
        }
    }

    public struct Server: Decodable, Identifiable, Sendable {
        public var server: String
        public var ok: Bool
        public var error: String?
        /// The server decides each call itself (valet.browser, valet.memory).
        public var ownRules: Bool
        public var tools: [Tool]

        public var id: String { server }

        public init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: AnyKey.self)
            guard let s: String = c.get("server") else { throw missing("server", in: decoder) }
            server = s
            ok = c.get("ok") ?? false
            error = c.get("error")
            ownRules = c.get("own_rules") ?? false
            tools = c.list("tools")
        }
    }

    public var agent: String
    /// The level its decisions are for.
    public var permissions: String
    public var servers: [Server]

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        agent = c.get("agent") ?? ""
        permissions = c.get("permissions") ?? ""
        servers = c.list("servers")
    }
}

/// An "Allow for an hour" answer on a gateway ask.
public struct ToolGrant: Decodable, Identifiable, Hashable, Sendable {
    public var id: Int
    public var agent: String
    public var server: String
    public var tool: String
    public var expiresAt: Double

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        guard let id: Int = c.get("id") else { throw missing("id", in: decoder) }
        self.id = id
        agent = c.get("agent") ?? ""
        server = c.get("server") ?? ""
        tool = c.get("tool") ?? ""
        expiresAt = c.get("expires_at") ?? 0
    }
}

// MARK: notifications

public struct NotificationSettings: Decodable, Sendable, Equatable {
    public struct PushInfo: Sendable, Equatable {
        public var configured = false
        public var error: String?
        public var topic = ""
        public var sandbox = false
        public var devices = 0
    }

    public var webhookURL: String?
    public var push: PushInfo

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        webhookURL = c.get("webhook_url")
        var p = PushInfo()
        if let o: JSONValue = c.get("push") {
            p.configured = o["configured"]?.bool ?? false
            p.error = o["error"]?.string
            p.topic = o["topic"]?.string ?? ""
            p.sandbox = o["sandbox"]?.bool ?? false
            p.devices = o["devices"]?.int ?? 0
        }
        push = p
    }
}

/// POST /api/notifications/test: what each channel did (nil where it isn't set up).
public struct NotifyTestResult: Decodable, Sendable {
    public var webhookOK: Bool?
    public var webhookError: String?
    public var pushSent: Int?
    public var pushFailed: Int?
    public var pushRemoved: Int?
    public var pushError: String?

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        if let w: JSONValue = c.get("webhook"), !w.isNull {
            webhookOK = w["ok"]?.bool ?? false
            webhookError = w["error"]?.string
        }
        if let p: JSONValue = c.get("push"), !p.isNull {
            pushSent = p["sent"]?.int ?? 0
            pushFailed = p["failed"]?.int ?? 0
            pushRemoved = p["removed"]?.int ?? 0
            pushError = p["error"]?.string
        }
    }

    public enum Tone: Sendable { case ok, bad, neutral }

    /// One line per channel, as the web's Notifications page says them: the ones that aren't set up too, so a
    /// test always says something.
    public var lines: [(tone: Tone, text: String)] {
        var out: [(Tone, String)] = []
        if let ok = webhookOK {
            out.append((ok ? .ok : .bad, ok ? "Webhook: sent." : "Webhook: " + (webhookError ?? "failed")))
        } else {
            out.append((.neutral, "Webhook: none set."))
        }
        let sent = pushSent ?? 0, failed = pushFailed ?? 0, removed = pushRemoved ?? 0
        if pushSent == nil {
            out.append((.neutral, "Push: not set up."))
        } else if let e = pushError, !e.isEmpty {
            out.append((.bad, "Push: \(e)"))
        } else if sent == 0 && failed == 0 && removed == 0 {
            out.append((.neutral, "Push: no devices yet."))
        } else {
            let parts = [sent > 0 ? "sent to \(sent) device\(sent == 1 ? "" : "s")" : nil,
                         failed > 0 ? "\(failed) failed" : nil,
                         removed > 0 ? "\(removed) no longer registered, removed" : nil].compactMap { $0 }
            out.append((failed > 0 && sent == 0 ? .bad : .ok, "Push: " + parts.joined(separator: ", ") + "."))
        }
        return out
    }
}

// MARK: continuing and handing off

public struct ClaudeSession: Decodable, Identifiable, Hashable, Sendable {
    public var id: String
    public var title: String
    public var cwd: String?
    public var updatedAt: Double
    public var size: Int
    /// The agent Valet has it under, if it has it.
    public var imported: String?

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        guard let id: String = c.get("id") else { throw missing("id", in: decoder) }
        self.id = id
        title = c.get("title") ?? ""
        cwd = c.get("cwd")
        updatedAt = c.get("updated_at") ?? 0
        size = c.get("size") ?? 0
        imported = c.get("imported")
    }
}

public struct ClaudeSessions: Decodable, Sendable {
    public var cwd: String
    public var runner: String
    public var folder: String?
    public var sessions: [ClaudeSession]

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        cwd = c.get("cwd") ?? ""
        runner = c.get("runner") ?? ""
        folder = c.get("folder")
        sessions = c.list("sessions")
    }
}

public struct ClaudeFolder: Decodable, Identifiable, Hashable, Sendable {
    public var folder: String
    public var cwd: String?
    public var sessions: Int
    public var updatedAt: Double

    public var id: String { folder }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        guard let f: String = c.get("folder") else { throw missing("folder", in: decoder) }
        folder = f
        cwd = c.get("cwd")
        sessions = c.get("sessions") ?? 0
        updatedAt = c.get("updated_at") ?? 0
    }
}

public struct ClaudeFolders: Decodable, Sendable {
    public var runner: String
    public var folders: [ClaudeFolder]

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        runner = c.get("runner") ?? ""
        folders = c.list("folders")
    }
}

/// Where a hand-off's conversation comes from.
public enum HandoffSource: Hashable, Sendable {
    case claude(folder: String, sessionID: String, runner: String?)
    case session(agent: String, sessionID: String)

    var json: JSONValue {
        switch self {
        case .claude(let folder, let sid, let runner):
            var o: [String: JSONValue] = ["kind": "claude", "folder": .string(folder), "session_id": .string(sid)]
            if let runner, !runner.isEmpty { o["runner"] = .string(runner) }
            return .object(o)
        case .session(let agent, let sid):
            return ["kind": "session", "agent": .string(agent), "session_id": .string(sid)]
        }
    }
}

public struct HandoffText: Decodable, Sendable {
    public var text: String
    public var bytes: Int
    public var entries: Int
    public var omitted: Int
    public var title: String
    public var source: String

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        text = c.get("text") ?? ""
        bytes = c.get("bytes") ?? 0
        entries = c.get("entries") ?? 0
        omitted = c.get("omitted") ?? 0
        title = c.get("title") ?? ""
        source = c.get("source") ?? ""
    }
}

struct HandoffResult: Decodable {
    let session: Session?

    init(from decoder: Decoder) throws {
        session = try decoder.container(keyedBy: AnyKey.self).get("session")
    }
}
