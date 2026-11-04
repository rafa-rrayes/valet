import Foundation

/// An agent's settings page: the saved definition and the draft the form edits, the persona, and what the form
/// reads beside them (the catalog, secrets, the gateway's servers and what each tool does, grants, browser
/// profiles). Save sends the whole draft (`PUT .../definition`), so fields the form doesn't show ride along.
@MainActor @Observable
public final class AgentSettings {
    public let name: String
    @ObservationIgnored let model: AppModel

    public private(set) var detail: AgentDetail?
    /// The definition as saved; nil when agent.yaml doesn't validate (the form can't show it: Files and history can).
    public private(set) var saved: DefinitionDraft?
    public var draft: DefinitionDraft?
    public private(set) var savedPersona = ""
    public var persona = ""
    public private(set) var loaded = false
    public private(set) var notFound = false
    public var loadError: String?

    public private(set) var secrets: [String: Secret]?
    public private(set) var servers: GatewayServers?
    public private(set) var serversError: String?
    /// The saved definition's tools, decided at the draft's level.
    public private(set) var tools: AgentTools?
    public private(set) var toolsError: String?
    @ObservationIgnored private var toolsSeq = 0
    public private(set) var toolGrants: [ToolGrant] = []
    public private(set) var profiles: [BrowserProfile]?
    public private(set) var profilesError: String?
    public private(set) var browserGrants: [BrowserGrant] = []

    public var errors = FieldErrors()
    public var busy: String?
    public var flash: String?
    public var barError: String?
    @ObservationIgnored private var flashTask: Task<Void, Never>?
    @ObservationIgnored private var subscription: UUID?

    public init(name: String, model: AppModel) {
        self.name = name
        self.model = model
    }

    public var agent: Agent? { model.agents[name] }

    public var isDirty: Bool {
        guard let draft, let saved else { return false }
        return !draft.same(as: saved) || persona != savedPersona
    }

    // MARK: loading

    public func start() {
        guard subscription == nil else { return }
        subscription = model.subscribe { [weak self] frame in self?.handle(frame) }
    }

    public func stop() {
        if let s = subscription { model.unsubscribe(s) }
        subscription = nil
        flashTask?.cancel()
    }

    public func load() async {
        async let c: Void = { _ = await self.model.loadCatalog() }()
        async let s: Void = loadSecrets()
        async let g: Void = loadServers()
        async let gr: Void = loadGrants()
        async let p: Void = loadProfiles()
        do {
            take(try await model.api.agentDetail(name))
            loadError = nil
        } catch let e as APIError where e.status == 404 {
            notFound = true
        } catch is CancellationError {
        } catch {
            loadError = error.localizedDescription
        }
        _ = await (c, s, g, gr, p)
        loaded = true
    }

    /// A fresh AgentDetail (load, save, restore): the new baseline, and the form drawn from it.
    func take(_ d: AgentDetail) {
        detail = d
        model.put(d.agent)
        saved = d.definition.map(DefinitionDraft.init)
        draft = saved
        savedPersona = d.persona
        persona = d.persona
        lastMissing = d.agent.missingSecrets
        errors = FieldErrors()
        if saved != nil { Task { await loadTools(fresh: true) } }
    }

    func loadSecrets() async {
        if let list = try? await model.api.secrets() {
            secrets = Dictionary(list.map { ($0.name, $0) }, uniquingKeysWith: { _, b in b })
        }
    }

    func loadServers() async {
        do {
            servers = try await model.api.gatewayServers()
            serversError = nil
        } catch {
            serversError = error.localizedDescription
        }
    }

    func loadGrants() async {
        if let g = try? await model.api.toolGrants(agent: name) { toolGrants = g }
        if let g = try? await model.api.browserGrants(agent: name) { browserGrants = g }
    }

    func loadProfiles() async {
        do {
            let r = try await model.api.browserProfiles()
            profiles = r.profiles
            profilesError = r.error
        } catch let e as APIError where e.status == 404 {
            profiles = []        // a server without the browser
        } catch {
            profilesError = error.localizedDescription
        }
    }

    /// What the saved definition's tools do, at the draft's level (`?permissions=` previews an unsaved one). The
    /// newest load wins; `fresh`: the saved definition changed, so the last answer is no good.
    public func loadTools(fresh: Bool = false) async {
        toolsSeq += 1
        let seq = toolsSeq
        if fresh { tools = nil }
        toolsError = nil
        guard let saved, !saved.mcp.isEmpty else { tools = nil; return }
        let level = draft?.permissions ?? saved.permissions
        do {
            let t = try await model.api.agentTools(name, permissions: level == saved.permissions ? nil : level)
            guard seq == toolsSeq else { return }
            tools = t
        } catch {
            guard seq == toolsSeq else { return }
            toolsError = error.localizedDescription
        }
    }

    private func handle(_ frame: Frame) {
        switch frame {
        case .hello:
            Task {
                await loadSecrets()
                await loadServers()
                await loadGrants()
                _ = await model.loadCatalog(force: true)
            }
        case .inbox(let item) where item.agent == name:
            // An answer that leaves a grant or a rule: the gateway records it, then says so with an agent frame.
            if let o = item.answeredOptionId, o == "allow_hour" || o == "allow_always" { scoped = true }
        case .agent(let a) where a.name == name:
            if scoped {
                scoped = false
                Task {
                    await loadGrants()
                    await syncRules()
                }
            }
            // Its missing secrets changed (one was set or deleted elsewhere): there's no secret frame.
            if a.missingSecrets != lastMissing {
                lastMissing = a.missingSecrets
                Task { await loadSecrets() }
            }
        default:
            break
        }
    }

    @ObservationIgnored private var scoped = false
    @ObservationIgnored private var lastMissing: [String] = []

    /// A rule written elsewhere ("Always allow" in the inbox): the form takes it, so a save from here doesn't take it
    /// back. Unedited, the whole definition is taken again; with edits, the rules the form hasn't changed.
    func syncRules() async {
        guard let d = try? await model.api.agentDetail(name), let fresh = d.definition.map(DefinitionDraft.init) else {
            return
        }
        guard isDirty, var draft, var saved else { take(d); return }
        if draft.toolRules == saved.toolRules { draft.toolRules = fresh.toolRules }
        if draft.siteRules == saved.siteRules { draft.siteRules = fresh.siteRules }
        saved.toolRules = fresh.toolRules
        saved.siteRules = fresh.siteRules
        self.saved = saved
        self.draft = draft
    }

    // MARK: saving

    /// The keys the form shows errors under (a 422's field goes to the longest that is it or its parent).
    var errorKeys: [String] {
        var keys = ["emoji", "display_name", "persona", "harness", "model", "computer", "computer.level",
                    "computer.network", "computer.allowlist", "computer.mounts", "computer.image", "computer.workdir",
                    "permissions", "tools", "tools.mcp", "tools.rules", "tools.skills", "secrets", "browser", "browser.profiles",
                    "browser.rules", "memory", "directives", "triggers"]
        if let d = draft {
            keys += d.allowlist.indices.map { "computer.allowlist.\($0)" }
            for i in d.mounts.indices {
                keys += ["computer.mounts.\(i)", "computer.mounts.\(i).host", "computer.mounts.\(i).path",
                         "computer.mounts.\(i).readonly"]
            }
            keys += d.siteRules.keys.map { "browser.rules.\($0)" }
            for i in d.triggers.indices {
                keys += ["triggers.\(i)"] + ["schedule", "webhook", "prompt", "session"].map { "triggers.\(i).\($0)" }
            }
        }
        return keys
    }

    /// Saves the draft (and the persona, if it changed). True once saved.
    @discardableResult
    public func save() async -> Bool {
        guard busy == nil, let draft else { return false }
        busy = "Saving…"
        barError = nil
        errors = FieldErrors()
        defer { busy = nil }
        do {
            take(try await model.api.saveDefinition(name, draft, persona: persona == savedPersona ? nil : persona))
            show("Saved.")
            return true
        } catch let e as APIError where e.status == 422 && e.body?["errors"]?.array != nil {
            errors = FieldErrors(body: e.body, keys: errorKeys)
            barError = errors.general.isEmpty ? "Some settings need fixing: see the notes in red."
                : errors.general.joined(separator: "\n")
        } catch {
            barError = error.localizedDescription
        }
        return false
    }

    public func discard() {
        draft = saved
        persona = savedPersona
        errors = FieldErrors()
        barError = nil
        if tools?.permissions != saved?.permissions { Task { await loadTools() } }
    }

    public func changed(_ key: String) {
        errors.clear(key)
        flash = nil
    }

    func show(_ text: String) {
        flash = text
        flashTask?.cancel()
        flashTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(3))
            if !Task.isCancelled { self?.flash = nil }
        }
    }

    /// agent.yaml or persona.md written as text, or an earlier version restored: the form starts again from it.
    public func reload() async {
        if let d = try? await model.api.agentDetail(name) { take(d) }
    }

    public func restore(_ commit: HistoryEntry) async throws {
        take(try await model.api.restoreAgent(name, commit: commit.commit))
        show("Restored: \(commit.message).")
    }

    public func revoke(_ g: ToolGrant) async {
        do { try await model.api.deleteToolGrant(g.id) } catch let e as APIError where e.status == 404 {} catch {
            barError = error.localizedDescription
        }
        await loadGrants()
    }

    public func revoke(_ g: BrowserGrant) async {
        do { try await model.api.deleteBrowserGrant(g.id) } catch let e as APIError where e.status == 404 {} catch {
            barError = error.localizedDescription
        }
        await loadGrants()
    }

    /// true set, false not set, nil unknown.
    public func secretStatus(_ n: String) -> Bool? {
        if let s = secrets?[n] { return s.set }
        if secrets != nil { return false }
        guard let a = agent, saved?.secretNames.contains(n) == true else { return nil }
        return !a.missingSecrets.contains(n)
    }

    public func grant(_ server: String, _ tool: String) -> ToolGrant? {
        toolGrants.first { $0.server == server && $0.tool == tool }
    }
}

/// What a call to a tool does under a server's rules: its own rule, else the server's "*", else its level; a live
/// hour's grant turns an ask into a run, never a block (app.js's toolFate).
public enum ToolFate {
    public static let rules = ["allow", "ask", "deny"]

    public static func decide(_ tool: AgentTools.Tool, rules: [String: String], grant: Bool) -> (decision: String, source: String) {
        let own = rules[tool.tool].flatMap { Self.rules.contains($0) ? $0 : nil }
            ?? rules["*"].flatMap { Self.rules.contains($0) ? $0 : nil }
        let level = tool.levelDecision ?? tool.decision
        if (own == "ask" || (own == nil && level == "ask")) && grant { return ("allow", "grant") }
        return own.map { ($0, "rule") } ?? (level, "level")
    }

    public static func words(_ decision: String) -> String {
        switch decision {
        case "allow": return "runs"
        case "ask": return "asks you"
        case "deny": return "blocked"
        default: return decision
        }
    }

    public static func choice(_ rule: String) -> String {
        switch rule {
        case "allow": return "Run"
        case "ask": return "Ask you"
        case "deny": return "Block"
        default: return rule
        }
    }
}
