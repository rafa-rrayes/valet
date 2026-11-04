import SwiftUI

/// One agent: where it stands, Start / Stop / Restart, what it needs, and its sessions, newest first.
public struct AgentView: View {
    let name: String
    @Environment(AppModel.self) private var model
    @State private var actions = AgentActions()
    @State private var loadError: String?
    @State private var missing = false
    @State private var creating = false
    @State private var newError: String?
    @State private var opened: SessionRef?
    /// Its definition (nil until loaded, or when agent.yaml doesn't validate): its memory switch and browser profiles.
    @State private var definition: DefinitionDraft?
    /// Its triggers, wake-ups and latest runs (M4).
    @State private var always: AgentTriggers?
    @State private var runs: [TriggerRun] = []
    /// What the New agent form had to say (it was created but didn't start).
    @State private var note: String?
    @Environment(\.dynamicTypeSize) private var typeSize

    public init(name: String) { self.name = name }

    public var body: some View {
        List {
            if let note {
                Section {
                    NoteBox(text: note) {
                        Button { self.note = nil } label: { Image(systemName: "xmark") }
                            .buttonStyle(.borderless).accessibilityLabel("Dismiss")
                    }
                    .accessibilityIdentifier("agent-note")
                    .listRowInsets(EdgeInsets(top: 6, leading: 12, bottom: 6, trailing: 12))
                }
            }
            if let a = model.agents[name] {
                Section { header(a) }
                if a.restartNeeded {
                    Section {
                        NoteBox(text: "Saved changes apply after a restart.", systemImage: "arrow.clockwise.circle") {
                            Button("Restart") { Task { await actions.run(.restart, name, model: model) } }
                                .buttonStyle(.borderedProminent).controlSize(.small)
                        }
                        .listRowInsets(EdgeInsets(top: 6, leading: 12, bottom: 6, trailing: 12))
                    }
                }
                if let needs = a.needsLine {
                    Section {
                        NoteBox(text: needs + ": it can't start until "
                                + (a.missingSecrets.count > 1 ? "they're" : "it's") + " set.",
                                systemImage: "key.fill") {
                            Button("Set") { actions.offer(a.missingSecrets, for: name) }
                                .buttonStyle(.bordered).controlSize(.small)
                        }
                        .listRowInsets(EdgeInsets(top: 6, leading: 12, bottom: 6, trailing: 12))
                    }
                }
            } else if missing {
                ContentUnavailableView("No agent named \(name)", systemImage: "questionmark.circle")
            }
            if let a = model.agents[name] {
                Section { more }
                AgentTriggersSection(agent: a, info: always, runs: runs) { await loadTriggers() }
            }
            Section {
                let list = model.sessionList(name)
                if list.isEmpty {
                    Text(loadError ?? "No sessions yet.").foregroundStyle(loadError == nil ? Color.secondary : .red)
                }
                ForEach(list) { s in
                    NavigationLink(value: SessionRef(agent: name, id: s.id)) { SessionRow(session: s) }
                        .accessibilityIdentifier("session-row")
                }
            } header: {
                HStack {
                    Text("Sessions")
                    Spacer()
                    Button {
                        Task { await newSession() }
                    } label: {
                        if creating { ProgressView().controlSize(.small) } else { Label("New session", systemImage: "plus") }
                    }
                    .disabled(creating || !(model.agents[name]?.isUp ?? false))
                    .textCase(nil)
                    .accessibilityIdentifier("new-session")
                }
            } footer: {
                if let newError { ErrorText(text: newError) }
            }
        }
        .navigationTitle(model.agents[name]?.title ?? name)
        .readableWidth()
        #if os(iOS)
        .navigationBarTitleDisplayMode(.inline)
        #endif
        .navigationDestination(item: $opened) { ref in SessionView(agent: ref.agent, id: ref.id) }
        .refreshable {
            await load()
            await loadTriggers()
        }
        .task(id: model.hellos) { await load() }
        // Back from its settings (triggers saved there): look again.
        .onAppear {
            if let n = model.agentNotes.removeValue(forKey: name) { note = n }
            if always != nil { Task { await loadTriggers() } }
        }
        // A trigger that fires moves next_trigger or starts a turn: look again then, not on every frame.
        .task(id: triggerKey) {
            if always != nil { try? await Task.sleep(for: .milliseconds(500)) }
            if !Task.isCancelled { await loadTriggers() }
        }
        .agentActions(actions)
    }

    @ViewBuilder private func header(_ a: Agent) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            if typeSize.isAccessibilitySize {
                // Big text: the name gets the whole width, the badge its own line.
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    if let e = a.emoji, !e.isEmpty { Text(e).font(.title2) }
                    Text(a.title).font(.title2.bold())
                }
                if a.title != a.name { Text(a.name).font(.caption.monospaced()).foregroundStyle(.secondary) }
                StatusBadge(agent: a).fixedSize()
            } else {
                HStack(alignment: .center, spacing: 10) {
                    if let e = a.emoji, !e.isEmpty { Text(e).font(.largeTitle) }
                    VStack(alignment: .leading, spacing: 2) {
                        Text(a.title).font(.title2.bold())
                        if a.title != a.name { Text(a.name).font(.caption.monospaced()).foregroundStyle(.secondary) }
                    }
                    .layoutPriority(1)
                    Spacer(minLength: 8)
                    StatusBadge(agent: a).fixedSize()
                }
            }
            if let act = a.activityText { Label(act, systemImage: "waveform").font(.callout) }
            if !a.meta.isEmpty { Text(a.meta).font(.caption).foregroundStyle(.secondary) }
            AgentExtras(agent: a)
            if let e = a.definitionError ?? a.error { ErrorText(text: e) }
            AgentButtons(agent: a, actions: actions).padding(.top, 2)
        }
        .padding(.vertical, 4)
    }

    /// Its main session, settings, memory, the live view of each browser profile it uses, continuing and handing off.
    @ViewBuilder private var more: some View {
        if let main = model.agents[name]?.mainSession {
            NavigationLink(value: SessionRef(agent: name, id: main)) {
                Label("Main session", systemImage: "house")
            }
            .accessibilityIdentifier("agent-main-session")
        }
        NavigationLink(value: AgentSettingsRef(name)) { Label("Settings", systemImage: "slider.horizontal.3") }
            .accessibilityIdentifier("agent-settings")
        if model.agents[name]?.hasComputer == true {
            NavigationLink(value: TerminalRef(name)) { Label("Terminal", systemImage: "terminal") }
                .accessibilityIdentifier("agent-terminal")
            NavigationLink(value: FilesRef(name)) { Label("Files", systemImage: "folder") }
                .accessibilityIdentifier("agent-files")
        }
        NavigationLink(value: SharedListRef(name)) {
            HStack {
                Label("Shared", systemImage: "rectangle.stack")
                Spacer()
                if let n = model.sharedCounts.agents[name], n > 0 { NewCount(n: n) }
            }
        }
        .accessibilityIdentifier("agent-shared")
        NavigationLink(value: MemoryRef(name)) {
            HStack {
                Label("Memory", systemImage: "brain")
                Spacer()
                if let d = definition { Text(d.memory ? "On" : "Off").foregroundStyle(.secondary) }
            }
        }
        .accessibilityIdentifier("agent-memory")
        ForEach(definition?.browserProfiles ?? [], id: \.self) { p in
            Button {
                model.openLiveView(LiveTarget(profile: p, tab: nil, itemID: nil))
            } label: {
                HStack {
                    Label("Browser: \(p)", systemImage: "safari")
                    Spacer()
                    Image(systemName: "rectangle.on.rectangle").foregroundStyle(.secondary)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityIdentifier("agent-live")
        }
        if model.agents[name]?.canContinue == true {
            NavigationLink(value: ContinueRef(name)) {
                Label("Continue a Claude Code session", systemImage: "arrow.uturn.forward")
            }
        }
        NavigationLink(value: HandoffRef(name)) { Label("Hand off a conversation", systemImage: "arrow.triangle.branch") }
            .accessibilityIdentifier("agent-handoff")
        CopyAgentButton(name: name)
            #if os(macOS)
            .buttonStyle(.borderless)       // a row like the others, not a bordered button
            #endif
    }

    private func load() async {
        do {
            let detail = try await model.api.agentDetail(name)
            model.put(detail.agent)
            definition = detail.definition.map(DefinitionDraft.init)
            try await model.loadSessions(name)
            loadError = nil
        } catch let e as APIError where e.status == 404 {
            missing = true
        } catch is CancellationError {
        } catch {
            loadError = error.localizedDescription
        }
    }

    private var triggerKey: String {
        let a = model.agents[name]
        return [String(model.hellos), String(describing: a?.nextTrigger), a?.status.rawValue ?? "", a?.desired ?? "",
                String(a?.queuedCount ?? 0)].joined(separator: "|")
    }

    private func loadTriggers() async {
        do {
            async let info = model.api.triggers(name)
            async let page = model.api.triggerRuns(name, limit: 10)
            always = try await info
            runs = try await page.runs
        } catch {
            // A server without triggers (or the agent gone): no section.
        }
    }

    private func newSession() async {
        creating = true
        newError = nil
        defer { creating = false }
        do {
            let s = try await model.newSession(name)
            opened = SessionRef(agent: name, id: s.id)
        } catch {
            newError = error.localizedDescription
        }
    }
}

struct SessionRow: View {
    let session: Session

    var body: some View {
        HStack {
            VStack(alignment: .leading, spacing: 3) {
                Text(session.displayTitle).lineLimit(2)
                Text(Date(timeIntervalSince1970: session.updatedAt).ago
                     + (session.queued > 0 ? " · \(session.queued) waiting" : ""))
                    .font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            if session.isMain { Chip(text: "main", color: .valet) }
            if session.isWorking { Chip(text: "working", color: .blue, dot: true) }
        }
    }
}
