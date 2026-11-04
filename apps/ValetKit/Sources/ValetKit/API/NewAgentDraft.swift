import Foundation

/// What the New agent form holds (the web UI's `NewAgentForm`): a name, the ID that follows it until it's typed, an
/// emoji, what it starts from (a template, or a copy of an agent), and its brain. `body` is `POST /api/agents`'s
/// (docs/API.md "New agents"); `check` and `errors(from:)` put what's wrong under the form's fields.
public struct NewAgentDraft: Equatable, Sendable {
    public enum Start: Hashable, Sendable {
        case template(String)
        case copy(String)
    }

    /// The form's fields, the keys of its errors: `form` is for what belongs to none of them.
    public enum Field {
        public static let displayName = "display_name", name = "name", emoji = "emoji", start = "start",
                          harness = "harness", model = "model"
        static let all = [displayName, name, emoji, harness, model]
    }

    /// The name as typed (the display name).
    public private(set) var displayName = ""
    /// The agent's ID: follows the name (`slug`) until it's typed.
    public private(set) var id = ""
    public private(set) var idEdited = false
    /// One emoji; empty: the template's, or the copied agent's.
    public var emoji = ""
    public var start: Start
    /// nil: the server's default (the template's, or the copy's).
    public var harness: String?
    /// nil: the harness's default model.
    public var model: String?

    /// The server's agent names (`valet/shared/definition.py` NAME_RE).
    public static let idPattern = #"^[a-z0-9][a-z0-9-]{0,30}$"#
    /// Stands for the owner where an agent's name could (the shared space, memory's history): no agent may take it.
    public static let reserved = "owner"
    public static let maxName = 40

    public init(start: Start = .template("blank")) {
        self.start = start
    }

    /// A new form against the catalog: the first harness a runner can run (else the first), the first template.
    public init(catalog: Catalog?) {
        let templates = catalog?.templates ?? []
        self.init(start: .template(templates.first?.id ?? "blank"))
        let hs = catalog?.harnesses ?? []
        harness = (hs.first { $0.ready != false } ?? hs.first)?.id ?? "claude"
    }

    // MARK: editing

    public mutating func setName(_ text: String) {
        displayName = text
        if !idEdited { id = Self.slug(text) }
    }

    /// The ID typed: it no longer follows the name.
    public mutating func setID(_ text: String) {
        idEdited = true
        id = text.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// A copy of `agent`: its brain too, as the web UI does (the copy keeps it unless it's changed here).
    public mutating func copy(_ agent: Agent) {
        start = .copy(agent.name)
        if let h = agent.harness, !h.isEmpty {
            harness = h
            model = agent.model.flatMap { $0.isEmpty ? nil : $0 }
        }
    }

    public mutating func pickHarness(_ id: String) {
        guard id != harness else { return }
        harness = id
        model = nil
    }

    public var copyFrom: String? {
        if case .copy(let n) = start { return n }
        return nil
    }

    public var template: String? {
        if case .template(let t) = start { return t }
        return nil
    }

    var trimmedName: String { displayName.trimmingCharacters(in: .whitespacesAndNewlines) }

    /// A display name -> an agent ID (app.js's `slugify`): accents off, lowercase, spaces and underscores to dashes,
    /// anything else dropped, at most 31, no dash at either end.
    public static func slug(_ text: String) -> String {
        let plain = String(String.UnicodeScalarView(text.decomposedStringWithCompatibilityMapping.unicodeScalars
            .filter { !(0x300...0x36F).contains($0.value) }))
        var s = plain.lowercased()
        s = s.replacingOccurrences(of: #"[\s_]+"#, with: "-", options: .regularExpression)
        s = s.replacingOccurrences(of: "[^a-z0-9-]", with: "", options: .regularExpression)
        s = s.replacingOccurrences(of: "-+", with: "-", options: .regularExpression)
        s = s.replacingOccurrences(of: "^-+", with: "", options: .regularExpression)
        s = String(s.prefix(31))
        return s.replacingOccurrences(of: "-+$", with: "", options: .regularExpression)
    }

    // MARK: checks

    public static func takenMessage(_ id: String) -> String { "There's already an agent with the ID \(id)." }

    /// What the form says as the ID changes: only that it's taken (the rest waits for Create).
    public func liveError(taken: (String) -> Bool) -> String? {
        !id.isEmpty && taken(id) ? Self.takenMessage(id) : nil
    }

    /// What's wrong before sending, under its field; empty: send it.
    public func check(taken: (String) -> Bool) -> FieldErrors {
        let name = trimmedName
        if id.isEmpty {
            return name.isEmpty ? FieldErrors([Field.displayName: "Give it a name."])
                : FieldErrors([Field.name: "Pick an ID."])
        }
        if name.unicodeScalars.count > Self.maxName {
            return FieldErrors([Field.displayName: "At most \(Self.maxName) characters."])
        }
        if id.range(of: Self.idPattern, options: .regularExpression) == nil {
            return FieldErrors([Field.name: "The ID takes lowercase letters, digits and dashes, starting with a letter "
                                + "or digit, 31 at most."])
        }
        if id == Self.reserved {
            return FieldErrors([Field.name: "\(Self.reserved) stands for you in Valet (the shared space, memory's "
                                + "history): pick another ID."])
        }
        if taken(id) { return FieldErrors([Field.name: Self.takenMessage(id)]) }
        return FieldErrors()
    }

    // MARK: the request

    /// `POST /api/agents`: the ID, the template or the agent to copy, and what the form sets on top of it.
    public var body: JSONValue {
        var definition: [String: JSONValue] = [:]
        let name = trimmedName
        if !name.isEmpty { definition["display_name"] = .string(name) }
        let e = emoji.trimmingCharacters(in: .whitespacesAndNewlines)
        if !e.isEmpty { definition["emoji"] = .string(e) }
        if let harness { definition["harness"] = .string(harness) }
        definition["model"] = model.map { .string($0) } ?? .null
        var body: [String: JSONValue] = ["name": .string(id), "definition": .object(definition)]
        switch start {
        case .copy(let n): body["copy_from"] = .string(n)
        case .template(let t): body["template"] = .string(t.isEmpty ? "blank" : t)
        }
        return .object(body)
    }

    /// A refused create's errors, under the fields they're about (as the web UI places them).
    public func errors(from error: Error) -> FieldErrors {
        guard let e = error as? APIError else { return FieldErrors(general: [error.localizedDescription]) }
        let message = e.localizedDescription
        switch e.status {
        case 409:
            return FieldErrors([Field.name: Self.takenMessage(id)])
        case 422 where e.body?["errors"]?.array != nil:
            let f = FieldErrors(body: e.body, keys: Field.all)
            return f.isEmpty ? FieldErrors(general: [message]) : f
        case 422 where APIError.detail(of: e.body).hasPrefix("names "):
            return FieldErrors([Field.name: message])
        case 422 where APIError.detail(of: e.body).hasPrefix("no template"), 404:
            return FieldErrors([Field.start: message])
        default:
            return FieldErrors(general: [message])
        }
    }
}
