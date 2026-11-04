import SwiftUI

public struct ContinueRef: Hashable, Sendable {
    public let agent: String
    public init(_ agent: String) { self.agent = agent }
}

public struct HandoffRef: Hashable, Sendable {
    public let agent: String
    public init(_ agent: String) { self.agent = agent }
}

extension Agent {
    /// Claude Code on the runner's machine: its transcripts are there, so it can continue one of its sessions.
    var canContinue: Bool { harness == "claude" && level == "host" }
}

private func kb(_ n: Int) -> String { byteSize(n) }

/// A Claude Code session as a row: its title, when, how big.
private struct ClaudeSessionRow<Action: View>: View {
    let session: ClaudeSession
    @ViewBuilder var action: Action

    var body: some View {
        HStack(alignment: .center, spacing: 10) {
            VStack(alignment: .leading, spacing: 2) {
                Text(session.title.isEmpty ? "Untitled session" : session.title).lineLimit(2)
                Text([Date(timeIntervalSince1970: session.updatedAt).ago, kb(session.size), String(session.id.prefix(8))]
                    .joined(separator: " · "))
                    .font(.caption).foregroundStyle(.secondary)
            }
            Spacer(minLength: 6)
            action
        }
    }
}

/// Continue a Claude Code session (one run in a terminal) in this agent, with its whole
/// history: the sessions Claude Code ran in the agent's working folder.
public struct ContinueView: View {
    let name: String
    @Environment(AppModel.self) private var model
    @State private var result: ClaudeSessions?
    @State private var note: String?
    @State private var error: String?
    @State private var loading: String?

    public init(name: String) { self.name = name }

    public var body: some View {
        List {
            if let a = model.agents[name], !a.canContinue {
                Section {
                    Text("Only a Claude Code agent on this machine can continue a Claude Code session: its transcripts "
                         + "are there. Hand the conversation off instead: the new session starts with it, condensed.")
                    NavigationLink(value: HandoffRef(name)) { Text("Hand off a conversation") }
                }
            } else if let r = result {
                Section {
                    if r.sessions.isEmpty {
                        Text("Claude Code has no sessions in this folder. Set the agent's working folder to the "
                             + "project's folder (Settings, Computer), or hand a session off instead.")
                            .foregroundStyle(.secondary)
                    }
                    ForEach(r.sessions) { s in
                        ClaudeSessionRow(session: s) { action(s) }
                    }
                } footer: {
                    Text("Sessions Claude Code ran in \(r.cwd), this agent's working folder. One continued here goes on "
                         + "with its whole history. Stop it wherever it still runs (a terminal, say) first: "
                         + "two of them writing to one session would mix it up.")
                }
            } else if let note {
                Section { Text(note) }
            } else if error == nil {
                ProgressView()
            }
            if let error { Section { ErrorText(text: error) } }
        }
        .navigationTitle("Continue a session")
        .task(id: model.hellos) { await load() }
        .refreshable { await load() }
    }

    @ViewBuilder private func action(_ s: ClaudeSession) -> some View {
        if let owner = s.imported {
            Button(owner == name ? "Open" : "With \(model.title(of: owner))") {
                model.openSession = SessionRef(agent: owner, id: s.id)
            }
            .buttonStyle(.bordered).controlSize(.small)
        } else if loading == s.id {
            HStack(spacing: 6) { ProgressView().controlSize(.small); Text("Loading its history…").font(.caption) }
        } else {
            Button("Continue here") { Task { await go(s) } }
                .buttonStyle(.borderedProminent).controlSize(.small)
                .disabled(loading != nil)
        }
    }

    private func load() async {
        guard model.agents[name]?.canContinue ?? true else { return }
        do {
            result = try await model.api.claudeSessions(agent: name)
            note = nil
            error = nil
        } catch let e as APIError where e.status == 503 {
            note = "\(model.title(of: name)) isn't running: start it, then come back."
        } catch is CancellationError {
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func go(_ s: ClaudeSession) async {
        loading = s.id
        error = nil
        defer { loading = nil }
        do {
            let session = try await model.api.importClaudeSession(agent: name, id: s.id)
            model.put(session)
            model.openSession = SessionRef(agent: name, id: session.id)
        } catch {
            self.error = error.localizedDescription
        }
    }
}

/// Hand off a conversation (a Claude Code transcript on a runner's machine, or a Valet session of any agent) to a new
/// session of this agent: its condensed text is the first message, then the owner's note.
public struct HandoffView: View {
    let name: String
    @Environment(AppModel.self) private var model
    @State private var from = "claude"
    @State private var runner: String?
    @State private var folders: ClaudeFolders?
    @State private var folder: String?
    @State private var claude: ClaudeSessions?
    @State private var agent: String?
    @State private var sessions: [Session]?
    @State private var source: HandoffSource?
    @State private var preview: HandoffText?
    @State private var condensing = false
    @State private var note = ""
    @State private var starting = false
    @State private var error: String?
    @State private var showText = false

    public init(name: String) { self.name = name }

    public var body: some View {
        Form {
            Section {
                ChoiceList(items: [
                    .init(id: "claude", name: "A Claude Code session",
                          summary: "From a terminal, on a runner's machine."),
                    .init(id: "session", name: "A Valet session", summary: "Of any agent, whatever its harness."),
                ], selection: from, field: "from") { f in
                    guard f != from else { return }
                    from = f
                    source = nil
                    preview = nil
                }
            } header: {
                Text("From")
            } footer: {
                Text("A new session of \(model.title(of: name)) starts with an earlier conversation as its first message. "
                     + "Valet condenses it without a model: what was said, each tool call with its main argument, the "
                     + "newest kept within 60 KB.")
            }
            if from == "claude" { claudePicker } else { sessionPicker }
            if condensing {
                Section { HStack { ProgressView().controlSize(.small); Text("Condensing…") } }
            }
            if let p = preview {
                Section {
                    Text("\(p.entries) \(p.entries == 1 ? "entry" : "entries"), \(kb(p.bytes))"
                         + (p.omitted > 0 ? "; \(p.omitted) earlier \(p.omitted == 1 ? "entry" : "entries") didn't fit" : "")
                         + ".")
                    DisclosureGroup("Read it", isExpanded: $showText) {
                        CodeText(text: clip(p.text, 60_000))
                    }
                    TextField("What to do now", text: $note,
                              prompt: Text("Optional. Without it, it says where things stand and waits."), axis: .vertical)
                        .lineLimit(2...6)
                        .accessibilityIdentifier("handoff-note")
                    Button {
                        Task { await start() }
                    } label: {
                        if starting { ProgressView().controlSize(.small) } else { Text("Start the session") }
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(starting || source == nil)
                    .accessibilityIdentifier("handoff-start")
                } header: {
                    Text("What it gets: \(p.title.isEmpty ? p.source : p.title)")
                }
            }
            if let error { Section { ErrorText(text: error) } }
        }
        .formStyle(.grouped)
        .navigationTitle("Hand off")
        .task(id: [from, runner ?? "", folder ?? "", agent ?? ""]) { await loadPicker() }
    }

    // MARK: picking the conversation

    @ViewBuilder private var claudePicker: some View {
        let runners = model.runnerList.filter(\.connected).map(\.name)
        Section {
            if runners.count > 1 {
                Picker("Runner", selection: Binding(get: { runner ?? runners.first ?? "" }, set: { runner = $0; folder = nil })) {
                    ForEach(runners, id: \.self) { Text($0).tag($0) }
                }
            }
            if let folder {
                Button { self.folder = nil } label: { Label("Folders", systemImage: "chevron.backward") }
                if let c = claude {
                    if c.sessions.isEmpty { Text("No sessions in this folder.").foregroundStyle(.secondary) }
                    ForEach(c.sessions) { s in
                        ClaudeSessionRow(session: s) {
                            useButton(.claude(folder: folder, sessionID: s.id, runner: c.runner))
                        }
                    }
                } else {
                    ProgressView()
                }
            } else if let f = folders {
                if f.folders.isEmpty { Text("No Claude Code sessions on that machine.").foregroundStyle(.secondary) }
                ForEach(f.folders) { x in
                    Button { folder = x.folder } label: {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(x.cwd ?? x.folder).font(.callout.monospaced()).lineLimit(2).truncationMode(.head)
                            Text("\(x.sessions) \(x.sessions == 1 ? "session" : "sessions") · "
                                 + Date(timeIntervalSince1970: x.updatedAt).ago)
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                }
            } else {
                ProgressView()
            }
        } header: {
            Text(folder == nil ? "Claude Code's project folders\(folders.map { " on \($0.runner)" } ?? "")" : "Its sessions")
        }
    }

    @ViewBuilder private var sessionPicker: some View {
        Section {
            Picker("Agent", selection: Binding(get: { agent ?? "" }, set: { agent = $0.isEmpty ? nil : $0 })) {
                Text("Pick one").tag("")
                ForEach(model.agentList) { a in Text(a.title).tag(a.name) }
            }
            if let agent {
                if let sessions {
                    if sessions.isEmpty { Text("No sessions yet.").foregroundStyle(.secondary) }
                    ForEach(sessions) { s in
                        HStack {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(s.displayTitle).lineLimit(2)
                                Text(Date(timeIntervalSince1970: s.updatedAt).ago).font(.caption).foregroundStyle(.secondary)
                            }
                            Spacer()
                            useButton(.session(agent: agent, sessionID: s.id))
                        }
                    }
                } else {
                    ProgressView()
                }
            }
        } header: {
            Text("The session")
        }
    }

    private func useButton(_ s: HandoffSource) -> some View {
        Button(source == s ? "Chosen" : "Use this") { Task { await choose(s) } }
            .buttonStyle(.bordered).controlSize(.small)
            .disabled(source == s || condensing)
    }

    private func loadPicker() async {
        error = nil
        do {
            if from == "claude" {
                if let folder {
                    claude = nil
                    claude = try await model.api.claudeFolderSessions(folder: folder, runner: runner)
                } else {
                    folders = try await model.api.claudeFolders(runner: runner)
                }
            } else if let agent {
                sessions = nil
                sessions = try await model.api.sessions(agent)
            }
        } catch is CancellationError {
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func choose(_ s: HandoffSource) async {
        source = s
        preview = nil
        condensing = true
        error = nil
        defer { condensing = false }
        do { preview = try await model.api.handoffPreview(s) } catch {
            source = nil
            self.error = error.localizedDescription
        }
    }

    private func start() async {
        guard let source else { return }
        starting = true
        error = nil
        defer { starting = false }
        do {
            let s = try await model.api.handoff(agent: name, source: source, note: note)
            model.put(s)
            model.openSession = SessionRef(agent: name, id: s.id)
        } catch {
            self.error = error.localizedDescription
        }
    }
}
