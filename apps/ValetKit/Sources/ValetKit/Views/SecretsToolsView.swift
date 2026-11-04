import SwiftUI

/// Settings › Secrets & tools (navigation value).
public struct SecretsToolsRef: Hashable, Sendable { public init() {} }

extension OutcomeTone {
    var color: Color {
        switch self {
        case .good: return .green
        case .bad: return .red
        case .waiting: return .orange
        case .working: return .blue
        case .quiet: return .gray
        }
    }
}

/// Everything agents share for their tools (app.js's SecretsToolsView): the secrets (set or not, who uses each,
/// a new one, a value changed, one deleted), the gateway's tool servers and mcp.yaml, and the recent tool calls.
/// There are no frames for these: it loads again on a hello, a pull, and Refresh.
public struct SecretsToolsView: View {
    @Environment(AppModel.self) private var model
    @State private var secrets: [Secret]?
    @State private var secretsError: String?
    @State private var servers: [GatewayServerDetail]?
    @State private var serversFileError: String?
    @State private var serversError: String?
    @State private var calls: [ToolCallRecord] = []
    @State private var moreCalls = false
    @State private var callsLoaded = false
    @State private var callsError: String?
    @State private var loadingOlder = false
    @State private var editing: SecretEditor.Mode?
    @State private var deleting: Secret?
    @State private var error: String?

    public init() {}

    public var body: some View {
        Form {
            secretsSection
            serversSection
            callsSection
        }
        .formStyle(.grouped)
        .readableWidth()
        .navigationTitle("Secrets & tools")
        #if os(iOS)
        .navigationBarTitleDisplayMode(.inline)
        #endif
        .task(id: model.hellos) { await loadAll() }
        .refreshable { await loadAll() }
        .sheet(item: $editing) { mode in
            SecretEditor(mode: mode, taken: Set((secrets ?? []).filter(\.set).map(\.name))) { await loadSecrets() }
        }
        .confirmationDialog(deleting.map(deleteQuestion) ?? "", isPresented: Binding(get: { deleting != nil },
                                                                                    set: { if !$0 { deleting = nil } }),
                            titleVisibility: .visible) {
            Button("Delete", role: .destructive) { if let s = deleting { Task { await delete(s) } } }
        }
        .errorAlert($error)
    }

    // MARK: secrets

    private var secretsSection: some View {
        Section {
            if let e = secretsError { ErrorText(text: e) }
            if secrets == nil && secretsError == nil { ProgressView() }
            if let list = secrets, list.isEmpty { Text("No secrets yet.").foregroundStyle(.secondary) }
            ForEach(secrets ?? []) { s in secretRow(s) }
        } header: {
            HStack {
                Text("Secrets")
                Spacer()
                Button { editing = .new } label: { Label("New secret", systemImage: "plus") }
                    .font(.subheadline)
                    .textCase(nil)
                    .accessibilityIdentifier("secret-new")
            }
        } footer: {
            Text("Keys and tokens your agents get as environment variables (an agent lists the ones it needs in its "
                 + "settings), and that tool servers use to sign in. Values are stored encrypted and never shown again.")
        }
    }

    private func users(_ s: Secret) -> [String] {
        s.usedBy.map { model.title(of: $0) } + s.servers.map { "the \($0) server" }
    }

    private func secretRow(_ s: Secret) -> some View {
        let used = users(s)
        let blocked = s.usedBy.count > 1 ? "they can't start until it's set" : !s.usedBy.isEmpty ? "it can't start until it's set"
            : s.servers.count > 1 ? "they can't connect until it's set" : !s.servers.isEmpty ? "it can't connect until it's set" : nil
        let sub = [used.isEmpty ? "Nothing uses it" : "Used by " + andList(used),
                   s.set ? s.updatedAt.map { "set " + Date(timeIntervalSince1970: $0).ago } : blocked]
            .compactMap { $0 }.joined(separator: " · ")
        return VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(s.name).font(.body.monospaced()).lineLimit(1).truncationMode(.middle)
                Spacer()
                Chip(text: s.set ? "set" : "not set", color: s.set ? .green : .orange)
            }
            Text(sub).font(.caption).foregroundStyle(.secondary)
            HStack {
                Button(s.set ? "Change value" : "Set value") { editing = .change(s.name) }
                    .modifier(OptionStyle(emphasis: s.set ? .normal : .primary))
                if s.set {
                    Button("Delete", role: .destructive) { deleting = s }
                        .buttonStyle(.bordered).tint(.red)
                }
            }
            .controlSize(.small)
        }
        .padding(.vertical, 2)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("secret-\(s.name)")
    }

    private func deleteQuestion(_ s: Secret) -> String {
        let used = s.usedBy.map { model.title(of: $0) }
        let servers = s.servers.map { "the \($0) server" }
        let many = used.count > 1
        var q = ""
        if !used.isEmpty {
            q += "\(andList(used)) \(many ? "use" : "uses") \(s.name). \(many ? "They keep" : "It keeps") it until "
                + "\(many ? "they stop" : "it stops"), then can't start again until it's set. "
        }
        if !servers.isEmpty {
            q += andList(servers).capitalizedFirst + " \(servers.count > 1 ? "stop" : "stops") working until it's set again. "
        }
        return q.isEmpty ? "Delete \(s.name)? Nobody can read it back, so you'd need the value again to set it."
            : q + "Delete it?"
    }

    private func delete(_ s: Secret) async {
        do {
            try await model.api.deleteSecret(s.name)
        } catch let e as APIError where e.status == 404 {
        } catch {
            self.error = error.localizedDescription
        }
        await loadSecrets()
    }

    // MARK: tool servers

    private var serversSection: some View {
        Section {
            if let e = serversError { ErrorText(text: e) }
            if servers == nil && serversError == nil { ProgressView() }
            if let e = serversFileError {
                NoteBox(text: "mcp.yaml has an error, so its servers can't be used. " + e,
                        systemImage: "exclamationmark.triangle")
            }
            ForEach(servers ?? []) { s in serverRow(s) }
            NavigationLink {
                RawFileEditor(title: "mcp.yaml", note: Self.registryNote, load: { try await model.api.registry() },
                              save: { try await model.api.saveRegistry($0) })
                    .onDisappear { Task { await loadServers(); await loadSecrets() } }
            } label: {
                Label("Edit the servers file", systemImage: "doc.text")
            }
            .accessibilityIdentifier("registry-edit")
        } header: {
            Text("Tool servers")
        } footer: {
            Text("MCP servers your agents can use through Valet's gateway. Turn one on for an agent in its settings.")
        }
    }

    static let registryNote = """
        mcp.yaml lists the servers agents can be given. Put credentials in as ${secret:NAME}, one of your secrets, \
        or ${env:NAME}, a variable of the control plane's environment: they stay in Valet, and the file, the agents \
        and their computers never see them. For example:

        servers:
          github:
            description: GitHub issues and pull requests
            url: https://api.githubcopilot.com/mcp/
            headers:
              Authorization: Bearer ${secret:GITHUB_TOKEN}
        """

    private func serverRow(_ d: GatewayServerDetail) -> some View {
        let s = d.server
        let used = s.agents.map { model.title(of: $0) }
        let creds = s.kind == "http" ? d.headers : d.env
        let unset = Set((secrets ?? []).filter { !$0.set }.map(\.name))
        return VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(s.name).font(.body.weight(.semibold))
                Spacer()
                if s.builtin {
                    Chip(text: "built in", color: .gray)
                } else {
                    Chip(text: s.connected ? "connected" : "not connected", color: s.connected ? .green : .gray, dot: true)
                }
            }
            if let desc = s.description, !desc.isEmpty {
                Text(brief(desc, 200)).font(.subheadline).foregroundStyle(.secondary)
            }
            if let w = d.whereText, !w.isEmpty {
                Text(w).font(.caption.monospaced()).lineLimit(2).truncationMode(.middle).textSelection(.enabled)
            }
            if !creds.isEmpty {
                Text((s.kind == "http" ? "Headers: " : "Environment: ") + creds.joined(separator: ", "))
                    .font(.caption).foregroundStyle(.secondary)
            }
            if !d.secrets.isEmpty {
                Text("Secrets: " + d.secrets.map { $0 + (unset.contains($0) ? " (not set)" : "") }.joined(separator: ", "))
                    .font(.caption).foregroundStyle(d.secrets.contains(where: unset.contains) ? .orange : .secondary)
            }
            Text([d.kindText, used.isEmpty ? "no agent uses it yet" : "used by " + andList(used)].joined(separator: " · "))
                .font(.caption).foregroundStyle(.secondary)
        }
        .padding(.vertical, 2)
        .accessibilityElement(children: .combine)
    }

    // MARK: recent tool calls

    private var callsSection: some View {
        Section {
            if let e = callsError { ErrorText(text: e) }
            if !callsLoaded && callsError == nil { ProgressView() }
            if callsLoaded && calls.isEmpty { Text("No tool calls yet.").foregroundStyle(.secondary) }
            ForEach(calls, id: \.rowKey) { c in ToolCallRow(call: c) }
            if moreCalls {
                Button {
                    Task { await loadCalls(older: true) }
                } label: {
                    HStack { Text("Older"); if loadingOlder { ProgressView().controlSize(.small) } }
                }
                .disabled(loadingOlder)
            }
        } header: {
            HStack {
                Text("Recent tool calls")
                Spacer()
                Button { Task { await loadCalls(older: false) } } label: { Label("Refresh", systemImage: "arrow.clockwise") }
                    .labelStyle(.iconOnly)
                    .textCase(nil)
                    .help("Refresh")
            }
        }
    }

    // MARK: loading

    private func loadAll() async {
        async let a: Void = loadSecrets()
        async let b: Void = loadServers()
        async let c: Void = loadCalls(older: false)
        _ = await (a, b, c)
    }

    private func loadSecrets() async {
        do {
            secrets = try await model.api.secrets().sorted { $0.name < $1.name }
            secretsError = nil
        } catch is CancellationError {
        } catch {
            secretsError = error.localizedDescription
        }
    }

    private func loadServers() async {
        do {
            let r = try await model.api.gatewayServerDetails()
            servers = r.servers
            serversFileError = r.error
            serversError = nil
        } catch is CancellationError {
        } catch {
            serversError = error.localizedDescription
        }
    }

    private func loadCalls(older: Bool) async {
        if older { loadingOlder = true }
        defer { loadingOlder = false }
        do {
            let r = try await model.api.toolCalls(before: older ? calls.last?.id : nil)
            calls = older ? calls + r.calls.filter { c in !calls.contains { $0.id == c.id } } : r.calls
            moreCalls = r.more
            callsError = nil
        } catch is CancellationError {
        } catch {
            callsError = error.localizedDescription
        }
        callsLoaded = true
    }
}

/// One call through the gateway: who called which tool, how it went, when, how long, and its arguments.
struct ToolCallRow: View {
    let call: ToolCallRecord
    @Environment(AppModel.self) private var model
    @State private var showArgs = false

    var body: some View {
        let c = call
        let o = c.outcomeLabel
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline) {
                Text("\(Text(verbatim: model.title(of: c.agent))) · \(Text(verbatim: c.server.isEmpty ? c.tool : c.server + " › " + c.tool).font(.callout.monospaced()))")
                    .lineLimit(2)
                Spacer(minLength: 6)
                Chip(text: o.text, color: o.tone.color).fixedSize()
            }
            Text([Date(timeIntervalSince1970: c.startedAt).ago, c.decision == "ask" ? "asked you" : nil,
                  durationText(c.duration)].compactMap { $0 }.joined(separator: " · "))
                .font(.caption).foregroundStyle(.secondary)
            if let e = c.error, !e.isEmpty { Text(clip(e, 500)).font(.caption).foregroundStyle(.red).lineLimit(4) }
            if let args = c.arguments {
                DisclosureGroup("Arguments", isExpanded: $showArgs) { CodeText(text: clip(args.display, 2000)) }
                    .font(.caption)
            }
        }
        .padding(.vertical, 2)
    }
}

/// A secret's value: a new secret (its name, then the value) or a new value for one. Write-only: the value is
/// never filled from anywhere and is dropped once stored.
struct SecretEditor: View {
    enum Mode: Identifiable, Hashable {
        case new, change(String)
        var id: String { if case .change(let n) = self { return n } else { return "\u{1}new" } }
    }

    let mode: Mode
    /// Names that are set already (a new one can't take them).
    let taken: Set<String>
    let saved: () async -> Void
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var value = ""
    @State private var busy = false
    @State private var error: String?

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    if case .change(let n) = mode {
                        LabeledContent("Name") { Text(n).font(.body.monospaced()) }
                    } else {
                        TextField("Name", text: $name, prompt: Text(verbatim: "GITHUB_TOKEN"))
                            .font(.body.monospaced())
                            .autocorrectionDisabled()
                            #if os(iOS)
                            .textInputAutocapitalization(.characters)
                            #endif
                            .accessibilityIdentifier("secret-name")
                    }
                    SecureField("Paste the value", text: $value)
                        .autocorrectionDisabled()
                        #if os(iOS)
                        .textInputAutocapitalization(.never)
                        #endif
                        .onSubmit { Task { await save() } }
                        .accessibilityIdentifier("secret-value")
                    if let error { ErrorText(text: error) }
                } footer: {
                    Text("Stored encrypted on the control plane. Nobody can read a value back, you included.")
                }
            }
            .formStyle(.grouped)
            .navigationTitle(mode == .new ? "New secret" : "Change value")
            #if os(iOS)
            .navigationBarTitleDisplayMode(.inline)
            #endif
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { value = ""; dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button { Task { await save() } } label: {
                        if busy { ProgressView().controlSize(.small) } else { Text("Save") }
                    }
                    .disabled(busy)
                    .accessibilityIdentifier("secret-save")
                }
            }
        }
        #if os(macOS)
        .frame(minWidth: 420, minHeight: 260)
        #endif
    }

    private func save() async {
        let n: String
        if case .change(let x) = mode { n = x } else { n = Secret.name(from: name) }
        error = nil
        if n.isEmpty { error = "Give it a name, like GITHUB_TOKEN."; return }
        guard Secret.isValidName(n) else {
            error = "Use capital letters, digits and _, starting with a letter, like GITHUB_TOKEN."
            return
        }
        if mode == .new && taken.contains(n) { error = "\(n) is already set: use Change value on it."; return }
        let v = SecretsSheet.cleaned(value)
        guard !v.isEmpty else { error = "Paste its value."; return }
        busy = true
        defer { busy = false }
        do {
            try await model.api.setSecret(n, value: v)
            value = ""
            await saved()
            dismiss()
        } catch {
            self.error = error.localizedDescription
        }
    }
}

extension View {
    /// A reading width on wide screens (an iPad, a Mac window): the content at most `max` wide, centred, instead of
    /// lines and fields the whole width of the screen.
    func readableWidth(_ max: CGFloat = 760) -> some View {
        modifier(ReadableWidth(max: max))
    }
}

/// The screen's width is measured on it (the list stays the screen's root, so large titles and search still follow
/// its scrolling), and the margins that leave `max` in the middle go on its scroll content. The sidebar floats over
/// the list's leading edge as a safe area inset (an iPad's, iOS 26 and macOS 26): on iOS a list's margin is counted
/// from the list's edge, under the sidebar, so that edge gets the inset on top; on the Mac it's counted from the
/// inset already. (A GeometryReader behind a List never reported its width through a preference.)
private struct ReadableWidth: ViewModifier {
    let max: CGFloat
    @State private var room = Room()

    struct Room: Equatable {
        var width: CGFloat = 0
        var leading: CGFloat = 0
        var trailing: CGFloat = 0
    }

    func body(content: Content) -> some View {
        let side = room.width > max + 40 ? (room.width - max) / 2 : nil
        #if os(iOS)
        let (leading, trailing) = (room.leading, room.trailing)
        #else
        let (leading, trailing) = (CGFloat(0), CGFloat(0))
        #endif
        content
            .contentMargins(.leading, side.map { $0 + leading }, for: .scrollContent)
            .contentMargins(.trailing, side.map { $0 + trailing }, for: .scrollContent)
            .onGeometryChange(for: Room.self) { g in
                Room(width: g.size.width, leading: g.safeAreaInsets.leading, trailing: g.safeAreaInsets.trailing)
            } action: { r in
                if abs(r.width - room.width) > 0.5 || r.leading != room.leading || r.trailing != room.trailing { room = r }
            }
    }
}
