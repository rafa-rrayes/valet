import Foundation

// The agent's computer (docs/API.md, "The agent's computer"): its terminal's socket, its files, its volumes.

extension APIClient {
    private func computer(_ agent: String) -> String { "/api/agents/\(Self.esc(agent))/computer" }

    // MARK: the terminal

    /// `/ws/terminal/{agent}?cols=&rows=`: ws:// or wss:// as the server's address is http or https.
    public func terminalURL(agent: String, size: TerminalSize) -> URL {
        var comps = URLComponents(url: url("/ws/terminal/\(Self.esc(agent))",
                                           query: [("cols", String(size.cols)), ("rows", String(size.rows))]),
                                  resolvingAgainstBaseURL: false) ?? URLComponents()
        comps.scheme = comps.scheme == "https" ? "wss" : "ws"
        return comps.url ?? baseURL
    }

    public func terminalRequest(agent: String, size: TerminalSize) -> URLRequest {
        var req = URLRequest(url: terminalURL(agent: agent, size: size))
        req.timeoutInterval = 30
        if let t = currentToken { req.setValue("Bearer \(t)", forHTTPHeaderField: "Authorization") }
        return req
    }

    // MARK: files

    /// What's at `path` (a folder's entries, or a file), or at its first root without one.
    public func files(_ agent: String, path: String? = nil) async throws -> FileListing {
        try await call("GET", computer(agent) + "/files", query: path.map { [("path", $0)] } ?? [])
    }

    /// The start of a file as text (or that it isn't text).
    public func fileText(_ agent: String, path: String) async throws -> FileText {
        try await call("GET", computer(agent) + "/files/text", query: [("path", path)])
    }

    /// A file's bytes.
    public func downloadFile(_ agent: String, path: String) async throws -> Data {
        let p = computer(agent) + "/files/download"
        return try await perform(rawRequest("GET", p, query: [("path", path)]), path: p).0
    }

    /// Writes a file into a folder; a taken name is a 409 with `.fail`.
    public func uploadFile(_ agent: String, dir: String, name: String, data: Data,
                           exists: UploadChoice = .fail) async throws -> UploadedFile {
        let p = computer(agent) + "/files/upload"
        let req = rawRequest("POST", p, query: [("dir", dir), ("name", name), ("exists", exists.rawValue)], body: data)
        return try decode(try await perform(req, path: p).0)
    }

    /// Makes a folder (and any missing above it); its path.
    public func makeFolder(_ agent: String, path: String) async throws -> String {
        let out: JSONValue = try await call("POST", computer(agent) + "/files/folder", body: ["path": .string(path)])
        return out["path"]?.string ?? path
    }

    // MARK: attachments

    /// Uploads a file to go with the next message.
    public func uploadAttachment(_ agent: String, name: String, data: Data) async throws -> Attachment {
        let p = "/api/agents/\(Self.esc(agent))/attachments"
        return try decode(try await perform(rawRequest("POST", p, query: [("name", name)], body: data), path: p).0)
    }

    /// Takes an unsent attachment off the message.
    public func deleteAttachment(_ agent: String, id: String) async throws {
        try await send("DELETE", "/api/agents/\(Self.esc(agent))/attachments/\(Self.esc(id))")
    }

    /// An attachment's bytes, from its `url` or an event's `uri`: only a path on this server (the token goes
    /// nowhere else).
    public func attachmentData(_ uri: String) async throws -> Data {
        guard Self.isServerPath(uri) else { throw APIError.invalidResponse }
        return try await perform(rawRequest("GET", uri, timeout: 60), path: uri).0
    }

    /// `/api/...`, already escaped, no query, no `..`.
    static func isServerPath(_ uri: String) -> Bool {
        var allowed = CharacterSet.urlPathAllowed
        allowed.insert("%")
        return uri.hasPrefix("/api/") && !uri.contains("/../") && !uri.hasSuffix("/..")
            && uri.rangeOfCharacter(from: allowed.inverted) == nil
    }

    // MARK: volumes

    public func volumes(_ agent: String) async throws -> AgentVolumes {
        try await call("GET", computer(agent) + "/volumes")
    }

    /// Deletes a stopped container agent's volumes for good; the names removed.
    public func deleteVolumes(_ agent: String) async throws -> [String] {
        let out: JSONValue = try await call("DELETE", computer(agent) + "/volumes")
        return out["removed"]?.array?.compactMap(\.string) ?? []
    }
}
