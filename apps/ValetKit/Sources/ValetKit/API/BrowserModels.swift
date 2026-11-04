import Foundation

// The browser's shapes (docs/API.md, "The browser", "The vault", "The live view"). Lenient like the rest.

/// What a browser ask is about: a BrowserAsk's `kind`.
public struct BrowserKind: OpenEnum {
    public let rawValue: String
    public init(rawValue: String) { self.rawValue = rawValue }
    /// read: look at a page; interact: type, choose, a key; commit: submit, buy, send; login: fill a vault entry;
    /// human: request_human, the owner takes over.
    public static let read: Self = "read", interact: Self = "interact", commit: Self = "commit", login: Self = "login",
                      human: Self = "human"

    /// As the apps say it.
    public var label: String {
        switch self {
        case .read: return "read"
        case .interact: return "interact"
        case .commit: return "commit"
        case .login: return "sign in"
        case .human: return "take over"
        default: return rawValue.isEmpty ? "browser" : rawValue
        }
    }
}

/// valet.browser asks before an action, or asks the owner to take over a tab (request_human). On an inbox item as
/// `browser`, and on its permission_request event's data.
public struct BrowserAsk: Decodable, Hashable, Sendable {
    public var profile: String
    public var tab: String?
    public var url: String
    public var host: String
    public var kind: BrowserKind
    /// Plain words: 'click "Save"', "open https://...", "sign in on x as y".
    public var summary: String
    /// Per action: url, ref, element {tag, type, name}, field, text (typing), values, key, entry and site (login),
    /// reason (human).
    public var detail: [String: JSONValue]
    /// A screenshot of the tab when it asked: `GET /api/browser/shots/<shot>`.
    public var shot: String?

    public init(profile: String, tab: String? = nil, url: String = "", host: String = "", kind: BrowserKind = .interact,
                summary: String = "", detail: [String: JSONValue] = [:], shot: String? = nil) {
        self.profile = profile
        self.tab = tab
        self.url = url
        self.host = host
        self.kind = kind
        self.summary = summary
        self.detail = detail
        self.shot = shot
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        profile = c.get("profile") ?? ""
        tab = c.get("tab")
        url = c.get("url") ?? ""
        host = c.get("host") ?? ""
        kind = c.get("kind") ?? .interact
        summary = c.get("summary") ?? ""
        detail = c.get("detail", as: JSONValue.self)?.object ?? [:]
        shot = c.get("shot")
    }

    public var isTakeOver: Bool { kind == .human }

    /// The take-over's reason (request_human's `reason`).
    public var reason: String? { detail["reason"]?.string.flatMap { $0.isEmpty ? nil : $0 } }

    /// The details worth showing, as label and text, in a stable order: what element, what's typed, which values,
    /// which key, which vault entry and site. The URL and the reason are shown elsewhere.
    public var detailLines: [(label: String, text: String)] {
        var out: [(String, String)] = []
        if let el = detail["element"]?.object {
            let name = el["name"]?.text ?? ""
            let what = [el["tag"]?.text, el["type"]?.text].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " ")
            let line = [name.isEmpty ? nil : "\u{201C}\(name)\u{201D}", what.isEmpty ? nil : "(\(what))"]
                .compactMap { $0 }.joined(separator: " ")
            if !line.isEmpty { out.append(("Element", line)) }
        }
        if let f = detail["field"]?.text, !f.isEmpty { out.append(("Field", f)) }
        if let t = detail["text"]?.string, !t.isEmpty { out.append(("Types", t)) }
        if let v = detail["values"]?.array {
            let s = v.compactMap(\.text).joined(separator: ", ")
            if !s.isEmpty { out.append(("Chooses", s)) }
        }
        if let k = detail["key"]?.text, !k.isEmpty { out.append(("Key", k)) }
        if let e = detail["entry"]?.text, !e.isEmpty { out.append(("Login", e)) }
        if let s = detail["site"]?.text, !s.isEmpty { out.append(("For site", s)) }
        if let s = detail["submit"]?.bool, s { out.append(("Then", "submits the form")) }
        return out
    }
}

/// A profile's or an agent's site rules: pattern -> kind -> allow | ask | deny.
public typealias SiteRules = [String: [String: String]]

/// browser.yaml's profiles, as the API shows them.
public struct BrowserProfile: Decodable, Identifiable, Hashable, Sendable {
    public var name: String
    public var title: String?
    public var note: String?
    public var sites: SiteRules
    public var usedBy: [String]
    public var vault: Int
    public var running: Bool

    public var id: String { name }

    public init(name: String, title: String? = nil, note: String? = nil, sites: SiteRules = [:], usedBy: [String] = [],
                vault: Int = 0, running: Bool = false) {
        self.name = name
        self.title = title
        self.note = note
        self.sites = sites
        self.usedBy = usedBy
        self.vault = vault
        self.running = running
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        guard let name: String = c.get("name") else { throw missing("name", in: decoder) }
        self.name = name
        title = c.get("title")
        note = c.get("note")
        sites = Self.rules(c.get("sites", as: JSONValue.self))
        usedBy = c.list("used_by")
        vault = c.get("vault") ?? 0
        running = c.get("running") ?? false
    }

    /// Site rules from JSON, dropping what isn't pattern -> kind -> string.
    static func rules(_ v: JSONValue?) -> SiteRules {
        var out: SiteRules = [:]
        for (pattern, kinds) in v?.object ?? [:] {
            var k: [String: String] = [:]
            for (kind, mode) in kinds.object ?? [:] { if let m = mode.string { k[kind] = m } }
            out[pattern] = k
        }
        return out
    }

    public var displayTitle: String {
        if let t = title?.trimmingCharacters(in: .whitespaces), !t.isEmpty { return t }
        return name
    }
}

struct BrowserProfiles: Decodable {
    let profiles: [BrowserProfile]
    let error: String?

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        profiles = c.list("profiles")
        error = c.get("error")
    }
}

public struct BrowserTab: Decodable, Identifiable, Hashable, Sendable {
    public var tab: String
    public var url: String
    public var title: String

    public var id: String { tab }

    public init(tab: String, url: String = "", title: String = "") {
        self.tab = tab
        self.url = url
        self.title = title
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        guard let tab: String = c.get("tab") ?? c.get("id") else { throw missing("tab", in: decoder) }
        self.tab = tab
        url = c.get("url") ?? ""
        title = c.get("title") ?? ""
    }

    /// Its title, else its URL, else "New tab".
    public var label: String {
        if !title.trimmingCharacters(in: .whitespaces).isEmpty { return title }
        return url.isEmpty || url == "about:blank" ? "New tab" : url
    }
}

public struct RunningBrowser: Decodable, Identifiable, Hashable, Sendable {
    public var profile: String
    public var tabs: Int
    public var startedAt: Double
    public var idle: Double
    public var busy: Bool
    public var viewers: Int

    public var id: String { profile }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        guard let p: String = c.get("profile") else { throw missing("profile", in: decoder) }
        profile = p
        tabs = c.get("tabs") ?? 0
        startedAt = c.get("started_at") ?? 0
        idle = c.get("idle") ?? 0
        busy = c.get("busy") ?? false
        viewers = c.get("viewers") ?? 0
    }
}

/// A saved login for a profile. Never a value: `hasPassword` and `hasTotp` say whether there is one.
public struct VaultEntry: Decodable, Identifiable, Hashable, Sendable {
    public var name: String
    public var profile: String
    public var site: String
    public var label: String?
    public var username: String?
    public var hasPassword: Bool
    public var hasTotp: Bool
    public var createdAt: Double
    public var updatedAt: Double
    public var usedAt: Double?
    public var usedBy: [String]

    public var id: String { name }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        guard let name: String = c.get("name") else { throw missing("name", in: decoder) }
        self.name = name
        profile = c.get("profile") ?? ""
        site = c.get("site") ?? ""
        label = c.get("label")
        username = c.get("username")
        hasPassword = c.get("has_password") ?? false
        hasTotp = c.get("has_totp") ?? false
        createdAt = c.get("created_at") ?? 0
        updatedAt = c.get("updated_at") ?? createdAt
        usedAt = c.get("used_at")
        usedBy = c.list("used_by")
    }
}

/// What to write to a vault entry: a field left nil isn't sent (it stays); an empty password or 2FA secret clears
/// it. Values only ever go to the server.
public struct VaultWrite: Sendable, Equatable {
    public var profile: String?
    public var site: String?
    public var label: String?
    public var username: String?
    public var password: String?
    public var totp: String?

    public init(profile: String? = nil, site: String? = nil, label: String? = nil, username: String? = nil,
                password: String? = nil, totp: String? = nil) {
        self.profile = profile
        self.site = site
        self.label = label
        self.username = username
        self.password = password
        self.totp = totp
    }

    func body(name: String? = nil) -> JSONValue {
        var o: [String: JSONValue] = [:]
        if let name { o["name"] = .string(name) }
        for (k, v) in [("profile", profile), ("site", site), ("label", label), ("username", username),
                       ("password", password), ("totp", totp)] {
            if let v { o[k] = .string(v) }
        }
        return .object(o)
    }
}

/// An "Allow for an hour" answer on a browser ask: that host and kind, for that agent.
public struct BrowserGrant: Decodable, Identifiable, Hashable, Sendable {
    public var id: Int
    public var agent: String
    public var profile: String
    public var host: String
    public var kind: String
    public var expiresAt: Double

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        guard let id: Int = c.get("id") else { throw missing("id", in: decoder) }
        self.id = id
        agent = c.get("agent") ?? ""
        profile = c.get("profile") ?? ""
        host = c.get("host") ?? ""
        kind = c.get("kind") ?? ""
        expiresAt = c.get("expires_at") ?? 0
    }
}

/// What to open in the live view: a profile, a tab in it (nil: its newest), and the take-over ask that sent the owner
/// there, if one did (its Done and I can't show under the view).
public struct LiveTarget: Hashable, Identifiable, Sendable {
    public var profile: String
    public var tab: String?
    public var itemID: String?

    public init(profile: String, tab: String? = nil, itemID: String? = nil) {
        self.profile = profile
        self.tab = tab
        self.itemID = itemID
    }

    public var id: String { [profile, tab ?? "", itemID ?? ""].joined(separator: "/") }
}
