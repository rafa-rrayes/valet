import Foundation

/// An agent's definition as the settings form edits it: the JSON the server sent (`AgentDetail.definition`), with
/// typed accessors for the fields the form shows. Everything else (triggers, provider, fields a newer server adds)
/// stays as it came, and the whole object goes back on save (`PUT /api/agents/{name}/definition`), as the web UI
/// does. Optional fields cleared become JSON null, which the server reads as their default.
public struct DefinitionDraft: Equatable, Sendable {
    public var json: [String: JSONValue]

    public init(_ json: [String: JSONValue]) { self.json = json }

    // MARK: paths

    /// The value at a dotted path ("computer.level"); nil if any step is missing or not an object.
    public subscript(path: String) -> JSONValue? {
        var cur: JSONValue = .object(json)
        for key in path.split(separator: ".") {
            guard let next = cur[String(key)] else { return nil }
            cur = next
        }
        return cur
    }

    /// Sets the value at a dotted path, making the objects on the way (nil: JSON null).
    public mutating func set(_ path: String, _ value: JSONValue?) {
        let keys = path.split(separator: ".").map(String.init)
        guard !keys.isEmpty else { return }
        json = Self.setting(json, keys[...], value ?? .null)
    }

    private static func setting(_ obj: [String: JSONValue], _ keys: ArraySlice<String>, _ value: JSONValue)
        -> [String: JSONValue] {
        var o = obj
        guard let first = keys.first else { return o }
        if keys.count == 1 {
            o[first] = value
        } else {
            o[first] = .object(setting(o[first]?.object ?? [:], keys.dropFirst(), value))
        }
        return o
    }

    public func string(_ path: String) -> String? { self[path]?.string }

    /// Sets a text field: trimmed, and empty means null (the default).
    public mutating func setString(_ path: String, _ text: String?) {
        let t = text?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        set(path, t.isEmpty ? .null : .string(t))
    }

    public func strings(_ path: String) -> [String] { (self[path]?.array ?? []).compactMap(\.string) }

    public mutating func setStrings(_ path: String, _ list: [String]) { set(path, .array(list.map(JSONValue.string))) }

    // MARK: the form's fields

    public var displayName: String? { string("display_name") }
    public var emoji: String? { string("emoji") }
    public var harness: String { string("harness") ?? "claude" }
    public var model: String? { string("model") }
    public var permissions: String { string("permissions") ?? "ask" }
    public var level: String { string("computer.level") ?? "container" }
    /// `none` means the same as `gateway` (the catalog shows it as that).
    public var network: String {
        let n = string("computer.network") ?? "open"
        return n == "none" ? "gateway" : n
    }
    public var allowlist: [String] { strings("computer.allowlist") }
    public var image: String? { string("computer.image") }
    public var workdir: String? { string("computer.workdir") }
    public var mcp: [String] { strings("tools.mcp") }
    public var browserProfiles: [String] { strings("browser.profiles") }
    public var directives: [String] { strings("directives") }
    public var memory: Bool { self["memory"]?.bool ?? false }

    public struct Mount: Hashable, Sendable {
        public var host: String
        public var path: String
        public var readonly: Bool

        public init(host: String = "", path: String = "", readonly: Bool = false) {
            self.host = host
            self.path = path
            self.readonly = readonly
        }

        var json: JSONValue { ["host": .string(host), "path": .string(path), "readonly": .bool(readonly)] }
    }

    public var mounts: [Mount] {
        get {
            (self["computer.mounts"]?.array ?? []).map {
                Mount(host: $0["host"]?.string ?? "", path: $0["path"]?.string ?? "", readonly: $0["readonly"]?.bool ?? false)
            }
        }
        set { set("computer.mounts", .array(newValue.map(\.json))) }
    }

    /// tools.rules: server -> tool name or "*" -> allow | ask | deny.
    public var toolRules: [String: [String: String]] {
        get { Self.ruleMap(self["tools.rules"]) }
        set { set("tools.rules", Self.ruleJSON(newValue)) }
    }

    /// browser.rules: site pattern -> kind -> allow | ask | deny.
    public var siteRules: SiteRules {
        get { Self.ruleMap(self["browser.rules"]) }
        set { set("browser.rules", Self.ruleJSON(newValue)) }
    }

    /// The rule for one tool of a server ("*": all of its tools), nil for none.
    public func toolRule(_ server: String, _ tool: String) -> String? { toolRules[server]?[tool] }

    /// Sets or (nil) removes a tool's rule; a server left without rules goes.
    public mutating func setToolRule(_ server: String, _ tool: String, _ rule: String?) {
        var all = toolRules
        var list = all[server] ?? [:]
        list[tool] = rule
        all[server] = list.isEmpty ? nil : list
        toolRules = all
    }

    static func ruleMap(_ v: JSONValue?) -> [String: [String: String]] {
        var out: [String: [String: String]] = [:]
        for (k, inner) in v?.object ?? [:] {
            var m: [String: String] = [:]
            for (k2, r) in inner.object ?? [:] { if let s = r.string { m[k2] = s } }
            out[k] = m
        }
        return out
    }

    static func ruleJSON(_ m: [String: [String: String]]) -> JSONValue {
        .object(m.filter { !$0.value.isEmpty }.mapValues { .object($0.mapValues(JSONValue.string)) })
    }

    /// The secrets it lists: names, and vault entries (`{"vault", "env"}`), as they came.
    public var secrets: [JSONValue] {
        get { self["secrets"]?.array ?? [] }
        set { set("secrets", .array(newValue)) }
    }

    /// The names among `secrets` (vault entries left out).
    public var secretNames: [String] { secrets.compactMap(\.string) }

    // MARK: lists the form switches entries of

    /// Adds `item` to the list at `path` (in its saved place, so off and on again is no change) or removes it.
    public mutating func toggle(_ path: String, _ item: String, on: Bool, savedOrder: [String] = []) {
        var rest = strings(path).filter { $0 != item }
        if on {
            rest.append(item)
            let rank = { (x: String) in savedOrder.firstIndex(of: x) ?? savedOrder.count }
            rest = rest.enumerated().sorted { (rank($0.element), $0.offset) < (rank($1.element), $1.offset) }
                .map(\.element)
        }
        setStrings(path, rest)
    }

    /// The order of DESIGN.md's layers: owner, authority/<preset>, persona.md, environment, memory.
    static let directiveOrder = ["owner", "authority/", "persona.md", "environment", "memory"]

    static func directiveRank(_ e: String) -> Int {
        let key = e.hasPrefix("authority/") ? "authority/" : e
        return directiveOrder.firstIndex(of: key) ?? directiveOrder.count
    }

    /// A directive layer on (in its place, the others staying where they are) or off (app.js's withDirective).
    public mutating func setDirective(_ entry: String, on: Bool) {
        var rest = directives.filter { $0 != entry }
        if on {
            let at = rest.firstIndex { Self.directiveRank($0) > Self.directiveRank(entry) } ?? rest.count
            rest.insert(entry, at: at)
        }
        setStrings("directives", rest)
    }

    /// The standing-authority preset it's told (`authority/<name>` in directives), if any.
    public var preset: String? {
        directives.first { $0.hasPrefix("authority/") }.map { String($0.dropFirst("authority/".count)) }
    }

    /// Its standing authority set to the preset `name` (nil: none), where the old one was (app.js's withPreset).
    public mutating func setPreset(_ name: String?) {
        let list = directives
        if let cur = list.first(where: { $0.hasPrefix("authority/") }), let name {
            setStrings("directives", list.map { $0 == cur ? "authority/" + name : $0 })
            return
        }
        setStrings("directives", list.filter { !$0.hasPrefix("authority/") })
        if let name { setDirective("authority/" + name, on: true) }
    }

    /// tools.skills: the shared skills it picks, by name, or "*" for all of them.
    public var skills: [String] { strings("tools.skills") }

    /// "All shared skills" on (`["*"]`) or off: back to what the saved definition picks by name, or, if it had
    /// all of them, each one the library has now (app.js's toggleAllSkills).
    public mutating func setAllSkills(_ on: Bool, saved: [String], library: [String]) {
        setStrings("tools.skills", on ? ["*"] : saved.contains("*") ? library : saved)
    }

    /// Memory on or off, with its layer (as the web UI's switch does).
    public mutating func setMemory(_ on: Bool) {
        set("memory", .bool(on))
        setDirective("memory", on: on)
    }

    // MARK: comparing

    /// The definition with nulls left out at every level, so a field cleared to null and one never set compare
    /// the same.
    public var normalized: JSONValue { Self.strip(.object(json)) }

    static func strip(_ v: JSONValue) -> JSONValue {
        switch v {
        case .object(let o): return .object(o.filter { !$0.value.isNull }.mapValues(strip))
        case .array(let a): return .array(a.map(strip))
        default: return v
        }
    }

    public func same(as other: DefinitionDraft) -> Bool { normalized == other.normalized }
}

/// A 422's per-field errors (`{"detail", "errors": [{"field", "msg"}]}`), placed under the form's controls: each goes
/// to the longest key the form shows that is its field or a parent of it (app.js's fieldFor); the rest are general.
public struct FieldErrors: Equatable, Sendable {
    public private(set) var byKey: [String: String] = [:]
    public private(set) var general: [String] = []

    public init() {}

    /// Errors found before sending (a form's own checks).
    public init(_ byKey: [String: String] = [:], general: [String] = []) {
        self.byKey = byKey
        self.general = general
    }

    public init(body: JSONValue?, keys: [String]) {
        for e in body?["errors"]?.array ?? [] {
            let field = e["field"]?.text ?? ""
            let msg = e["msg"]?.text ?? ""
            if let k = Self.key(for: field, in: keys) {
                byKey[k] = byKey[k].map { $0 + "\n" + msg } ?? msg
            } else {
                general.append(field.isEmpty ? msg : "\(field): \(msg)")
            }
        }
    }

    public static func key(for field: String, in keys: [String]) -> String? {
        var best: String?
        for k in keys where field == k || field.hasPrefix(k + ".") {
            if best == nil || k.count > best!.count { best = k }
        }
        return best
    }

    public subscript(key: String) -> String? { byKey[key] }

    public var isEmpty: Bool { byKey.isEmpty && general.isEmpty }

    public mutating func clear(_ key: String) { byKey[key] = nil }
}
