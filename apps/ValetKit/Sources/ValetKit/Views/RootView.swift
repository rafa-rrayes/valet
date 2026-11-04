import SwiftUI

public enum AppTab: String, Hashable, Sendable, CaseIterable {
    case agents, inbox, settings

    var title: String { rawValue.capitalized }

    var icon: String {
        switch self {
        case .agents: return "person.2"
        case .inbox: return "tray"
        case .settings: return "gearshape"
        }
    }
}

/// Where a navigation stack can go. An agent's screens close once it's deleted (leavesWhenDeleted).
struct AppDestinations: ViewModifier {
    func body(content: Content) -> some View {
        content
            .navigationDestination(for: AgentRef.self) { AgentView(name: $0.name).leavesWhenDeleted($0.name) }
            .navigationDestination(for: SessionRef.self) {
                SessionView(agent: $0.agent, id: $0.id).leavesWhenDeleted($0.agent)
            }
            .navigationDestination(for: SharedRef.self) { SharedItemView(id: $0.id) }
            .navigationDestination(for: SharedListRef.self) { SharedView(agent: $0.agent).leavesWhenDeleted($0.agent) }
            .navigationDestination(for: MemoryRef.self) { MemoryView(agent: $0.agent).leavesWhenDeleted($0.agent) }
            .navigationDestination(for: MemoryFileRef.self) { MemoryFileView(ref: $0).leavesWhenDeleted($0.agent) }
            .navigationDestination(for: AgentSettingsRef.self) {
                AgentSettingsView(name: $0.agent).leavesWhenDeleted($0.agent)
            }
            .navigationDestination(for: BrowserSettingsRef.self) { _ in BrowserSettingsView() }
            .navigationDestination(for: BrowserProfileRef.self) { BrowserProfileView(name: $0.name) }
            .navigationDestination(for: VaultRef.self) { _ in VaultListView() }
            .navigationDestination(for: ContinueRef.self) { ContinueView(name: $0.agent).leavesWhenDeleted($0.agent) }
            .navigationDestination(for: HandoffRef.self) { HandoffView(name: $0.agent).leavesWhenDeleted($0.agent) }
            .navigationDestination(for: TriggerRunsRef.self) {
                TriggerRunsView(agent: $0.agent).leavesWhenDeleted($0.agent)
            }
            .navigationDestination(for: TerminalRef.self) {
                TerminalScreen(agent: $0.agent).leavesWhenDeleted($0.agent)
            }
            .navigationDestination(for: FilesRef.self) { FilesView(ref: $0).leavesWhenDeleted($0.agent) }
            .navigationDestination(for: SecretsToolsRef.self) { _ in SecretsToolsView() }
            .navigationDestination(for: DirectivesRef.self) { _ in DirectivesView() }
            .navigationDestination(for: PresetRef.self) { PresetView(name: $0.name) }
            .navigationDestination(for: SkillsRef.self) { _ in SkillsView() }
            .navigationDestination(for: SkillRef.self) { SkillView(ref: $0) }
    }
}

/// Sign in, then the lock, then the app: tabs on iOS, a sidebar on macOS. Both platforms' windows use it.
public struct RootView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.scenePhase) private var phase
    @State private var tab: AppTab = .agents
    @State private var agentsPath = NavigationPath()
    @State private var inboxPath = NavigationPath()
    @State private var live: LiveTarget?
    #if os(macOS)
    @State private var selection: SidebarItem? = .agents
    @State private var detailPath = NavigationPath()
    /// The sidebar's selection as it was last, so the window opens where it was left.
    @AppStorage("sidebar") private var savedSelection = SidebarItem.agents.key
    @State private var restored = false
    #endif

    public init() {}

    public var body: some View {
        @Bindable var model = model
        Group {
            if !model.signedIn {
                SignInView()
            } else if model.locked {
                LockView()
            } else {
                main
                    .sheet(item: Binding(get: { model.openItemID.map(ItemRef.init) },
                                         set: { model.openItemID = $0?.id })) { ref in
                        InboxItemSheet(id: ref.id)
                    }
                    .sheet(item: $model.newAgent) { NewAgentView(request: $0) }
                    .onChange(of: model.openAgent) {
                        guard let name = model.openAgent else { return }
                        model.openAgent = nil
                        showAgent(name)
                    }
                    .onChange(of: model.openShared, initial: true) { showShared() }
                    // What went wrong out of sight (a notification's answer, the Keychain); the sign-in screen
                    // shows it there.
                    .alert("Valet", isPresented: Binding(get: { model.notice != nil }, set: { if !$0 { model.notice = nil } })) {
                        Button("OK") { model.notice = nil }
                    } message: {
                        Text(model.notice ?? "")
                    }
                    #if os(iOS)
                    .fullScreenCover(item: $live) { LiveView(target: $0) }
                    #else
                    .sheet(item: $live) { LiveView(target: $0) }
                    #endif
                    .onChange(of: model.openLive) { showLive() }
                    .onAppear { showLive() }
            }
        }
        #if os(iOS)
        .overlay {
            if phase != .active && model.signedIn && model.requireLock && Biometry.available { PrivacyCover() }
        }
        #endif
        .animation(.default, value: model.signedIn)
        .animation(.default, value: model.locked)
        .tint(.valet)
        #if os(macOS)
        .onAppear {
            // Before the debug hooks (.task), which may pick another screen.
            guard !restored else { return }
            restored = true
            selection = SidebarItem(key: savedSelection) ?? .agents
        }
        #endif
        #if DEBUG
        .task {
            if let t = DebugLaunch.tab.flatMap(AppTab.init(rawValue:)) { show(t) }
            await DebugLaunch.run(model)
            if let r = DebugLaunch.newAgent {
                try? await Task.sleep(for: .milliseconds(500))
                model.newAgent = r
            }
            if let name = DebugLaunch.agent {
                showAgent(name)
                if let what = DebugLaunch.open {
                    try? await Task.sleep(for: .milliseconds(300))
                    debugOpen(what, agent: name)
                }
            }
            if let screen = DebugLaunch.screen {
                try? await Task.sleep(for: .milliseconds(300))
                debugScreen(screen)
            }
        }
        #endif
    }

    struct ItemRef: Identifiable { let id: String }

    #if DEBUG
    /// VALET_DEBUG_OPEN's screen of an agent: `terminal`, `files`, `files:/a/path`, `settings`, `shared`.
    private func debugOpen(_ what: String, agent: String) {
        if what == "terminal" { push(TerminalRef(agent)) }
        if what == "shared" { push(SharedListRef(agent)) }
        if what == "settings" { push(AgentSettingsRef(agent)) }
        if what == "files" { push(FilesRef(agent)) }
        if what.hasPrefix("files:/") { push(FilesRef(agent, path: String(what.dropFirst("files:".count)))) }
    }

    /// VALET_DEBUG_SCREEN's screen, on top of the first one: browser, vault, secrets, directives, skills,
    /// profile:<name>, settings:<agent>.
    private func debugScreen(_ s: String) {
        let arg = s.split(separator: ":", maxSplits: 1).dropFirst().first.map(String.init) ?? ""
        switch s.split(separator: ":").first.map(String.init) ?? s {
        case "browser": push(BrowserSettingsRef())
        case "vault": push(VaultRef())
        case "secrets": push(SecretsToolsRef())
        case "directives": push(DirectivesRef())
        case "skills": push(SkillsRef())
        case "profile": push(BrowserProfileRef(arg))
        case "settings": push(AgentSettingsRef(arg))
        default: break
        }
    }

    private func push<V: Hashable>(_ value: V) {
        #if os(iOS)
        agentsPath.append(value)
        #else
        detailPath.append(value)
        #endif
    }
    #endif

    /// The live view asked for: shown once the item sheet (where Take over may have been tapped) is gone.
    private func showLive() {
        guard let t = model.openLive else { return }
        model.openLive = nil
        if model.openItemID != nil || live != nil {
            model.openItemID = nil
            live = nil
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) { live = t }
        } else {
            live = t
        }
    }

    #if os(iOS)
    @ViewBuilder private var main: some View {
        TabView(selection: $tab) {
            NavigationStack(path: $agentsPath) { AgentsView().modifier(AppDestinations()) }
                .environment(\.popScreens, PopScreens { agentsPath.removeLast(min($0, agentsPath.count)) })
                .tabItem { Label(AppTab.agents.title, systemImage: AppTab.agents.icon) }
                .badge(model.sharedUnseen)      // what's new in the agents' Shared: "N new in Shared" on each
                .tag(AppTab.agents)
            NavigationStack(path: $inboxPath) { InboxView().modifier(AppDestinations()) }
                .environment(\.popScreens, PopScreens { inboxPath.removeLast(min($0, inboxPath.count)) })
                .tabItem { Label(AppTab.inbox.title, systemImage: AppTab.inbox.icon) }
                .badge(model.pendingCount)
                .tag(AppTab.inbox)
            NavigationStack { SettingsView().modifier(AppDestinations()) }
                .tabItem { Label(AppTab.settings.title, systemImage: AppTab.settings.icon) }
                .tag(AppTab.settings)
        }
        .modifier(AdaptableTabs())
        .onChange(of: model.openSession) { open() }
        .onAppear { open() }
    }

    /// A session asked for (the inbox, a debug hook): on the Agents tab, under its agent.
    private func open() {
        guard let ref = model.openSession else { return }
        model.openSession = nil
        model.openItemID = nil
        if tab == .inbox {
            inboxPath.append(ref)
        } else {
            tab = .agents
            agentsPath = NavigationPath([AgentRef(ref.agent)])
            agentsPath.append(ref)
        }
    }

    private func show(_ t: AppTab) { tab = t }

    private func showAgent(_ name: String) {
        tab = .agents
        agentsPath = NavigationPath([AgentRef(name)])
    }

    /// A shared item asked for (a push's tap): on the Agents tab, under its agent's Shared.
    private func showShared() {
        guard let s = model.openShared else { return }
        model.openShared = nil
        model.openItemID = nil
        tab = .agents
        var path = NavigationPath()
        if let a = s.agent {
            path.append(AgentRef(a))
            path.append(SharedListRef(a))
        }
        path.append(SharedRef(s.id))
        agentsPath = path
    }
    #else
    @ViewBuilder private var main: some View {
        NavigationSplitView {
            // Never nothing: a click on empty space, or a row that's briefly gone while the agents reload, keeps
            // what's selected (and shown).
            Sidebar(selection: Binding(get: { selection }, set: { if let s = $0 { selection = s } }))
                .navigationSplitViewColumnWidth(min: 210, ideal: 240)
        } detail: {
            NavigationStack(path: $detailPath) {
                // The destinations go inside the stack, on its root: on the stack itself SwiftUI doesn't see them
                // and every NavigationLink(value:) on the page does nothing.
                Group {
                    switch selection {
                    case .agent(let name): AgentView(name: name).id(name)
                    case .inbox: InboxView()
                    case .settings: SettingsView()
                    case .agents, nil: AgentsView()
                    }
                }
                .modifier(AppDestinations())
            }
            .environment(\.popScreens, PopScreens { detailPath.removeLast(min($0, detailPath.count)) })
            // An agent opened from All agents is that agent's item in the sidebar, not a screen pushed on the list.
            .environment(\.showAgent, ShowAgent { showAgent($0) })
        }
        .onChange(of: selection) {
            detailPath = NavigationPath()
            if let s = selection { savedSelection = s.key }
        }
        .onChange(of: model.agentList.map(\.name)) { checkSelection() }
        .onChange(of: model.loadedOnce) { checkSelection() }
        .onChange(of: model.openSession) { open() }
        .onAppear { open() }
    }

    /// An agent that's gone (deleted, or never there: a stale saved selection) leaves All agents selected.
    private func checkSelection() {
        if let s = selection, let fixed = s.fallback(agents: Set(model.agents.keys), loaded: model.loadedOnce) {
            selection = fixed
        }
    }

    private func open() {
        guard let ref = model.openSession else { return }
        model.openSession = nil
        model.openItemID = nil
        selection = .agent(ref.agent)
        DispatchQueue.main.async { detailPath = NavigationPath([ref]) }
    }

    private func showAgent(_ name: String) { selection = .agent(name) }

    /// A shared item asked for (a push's tap): its agent selected, then its Shared, then the item.
    private func showShared() {
        guard let s = model.openShared else { return }
        model.openShared = nil
        model.openItemID = nil
        selection = s.agent.map(SidebarItem.agent) ?? .agents
        DispatchQueue.main.async {
            var path = NavigationPath()
            if let a = s.agent { path.append(SharedListRef(a)) }
            path.append(SharedRef(s.id))
            detailPath = path
        }
    }

    private func show(_ t: AppTab) {
        switch t {
        case .agents: selection = .agents
        case .inbox: selection = .inbox
        case .settings: selection = .settings
        }
    }
    #endif
}

/// Opening an agent where the app has a place for each one (the Mac's sidebar) rather than pushing its page.
struct ShowAgent {
    let show: @MainActor (String) -> Void
}

extension EnvironmentValues {
    @Entry var showAgent: ShowAgent? = nil
}

#if os(iOS)
/// On an iPad with iOS 18, the tabs can become a sidebar (the tab bar's own button); a phone keeps its tab bar.
private struct AdaptableTabs: ViewModifier {
    func body(content: Content) -> some View {
        if #available(iOS 18.0, *) {
            content.tabViewStyle(.sidebarAdaptable)
        } else {
            content
        }
    }
}
#endif

#if os(macOS)
enum SidebarItem: Hashable {
    case agents, agent(String), inbox, settings

    /// As it's saved: "agents", "inbox", "settings", "agent:<name>". (A saved "shared", from when Shared had its
    /// own item, is nil: All agents.)
    var key: String {
        switch self {
        case .agents: return "agents"
        case .inbox: return "inbox"
        case .settings: return "settings"
        case .agent(let name): return "agent:" + name
        }
    }

    init?(key: String) {
        switch key {
        case "agents": self = .agents
        case "inbox": self = .inbox
        case "settings": self = .settings
        default:
            guard key.hasPrefix("agent:"), key.count > 6 else { return nil }
            self = .agent(String(key.dropFirst(6)))
        }
    }

    /// What to show instead, once the agents are known: All agents for an agent that isn't there; nil: keep it.
    func fallback(agents: Set<String>, loaded: Bool) -> SidebarItem? {
        if case .agent(let name) = self, loaded, !agents.contains(name) { return .agents }
        return nil
    }
}

/// The Mac's sidebar: All agents and the inbox, every agent with its status (and what's new in its Shared), then
/// settings.
struct Sidebar: View {
    @Binding var selection: SidebarItem?
    @Environment(AppModel.self) private var model
    @State private var actions = AgentActions()

    var body: some View {
        List(selection: $selection) {
            Section {
                Label("All agents", systemImage: "person.2").tag(SidebarItem.agents)
                Label("Inbox", systemImage: "tray").badge(model.pendingCount).tag(SidebarItem.inbox)
            }
            Section {
                ForEach(model.agentList) { a in
                    HStack(spacing: 6) {
                        Text(a.emoji ?? "").frame(width: 20)
                        Text(a.title).lineLimit(1)
                        Spacer()
                        Circle().fill(a.statusColor).frame(width: 8, height: 8).help(a.statusLabel)
                    }
                    .badge(model.sharedCounts.agents[a.name] ?? 0)
                    .help(sharedHelp(a.name))
                    .tag(SidebarItem.agent(a.name))
                    .contextMenu {
                        CopyAgentButton(name: a.name)
                        Divider()
                        DeleteAgentButton(name: a.name, actions: actions)
                    }
                }
            } header: {
                HStack {
                    Text("Agents")
                    Spacer()
                    Button { model.newAgent = NewAgentRequest() } label: { Image(systemName: "plus") }
                        .buttonStyle(.borderless)
                        .help("New agent (⌘N)")
                        .accessibilityLabel("New agent")
                }
            }
            Section {
                Label("Settings", systemImage: "gearshape").tag(SidebarItem.settings)
            }
        }
        .safeAreaInset(edge: .bottom) {
            HStack { ConnectionDot(); Spacer() }.padding(10)
        }
        .agentActions(actions)
    }

    private func sharedHelp(_ name: String) -> String {
        let n = model.sharedCounts.agents[name] ?? 0
        return n > 0 ? "\(n) new in Shared" : ""
    }
}

/// The menu bar extra: how many requests wait, and quick answers.
public struct MenuBarView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.openWindow) private var openWindow

    public init() {}

    public var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text("Valet").font(.headline)
                Spacer()
                if model.signedIn && !model.locked {
                    Text(model.pendingCount == 0 ? "Nothing waiting" : "\(model.pendingCount) waiting")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            .padding(12)
            Divider()
            if !model.signedIn {
                Text("Open Valet to sign in.").foregroundStyle(.secondary).padding(12)
            } else if model.locked {
                Button { Task { await model.unlock() } } label: {
                    Label("Unlock with \(Biometry.name)", systemImage: Biometry.symbol)
                }
                .padding(12)
            } else if model.pending.isEmpty {
                Label("Nothing is waiting on you", systemImage: "checkmark.circle")
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity)
                    .padding(20)
            } else {
                ScrollView {
                    VStack(spacing: 8) {
                        ForEach(model.pending) { item in
                            InboxCard(item: item, compact: true)
                                .overlay(alignment: .topTrailing) {
                                    Button {
                                        model.openItem(item.id)
                                        openMain()
                                    } label: { Image(systemName: "arrow.up.forward.app") }
                                        .buttonStyle(.borderless)
                                        .help("Open in Valet")
                                        .padding(8)
                                }
                        }
                    }
                    .padding(10)
                }
                .frame(maxHeight: 460)
            }
            Divider()
            HStack {
                Button("Open Valet") { openMain() }
                Spacer()
                Button("Quit") { NSApplication.shared.terminate(nil) }
            }
            .buttonStyle(.borderless)
            .padding(10)
        }
        .frame(width: 360)
        .tint(.valet)
        // Take over in the menu bar: the live view opens in the main window.
        .onChange(of: model.openLive) { if model.openLive != nil { openMain() } }
    }

    private func openMain() {
        openWindow(id: "main")
        NSApplication.shared.activate(ignoringOtherApps: true)
    }
}

/// The menu bar icon: a tray, with the count while anything waits.
public struct MenuBarLabel: View {
    let model: AppModel

    public init(model: AppModel) { self.model = model }

    public var body: some View {
        let n = model.signedIn && !model.locked ? model.pendingCount : 0
        if n > 0 {
            Label("\(n)", systemImage: "tray.full.fill").labelStyle(.titleAndIcon)
        } else {
            Image(systemName: "tray")
        }
    }
}
#endif
