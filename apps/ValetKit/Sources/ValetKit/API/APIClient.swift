import Foundation
import os

public enum APIError: LocalizedError, Equatable, Sendable {
    /// 401 from any route but the login: the token is gone or expired.
    case unauthorized
    /// Any other non-2xx: its status, FastAPI's `detail` as text, and the whole body (a refused start's
    /// `missing_secrets`, a 410's `item`).
    case http(status: Int, detail: String, body: JSONValue?)
    /// The request didn't get an answer (no network, the server down, a timeout).
    case offline(String)
    /// An answer that isn't what the route returns (a proxy's page, a server too old for the route).
    case invalidResponse

    public var errorDescription: String? {
        switch self {
        case .unauthorized: return "Your session expired. Sign in again."
        case .http(let status, let detail, _): return detail.isEmpty ? "Valet answered \(status)." : detail.capitalizedFirst
        case .offline: return "Can't reach Valet. Check the connection."
        case .invalidResponse: return "Valet sent an answer this app doesn't understand."
        }
    }

    public var status: Int? {
        switch self {
        case .unauthorized: return 401
        case .http(let s, _, _): return s
        default: return nil
        }
    }

    public var body: JSONValue? {
        if case .http(_, _, let b) = self { return b }
        return nil
    }

    /// A start refused because secrets its definition lists aren't set (422 {detail, missing_secrets}).
    public var missingSecrets: [String] {
        guard status == 422, let list = body?["missing_secrets"]?.array else { return [] }
        return list.compactMap(\.string)
    }

    /// The inbox item a 410 carries (the runner let go of the request): `item`, or `detail.item`.
    public var item: InboxItem? {
        guard let v = body?["item"] ?? body?["detail"]?["item"] else { return nil }
        return v.decode(InboxItem.self)
    }

    /// FastAPI's detail as one text: a string, a 422's list of {msg}, or {detail}.
    static func detail(of body: JSONValue?) -> String {
        guard let d = body?["detail"] else { return "" }
        if let s = d.string { return s }
        if let list = d.array { return list.map { $0["msg"]?.string ?? $0.pretty }.joined(separator: "\n") }
        if let s = d["detail"]?.string { return s }
        return ""
    }
}

extension String {
    var capitalizedFirst: String { prefix(1).uppercased() + dropFirst() }
}

/// The owner API (docs/API.md). Thread-safe: requests and decoding run off the main actor, and the token can be
/// replaced at any time.
public final class APIClient: Sendable {
    public let baseURL: URL
    let session: URLSession
    private let token: OSAllocatedUnfairLock<String?>
    private let onUnauthorized: @Sendable () -> Void

    public init(baseURL: URL, token: String? = nil, urlSession: URLSession = .shared,
                onUnauthorized: @escaping @Sendable () -> Void = {}) {
        self.baseURL = baseURL
        self.session = urlSession
        self.onUnauthorized = onUnauthorized
        self.token = OSAllocatedUnfairLock(initialState: token)
    }

    public var currentToken: String? { token.withLock { $0 } }
    public func setToken(_ value: String?) { token.withLock { $0 = value } }

    // MARK: transport

    /// One path component, percent-encoded (a session id is the harness's and may hold anything).
    static func esc(_ s: String) -> String {
        var allowed = CharacterSet.urlPathAllowed
        allowed.remove(charactersIn: "/?#%")
        return s.addingPercentEncoding(withAllowedCharacters: allowed) ?? s
    }

    /// A name that may hold slashes (a vault entry, `github/alice`): its route takes the rest of the path.
    static func escPath(_ s: String) -> String {
        var allowed = CharacterSet.urlPathAllowed
        allowed.remove(charactersIn: "?#%")
        return s.addingPercentEncoding(withAllowedCharacters: allowed) ?? s
    }

    /// `path` (already escaped, starting with "/") under the server's address, which may have a path of its own.
    func url(_ path: String, query: [(String, String)] = []) -> URL {
        var comps = URLComponents(url: baseURL, resolvingAgainstBaseURL: false) ?? URLComponents()
        let prefix = comps.percentEncodedPath.hasSuffix("/") ? String(comps.percentEncodedPath.dropLast())
            : comps.percentEncodedPath
        comps.percentEncodedPath = prefix + path
        comps.queryItems = query.isEmpty ? nil : query.map { URLQueryItem(name: $0.0, value: $0.1) }
        return comps.url ?? baseURL
    }

    public func request(_ method: String, _ path: String, query: [(String, String)] = [],
                        body: JSONValue? = nil) -> URLRequest {
        var req = URLRequest(url: url(path, query: query))
        req.httpMethod = method
        req.timeoutInterval = 30
        req.cachePolicy = .reloadIgnoringLocalCacheData
        req.setValue("application/json", forHTTPHeaderField: "Accept")
        if let t = currentToken { req.setValue("Bearer \(t)", forHTTPHeaderField: "Authorization") }
        if let body {
            // Sorted keys: the same body every time (a definition's rules are written in the order they come).
            let enc = JSONEncoder()
            enc.outputFormatting = [.sortedKeys]
            req.httpBody = try? enc.encode(body)
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        return req
    }

    func data(_ method: String, _ path: String, query: [(String, String)] = [], body: JSONValue? = nil)
        async throws -> Data {
        try await perform(request(method, path, query: query, body: body), path: path).0
    }

    /// A request with a body that isn't JSON (a file's bytes), or a long transfer.
    func rawRequest(_ method: String, _ path: String, query: [(String, String)] = [], body: Data? = nil,
                    contentType: String = "application/octet-stream", timeout: TimeInterval = 300) -> URLRequest {
        var req = request(method, path, query: query)
        req.timeoutInterval = timeout
        if let body {
            req.httpBody = body
            req.setValue(contentType, forHTTPHeaderField: "Content-Type")
        }
        req.setValue("*/*", forHTTPHeaderField: "Accept")
        return req
    }

    /// Sends a request; its body and response on a 2xx, the API's errors otherwise.
    func perform(_ req: URLRequest, path: String) async throws -> (Data, HTTPURLResponse) {
        let data: Data, resp: URLResponse
        do {
            (data, resp) = try await session.data(for: req)
        } catch let e as URLError where e.code == .cancelled {
            throw CancellationError()
        } catch is CancellationError {
            throw CancellationError()
        } catch {
            throw APIError.offline(error.localizedDescription)
        }
        guard let http = resp as? HTTPURLResponse else { throw APIError.invalidResponse }
        guard (200..<300).contains(http.statusCode) else {
            let body = try? JSONDecoder().decode(JSONValue.self, from: data)
            if http.statusCode == 401 && path != "/api/login" {
                onUnauthorized()
                throw APIError.unauthorized
            }
            throw APIError.http(status: http.statusCode, detail: APIError.detail(of: body), body: body)
        }
        return (data, http)
    }

    func call<T: Decodable>(_ method: String, _ path: String, query: [(String, String)] = [],
                            body: JSONValue? = nil, as: T.Type = T.self) async throws -> T {
        let d = try await data(method, path, query: query, body: body)
        do {
            return try JSONDecoder().decode(T.self, from: d)
        } catch {
            throw APIError.invalidResponse
        }
    }

    /// A list route: elements that don't decode are dropped rather than failing the list.
    func list<T: Decodable>(_ path: String, query: [(String, String)] = [], of: T.Type = T.self) async throws -> [T] {
        try await call("GET", path, query: query, as: [Lenient<T>].self).compactMap(\.value)
    }

    func send(_ method: String, _ path: String, query: [(String, String)] = [], body: JSONValue? = nil) async throws {
        _ = try await data(method, path, query: query, body: body)
    }

    /// Decodes what a raw request answered (an upload's item).
    func decode<T: Decodable>(_ data: Data, as: T.Type = T.self) throws -> T {
        do { return try JSONDecoder().decode(T.self, from: data) } catch { throw APIError.invalidResponse }
    }

    // MARK: auth

    public func me() async throws -> Me { try await call("GET", "/api/me") }

    /// Signs in as a native client: returns the `vown_` token (the caller keeps it in the Keychain) and uses it.
    public func login(password: String, device: String) async throws -> String {
        let r: LoginResult = try await call("POST", "/api/login",
                                            body: ["password": .string(password), "client": "native",
                                                   "device": .string(device)])
        guard let t = r.token, !t.isEmpty else { throw APIError.invalidResponse }
        setToken(t)
        return t
    }

    public func logout() async throws { try await send("POST", "/api/logout") }

    // MARK: agents and runners

    public func agents() async throws -> [Agent] { try await list("/api/agents") }
    public func agent(_ name: String) async throws -> Agent { try await call("GET", "/api/agents/\(Self.esc(name))") }
    public func runners() async throws -> [Runner] { try await list("/api/runners") }
    public func catalog() async throws -> Catalog { try await call("GET", "/api/catalog") }

    /// Starts it (restarts it if it runs). Refused for missing secrets: `APIError.missingSecrets`.
    public func start(_ name: String) async throws -> Agent {
        try await call("POST", "/api/agents/\(Self.esc(name))/start")
    }

    public func stop(_ name: String) async throws -> Agent {
        try await call("POST", "/api/agents/\(Self.esc(name))/stop")
    }

    /// Sets a secret's value (write-only: nothing ever reads one back). `vault:<entry>` (what `missing_secrets`
    /// says of a vault entry without a password) sets that entry's password instead (app.js's setSecret).
    public func setSecret(_ name: String, value: String) async throws {
        guard let entry = Self.vaultEntry(name) else {
            try await send("PUT", "/api/secrets/\(Self.esc(name))", body: ["value": .string(value)])
            return
        }
        do {
            try await send("PUT", "/api/vault/" + Self.vaultPath(entry), body: ["password": .string(value)])
        } catch let e as APIError where e.status == 404 {
            throw APIError.http(status: 404, detail: "There's no vault entry \(entry): add it in the vault first.",
                                body: e.body)
        }
    }

    public func deleteSecret(_ name: String) async throws { try await send("DELETE", "/api/secrets/\(Self.esc(name))") }

    /// The vault entry a `vault:<entry>` name in `missing_secrets` stands for; nil for a plain secret.
    static func vaultEntry(_ name: String) -> String? {
        name.hasPrefix("vault:") ? String(name.dropFirst("vault:".count)) : nil
    }

    /// A vault entry's name in a path: github/alice stays github/alice, each part escaped.
    static func vaultPath(_ entry: String) -> String {
        entry.split(separator: "/", omittingEmptySubsequences: false).map { esc(String($0)) }.joined(separator: "/")
    }

    // MARK: sessions

    public func sessions(_ agent: String) async throws -> [Session] {
        try await list("/api/agents/\(Self.esc(agent))/sessions")
    }

    public func newSession(_ agent: String) async throws -> Session {
        try await call("POST", "/api/agents/\(Self.esc(agent))/sessions")
    }

    public func session(_ agent: String, _ id: String) async throws -> Session {
        try await call("GET", "/api/agents/\(Self.esc(agent))/sessions/\(Self.esc(id))")
    }

    public func events(_ agent: String, _ id: String, after: Int, limit: Int = 500) async throws -> EventPage {
        try await call("GET", "/api/agents/\(Self.esc(agent))/sessions/\(Self.esc(id))/events",
                       query: [("after", String(after)), ("limit", String(limit))])
    }

    /// Sends a prompt: text, attachments (their ids), or both.
    public func prompt(_ agent: String, _ id: String, text: String, attachments: [String] = []) async throws -> PromptResult {
        var body: [String: JSONValue] = ["text": .string(text)]
        if !attachments.isEmpty { body["attachments"] = .array(attachments.map { .string($0) }) }
        return try await call("POST", "/api/agents/\(Self.esc(agent))/sessions/\(Self.esc(id))/prompt",
                              body: .object(body))
    }

    public func cancel(_ agent: String, _ id: String) async throws {
        try await send("POST", "/api/agents/\(Self.esc(agent))/sessions/\(Self.esc(id))/cancel")
    }

    // MARK: inbox

    public func inbox(all: Bool = false) async throws -> [InboxItem] {
        try await list("/api/inbox", query: [("status", all ? "all" : "pending")])
    }

    public func inboxItem(_ id: String) async throws -> InboxItem {
        try await call("GET", "/api/inbox/\(Self.esc(id))")
    }

    /// Answers with one of its options, or with text (a `question`).
    public func answer(_ id: String, optionId: String? = nil, text: String? = nil) async throws -> InboxItem {
        var body: [String: JSONValue] = [:]
        if let optionId { body["option_id"] = .string(optionId) }
        if let text { body["text"] = .string(text) }
        return try await call("POST", "/api/inbox/\(Self.esc(id))/answer", body: .object(body))
    }

    // MARK: push devices (the m3-inbox track's routes: a server without them answers 404)

    /// Registers this device's APNs token; the server's id for it, if it says.
    @discardableResult
    public func registerDevice(token: String, platform: String, name: String) async throws -> String? {
        let d = try await data("POST", "/api/devices",
                               body: ["token": .string(token), "platform": .string(platform), "name": .string(name)])
        let v = try? JSONDecoder().decode(JSONValue.self, from: d)
        return (v?["id"] ?? v?["device"]?["id"])?.text
    }

    public func devices() async throws -> [Device] { try await call("GET", "/api/devices", as: DeviceList.self).devices }

    public func deleteDevice(_ id: String) async throws { try await send("DELETE", "/api/devices/\(Self.esc(id))") }

    // MARK: the live socket

    /// `/ws/client` on the same server: ws:// for http, wss:// for https.
    public var socketURL: URL {
        var comps = URLComponents(url: url("/ws/client"), resolvingAgainstBaseURL: false) ?? URLComponents()
        comps.scheme = comps.scheme == "https" ? "wss" : "ws"
        return comps.url ?? baseURL
    }

    public func socketRequest() -> URLRequest {
        var req = URLRequest(url: socketURL)
        req.timeoutInterval = 30
        if let t = currentToken { req.setValue("Bearer \(t)", forHTTPHeaderField: "Authorization") }
        return req
    }
}
