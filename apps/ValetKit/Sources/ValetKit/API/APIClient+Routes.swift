import Foundation

// Phase 2's routes (docs/API.md): the browser and the vault, the shared space, memory, an agent's settings, secrets,
// the gateway's tools and grants, notifications, hand-off, and (M4) triggers, wake-ups, Retry and the queue.

extension APIClient {
    private func agentPath(_ name: String) -> String { "/api/agents/\(Self.esc(name))" }

    // MARK: the browser

    /// A screenshot an ask or the audit names (JPEG bytes).
    public func shot(_ name: String) async throws -> Data {
        try await perform(rawRequest("GET", "/api/browser/shots/\(Self.esc(name))", timeout: 30),
                          path: "/api/browser/shots").0
    }

    /// The profiles, and browser.yaml's error when it doesn't validate.
    public func browserProfiles() async throws -> (profiles: [BrowserProfile], error: String?) {
        let r: BrowserProfiles = try await call("GET", "/api/browser/profiles")
        return (r.profiles, r.error)
    }

    public func createProfile(name: String, title: String?, note: String?, sites: SiteRules) async throws -> BrowserProfile {
        try await call("POST", "/api/browser/profiles", body: Self.profileBody(name: name, title: title, note: note,
                                                                               sites: sites))
    }

    /// Replaces the profile (a field left out is cleared).
    public func updateProfile(_ name: String, title: String?, note: String?, sites: SiteRules) async throws -> BrowserProfile {
        try await call("PUT", "/api/browser/profiles/\(Self.esc(name))",
                       body: Self.profileBody(name: nil, title: title, note: note, sites: sites))
    }

    static func profileBody(name: String?, title: String?, note: String?, sites: SiteRules) -> JSONValue {
        var o: [String: JSONValue] = ["sites": DefinitionDraft.ruleJSON(sites)]
        if let name { o["name"] = .string(name) }
        if let t = title?.trimmingCharacters(in: .whitespacesAndNewlines), !t.isEmpty { o["title"] = .string(t) }
        if let n = note?.trimmingCharacters(in: .whitespacesAndNewlines), !n.isEmpty { o["note"] = .string(n) }
        return .object(o)
    }

    /// Deletes a profile; `wipe` also deletes its cookies and sessions.
    public func deleteProfile(_ name: String, wipe: Bool) async throws {
        try await send("DELETE", "/api/browser/profiles/\(Self.esc(name))", query: [("wipe", wipe ? "true" : "false")])
    }

    public func stopBrowser(_ name: String) async throws {
        try await send("POST", "/api/browser/profiles/\(Self.esc(name))/stop")
    }

    public func browserTabs(_ profile: String) async throws -> [BrowserTab] {
        try await list("/api/browser/profiles/\(Self.esc(profile))/tabs")
    }

    public func openTab(_ profile: String, url: String?) async throws -> BrowserTab {
        var body: [String: JSONValue] = [:]
        if let url, !url.isEmpty { body["url"] = .string(url) }
        return try await call("POST", "/api/browser/profiles/\(Self.esc(profile))/tabs", body: .object(body))
    }

    public func runningBrowsers() async throws -> [RunningBrowser] { try await list("/api/browser/running") }

    public func browserYAML() async throws -> (text: String, commit: String) {
        let v: JSONValue = try await call("GET", "/api/browser/yaml")
        return (v["text"]?.string ?? "", v["commit"]?.string ?? "")
    }

    public func saveBrowserYAML(_ text: String) async throws {
        try await send("PUT", "/api/browser/yaml", body: ["text": .string(text)])
    }

    public func browserGrants(agent: String? = nil) async throws -> [BrowserGrant] {
        try await list("/api/browser/grants", query: agent.map { [("agent", $0)] } ?? [])
    }

    public func deleteBrowserGrant(_ id: Int) async throws { try await send("DELETE", "/api/browser/grants/\(id)") }

    /// `/ws/browser/{profile}?tab=`: the live view, ws:// or wss:// as the server's address is http or https.
    public func liveURL(profile: String, tab: String?) -> URL {
        var comps = URLComponents(url: url("/ws/browser/\(Self.esc(profile))", query: tab.map { [("tab", $0)] } ?? []),
                                  resolvingAgainstBaseURL: false) ?? URLComponents()
        comps.scheme = comps.scheme == "https" ? "wss" : "ws"
        return comps.url ?? baseURL
    }

    public func liveRequest(profile: String, tab: String?) -> URLRequest {
        var req = URLRequest(url: liveURL(profile: profile, tab: tab))
        req.timeoutInterval = 30
        if let t = currentToken { req.setValue("Bearer \(t)", forHTTPHeaderField: "Authorization") }
        return req
    }

    // MARK: the vault

    public func vault(profile: String? = nil) async throws -> [VaultEntry] {
        try await list("/api/vault", query: profile.map { [("profile", $0)] } ?? [])
    }

    public func createVaultEntry(_ name: String, _ w: VaultWrite) async throws -> VaultEntry {
        try await call("POST", "/api/vault", body: w.body(name: name))
    }

    /// Changes the fields set in `w`; the others stay. An empty password or 2FA secret clears it.
    public func updateVaultEntry(_ name: String, _ w: VaultWrite) async throws -> VaultEntry {
        try await call("PUT", "/api/vault/\(Self.escPath(name))", body: w.body())
    }

    public func deleteVaultEntry(_ name: String) async throws { try await send("DELETE", "/api/vault/\(Self.escPath(name))") }

    // MARK: the shared space

    /// Where an agent's shared space is; nil: the unassigned items (from when Shared was one list for every agent).
    private func sharedPath(_ agent: String?) -> String {
        agent.map { "\(agentPath($0))/shared" } ?? "/api/shared/unassigned"
    }

    /// One agent's items (nil: the unassigned), most recently changed first.
    public func shared(_ agent: String?, _ query: SharedQuery = SharedQuery(), before: Double? = nil,
                       limit: Int = 50) async throws -> SharedPage {
        var q: [(String, String)] = [("limit", String(limit))]
        if !query.q.isEmpty { q.append(("q", query.q)) }
        if !query.by.isEmpty { q.append(("by", query.by)) }
        if !query.kind.isEmpty { q.append(("kind", query.kind)) }
        if let before { q.append(("before", String(before))) }
        return try await call("GET", sharedPath(agent), query: q)
    }

    public func sharedCounts() async throws -> SharedCounts { try await call("GET", "/api/shared/unseen") }

    /// Marks items seen (nil: everything); the counts after.
    @discardableResult
    public func markSeen(_ ids: [Int]?) async throws -> SharedCounts {
        let body: JSONValue = ids.map { ["ids": .array($0.map { .number(Double($0)) })] } ?? [:]
        return try await call("POST", "/api/shared/seen", body: body)
    }

    public func sharedItem(_ id: Int) async throws -> SharedItem { try await call("GET", "/api/shared/\(id)") }

    /// A new note in the agent's space.
    public func addNote(agent: String, title: String?, text: String) async throws -> SharedItem {
        var body: [String: JSONValue] = ["text": .string(text)]
        if let t = title?.trimmingCharacters(in: .whitespacesAndNewlines), !t.isEmpty { body["title"] = .string(t) }
        return try await call("POST", "\(sharedPath(agent))/notes", body: .object(body))
    }

    /// Uploads one file into the agent's space (the raw body form); `text` is its description.
    public func upload(agent: String, name: String, data: Data, text: String? = nil,
                       mime: String? = nil) async throws -> [SharedItem] {
        var q: [(String, String)] = [("name", name)]
        if let text, !text.isEmpty { q.append(("text", text)) }
        let path = "\(sharedPath(agent))/files"
        let req = rawRequest("POST", path, query: q, body: data, contentType: mime ?? "application/octet-stream")
        let (d, _) = try await perform(req, path: path)
        if let list = try? JSONDecoder().decode([Lenient<SharedItem>].self, from: d) { return list.compactMap(\.value) }
        return [try decode(d, as: SharedItem.self)]
    }

    /// An item (an unassigned one) into an agent's space.
    public func moveShared(_ id: Int, to agent: String) async throws -> SharedItem {
        try await call("POST", "/api/shared/\(id)/move", body: ["agent": .string(agent)])
    }

    /// A note's title and text, a file's name and description: only the ones given change.
    public func updateShared(_ id: Int, title: String? = nil, text: String? = nil) async throws -> SharedItem {
        var body: [String: JSONValue] = [:]
        if let title { body["title"] = .string(title) }
        if let text { body["text"] = .string(text) }
        return try await call("PATCH", "/api/shared/\(id)", body: .object(body))
    }

    public func deleteShared(_ id: Int) async throws { try await send("DELETE", "/api/shared/\(id)") }

    /// A file's bytes.
    public func sharedFile(_ id: Int) async throws -> Data {
        try await perform(rawRequest("GET", "/api/shared/\(id)/file"), path: "/api/shared/file").0
    }

    /// Replaces a file's bytes (`name` renames it).
    public func replaceSharedFile(_ id: Int, data: Data, name: String?, mime: String? = nil) async throws -> SharedItem {
        let req = rawRequest("PUT", "/api/shared/\(id)/file", query: name.map { [("name", $0)] } ?? [], body: data,
                             contentType: mime ?? "application/octet-stream")
        return try decode(try await perform(req, path: "/api/shared/file").0)
    }

    // MARK: memory

    public func memory(_ agent: String) async throws -> AgentMemory { try await call("GET", agentPath(agent) + "/memory") }

    /// A memory file's text now, or (`at`) as of a commit in its history.
    public func memoryFile(_ agent: String, _ file: String, at commit: String? = nil) async throws -> MemoryText {
        try await call("GET", agentPath(agent) + "/memory/\(Self.esc(file))", query: commit.map { [("at", $0)] } ?? [])
    }

    @discardableResult
    public func writeMemory(_ agent: String, _ file: String, text: String) async throws -> MemoryFile? {
        let r: MemoryWrite = try await call("PUT", agentPath(agent) + "/memory/\(Self.esc(file))",
                                            body: ["text": .string(text)])
        return r.file
    }

    public func deleteMemory(_ agent: String, _ file: String) async throws {
        try await send("DELETE", agentPath(agent) + "/memory/\(Self.esc(file))")
    }

    public func memoryHistory(_ agent: String, _ file: String) async throws -> [HistoryEntry] {
        let r: MemoryHistory = try await call("GET", agentPath(agent) + "/memory/\(Self.esc(file))/history",
                                              query: [("limit", "50")])
        return r.history
    }

    // MARK: an agent's settings

    public func agentDetail(_ name: String) async throws -> AgentDetail { try await call("GET", agentPath(name)) }

    /// Creates an agent (the New agent form's `NewAgentDraft.body`): 201 its detail.
    public func createAgent(_ draft: NewAgentDraft) async throws -> AgentDetail {
        try await call("POST", "/api/agents", body: draft.body)
    }

    /// Deletes an agent: it stops, its runner forgets it, its folder leaves the definitions repo and the control
    /// plane forgets the rest, its Shared items included. `volumes`: a container agent's volumes go too.
    @discardableResult
    public func deleteAgent(_ name: String, volumes: Bool = false) async throws -> AgentDeleted {
        try await call("DELETE", agentPath(name), query: volumes ? [("volumes", "1")] : [])
    }

    /// The settings page's save: the whole definition, and the persona if it changed (nil leaves persona.md).
    public func saveDefinition(_ name: String, _ definition: DefinitionDraft, persona: String?) async throws -> AgentDetail {
        var body: [String: JSONValue] = ["definition": .object(definition.json)]
        if let persona { body["persona"] = .string(persona) }
        return try await call("PUT", agentPath(name) + "/definition", body: .object(body))
    }

    /// Writes agent.yaml or persona.md as text; the new commit.
    @discardableResult
    public func saveAgentFile(_ name: String, file: String, text: String) async throws -> String {
        let v: JSONValue = try await call("PUT", agentPath(name) + "/files/\(Self.esc(file))", body: ["text": .string(text)])
        return v["commit"]?.string ?? ""
    }

    public func agentHistory(_ name: String) async throws -> [HistoryEntry] { try await list(agentPath(name) + "/history") }

    public func restoreAgent(_ name: String, commit: String) async throws -> AgentDetail {
        try await call("POST", agentPath(name) + "/restore", body: ["commit": .string(commit)])
    }

    public func secrets() async throws -> [Secret] { try await list("/api/secrets") }

    public func gatewayServers() async throws -> GatewayServers { try await call("GET", "/api/gateway/servers") }

    /// What the agent gets through the gateway, decided at `permissions` (a preview) or its saved level.
    public func agentTools(_ name: String, permissions: String? = nil) async throws -> AgentTools {
        try await call("GET", agentPath(name) + "/tools", query: permissions.map { [("permissions", $0)] } ?? [])
    }

    public func toolGrants(agent: String) async throws -> [ToolGrant] {
        try await list("/api/gateway/grants", query: [("agent", agent)])
    }

    public func deleteToolGrant(_ id: Int) async throws { try await send("DELETE", "/api/gateway/grants/\(id)") }

    // MARK: notifications

    public func notifications() async throws -> NotificationSettings { try await call("GET", "/api/notifications") }

    /// Sets the webhook (nil or "": removes it).
    public func setWebhook(_ url: String?) async throws -> NotificationSettings {
        let u = url?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return try await call("PUT", "/api/notifications", body: ["webhook_url": u.isEmpty ? .null : .string(u)])
    }

    public func testNotifications() async throws -> NotifyTestResult { try await call("POST", "/api/notifications/test") }

    // MARK: continuing and handing off

    /// Claude Code's sessions in the agent's working folder.
    public func claudeSessions(agent: String) async throws -> ClaudeSessions {
        try await call("GET", agentPath(agent) + "/claude-sessions")
    }

    /// Continues a Claude Code session in the agent (it may take minutes: the history loads first).
    public func importClaudeSession(agent: String, id: String) async throws -> Session {
        var req = request("POST", agentPath(agent) + "/claude-sessions/\(Self.esc(id))/import")
        req.timeoutInterval = 330
        return try decode(try await perform(req, path: "/import").0)
    }

    public func claudeFolders(runner: String?) async throws -> ClaudeFolders {
        try await call("GET", "/api/claude/folders", query: runner.map { [("runner", $0)] } ?? [])
    }

    public func claudeFolderSessions(folder: String, runner: String?) async throws -> ClaudeSessions {
        var q = [("folder", folder)]
        if let runner { q.append(("runner", runner)) }
        return try await call("GET", "/api/claude/sessions", query: q)
    }

    public func handoffPreview(_ source: HandoffSource) async throws -> HandoffText {
        try await call("POST", "/api/handoff/preview", body: ["source": source.json])
    }

    /// Starts a new session of `agent` with the conversation handed off; the session.
    public func handoff(agent: String, source: HandoffSource, note: String) async throws -> Session {
        var body: [String: JSONValue] = ["source": source.json]
        let n = note.trimmingCharacters(in: .whitespacesAndNewlines)
        if !n.isEmpty { body["note"] = .string(n) }
        var req = request("POST", agentPath(agent) + "/handoff", body: .object(body))
        req.timeoutInterval = 120
        let r: HandoffResult = try decode(try await perform(req, path: "/handoff").0)
        guard let s = r.session else { throw APIError.invalidResponse }
        return s
    }

    // MARK: always-on

    /// Its schedules and webhooks (from the saved agent.yaml), pending wake-ups, and the soonest run.
    public func triggers(_ agent: String) async throws -> AgentTriggers { try await call("GET", agentPath(agent) + "/triggers") }

    /// Its runs, newest first; `before` (a run id) pages back.
    public func triggerRuns(_ agent: String, before: Int? = nil, limit: Int = 50) async throws -> (runs: [TriggerRun], more: Bool) {
        var q = [("limit", String(limit))]
        if let before { q.append(("before", String(before))) }
        let p: TriggerRunPage = try await call("GET", agentPath(agent) + "/trigger-runs", query: q)
        return (p.runs, p.more)
    }

    /// What a schedule means before it's saved (422 with the parser's message when it isn't one).
    public func schedulePreview(_ schedule: String, count: Int = 3) async throws -> SchedulePreview {
        try await call("GET", "/api/triggers/preview", query: [("schedule", schedule), ("count", String(count))])
    }

    /// A webhook's new key: the old one stops working at once.
    public func newWebhookKey(_ agent: String, hook: String) async throws -> WebhookKey {
        try await call("POST", agentPath(agent) + "/webhooks/\(Self.esc(hook))/key")
    }

    public func cancelWakeup(_ agent: String, id: Int) async throws {
        try await send("DELETE", agentPath(agent) + "/wakeups/\(id)")
    }

    /// Sends an interrupted or failed turn's prompt again, as a new turn (or queued behind the running one).
    public func retryTurn(_ agent: String, _ session: String, turn: String) async throws -> PromptResult {
        try await call("POST", agentPath(agent) + "/sessions/\(Self.esc(session))/turns/\(Self.esc(turn))/retry")
    }

    /// Takes a prompt out of a session's queue (404: it's not waiting there any more).
    public func removeQueued(_ agent: String, _ session: String, id: String) async throws {
        try await send("DELETE", agentPath(agent) + "/sessions/\(Self.esc(session))/queue/\(Self.esc(id))")
    }
}
