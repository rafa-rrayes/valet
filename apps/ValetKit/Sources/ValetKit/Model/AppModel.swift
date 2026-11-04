import Foundation
import Observation
import UserNotifications

/// A session to open (a debug hook, the inbox's "Open session").
public struct SessionRef: Hashable, Identifiable, Sendable {
    public var agent: String
    public var id: String

    public init(agent: String, id: String) {
        self.agent = agent
        self.id = id
    }
}

/// The New agent form, asked for: blank, or a copy of an agent.
public struct NewAgentRequest: Identifiable, Hashable, Sendable {
    public let id = UUID()
    public var copyFrom: String?

    public init(copyFrom: String? = nil) { self.copyFrom = copyFrom }
}

/// App-wide state, shared by the iOS and macOS apps: the server and sign-in, the lock, and the live picture of
/// agents, runners, the inbox and sessions that the client socket keeps current. Screens read it; open sessions
/// subscribe to its frames.
@MainActor @Observable
public final class AppModel {
    public static let shared = AppModel()

    public private(set) var api: APIClient
    public private(set) var signedIn: Bool
    public private(set) var locked: Bool
    public var requireLock: Bool {
        didSet { defaults.set(requireLock, forKey: Keys.requireLock) }
    }

    public private(set) var me: Me?
    public private(set) var agents: [String: Agent] = [:]
    public private(set) var runners: [String: Runner] = [:]
    public private(set) var inbox: [String: InboxItem] = [:]
    /// agent -> session id -> Session, from the sessions route and the socket.
    public private(set) var sessions: [String: [String: Session]] = [:]
    public private(set) var connection: ClientSocket.State = .idle
    public private(set) var loadedOnce = false
    /// The highest event id the server had at the last hello.
    public private(set) var lastEventId = 0
    /// Counts hellos: screens that fetch their own data (an agent's sessions) refetch when it changes, since
    /// frames sent while the socket was down are gone.
    public private(set) var hellos = 0
    /// Something to tell the owner once (a failed notification action, an expired sign-in).
    public var notice: String?

    /// Set by a notification tap: the root shows that inbox item.
    public var openItemID: String?
    /// Set to open a session (the inbox, a debug hook): the root navigates to it.
    public var openSession: SessionRef?
    /// Set to open the live view of a browser profile (a take-over, a profile, an agent): the root shows it.
    public var openLive: LiveTarget?
    /// Set to show the New agent form (the Agents screen's +, File > New Agent, Copy…): the root presents it.
    public var newAgent: NewAgentRequest?
    /// Set to open an agent's page (a new agent): the root navigates to it.
    public var openAgent: String?
    /// A note for an agent's page, shown once when it opens (a new agent that didn't start).
    public var agentNotes: [String: String] = [:]
    /// Set by a shared push's tap: the root opens that item under its agent's Shared.
    public var openShared: SharedPushTarget?
    /// Agents deleted while the app runs (here or on another device: `agent_deleted`), until one is made again
    /// with the name. Their open screens close.
    public private(set) var deletedAgents: Set<String> = []

    /// What the agents added or changed in their shared spaces since the owner last looked, in all and per agent,
    /// and how many items have no agent.
    public private(set) var sharedCounts = SharedCounts()
    public var sharedUnseen: Int { sharedCounts.unseen }
    /// The settings catalog (harnesses, levels, templates), loaded when a settings screen needs it.
    public private(set) var catalog: Catalog?

    public enum PushState: Equatable, Sendable {
        case off, registered, unsupported, denied, failed(String)
    }

    public private(set) var push: PushState = .off
    /// Calls `registerForRemoteNotifications()` on the platform's application (set by the app delegate).
    @ObservationIgnored public var registerForRemoteNotifications: (() -> Void)?

    @ObservationIgnored private let store: any TokenStore
    @ObservationIgnored private let defaults: UserDefaults
    @ObservationIgnored private var socket: ClientSocket!
    @ObservationIgnored private var subscribers: [UUID: (Frame) -> Void] = [:]
    @ObservationIgnored private var backgroundedAt: Date?
    @ObservationIgnored private let transport: any SocketTransport
    @ObservationIgnored private let urlSession: URLSession

    static let lockAfter: TimeInterval = 60
    /// Away this long and the socket may be dead (a phone asleep): redial on return.
    static let redialAfter: TimeInterval = 10
    public static let defaultServer = URL(string: "http://127.0.0.1:8780")!

    enum Keys {
        static let server = "server", requireLock = "requireLock", pushToken = "pushToken", deviceID = "deviceID"
        static let token = "token"
    }

    public init(store: any TokenStore = Keychain.standard, defaults: UserDefaults = .standard,
                transport: any SocketTransport = URLSessionTransport(), urlSession: URLSession = .shared) {
        self.store = store
        self.defaults = defaults
        self.transport = transport
        self.urlSession = urlSession
        defaults.register(defaults: [Keys.requireLock: true])
        #if DEBUG
        if DebugLaunch.reset {
            store.set(nil, for: Keys.token)
            for k in [Keys.server, Keys.deviceID, Keys.requireLock] { defaults.removeObject(forKey: k) }
        }
        #endif
        let server = defaults.string(forKey: Keys.server).flatMap(URL.init(string:)) ?? Self.defaultServer
        let token = store.get(Keys.token)
        api = APIClient(baseURL: server, token: token, urlSession: urlSession)
        signedIn = token != nil
        requireLock = defaults.bool(forKey: Keys.requireLock)
        locked = token != nil && defaults.bool(forKey: Keys.requireLock) && Biometry.available
        #if DEBUG
        if DebugLaunch.noLock { locked = false }
        #endif
        api = makeClient(server: server, token: token)
        socket = makeSocket()
    }

    private func makeClient(server: URL, token: String?) -> APIClient {
        APIClient(baseURL: server, token: token, urlSession: urlSession, onUnauthorized: { [weak self] in
            Task { @MainActor in self?.signOut(expired: true) }
        })
    }

    private func makeSocket() -> ClientSocket {
        let s = ClientSocket(transport: transport, request: { [weak self] in
            self?.api.socketRequest() ?? URLRequest(url: AppModel.defaultServer)
        })
        s.onFrame = { [weak self] in self?.apply($0) }
        s.onState = { [weak self] in self?.connection = $0 }
        s.onUnauthorized = { [weak self] in await self?.stillSignedIn() ?? false }
        return s
    }

    public var server: URL { api.baseURL }

    /// The server as the owner typed it last (for the sign-in screen).
    public var rememberedServer: String { defaults.string(forKey: Keys.server) ?? "" }

    // MARK: sign in and out

    /// Signs in with the owner's password as a native client and keeps the token in the Keychain.
    public func signIn(server: URL, password: String) async throws {
        let client = makeClient(server: server, token: nil)
        let me = try await client.me()
        guard me.setup else { throw SignInError.noPassword }
        let token = try await client.login(password: password, device: DeviceInfo.name)
        let saved = store.set(token, for: Keys.token)
        defaults.set(server.absoluteString, forKey: Keys.server)
        api = client
        self.me = me
        signedIn = true
        locked = false
        notice = saved ? nil : "Couldn't keep the session in the Keychain: you'll sign in again next time Valet opens."
        await connect()
        await enablePush()
    }

    public enum SignInError: LocalizedError {
        case noPassword
        public var errorDescription: String? {
            "This Valet has no password yet. Run `valet-control set-password` on the server."
        }
    }

    public func signOut(expired: Bool = false) {
        guard signedIn else { return }
        if let id = defaults.string(forKey: Keys.deviceID) {
            let api = self.api
            Task { try? await api.deleteDevice(id) }       // stop pushes to this device; best effort
            defaults.removeObject(forKey: Keys.deviceID)
        }
        store.set(nil, for: Keys.token)
        api.setToken(nil)
        signedIn = false
        locked = false
        socket.stop()
        agents = [:]
        runners = [:]
        inbox = [:]
        sessions = [:]
        me = nil
        loadedOnce = false
        push = .off
        openItemID = nil
        openSession = nil
        openLive = nil
        newAgent = nil
        openAgent = nil
        agentNotes = [:]
        openShared = nil
        deletedAgents = []
        sharedCounts = SharedCounts()
        catalog = nil
        updateBadge()
        if expired { notice = APIError.unauthorized.errorDescription }
    }

    /// The socket was closed as not signed in: true if the token still works (keep redialing).
    private func stillSignedIn() async -> Bool {
        do {
            let me = try await api.me()
            if me.owner { return true }
        } catch APIError.unauthorized {
        } catch {
            return true     // offline: keep trying
        }
        signOut(expired: true)
        return false
    }

    // MARK: live state

    /// Loads everything once over REST, then keeps it current through the socket (whose hello replaces it).
    public func connect() async {
        guard signedIn else { return }
        await refresh()
        socket.start()
    }

    public func refresh() async {
        guard signedIn else { return }
        do {
            async let a = api.agents()
            async let r = api.runners()
            async let i = api.inbox()
            let (agents, runners, items) = try await (a, r, i)
            setAll(agents: agents, runners: runners, inbox: items)
            if me == nil { me = try? await api.me() }
        } catch is CancellationError {
        } catch APIError.unauthorized {
        } catch {
            notice = error.localizedDescription
        }
    }

    private func setAll(agents: [Agent], runners: [Runner], inbox items: [InboxItem]) {
        self.agents = Dictionary(agents.map { ($0.name, $0) }, uniquingKeysWith: { _, b in b })
        self.runners = Dictionary(runners.map { ($0.name, $0) }, uniquingKeysWith: { _, b in b })
        inbox = Dictionary(items.map { ($0.id, $0) }, uniquingKeysWith: { _, b in b })
        loadedOnce = true
        updateBadge()
    }

    /// Applies one socket frame, then hands it to the open screens.
    public func apply(_ frame: Frame) {
        switch frame {
        case .hello(let h):
            // One deleted while the socket was down: its agent_deleted frame is gone, so the list says it.
            let names = Set(h.agents.map(\.name))
            if loadedOnce { deletedAgents.formUnion(Set(agents.keys).subtracting(names)) }
            deletedAgents.subtract(names)
            setAll(agents: h.agents, runners: h.runners, inbox: h.inbox)
            lastEventId = h.lastEventId
            hellos += 1
            Task { await refreshSharedUnseen() }
        case .agent(let a): put(a)
        case .agentDeleted(let name):
            agents[name] = nil
            sessions[name] = nil
            inbox = inbox.filter { $0.value.agent != name }
            deletedAgents.insert(name)
            updateBadge()
            // Its Shared items went with it, and no shared frame says so.
            Task { await refreshSharedUnseen() }
        case .runner(let r): runners[r.name] = r
        case .inbox(let item):
            inbox[item.id] = item
            updateBadge()
        case .session(let s): sessions[s.agent, default: [:]][s.id] = s
        case .event(let e): lastEventId = max(lastEventId, e.id)
        case .shared(let c): if let n = c.counts { sharedCounts = n }
        case .memory, .unknown: break
        }
        for handler in subscribers.values { handler(frame) }
    }

    /// Frames for a screen that follows them (an open session) until it unsubscribes.
    public func subscribe(_ handler: @escaping (Frame) -> Void) -> UUID {
        let id = UUID()
        subscribers[id] = handler
        return id
    }

    public func unsubscribe(_ id: UUID) { subscribers[id] = nil }

    public func put(_ item: InboxItem) {
        inbox[item.id] = item
        updateBadge()
        for handler in subscribers.values { handler(.inbox(item)) }
    }

    public func put(_ agent: Agent) {
        agents[agent.name] = agent
        deletedAgents.remove(agent.name)        // made again with that name
    }

    public func put(_ session: Session) { sessions[session.agent, default: [:]][session.id] = session }

    // MARK: what screens show

    /// Agents by name (as the web UI lists them).
    public var agentList: [Agent] { agents.values.sorted { $0.name < $1.name } }

    public var runnerList: [Runner] { runners.values.sorted { $0.name < $1.name } }

    /// What waits on the owner, newest first.
    public var pending: [InboxItem] {
        inbox.values.filter(\.isPending).sorted { ($0.createdAt, $0.id) > ($1.createdAt, $1.id) }
    }

    public var pendingCount: Int { inbox.values.reduce(0) { $0 + ($1.isPending ? 1 : 0) } }

    public func sessionList(_ agent: String) -> [Session] {
        (sessions[agent] ?? [:]).values.sorted { ($0.updatedAt, $0.id) > ($1.updatedAt, $1.id) }
    }

    public func title(of agent: String) -> String { agents[agent]?.title ?? agent }

    // MARK: actions

    /// Creates an agent from the New agent form; it's in `agents` at once (its frame may come after).
    public func createAgent(_ draft: NewAgentDraft) async throws -> Agent {
        let d = try await api.createAgent(draft)
        put(d.agent)
        return d.agent
    }

    /// Create & start's second half: starts the new agent. What to say on its page when it didn't (nil: it did).
    public func startNew(_ name: String) async -> String? {
        do {
            try await start(name)
            return nil
        } catch let e as APIError where e.status == 503 {
            // The server says why and that it starts later ("no runner is connected; the agent starts when one
            // connects"); without a detail, the web UI's words.
            let d = APIError.detail(of: e.body).trimmingCharacters(in: CharacterSet(charactersIn: ". "))
            return d.isEmpty ? "Created. It starts when a runner connects." : "Created. \(d.capitalizedFirst)."
        } catch {
            return "Created, but it didn't start: \(error.localizedDescription)"
        }
    }

    /// Start (or restart) an agent. A refusal for missing secrets throws an APIError with `missingSecrets`; the
    /// agent is fetched again either way, so its card shows where it stands.
    public func start(_ name: String) async throws {
        do {
            put(try await api.start(name))
        } catch {
            if let a = try? await api.agent(name) { put(a) }
            throw error
        }
    }

    public func stop(_ name: String) async throws {
        do {
            put(try await api.stop(name))
        } catch {
            if let a = try? await api.agent(name) { put(a) }
            throw error
        }
    }

    /// Deletes an agent for good (`volumes`: a container agent's volumes too). It's off every list at once, and its
    /// open screens close, as when another device deletes it (the server's agent_deleted frame does the same).
    @discardableResult
    public func deleteAgent(_ name: String, volumes: Bool = false) async throws -> AgentDeleted {
        let r = try await api.deleteAgent(name, volumes: volumes)
        if !deletedAgents.contains(name) { apply(.agentDeleted(name)) }     // unless its frame came first
        return r
    }

    /// Sets secrets one by one; the ones that failed come back with their errors.
    public func setSecrets(_ values: [String: String]) async -> [String: String] {
        var failed: [String: String] = [:]
        for (name, value) in values.sorted(by: { $0.key < $1.key }) {
            do { try await api.setSecret(name, value: value) } catch { failed[name] = error.localizedDescription }
        }
        return failed
    }

    public func loadSessions(_ agent: String) async throws {
        let list = try await api.sessions(agent)
        sessions[agent] = Dictionary(list.map { ($0.id, $0) }, uniquingKeysWith: { _, b in b })
    }

    public func newSession(_ agent: String) async throws -> Session {
        let s = try await api.newSession(agent)
        put(s)
        return s
    }

    /// Answers an inbox item with an option or text. A 410 (the agent stopped waiting) records the expired item
    /// and isn't an error; a 409 (already answered elsewhere) refetches it.
    public func answer(_ id: String, optionId: String? = nil, text: String? = nil) async throws {
        do {
            put(try await api.answer(id, optionId: optionId, text: text))
        } catch let e as APIError {
            if let item = e.item { put(item); return }
            if e.status == 409, let item = try? await api.inboxItem(id) { put(item); return }
            throw e
        }
    }

    public func openItem(_ id: String) { openItemID = id }

    /// Shows a profile's live view (from a take-over ask: its Done and I can't come along).
    public func openLiveView(_ target: LiveTarget) { openLive = target }

    public func refreshSharedUnseen() async {
        guard signedIn, let n = try? await api.sharedCounts() else { return }
        sharedCounts = n
    }

    /// The counts after the owner looked at (or marked seen) shared items.
    public func setSharedCounts(_ n: SharedCounts) { sharedCounts = n }

    /// The settings catalog, fetched once per hello (a runner connecting changes what's ready).
    @discardableResult
    public func loadCatalog(force: Bool = false) async -> Catalog? {
        if let c = catalog, !force { return c }
        if let c = try? await api.catalog() { catalog = c }
        return catalog
    }

    // MARK: lock

    public func didEnterBackground() { backgroundedAt = Date() }

    public func willEnterForeground() {
        let away = backgroundedAt.map { Date().timeIntervalSince($0) } ?? 0
        backgroundedAt = nil
        if away > Self.lockAfter { lock() }
        guard signedIn else { return }
        if away > Self.redialAfter || connection != .live { socket.start() }
    }

    public func lock() {
        if signedIn && requireLock && Biometry.available { locked = true }
    }

    #if DEBUG
    func unlockWithoutAsking() { locked = false }
    #endif

    /// Why the last unlock didn't (nil: it did, or nothing to say, as after a cancel).
    public private(set) var unlockError: String?

    public func unlock() async {
        guard locked else { return }
        unlockError = nil
        if let why = await Biometry.authenticate(reason: "Unlock Valet") {
            unlockError = why.isEmpty ? nil : "Couldn't unlock: " + why
        } else {
            locked = false
        }
    }

    // MARK: push

    /// Asks to notify, then registers with APNs (the app delegate's callback sends the token here).
    public func enablePush() async {
        #if DEBUG
        if DebugLaunch.noPush { return }
        #endif
        guard signedIn else { return }
        guard await Push.requestAuthorization() else { push = .denied; return }
        registerForRemoteNotifications?()
    }

    public func didRegisterForPush(deviceToken: Data) async {
        let token = deviceToken.map { String(format: "%02x", $0) }.joined()
        defaults.set(token, forKey: Keys.pushToken)
        await registerDevice()
    }

    public func didFailToRegisterForPush(_ error: Error) {
        push = .failed(error.localizedDescription)
    }

    /// Sends the APNs token to the server (POST /api/devices). A server without the route (404) has no push yet.
    public func registerDevice() async {
        guard signedIn, let token = defaults.string(forKey: Keys.pushToken) else { return }
        do {
            let id = try await api.registerDevice(token: token, platform: DeviceInfo.platform, name: DeviceInfo.name)
            if let id { defaults.set(id, forKey: Keys.deviceID) }
            push = .registered
        } catch let e as APIError where e.status == 404 || e.status == 405 {
            push = .unsupported
        } catch {
            push = .failed(error.localizedDescription)
        }
    }

    /// This device's registration, to mark it in the list.
    public var deviceID: String? { defaults.string(forKey: Keys.deviceID) }
    public var pushToken: String? { defaults.string(forKey: Keys.pushToken) }

    private func updateBadge() {
        guard Push.available else { return }
        UNUserNotificationCenter.current().setBadgeCount(pendingCount) { _ in }
    }
}
