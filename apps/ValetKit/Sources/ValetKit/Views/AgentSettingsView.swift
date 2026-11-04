import SwiftUI

public struct AgentSettingsRef: Hashable, Sendable {
    public let agent: String
    public init(_ agent: String) { self.agent = agent }
}

/// An agent's settings, as the web UI's form: name and emoji, personality, brain, computer, permissions, tools and
/// their rules, secrets, browser profiles and site rules, memory; then its files as text and their history. The
/// save bar shows while there's something to save or a restart to do.
public struct AgentSettingsView: View {
    let name: String
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var settings: AgentSettings?
    @State private var actions = AgentActions()
    @State private var leaving = false

    public init(name: String) { self.name = name }

    public var body: some View {
        Group {
            if let s = settings { content(s) } else { ProgressView() }
        }
        .navigationTitle("Settings")
        .readableWidth()
        #if os(iOS)
        .navigationBarTitleDisplayMode(.inline)
        #endif
        .task {
            if settings == nil { settings = AgentSettings(name: name, model: model) }
            settings?.start()
            if settings?.loaded == false { await settings?.load() }
        }
        .onDisappear { settings?.stop() }
        .agentActions(actions)
    }

    @ViewBuilder private func content(_ s: AgentSettings) -> some View {
        if s.notFound {
            ContentUnavailableView("No agent named \(name)", systemImage: "questionmark.circle")
        } else if s.detail == nil {
            if let e = s.loadError {
                ContentUnavailableView {
                    Label("Can't load its settings", systemImage: "exclamationmark.triangle")
                } description: { Text(e) } actions: {
                    Button("Try again") { Task { await s.load() } }
                }
            } else {
                ProgressView()
            }
        } else {
            SettingsForm(settings: s, actions: actions)
                .navigationBarBackButtonHidden(s.isDirty)
                .toolbar {
                    if s.isDirty {
                        ToolbarItem(placement: .cancellationAction) {
                            Button { leaving = true } label: { Label("Back", systemImage: "chevron.backward") }
                        }
                    }
                }
                .confirmationDialog("You have unsaved changes", isPresented: $leaving, titleVisibility: .visible) {
                    Button("Save") { Task { if await s.save() { dismiss() } } }
                    Button("Discard & leave", role: .destructive) { s.discard(); dismiss() }
                    Button("Stay", role: .cancel) {}
                }
        }
    }
}

private struct SettingsForm: View {
    @Bindable var settings: AgentSettings
    let actions: AgentActions
    @Environment(AppModel.self) private var model

    private var s: AgentSettings { settings }

    var body: some View {
        Form {
            if s.draft == nil {
                Section {
                    NoteBox(text: "agent.yaml has an error, so the form can't show it. Fix the file, or restore an "
                            + "earlier version from its history.", systemImage: "exclamationmark.triangle")
                    if let e = s.agent?.definitionError { ErrorText(text: e) }
                }
            } else {
                IdentitySection(settings: s)
                PersonalitySection(settings: s)
                KnowsSection(settings: s)
                BrainSection(settings: s)
                ComputerSection(settings: s)
                PermissionsSection(settings: s)
                ToolsSection(settings: s)
                SkillsSection(settings: s)
                SecretsSection(settings: s, actions: actions)
                TriggersFormSection(settings: s)
                BrowserSection(settings: s)
                MemorySection(settings: s)
            }
            Section {
                NavigationLink { AgentFilesView(settings: s) } label: {
                    Label("Files and history", systemImage: "doc.text")
                }
                .accessibilityIdentifier("settings-files")
            } header: {
                Text("Advanced")
            } footer: {
                Text("agent.yaml and persona.md as text, for anything the form doesn't show, "
                     + "and earlier versions to restore.")
            }
            Section {
                DeleteAgentButton(name: s.name, actions: actions)
            } footer: {
                Text("It stops, and Valet forgets it: you're asked first, with what goes.")
            }
        }
        .formStyle(.grouped)
        .scrollDismissesKeyboard(.interactively)
        .safeAreaInset(edge: .bottom, spacing: 0) { SaveBar(settings: s, actions: actions) }
    }
}

// MARK: the save bar

private struct SaveBar: View {
    @Bindable var settings: AgentSettings
    let actions: AgentActions
    @Environment(AppModel.self) private var model

    var body: some View {
        let s = settings
        let dirty = s.isDirty
        let restart = !dirty && (s.agent?.restartNeeded ?? false)
        let message = s.busy ?? (dirty ? "Unsaved changes" : s.flash ?? (restart ? "Saved changes apply after a restart." : nil))
        if dirty || restart || s.busy != nil || s.flash != nil || s.barError != nil {
            VStack(alignment: .leading, spacing: 8) {
                if let message {
                    Text(message).font(.callout.weight(.medium)).accessibilityIdentifier("settings-bar-message")
                }
                if let e = s.barError { ErrorText(text: e) }
                HStack(spacing: 10) {
                    Spacer()
                    if dirty {
                        Button("Discard") { s.discard() }
                            .accessibilityIdentifier("settings-discard")
                        if s.agent?.isUp == true {
                            Button("Save & restart") {
                                endEditing()
                                Task { if await s.save() { await actions.run(.restart, s.name, model: model) } }
                            }
                            .accessibilityIdentifier("settings-save-restart")
                        }
                        Button("Save") {
                            endEditing()
                            Task { await s.save() }
                        }
                        .buttonStyle(.borderedProminent)
                        .keyboardShortcut("s", modifiers: .command)
                        .accessibilityIdentifier("settings-save")
                    } else if restart {
                        Button("Restart") { Task { await actions.run(.restart, s.name, model: model) } }
                            .buttonStyle(.borderedProminent)
                            .accessibilityIdentifier("settings-restart")
                    }
                }
                .buttonStyle(.bordered)
                .disabled(s.busy != nil || actions.busy.contains(s.name))
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(.bar)
            .overlay(alignment: .top) { Divider() }
        }
    }
}

// MARK: name and personality

private struct IdentitySection: View {
    @Bindable var settings: AgentSettings

    var body: some View {
        Section {
            DraftField(title: "Emoji", prompt: "🙂", path: "emoji", labeled: true, settings: settings)
            FieldError(text: settings.errors["emoji"])
            DraftField(title: "Name", prompt: settings.name, path: "display_name", labeled: true, settings: settings)
            FieldError(text: settings.errors["display_name"])
        } header: {
            Text("Name")
        } footer: {
            Text("ID \(Text(settings.name).font(.caption.monospaced())) · it can't change; copy the agent for a new one.")
        }
    }
}

private struct PersonalitySection: View {
    @Bindable var settings: AgentSettings
    @Environment(AppModel.self) private var model
    @State private var replacing: Catalog.Template?

    var body: some View {
        let s = settings
        Section {
            TextEditor(text: Binding(get: { s.persona }, set: { s.persona = $0; s.changed("persona") }))
                .font(.body)
                .frame(minHeight: 150)
                .accessibilityIdentifier("field-persona")
            FieldError(text: s.errors["persona"])
            if let templates = model.catalog?.templates, !templates.isEmpty {
                Menu("Start from a template") {
                    ForEach(templates, id: \.id) { t in
                        Button((t.emoji.map { $0 + " " } ?? "") + t.name) { use(t) }
                    }
                }
            }
        } header: {
            Text("Personality")
        } footer: {
            Text("Who it is and how it should work, in your words. It reads this at the start of every session.")
        }
        .confirmationDialog("Replace your text with the \(replacing?.name ?? "") template?",
                            isPresented: Binding(get: { replacing != nil }, set: { if !$0 { replacing = nil } }),
                            titleVisibility: .visible) {
            Button("Replace", role: .destructive) { if let t = replacing { apply(t) } }
        }
    }

    private func text(_ t: Catalog.Template) -> String {
        t.persona.replacingOccurrences(of: "{who}", with: settings.draft?.displayName ?? settings.name)
    }

    private func use(_ t: Catalog.Template) {
        let p = settings.persona.trimmingCharacters(in: .whitespacesAndNewlines)
        if !p.isEmpty && settings.persona != text(t) { replacing = t } else { apply(t) }
    }

    private func apply(_ t: Catalog.Template) {
        settings.persona = text(t)
        settings.changed("persona")
        replacing = nil
    }
}

// MARK: brain

private struct BrainSection: View {
    @Bindable var settings: AgentSettings
    @Environment(AppModel.self) private var model
    @State private var other = false

    private static let otherTag = "\u{1}other"

    var body: some View {
        let s = settings
        let harness = s.draft?.harness ?? "claude"
        let list = model.catalog?.harnesses ?? []
        let known = list.contains { $0.id == harness }
        let current = list.first { $0.id == harness }
        let models = current?.models ?? []
        let modelID = s.draft?.model
        Section {
            ChoiceList(items: list.map(ChoiceList.Item.init(harness:)) + (known ? [] : [ChoiceList.Item(id: harness, name: harness, summary: "Set in the file")]),
                       selection: harness, field: "harness") { id in
                guard id != harness else { return }
                s.draft?.setString("harness", id)
                s.draft?.set("model", nil)
                other = false
                s.changed("harness")
            }
            FieldError(text: s.errors["harness"])
            Picker("Model", selection: Binding(get: {
                other || (modelID != nil && !models.contains { $0.id == modelID }) ? Self.otherTag : (modelID ?? "")
            }, set: { v in
                if v == Self.otherTag {
                    other = true
                } else {
                    other = false
                    s.draft?.setString("model", v)
                }
                s.changed("model")
            })) {
                Text("Default").tag("")
                ForEach(models, id: \.id) { m in
                    Text(m.name + (m.note.map { " — " + $0 } ?? "")).tag(m.id)
                }
                Text("Other…").tag(Self.otherTag)
            }
            .accessibilityIdentifier("field-model")
            if other || (modelID != nil && !models.contains { $0.id == modelID }) {
                DraftField(title: "Model id", prompt: "the model's id", path: "model", mono: true, settings: s)
            }
            FieldError(text: s.errors["model"])
        } header: {
            Text("Brain")
        } footer: {
            Text("The AI that does the thinking.")
        }
    }
}

// MARK: computer and permissions

private struct ComputerSection: View {
    @Bindable var settings: AgentSettings
    @Environment(AppModel.self) private var model
    @State private var keptMounts: [DefinitionDraft.Mount]?
    @State private var newSite = ""

    var body: some View {
        let s = settings
        let level = s.draft?.level ?? "container"
        var levels = (model.catalog?.levels ?? []).map { ChoiceList.Item(id: $0.id, name: $0.name, summary: $0.summary) }
        if !levels.contains(where: { $0.id == level }) {
            levels.append(ChoiceList.Item(id: level, name: level, summary: "Set in the file."))
        }
        return Group {
            Section {
                ChoiceList(items: levels, selection: level, field: "level") { id in pick(id, from: level) }
                FieldError(text: s.errors["computer.level"])
                FieldError(text: s.errors["computer"])
                if level == "host" {
                    NoteBox(text: "Careful: on this machine it isn't in a container. It runs as the runner's user and "
                            + "can read and change everything that user can, and reach the network. Pick this only "
                            + "for an agent you trust.", systemImage: "exclamationmark.triangle")
                    DraftField(title: "Working folder", prompt: "its own folder", path: "computer.workdir", mono: true,
                               settings: s)
                    FieldError(text: s.errors["computer.workdir"])
                } else if level == "none" {
                    Text("It has no files and runs no commands. It reaches its model and the tool servers you turn on "
                         + "below, and nothing else.").font(.callout).foregroundStyle(.secondary)
                }
            } header: {
                Text("Computer")
            } footer: {
                if level == "host" {
                    Text("Where it runs and its sessions start: the absolute path of a folder that exists on the "
                         + "runner's machine, like /Users/you/Code/project. Empty: a folder of its own, under the "
                         + "runner's data.")
                } else {
                    Text("Where it runs and what it can reach.")
                }
            }
            if level == "container" {
                internet(s)
                folders(s)
            }
            if level != "host" {
                Section("Image") {
                    DraftField(title: "Image", prompt: "the runner's default", path: "computer.image", mono: true, settings: s)
                    FieldError(text: s.errors["computer.image"])
                }
            }
        }
    }

    private func pick(_ id: String, from level: String) {
        guard id != level, var d = settings.draft else { return }
        if id == "none", !d.mounts.isEmpty {
            keptMounts = d.mounts
            d.mounts = []
        } else if level == "none", let kept = keptMounts, d.mounts.isEmpty {
            d.mounts = kept
            keptMounts = nil
        }
        d.setString("computer.level", id)
        settings.draft = d
        settings.changed("computer.level")
        Task { await settings.loadTools() }
    }

    @ViewBuilder private func internet(_ s: AgentSettings) -> some View {
        let network = s.draft?.network ?? "open"
        let nets = (model.catalog?.networks ?? []).map { ChoiceList.Item(id: $0.id, name: $0.name, summary: $0.summary) }
        Section {
            ChoiceList(items: nets, selection: network, field: "network") { id in
                guard id != network else { return }
                s.draft?.setString("computer.network", id)
                s.changed("computer.network")
            }
            FieldError(text: s.errors["computer.network"])
            if network == "allowlist" {
                let sites = s.draft?.allowlist ?? []
                if sites.isEmpty {
                    Text("None yet: it reaches only its model and Valet's tools.").foregroundStyle(.secondary)
                }
                ForEach(Array(sites.enumerated()), id: \.offset) { i, site in
                    HStack {
                        Text(site).font(.body.monospaced())
                        Spacer()
                        Button(role: .destructive) {
                            var list = sites
                            list.remove(at: i)
                            s.draft?.setStrings("computer.allowlist", list)
                            s.changed("computer.allowlist")
                        } label: { Image(systemName: "minus.circle.fill") }
                            .buttonStyle(.borderless).foregroundStyle(.red)
                            .accessibilityLabel("Remove \(site)")
                    }
                    FieldError(text: s.errors["computer.allowlist.\(i)"])
                }
                HStack {
                    TextField("Add a site", text: $newSite, prompt: Text(verbatim: "github.com"))
                        .font(.body.monospaced())
                        .autocorrectionDisabled()
                        #if os(iOS)
                        .textInputAutocapitalization(.never)
                        .keyboardType(.URL)
                        #endif
                        .onSubmit { addSite(s) }
                        .accessibilityIdentifier("field-site-new")
                    Button("Add") { addSite(s) }.disabled(siteOf(newSite).isEmpty)
                }
                FieldError(text: s.errors["computer.allowlist"])
            }
        } header: {
            Text("Internet")
        } footer: {
            if network == "allowlist" { Text("A site includes its subdomains: github.com covers api.github.com.") }
        }
    }

    private func addSite(_ s: AgentSettings) {
        let site = siteOf(newSite)
        guard !site.isEmpty else { return }
        var list = s.draft?.allowlist ?? []
        if !list.contains(site) { list.append(site) }
        s.draft?.setStrings("computer.allowlist", list)
        s.changed("computer.allowlist")
        newSite = ""
    }

    @ViewBuilder private func folders(_ s: AgentSettings) -> some View {
        let mounts = s.draft?.mounts ?? []
        Section {
            if mounts.isEmpty { Text("None. Its computer only sees its own files.").foregroundStyle(.secondary) }
            ForEach(Array(mounts.indices), id: \.self) { i in
                VStack(alignment: .leading, spacing: 6) {
                    TextField("Folder on the runner's machine", text: mountBinding(s, i, \.host),
                              prompt: Text(verbatim: "/Users/you/notes"))
                        .font(.body.monospaced())
                        .autocorrectionDisabled()
                        #if os(iOS)
                        .textInputAutocapitalization(.never)
                        #endif
                    FieldError(text: s.errors["computer.mounts.\(i).host"])
                    TextField("Shows up inside at", text: mountBinding(s, i, \.path), prompt: Text("/work/notes"))
                        .font(.body.monospaced())
                        .autocorrectionDisabled()
                        #if os(iOS)
                        .textInputAutocapitalization(.never)
                        #endif
                    FieldError(text: s.errors["computer.mounts.\(i).path"])
                    HStack {
                        Toggle("Read-only", isOn: Binding(get: { (s.draft?.mounts ?? [])[safe: i]?.readonly ?? false },
                                                          set: { v in edit(s, i) { $0.readonly = v } }))
                        Spacer()
                        Button("Remove", role: .destructive) {
                            var list = s.draft?.mounts ?? []
                            if list.indices.contains(i) { list.remove(at: i) }
                            s.draft?.mounts = list
                            s.changed("computer.mounts")
                        }
                        .buttonStyle(.borderless).tint(.red)
                    }
                    FieldError(text: s.errors["computer.mounts.\(i)"])
                }
            }
            Button("Add a folder") {
                var list = s.draft?.mounts ?? []
                list.append(DefinitionDraft.Mount())
                s.draft?.mounts = list
                s.changed("computer.mounts")
            }
            FieldError(text: s.errors["computer.mounts"])
        } header: {
            Text("Shared folders")
        }
    }

    private func mountBinding(_ s: AgentSettings, _ i: Int, _ key: WritableKeyPath<DefinitionDraft.Mount, String>) -> Binding<String> {
        Binding(get: { (s.draft?.mounts ?? [])[safe: i]?[keyPath: key] ?? "" }, set: { v in
            edit(s, i) { m in
                m[keyPath: key] = v
                // Where it shows up inside follows the folder's name until it's typed.
                if key == \.host, m.path.isEmpty || m.path == Self.suggested(for: (s.draft?.mounts ?? [])[safe: i]?.host ?? "") {
                    m.path = Self.suggested(for: v)
                }
            }
        })
    }

    static func suggested(for host: String) -> String {
        let last = host.trimmingCharacters(in: CharacterSet(charactersIn: "/ ")).split(separator: "/").last
        return last.map { "/work/\($0)" } ?? ""
    }

    private func edit(_ s: AgentSettings, _ i: Int, _ change: (inout DefinitionDraft.Mount) -> Void) {
        var list = s.draft?.mounts ?? []
        guard list.indices.contains(i) else { return }
        change(&list[i])
        s.draft?.mounts = list
        s.changed("computer.mounts.\(i)")
    }
}

extension Array {
    subscript(safe i: Int) -> Element? { indices.contains(i) ? self[i] : nil }
}

private struct PermissionsSection: View {
    @Bindable var settings: AgentSettings
    @Environment(AppModel.self) private var model

    var body: some View {
        let s = settings
        let current = s.draft?.permissions ?? "ask"
        let host = s.draft?.level == "host"
        Section {
            ChoiceList(items: (model.catalog?.permissions ?? []).map {
                ChoiceList.Item(id: $0.id, name: $0.name, summary: $0.summary, warning: host ? $0.host : nil)
            }, selection: current, field: "permissions") { id in
                guard id != current else { return }
                s.draft?.setString("permissions", id)
                s.changed("permissions")
                Task { await s.loadTools() }
            }
            FieldError(text: s.errors["permissions"])
        } header: {
            Text("Permissions")
        } footer: {
            Text("What it may do without asking you first.")
        }
    }
}

// MARK: tools

private struct ToolsSection: View {
    @Bindable var settings: AgentSettings
    @Environment(AppModel.self) private var model

    var body: some View {
        let s = settings
        let mcp = s.draft?.mcp ?? []
        let savedMcp = s.saved?.mcp ?? []
        let known = (s.servers?.servers ?? []).filter { $0.comesWith == nil }
        let names = known.map(\.name) + mcp.filter { n in !known.contains { $0.name == n } }
        let rules = s.draft?.toolRules ?? [:]
        let levelName = model.catalog?.permissions.first { $0.id == s.draft?.permissions }?.name ?? s.draft?.permissions ?? ""
        Section {
            if let e = s.servers?.error {
                NoteBox(text: "The servers file has an error, so its servers can't be used. \(e)",
                        systemImage: "exclamationmark.triangle")
            }
            if let e = s.serversError { ErrorText(text: e) }
            if s.servers == nil && s.serversError == nil { ProgressView() }
            if s.servers != nil && names.isEmpty { Text("No tool servers yet.").foregroundStyle(.secondary) }
            if mcp.contains(where: savedMcp.contains), s.draft?.permissions != s.saved?.permissions {
                Text("What each tool does below is for \(levelName), which isn't saved yet.")
                    .font(.caption).foregroundStyle(.orange)
            }
            ForEach(names, id: \.self) { n in
                serverRow(n, server: known.first { $0.name == n }, on: mcp.contains(n), savedOn: savedMcp.contains(n),
                          rules: rules[n] ?? [:])
            }
            otherRules(names: names, rules: rules, mcp: mcp)
            if !s.toolGrants.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Allowed for an hour").font(.subheadline.weight(.semibold))
                    ForEach(s.toolGrants) { g in
                        HStack {
                            Text("\(g.server) › \(g.tool)").font(.callout.monospaced()).lineLimit(1)
                            Spacer()
                            Text("until \(Date(timeIntervalSince1970: g.expiresAt).formatted(date: .omitted, time: .shortened))")
                                .font(.caption).foregroundStyle(.secondary)
                            Button("Revoke") { Task { await s.revoke(g) } }.buttonStyle(.bordered).controlSize(.small)
                        }
                    }
                }
            }
            FieldError(text: s.errors["tools.mcp"])
            FieldError(text: s.errors["tools.rules"])
            FieldError(text: s.errors["tools"])
        } header: {
            Text("Tools")
        } footer: {
            Text("Tool servers it can use, like GitHub. Valet connects them for it and checks every call against its "
                 + "permissions, and against your rule for the tool if you set one.")
        }
    }

    @ViewBuilder private func serverRow(_ n: String, server: GatewayServer?, on: Bool, savedOn: Bool,
                                        rules: [String: String]) -> some View {
        let s = settings
        VStack(alignment: .leading, spacing: 6) {
            Toggle(isOn: Binding(get: { on }, set: { v in
                s.draft?.toggle("tools.mcp", n, on: v, savedOrder: s.saved?.mcp ?? [])
                s.changed("tools.mcp")
            })) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(n)
                    if let d = server?.description, !d.isEmpty {
                        Text(brief(d, 160)).font(.caption).foregroundStyle(.secondary)
                    } else if server == nil && s.servers != nil {
                        Text("Valet has no tool server by this name.").font(.caption).foregroundStyle(.orange)
                    }
                }
            }
            .accessibilityIdentifier("mcp:\(n)")
            if on { serverTools(n, savedOn: savedOn, rules: rules) }
        }
    }

    @ViewBuilder private func serverTools(_ n: String, savedOn: Bool, rules: [String: String]) -> some View {
        let s = settings
        if !savedOn {
            Text("Save to see its tools.").font(.caption).foregroundStyle(.secondary)
        } else if let e = s.toolsError {
            ErrorText(text: e)
        } else if let t = s.tools?.servers.first(where: { $0.server == n }) {
            if !t.ok {
                Text("Can't use it right now: \(t.error ?? "unreachable")").font(.caption).foregroundStyle(.orange)
            } else if t.tools.isEmpty {
                Text("It offers no tools.").font(.caption).foregroundStyle(.secondary)
            } else {
                let fates = t.tools.map { ToolFate.decide($0, rules: rules, grant: s.grant(n, $0.tool) != nil) }
                let parts = [("allow", "run"), ("ask", "ask you"), ("deny", "blocked")].compactMap { d, words in
                    let k = fates.filter { $0.decision == d }.count
                    return k > 0 ? "\(k) \(words)" : nil
                }
                DisclosureGroup("\(t.tools.count) tool\(t.tools.count == 1 ? "" : "s"): \(parts.joined(separator: ", "))") {
                    VStack(alignment: .leading, spacing: 10) {
                        VStack(alignment: .leading, spacing: 2) {
                            Text("All its tools").font(.callout.weight(.medium))
                            Text("A rule here covers each tool below that has none of its own.")
                                .font(.caption).foregroundStyle(.secondary)
                            rulePicker(n, "*", fallback: "As each one's level says", label: "All of \(n)")
                        }
                        ForEach(Array(t.tools.enumerated()), id: \.element.id) { i, x in
                            toolRow(n, x, rules: rules, fate: fates[i])
                        }
                    }
                    .padding(.top, 4)
                }
                .font(.callout)
                .opacity(s.tools?.permissions != s.draft?.permissions ? 0.5 : 1)
            }
        } else {
            Text("Loading its tools…").font(.caption).foregroundStyle(.secondary)
        }
    }

    private func toolRow(_ n: String, _ x: AgentTools.Tool, rules: [String: String],
                         fate: (decision: String, source: String)) -> some View {
        let s = settings
        let all = rules["*"].flatMap { ToolFate.rules.contains($0) ? $0 : nil }
        let level = x.levelDecision ?? (x.source == "level" || x.source.isEmpty ? x.decision : nil)
        let fallback = all.map { "Same as all its tools (\(ToolFate.words($0)))" }
            ?? "As its level says" + (level.map { " (\(ToolFate.words($0)))" } ?? "")
        return VStack(alignment: .leading, spacing: 3) {
            HStack {
                Text(x.label).font(.callout.weight(.medium)).lineLimit(1)
                Spacer()
                Chip(text: ToolFate.words(fate.decision), color: fate.decision == "allow" ? .green
                     : fate.decision == "deny" ? .red : .orange)
            }
            if let d = x.description, !d.isEmpty { Text(brief(d, 160)).font(.caption).foregroundStyle(.secondary) }
            if fate.source == "grant", let g = s.grant(n, x.tool) {
                Text("Allowed for an hour, until \(Date(timeIntervalSince1970: g.expiresAt).formatted(date: .omitted, time: .shortened))")
                    .font(.caption).foregroundStyle(.secondary)
            }
            rulePicker(n, x.tool, fallback: fallback, label: "Rule for \(x.label)")
        }
    }

    private func rulePicker(_ server: String, _ tool: String, fallback: String, label: String) -> some View {
        let s = settings
        return Picker(label, selection: Binding(get: { s.draft?.toolRule(server, tool) ?? "" }, set: { v in
            s.draft?.setToolRule(server, tool, v.isEmpty ? nil : v)
            s.changed("tools.rules")
        })) {
            Text(fallback).tag("")
            ForEach(ToolFate.rules, id: \.self) { r in Text(ToolFate.choice(r)).tag(r) }
        }
        .pickerStyle(.menu)
        .labelsHidden()
        .accessibilityIdentifier("rule:\(server):\(tool)")
    }

    /// Rules the tool lists don't show (a server it no longer has, one that can't be reached, a tool its server no
    /// longer offers): they still apply, and each can be removed.
    @ViewBuilder private func otherRules(names: [String], rules: [String: [String: String]], mcp: [String]) -> some View {
        let s = settings
        let shown: Set<String> = Set(names.filter(mcp.contains).flatMap { n -> [String] in
            guard s.saved?.mcp.contains(n) == true, let t = s.tools?.servers.first(where: { $0.server == n }), t.ok else {
                return []
            }
            return [n + "/*"] + t.tools.map { n + "/" + $0.tool }
        })
        let rest = rules.keys.sorted().flatMap { server in
            (rules[server] ?? [:]).keys.sorted().compactMap { tool in
                shown.contains(server + "/" + tool) ? nil : OtherRule(server: server, tool: tool, rule: rules[server]?[tool] ?? "")
            }
        }
        if !rest.isEmpty {
            VStack(alignment: .leading, spacing: 6) {
                Text("Other rules").font(.subheadline.weight(.semibold))
                Text("For tools not listed above. They apply whenever the tool is there.")
                    .font(.caption).foregroundStyle(.secondary)
                ForEach(rest, id: \.self) { r in
                    let (server, tool, rule) = (r.server, r.tool, r.rule)
                    HStack {
                        Text("\(server) › \(tool == "*" ? "all its tools" : tool)").font(.callout.monospaced())
                        Spacer()
                        Chip(text: ToolFate.words(rule), color: rule == "allow" ? .green : rule == "deny" ? .red : .orange)
                        Button("Remove") {
                            s.draft?.setToolRule(server, tool, nil)
                            s.changed("tools.rules")
                        }
                        .buttonStyle(.bordered).controlSize(.small)
                    }
                }
            }
        }
    }
}

private struct OtherRule: Hashable {
    let server: String
    let tool: String
    let rule: String
}

// MARK: secrets

private struct SecretsSection: View {
    @Bindable var settings: AgentSettings
    let actions: AgentActions
    @State private var newName = ""
    @State private var newError: String?

    var body: some View {
        let s = settings
        let list = s.draft?.secrets ?? []
        let names = list.compactMap(\.string)
        let have = (s.secrets?.values.filter { $0.set && !names.contains($0.name) } ?? []).sorted { $0.name < $1.name }
        Section {
            if list.isEmpty { Text("None.").foregroundStyle(.secondary) }
            ForEach(Array(list.enumerated()), id: \.offset) { i, entry in
                if let n = entry.string { secretRow(n, at: i) } else { vaultRow(entry) }
            }
            FieldError(text: s.errors["secrets"])
            HStack {
                TextField("Add a secret", text: $newName, prompt: Text("GITHUB_TOKEN"))
                    .font(.body.monospaced())
                    .autocorrectionDisabled()
                    #if os(iOS)
                    .textInputAutocapitalization(.characters)
                    #endif
                    .onSubmit { add(Secret.name(from: newName), typed: true) }
                    .accessibilityIdentifier("field-secret-new")
                Button("Add") { add(Secret.name(from: newName), typed: true) }
                    .disabled(newName.trimmingCharacters(in: .whitespaces).isEmpty)
                if !have.isEmpty {
                    Menu {
                        ForEach(have) { x in Button(x.name) { add(x.name, typed: false) } }
                    } label: { Image(systemName: "key.horizontal") }
                        .fixedSize()
                        .accessibilityLabel("Add one you have")
                }
            }
            FieldError(text: newError)
        } header: {
            Text("Secrets")
        } footer: {
            Text("Keys and tokens it gets as environment variables, like GITHUB_TOKEN. A value is stored encrypted and "
                 + "never shown again.")
        }
    }

    private func secretRow(_ n: String, at i: Int) -> some View {
        let s = settings
        let st = s.secretStatus(n)
        let info = s.secrets?[n]
        let others = (info?.usedBy.filter { $0 != s.name } ?? []) + (info?.servers.map { "the \($0) server" } ?? [])
        let sub = [st == false ? "Not set: it can't start until it is"
                   : info?.updatedAt.map { "Set " + Date(timeIntervalSince1970: $0).ago },
                   others.isEmpty ? nil : "also used by " + others.joined(separator: ", ")]
            .compactMap { $0 }.joined(separator: " · ")
        return VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(n).font(.body.monospaced())
                Spacer()
                if st == true { Chip(text: "set", color: .green) } else if st == false { Chip(text: "not set", color: .orange) }
            }
            if !sub.isEmpty { Text(sub).font(.caption).foregroundStyle(.secondary) }
            HStack {
                Button(st == false ? "Set value" : "Change value") { actions.offer([n], for: s.name) }
                    .buttonStyle(.bordered).controlSize(.small)
                Button("Remove", role: .destructive) {
                    var list = s.draft?.secrets ?? []
                    if list.indices.contains(i) { list.remove(at: i) }
                    s.draft?.secrets = list
                    s.changed("secrets")
                }
                .buttonStyle(.bordered).controlSize(.small).tint(.red)
            }
        }
        .accessibilityIdentifier("secret-row-\(n)")
    }

    private func vaultRow(_ entry: JSONValue) -> some View {
        let vault = entry["vault"]?.string ?? "?"
        let env = entry["env"]?.string
        return VStack(alignment: .leading, spacing: 2) {
            HStack {
                Text(env ?? vault).font(.body.monospaced())
                Spacer()
                Chip(text: "vault", color: .purple)
            }
            Text("The password of the vault login \(vault), set in the file.").font(.caption).foregroundStyle(.secondary)
        }
    }

    private func add(_ n: String, typed: Bool) {
        let s = settings
        newError = nil
        guard !n.isEmpty else { newError = "Type its name, like GITHUB_TOKEN."; return }
        guard Secret.isValidName(n) else {
            newError = "A secret's name is capital letters, digits and underscores, starting with a letter."
            return
        }
        let list = s.draft?.secrets ?? []
        guard !list.contains(where: { $0.string == n || $0["env"]?.string == n }) else {
            newError = "It already has \(n)."
            return
        }
        s.draft?.secrets = list + [.string(n)]
        s.changed("secrets")
        if typed { newName = "" }
        if s.secretStatus(n) == false { actions.offer([n], for: s.name) }
    }
}

// MARK: browser and memory

private struct BrowserSection: View {
    @Bindable var settings: AgentSettings

    var body: some View {
        let s = settings
        let listed = s.draft?.browserProfiles ?? []
        let known = s.profiles ?? []
        let names = known.map(\.name) + listed.filter { n in !known.contains { $0.name == n } }
        Section {
            if let e = s.profilesError { NoteBox(text: e, systemImage: "exclamationmark.triangle") }
            if s.profiles == nil && s.profilesError == nil { ProgressView() }
            if s.profiles != nil && names.isEmpty {
                Text("No browser profiles yet: add one in Settings › Browser.").foregroundStyle(.secondary)
            }
            ForEach(names, id: \.self) { n in
                let p = known.first { $0.name == n }
                Toggle(isOn: Binding(get: { listed.contains(n) }, set: { v in
                    s.draft?.toggle("browser.profiles", n, on: v, savedOrder: s.saved?.browserProfiles ?? [])
                    s.changed("browser.profiles")
                })) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(p?.displayTitle ?? n)
                        if let p, p.displayTitle != n { Text(n).font(.caption.monospaced()).foregroundStyle(.secondary) }
                        if let note = p?.note, !note.isEmpty { Text(brief(note, 160)).font(.caption).foregroundStyle(.secondary) }
                        if p == nil && s.profiles != nil {
                            Text("There's no such profile.").font(.caption).foregroundStyle(.orange)
                        }
                    }
                }
                .accessibilityIdentifier("profile:\(n)")
            }
            FieldError(text: s.errors["browser.profiles"])
            if !listed.isEmpty, s.draft?.mcp.contains("valet.browser") == false {
                NoteBox(text: "It needs the valet.browser tool server to use them.", systemImage: "wrench.and.screwdriver") {
                    Button("Turn it on") {
                        s.draft?.toggle("tools.mcp", "valet.browser", on: true, savedOrder: s.saved?.mcp ?? [])
                        s.changed("tools.mcp")
                    }
                    .buttonStyle(.bordered).controlSize(.small)
                }
            }
        } header: {
            Text("Browser")
        } footer: {
            Text("Chrome profiles it can use, with their logins. Its own site rules below beat the profile's.")
        }
        Section {
            SiteRulesEditor(rules: Binding(get: { s.draft?.siteRules ?? [:] }, set: { s.draft?.siteRules = $0 }),
                            error: s.errors["browser.rules"], errorFor: { s.errors["browser.rules.\($0)"] },
                            onChange: { s.changed("browser.rules") })
            if !s.browserGrants.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Allowed for an hour").font(.subheadline.weight(.semibold))
                    ForEach(s.browserGrants) { g in
                        HStack {
                            Text("\(BrowserKind(rawValue: g.kind).label) on \(g.host)").font(.callout).lineLimit(1)
                            Spacer()
                            Text("until \(Date(timeIntervalSince1970: g.expiresAt).formatted(date: .omitted, time: .shortened))")
                                .font(.caption).foregroundStyle(.secondary)
                            Button("Revoke") { Task { await s.revoke(g) } }.buttonStyle(.bordered).controlSize(.small)
                        }
                    }
                }
            }
        } header: {
            Text("Its site rules")
        } footer: {
            Text("For each kind of action on a site: allow, ask you, or deny. A kind left unset falls to the profile's "
                 + "rules, then its permissions.")
        }
    }
}

private struct MemorySection: View {
    @Bindable var settings: AgentSettings

    var body: some View {
        let s = settings
        let on = s.draft?.memory ?? false
        let layer = s.draft?.directives.contains("memory") ?? false
        Section {
            Toggle(isOn: Binding(get: { on }, set: { v in
                s.draft?.setMemory(v)
                s.changed("memory")
            })) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Memory")
                    Text(!on ? "Off: no memory tools, and nothing about memory in what it's told."
                         : layer ? "On: it reads and writes its files with its memory tools, never waiting for your "
                            + "approval, and every session starts with them in what it's told."
                         : "On, but what it's told at the start leaves them out: it has to look them up.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            .accessibilityIdentifier("field-memory")
            if on && !layer {
                NoteBox(text: "Its directives leave the memory layer out.") {
                    Button("Tell it") {
                        s.draft?.setDirective("memory", on: true)
                        s.changed("directives")
                    }
                    .buttonStyle(.bordered).controlSize(.small)
                }
            }
            FieldError(text: s.errors["memory"])
            FieldError(text: s.errors["directives"])
            NavigationLink(value: MemoryRef(s.name)) { Label("Its memory files", systemImage: "brain") }
        } header: {
            Text("Memory")
        } footer: {
            Text("Notes it keeps for itself across sessions and restarts, as Markdown files, one per subject.")
        }
    }
}

// MARK: files and history

/// agent.yaml and persona.md as text, and the history of both with Restore.
struct AgentFilesView: View {
    let settings: AgentSettings
    @Environment(AppModel.self) private var model
    @State private var history: [HistoryEntry]?
    @State private var error: String?
    @State private var restoring: HistoryEntry?

    var body: some View {
        let s = settings
        List {
            Section {
                Text("The settings live in these two files. Saving the form rewrites agent.yaml, and saving a file here "
                     + "replaces what the form shows: save one before you edit the other.")
                    .font(.callout).foregroundStyle(.secondary)
                ForEach(["agent.yaml", "persona.md"], id: \.self) { f in
                    NavigationLink {
                        RawFileEditor(title: f, load: {
                            let d = try await model.api.agentDetail(s.name)
                            return d.files[f] ?? ""
                        }, save: { text in
                            try await model.api.saveAgentFile(s.name, file: f, text: text)
                            await s.reload()
                            await loadHistory()
                        })
                    } label: {
                        Label(f, systemImage: f.hasSuffix(".yaml") ? "curlybraces" : "text.quote")
                    }
                    .accessibilityIdentifier("file-\(f)")
                }
            }
            Section("History") {
                if let error { ErrorText(text: error) }
                if let history {
                    if history.isEmpty { Text("No changes yet.").foregroundStyle(.secondary) }
                    ForEach(Array(history.enumerated()), id: \.element.id) { i, c in
                        HStack {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(c.message).lineLimit(2)
                                Text("\(Date(timeIntervalSince1970: c.at).ago) · \(c.short)")
                                    .font(.caption.monospaced()).foregroundStyle(.secondary)
                            }
                            Spacer()
                            if i == 0 {
                                Chip(text: "current", color: .green)
                            } else {
                                Button("Restore") { restoring = c }.buttonStyle(.bordered).controlSize(.small)
                            }
                        }
                    }
                } else if error == nil {
                    ProgressView()
                }
            }
        }
        .navigationTitle("Files and history")
        #if os(iOS)
        .navigationBarTitleDisplayMode(.inline)
        #endif
        .task { await loadHistory() }
        .refreshable { await loadHistory() }
        .confirmationDialog(s.isDirty ? "Restore this version? Your unsaved changes are discarded."
                            : "Restore this version? What you have now stays in the history.",
                            isPresented: Binding(get: { restoring != nil }, set: { if !$0 { restoring = nil } }),
                            titleVisibility: .visible) {
            Button("Restore", role: .destructive) {
                if let c = restoring {
                    Task {
                        do {
                            try await s.restore(c)
                            await loadHistory()
                        } catch {
                            self.error = error.localizedDescription
                        }
                    }
                }
            }
        }
    }

    private func loadHistory() async {
        do {
            history = try await model.api.agentHistory(settings.name)
            error = nil
        } catch is CancellationError {
        } catch {
            self.error = error.localizedDescription
        }
    }
}
