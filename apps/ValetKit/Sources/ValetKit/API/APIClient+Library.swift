import Foundation

// The routes for what agents share (docs/API.md): what they're told, skills, the gateway's servers, file and log,
// and the browser's activity log.

extension APIClient {
    // MARK: what agents are told

    public func owner() async throws -> OwnerProfile { try await call("GET", "/api/owner") }

    public func saveOwner(_ text: String) async throws -> OwnerProfile {
        try await call("PUT", "/api/owner", body: ["text": .string(text)])
    }

    public func authority() async throws -> [AuthorityPreset] { try await list("/api/authority") }

    /// Creates or replaces a preset.
    @discardableResult
    public func savePreset(_ name: String, text: String) async throws -> AuthorityPreset {
        try await call("PUT", "/api/authority/\(Self.esc(name))", body: ["text": .string(text)])
    }

    public func deletePreset(_ name: String) async throws { try await send("DELETE", "/api/authority/\(Self.esc(name))") }

    /// What an agent would be told with unsaved settings (nothing is written).
    public func previewDirectives(_ agent: String, definition: DefinitionDraft, persona: String) async throws -> Directives {
        try await call("POST", "/api/agents/\(Self.esc(agent))/directives/preview",
                       body: ["definition": .object(definition.json), "persona": .string(persona)])
    }

    // MARK: skills

    /// A skill's route: the library's (`agent` nil) or an agent's own.
    static func skillPath(_ name: String, agent: String?) -> String {
        agent.map { "/api/agents/\(esc($0))/skills/\(esc(name))" } ?? "/api/skills/\(esc(name))"
    }

    public func skills() async throws -> [SkillInfo] { try await list("/api/skills") }

    public func agentSkills(_ agent: String) async throws -> AgentSkills {
        try await call("GET", "/api/agents/\(Self.esc(agent))/skills")
    }

    public func skill(_ name: String, agent: String?) async throws -> Skill {
        try await call("GET", Self.skillPath(name, agent: agent))
    }

    public func createSkill(_ name: String, description: String, instructions: String, agent: String?) async throws -> Skill {
        let path = agent.map { "/api/agents/\(Self.esc($0))/skills" } ?? "/api/skills"
        return try await call("POST", path, body: ["name": .string(name), "description": .string(description),
                                                   "instructions": .string(instructions)])
    }

    /// Writes what changed (Skill.editBody).
    public func updateSkill(_ name: String, agent: String?, body: JSONValue) async throws -> Skill {
        try await call("PUT", Self.skillPath(name, agent: agent), body: body)
    }

    public func deleteSkill(_ name: String, agent: String?) async throws {
        try await send("DELETE", Self.skillPath(name, agent: agent))
    }

    // MARK: the gateway

    /// The servers with where each is and the secrets it uses.
    public func gatewayServerDetails() async throws -> (servers: [GatewayServerDetail], error: String?) {
        let r: GatewayServerDetails = try await call("GET", "/api/gateway/servers")
        return (r.servers, r.error)
    }

    /// mcp.yaml as text.
    public func registry() async throws -> String {
        let v: JSONValue = try await call("GET", "/api/gateway/registry")
        return v["text"]?.string ?? ""
    }

    /// Validates and commits mcp.yaml (422 with the reason if it doesn't validate); the new commit.
    @discardableResult
    public func saveRegistry(_ text: String) async throws -> String {
        let v: JSONValue = try await call("PUT", "/api/gateway/registry", body: ["text": .string(text)])
        return v["commit"]?.string ?? ""
    }

    /// The gateway's log, newest first; `before` (an id) pages back.
    public func toolCalls(before: Int? = nil, limit: Int = 20) async throws -> (calls: [ToolCallRecord], more: Bool) {
        var q = [("limit", String(limit))]
        if let before { q.append(("before", String(before))) }
        let p: ToolCallPage = try await call("GET", "/api/gateway/calls", query: q)
        return (p.calls, p.more)
    }

    // MARK: the browser's activity

    /// The browser's activity log, newest first, of one agent and one profile (nil: all); `before` (an id) pages back.
    public func browserAudit(agent: String? = nil, profile: String? = nil, before: Int? = nil,
                             limit: Int = 30) async throws -> (actions: [BrowserAction], more: Bool) {
        var q = [("limit", String(limit))]
        if let agent { q.append(("agent", agent)) }
        if let profile { q.append(("profile", profile)) }
        if let before { q.append(("before", String(before))) }
        let p: BrowserActionPage = try await call("GET", "/api/browser/audit", query: q)
        return (p.actions, p.more)
    }
}
