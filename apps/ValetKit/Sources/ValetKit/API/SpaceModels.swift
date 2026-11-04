import Foundation

// The shared space and memory (docs/API.md, "The shared space", "Memory"), and the git history entries both use.

public struct SharedKind: OpenEnum {
    public let rawValue: String
    public init(rawValue: String) { self.rawValue = rawValue }
    public static let note: Self = "note", file: Self = "file"
}

/// A note or a file between the owner and one agent.
public struct SharedItem: Decodable, Identifiable, Hashable, Sendable {
    public var id: Int
    /// The agent whose space it's in; nil: unassigned (from when Shared was one list for every agent).
    public var agent: String?
    public var kind: SharedKind
    /// A note's title; a file's name.
    public var title: String
    public var mime: String?
    public var size: Int?
    /// "owner" or an agent's name.
    public var addedBy: String
    public var updatedBy: String
    public var createdAt: Double
    public var updatedAt: Double
    /// An agent added or changed it after the owner last saw it.
    public var unseen: Bool
    public var preview: String
    /// A note's Markdown, a file's description: single items only.
    public var text: String?

    public init(id: Int, agent: String? = nil, kind: SharedKind = .note, title: String = "", mime: String? = nil,
                size: Int? = nil, addedBy: String = "owner", updatedBy: String? = nil, createdAt: Double = 0,
                updatedAt: Double? = nil, unseen: Bool = false, preview: String = "", text: String? = nil) {
        self.id = id
        self.agent = agent
        self.kind = kind
        self.title = title
        self.mime = mime
        self.size = size
        self.addedBy = addedBy
        self.updatedBy = updatedBy ?? addedBy
        self.createdAt = createdAt
        self.updatedAt = updatedAt ?? createdAt
        self.unseen = unseen
        self.preview = preview
        self.text = text
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        guard let id: Int = c.get("id") else { throw missing("id", in: decoder) }
        self.id = id
        agent = c.get("agent")
        kind = c.get("kind") ?? .note
        title = c.get("title") ?? ""
        mime = c.get("mime")
        size = c.get("size")
        addedBy = c.get("added_by") ?? "owner"
        updatedBy = c.get("updated_by") ?? addedBy
        createdAt = c.get("created_at") ?? 0
        updatedAt = c.get("updated_at") ?? createdAt
        unseen = c.get("unseen") ?? false
        preview = c.get("preview") ?? ""
        text = c.get("text")
    }

    public var isFile: Bool { kind == .file }
    public var isImage: Bool {
        guard let m = mime else { return false }
        return ["image/png", "image/jpeg", "image/gif", "image/webp", "image/heic", "image/bmp", "image/avif"].contains(m)
    }

    /// Text the app shows in place (plain text, Markdown, JSON, YAML, code), when it's small enough.
    public var isText: Bool {
        if title.lowercased().hasSuffix(".md") || title.lowercased().hasSuffix(".markdown") { return true }
        guard let m = mime else { return false }
        return m.hasPrefix("text/") || ["application/json", "application/xml", "application/yaml", "application/x-yaml",
                                        "application/toml", "application/javascript", "application/x-sh",
                                        "application/sql"].contains(m)
    }

    public var isMarkdown: Bool {
        let t = title.lowercased()
        return t.hasSuffix(".md") || t.hasSuffix(".markdown") || mime == "text/markdown"
    }
}

public struct SharedPage: Decodable, Sendable {
    public var items: [SharedItem]
    public var more: Bool
    public var unseen: Int

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        items = c.list("items")
        more = c.get("more") ?? false
        unseen = c.get("unseen") ?? 0
    }
}

/// What's new in the shared spaces (GET /api/shared/unseen, POST /api/shared/seen, every `shared` frame): unseen in
/// all, per agent (an agent with none left out), and how many items have no agent, seen or not.
public struct SharedCounts: Decodable, Sendable, Equatable {
    public var unseen: Int
    public var agents: [String: Int]
    public var unassigned: Int

    public init(unseen: Int = 0, agents: [String: Int] = [:], unassigned: Int = 0) {
        self.unseen = unseen
        self.agents = agents
        self.unassigned = unassigned
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        unseen = c.get("unseen") ?? 0
        agents = c.get("agents") ?? [:]
        unassigned = c.get("unassigned") ?? 0
    }
}

/// A `shared` frame: an item added, changed or moved, one deleted, or items marked seen; the counts after it.
public struct SharedChange: Sendable, Equatable {
    public var item: SharedItem?
    public var deleted: Int?
    /// The ids marked seen; nil with `seenAll`.
    public var seen: [Int]?
    public var seenAll: Bool
    public var counts: SharedCounts?

    public init(item: SharedItem? = nil, deleted: Int? = nil, seen: [Int]? = nil, seenAll: Bool = false,
                counts: SharedCounts? = nil) {
        self.item = item
        self.deleted = deleted
        self.seen = seen
        self.seenAll = seenAll
        self.counts = counts
    }
}

/// The filters of the shared list.
public struct SharedQuery: Hashable, Sendable {
    public var q = ""
    /// "", "owner", or the agent's name (who added it).
    public var by = ""
    /// "", "note" or "file".
    public var kind = ""

    public init(q: String = "", by: String = "", kind: String = "") {
        self.q = q
        self.by = by
        self.kind = kind
    }

    public var isFiltered: Bool { !q.isEmpty || !by.isEmpty || !kind.isEmpty }
}

// MARK: memory

public struct MemoryFile: Decodable, Identifiable, Hashable, Sendable {
    /// Without .md: lowercase letters and digits, words joined by dashes.
    public var name: String
    /// Its first heading, else its first line.
    public var title: String
    public var size: Int
    public var updatedAt: Double

    public var id: String { name }

    public init(name: String, title: String = "", size: Int = 0, updatedAt: Double = 0) {
        self.name = name
        self.title = title
        self.size = size
        self.updatedAt = updatedAt
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        guard let name: String = c.get("name") else { throw missing("name", in: decoder) }
        self.name = name
        title = c.get("title") ?? ""
        size = c.get("size") ?? 0
        updatedAt = c.get("updated_at") ?? 0
    }

    /// The name rule (valet/control/memory.py).
    public static func isValidName(_ s: String) -> Bool {
        guard !s.isEmpty, s.count <= 64 else { return false }
        return s.range(of: #"^[a-z0-9]+(-[a-z0-9]+)*$"#, options: .regularExpression) != nil
    }

    /// What the owner typed -> a file's name: "Project Notes.md" -> "project-notes" (memory.js's memSlug).
    public static func slug(_ text: String) -> String {
        var s = text.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if s.hasSuffix(".md") { s.removeLast(3) }
        s = s.replacingOccurrences(of: "[^a-z0-9]+", with: "-", options: .regularExpression)
        s = s.trimmingCharacters(in: CharacterSet(charactersIn: "-"))
        if s.count > 64 { s = String(s.prefix(64)) }
        while s.hasSuffix("-") { s.removeLast() }
        return s
    }
}

public struct AgentMemory: Decodable, Sendable {
    public struct Limits: Sendable, Equatable {
        public var file = 65536, total = 1048576, files = 200, layer = 32768
    }

    public var agent: String
    /// `memory: true` (nil: agent.yaml doesn't validate).
    public var enabled: Bool?
    /// Its directives list `memory`.
    public var layer: Bool?
    public var files: [MemoryFile]
    public var size: Int
    public var limits: Limits

    public init(agent: String, enabled: Bool? = true, layer: Bool? = true, files: [MemoryFile] = [], size: Int = 0) {
        self.agent = agent
        self.enabled = enabled
        self.layer = layer
        self.files = files
        self.size = size
        limits = Limits()
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        agent = c.get("agent") ?? ""
        enabled = c.get("enabled")
        layer = c.get("layer")
        files = c.list("files")
        size = c.get("size") ?? 0
        var l = Limits()
        if let o: JSONValue = c.get("limits") {
            l.file = o["file"]?.int ?? l.file
            l.total = o["total"]?.int ?? l.total
            l.files = o["files"]?.int ?? l.files
            l.layer = o["layer"]?.int ?? l.layer
        }
        limits = l
    }

    /// A `memory` frame applied: the file added, replaced or (nil) removed.
    public mutating func apply(_ change: MemoryChange) {
        files.removeAll { $0.name == change.name }
        if let f = change.file { files.append(f); files.sort { $0.name < $1.name } }
        size = files.reduce(0) { $0 + $1.size }
    }
}

/// GET .../memory/{file}: the file now (`file` set), or as of a commit (`commit` set).
public struct MemoryText: Decodable, Sendable {
    public var name: String
    public var commit: String?
    public var file: MemoryFile?
    public var text: String

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        name = c.get("name") ?? ""
        commit = c.get("commit")
        file = c.get("file")
        text = c.get("text") ?? ""
    }
}

/// A `memory` frame: a file written (`file`) or deleted (nil).
public struct MemoryChange: Sendable, Equatable {
    public var agent: String
    public var name: String
    public var file: MemoryFile?

    public init(agent: String, name: String, file: MemoryFile?) {
        self.agent = agent
        self.name = name
        self.file = file
    }
}

/// A commit in a definitions repo's history (an agent's files, a memory file).
public struct HistoryEntry: Decodable, Identifiable, Hashable, Sendable {
    public var commit: String
    public var message: String
    public var at: Double

    public var id: String { commit }

    public init(commit: String, message: String = "", at: Double = 0) {
        self.commit = commit
        self.message = message
        self.at = at
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        guard let commit: String = c.get("commit") else { throw missing("commit", in: decoder) }
        self.commit = commit
        message = c.get("message") ?? ""
        at = c.get("at") ?? 0
    }

    public var short: String { String(commit.prefix(7)) }
}

struct MemoryHistory: Decodable {
    let history: [HistoryEntry]

    init(from decoder: Decoder) throws {
        history = try decoder.container(keyedBy: AnyKey.self).list("history")
    }
}

struct MemoryWrite: Decodable {
    let file: MemoryFile?

    init(from decoder: Decoder) throws {
        file = try decoder.container(keyedBy: AnyKey.self).get("file")
    }
}

/// Byte counts as the apps show them: "820 B", "12 KB", "3.4 MB".
public func byteSize(_ n: Int) -> String {
    if n < 1024 { return "\(n) B" }
    if n < 1_048_576 { return "\(Int((Double(n) / 1024).rounded())) KB" }
    return String(format: "%.1f MB", Double(n) / 1_048_576)
}
