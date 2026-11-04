import SwiftUI

// What agents are told besides their personality (app.js's DirectivesView and the settings form's "What it
// knows"): owner.md, about the owner, and the standing-authority presets, shared by every agent that lists them.

/// Settings › About you & authority (navigation value).
public struct DirectivesRef: Hashable, Sendable { public init() {} }

/// A preset's screen (navigation value).
struct PresetRef: Hashable, Sendable { let name: String }

/// The text an empty owner.md starts from.
let ownerPlaceholder = """
    # About the owner

    - Where you live, your time zone, the languages you use
    - What you do, and what you're working on
    - How you like to work, and how you like answers
    - Your machines and accounts
    """

/// What a saved layer reaches: the running agents need a restart, the others read it when they next start.
@MainActor func appliesNote(_ used: [String], _ model: AppModel) -> String {
    guard !used.isEmpty else { return "No agent includes it yet." }
    let up = used.filter { model.agents[$0]?.isUp == true }
    if !up.isEmpty {
        return "\(andList(up.map(model.title))) \(up.count > 1 ? "need" : "needs") a restart to use it."
    }
    let many = used.count > 1
    return "\(andList(used.map(model.title))) \(many ? "read" : "reads") it at \(many ? "their" : "its") next start."
}

/// Lowercase letters, digits and dashes, at most 31: a preset's name from what was typed (app.js's slugify).
func slugify(_ text: String) -> String {
    let s = text.lowercased().replacingOccurrences(of: #"[^a-z0-9]+"#, with: "-", options: .regularExpression)
        .trimmingCharacters(in: CharacterSet(charactersIn: "-"))
    return String(s.prefix(31)).trimmingCharacters(in: CharacterSet(charactersIn: "-"))
}

/// The owner profile (owner.md) with its Save, then the presets, each opening its own editor, and New preset.
public struct DirectivesView: View {
    @Environment(AppModel.self) private var model
    @State private var owner: OwnerProfile?
    @State private var ownerError: String?
    @State private var text = ""
    @State private var savedText: String?
    @State private var saving = false
    @State private var message: String?
    @State private var saveError: String?
    @State private var presets: [AuthorityPreset]?
    @State private var presetsError: String?
    @State private var naming = false
    @State private var newName = ""
    @State private var newError: String?

    public init() {}

    public var body: some View {
        Form {
            Section {
                if let e = ownerError { ErrorText(text: e) }
                if owner == nil && ownerError == nil { ProgressView() }
                if owner != nil {
                    ZStack(alignment: .topLeading) {
                        if text.isEmpty {
                            Text(ownerPlaceholder).foregroundStyle(.tertiary).padding(.top, 8).padding(.leading, 5)
                                .allowsHitTesting(false)
                        }
                        TextEditor(text: $text)
                            .frame(minHeight: 200)
                            .scrollContentBackground(.hidden)
                            .accessibilityIdentifier("field-owner")
                    }
                    if let saveError { ErrorText(text: saveError) }
                    if let message { Label(message, systemImage: "checkmark.circle.fill").font(.callout).foregroundStyle(.green) }
                    HStack {
                        let used = owner?.usedBy ?? []
                        Text(used.isEmpty ? "No agent includes it yet" : "Included by " + andList(used.map(model.title)))
                            .font(.caption).foregroundStyle(.secondary)
                        Spacer()
                        Button { Task { await saveOwner() } } label: {
                            if saving { ProgressView().controlSize(.small) } else { Text("Save") }
                        }
                        .buttonStyle(.borderedProminent)
                        .controlSize(.small)
                        .disabled(saving || text == savedText)
                        .accessibilityIdentifier("owner-save")
                    }
                }
            } header: {
                Text("About you")
            } footer: {
                Text("Who you are and how you work. Agents that include it read it at the start of every session, so "
                     + "they don't need to ask. Turn it on in an agent's settings, under What it knows.")
            }

            Section {
                if let e = presetsError { ErrorText(text: e) }
                if presets == nil && presetsError == nil { ProgressView() }
                if let p = presets, p.isEmpty { Text("No presets.").foregroundStyle(.secondary) }
                ForEach(sortedPresets) { p in
                    NavigationLink(value: PresetRef(name: p.name)) {
                        VStack(alignment: .leading, spacing: 3) {
                            HStack {
                                Text(p.label).font(.body.weight(.medium))
                                Spacer()
                                Text("authority/\(p.name)").font(.caption.monospaced()).foregroundStyle(.secondary)
                                    .lineLimit(1)
                            }
                            let g = gist(p.text)
                            if !g.isEmpty { Text(g).font(.caption).foregroundStyle(.secondary).lineLimit(2) }
                            Text(p.usedBy.isEmpty ? "No agent uses it" : "Used by " + andList(p.usedBy.map(model.title)))
                                .font(.caption).foregroundStyle(.secondary)
                        }
                    }
                    .accessibilityIdentifier("preset-\(p.name)")
                }
            } header: {
                HStack {
                    Text("Standing authority")
                    Spacer()
                    Button { newName = ""; newError = nil; naming = true } label: { Label("New preset", systemImage: "plus") }
                        .font(.subheadline)
                        .textCase(nil)
                        .accessibilityIdentifier("preset-new")
                }
            } footer: {
                Text("What an agent may do for you without asking first. Each agent uses one preset (or none), picked "
                     + "in its settings. Valet still enforces its permission level: this tells it what you want, so it "
                     + "acts instead of hesitating.")
            }
        }
        .formStyle(.grouped)
        .readableWidth()
        .navigationTitle("About you & authority")
        #if os(iOS)
        .navigationBarTitleDisplayMode(.inline)
        #endif
        .task(id: model.hellos) { await load() }
        .onAppear { if presets != nil { Task { await loadPresets() } } }
        .refreshable { await load() }
        .onChange(of: text) { message = nil }
        .alert("New preset", isPresented: $naming) {
            TextField("Name, like GitHub only", text: $newName)
            Button("Create") { Task { await create() } }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text(newError ?? "Lowercase letters, digits and dashes.")
        }
    }

    private var sortedPresets: [AuthorityPreset] {
        let list = presets ?? []
        return AuthorityPreset.sorted(list.map(\.name)).compactMap { n in list.first { $0.name == n } }
    }

    private func load() async {
        async let o: Void = loadOwner()
        async let p: Void = loadPresets()
        _ = await (o, p)
    }

    private func loadOwner() async {
        do {
            let o = try await model.api.owner()
            owner = o
            // Never over what's being typed.
            if savedText == nil || text == savedText { text = o.text }
            savedText = o.text
            ownerError = nil
        } catch is CancellationError {
        } catch {
            ownerError = error.localizedDescription
        }
    }

    private func loadPresets() async {
        do {
            presets = try await model.api.authority()
            presetsError = nil
        } catch is CancellationError {
        } catch {
            presetsError = error.localizedDescription
        }
    }

    private func saveOwner() async {
        saving = true
        saveError = nil
        defer { saving = false }
        do {
            let o = try await model.api.saveOwner(text)
            owner = o
            savedText = text
            message = "Saved. " + appliesNote(o.usedBy, model)
            endEditing()
        } catch {
            saveError = error.localizedDescription
        }
    }

    private func create() async {
        let name = slugify(newName)
        func fail(_ m: String) { newError = m; naming = true }
        guard !name.isEmpty else { return fail("Give it a name.") }
        guard name.range(of: #"^[a-z0-9][a-z0-9-]{0,30}$"#, options: .regularExpression) != nil else {
            return fail("Use letters, digits and dashes.")
        }
        if presets?.contains(where: { $0.name == name }) == true { return fail("There's already a preset called \(name).") }
        do {
            try await model.api.savePreset(name, text: "# Standing authority: \(AuthorityPreset.title(name))\n\n")
            await loadPresets()
        } catch {
            fail(error.localizedDescription)
        }
    }
}

/// One preset: its text, Save, and Delete while no agent uses it.
struct PresetView: View {
    let name: String
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var preset: AuthorityPreset?
    @State private var loadError: String?
    @State private var text = ""
    @State private var savedText: String?
    @State private var saving = false
    @State private var message: String?
    @State private var error: String?
    @State private var confirmDelete = false

    var body: some View {
        Form {
            if let e = loadError { Section { ErrorText(text: e) } }
            if preset == nil && loadError == nil { Section { ProgressView() } }
            if let p = preset {
                Section {
                    TextEditor(text: $text)
                        .font(.body)
                        .frame(minHeight: 280)
                        .scrollContentBackground(.hidden)
                        .accessibilityIdentifier("field-preset")
                    if let error { ErrorText(text: error) }
                    if let message { Label(message, systemImage: "checkmark.circle.fill").font(.callout).foregroundStyle(.green) }
                } header: {
                    Text("authority/\(p.name)").textCase(nil).font(.caption.monospaced())
                } footer: {
                    Text(p.usedBy.isEmpty ? "No agent uses it." : "Used by " + andList(p.usedBy.map(model.title)) + ".")
                }
                if p.usedBy.isEmpty {
                    Section {
                        Button("Delete the preset", role: .destructive) { confirmDelete = true }
                            .accessibilityIdentifier("preset-delete")
                    }
                }
            }
        }
        .formStyle(.grouped)
        .readableWidth()
        .navigationTitle(preset?.label ?? AuthorityPreset.title(name))
        #if os(iOS)
        .navigationBarTitleDisplayMode(.inline)
        #endif
        .toolbar {
            ToolbarItem(placement: .confirmationAction) {
                Button { Task { await save() } } label: {
                    if saving { ProgressView().controlSize(.small) } else { Text("Save") }
                }
                .disabled(saving || preset == nil || text == savedText)
                .accessibilityIdentifier("preset-save")
            }
        }
        .task { await load() }
        .onChange(of: text) { message = nil }
        .confirmationDialog("Delete \(preset?.label ?? name)? It stays in the definitions' history.",
                            isPresented: $confirmDelete, titleVisibility: .visible) {
            Button("Delete", role: .destructive) { Task { await delete() } }
        }
    }

    private func load() async {
        do {
            guard let p = try await model.api.authority().first(where: { $0.name == name }) else {
                loadError = "There's no preset called \(name) any more."
                return
            }
            preset = p
            if savedText == nil || text == savedText { text = p.text }
            savedText = p.text
        } catch is CancellationError {
        } catch {
            loadError = error.localizedDescription
        }
    }

    private func save() async {
        saving = true
        error = nil
        defer { saving = false }
        do {
            let p = try await model.api.savePreset(name, text: text)
            preset = p
            savedText = text
            message = "Saved. " + appliesNote(p.usedBy, model)
            endEditing()
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func delete() async {
        do {
            try await model.api.deletePreset(name)
            dismiss()
        } catch let e as APIError where e.status == 404 {
            dismiss()
        } catch {
            self.error = error.localizedDescription
        }
    }
}

// MARK: the settings form's "What it knows"

/// The layers an agent is told besides its personality (app.js's drawKnows): about you, its standing authority
/// (with the preset suggested for its permission level), its environment; "Tell it" when persona.md is left out;
/// and a preview of the whole text with the unsaved settings.
struct KnowsSection: View {
    @Bindable var settings: AgentSettings
    @Environment(AppModel.self) private var model
    @State private var owner: OwnerProfile?
    @State private var ownerError: String?
    @State private var presets: [AuthorityPreset]?
    @State private var presetsError: String?
    @State private var previewing = false

    var body: some View {
        let s = settings
        let directives = s.draft?.directives ?? []
        let preset = s.draft?.preset
        let level = s.draft?.permissions ?? "ask"
        let suggested = model.catalog?.authority[level]
        let levelName = model.catalog?.permissions.first { $0.id == level }?.name ?? level
        let names = AuthorityPreset.sorted((presets ?? []).map(\.name))
        let missing = preset.flatMap { p in presets != nil && !names.contains(p) ? p : nil }
        let ownerText = owner?.text.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        Section {
            if !directives.contains("persona.md") {
                NoteBox(text: "It isn't told its personality: its agent.yaml leaves persona.md out of its directives.") {
                    Button("Tell it") { edit { $0.setDirective("persona.md", on: true) } }
                        .buttonStyle(.bordered).controlSize(.small)
                        .accessibilityIdentifier("directive-tell-persona")
                }
            }
            layerToggle("owner", "About you",
                        ownerError ?? (owner == nil ? "Loading…" : ownerText.isEmpty
                            ? "You haven't written it yet, so it learns nothing about you from this." : gist(ownerText)))
            NavigationLink(value: DirectivesRef()) {
                Text(owner != nil && ownerText.isEmpty ? "Write about you" : "Edit about you")
            }
            VStack(alignment: .leading, spacing: 4) {
                Text("Standing authority")
                Text("What it may do for you without asking first. Its permission level above still applies: Valet "
                     + "enforces that.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            if let e = presetsError { ErrorText(text: e) }
            if presets == nil && presetsError == nil { ProgressView() }
            if presets != nil {
                ChoiceList(items: [ChoiceList.Item(id: "", name: "None",
                                                   summary: "It acts on what you ask, with its own judgment about the rest.")]
                           + (names + (missing.map { [$0] } ?? [])).map { n in
                               let p = presets?.first { $0.name == n }
                               return ChoiceList.Item(
                                   id: n, name: p?.label ?? AuthorityPreset.title(n),
                                   summary: [p.map { gist($0.text) }, n == suggested ? "Suggested for \(levelName)" : nil]
                                       .compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: "\n"),
                                   warning: n == missing ? "There's no such preset any more: pick another." : nil)
                           },
                           selection: preset ?? "", field: "authority") { id in
                    edit { $0.setPreset(id.isEmpty ? nil : id) }
                }
            }
            FieldError(text: s.errors["directives"])
            NavigationLink(value: DirectivesRef()) { Text("Edit the presets") }
            layerToggle("environment", "Its environment",
                        "What Valet set up for it, written at every start: its computer, permission level, tools and "
                        + "what each call does, its skills, its secrets by name, and how Valet enforces your limits. It "
                        + "knows what's allowed, so it acts instead of hesitating.")
            Button { previewing = true } label: { Label("Preview what it's told", systemImage: "eye") }
                .accessibilityIdentifier("directives-preview")
                // On the row: a sheet hung on a Form's Section never shows.
                .sheet(isPresented: $previewing) { DirectivesPreview(settings: s) }
        } header: {
            Text("What it knows")
        } footer: {
            Text("Besides its personality, what it's told at the start of every session. Valet joins it all in this "
                 + "order: about you, its standing authority, its personality, its environment, its memory.")
        }
        .task(id: model.hellos) { await load() }
    }

    private func layerToggle(_ key: String, _ title: String, _ sub: String) -> some View {
        let on = settings.draft?.directives.contains(key) ?? false
        return Toggle(isOn: Binding(get: { on }, set: { v in edit { $0.setDirective(key, on: v) } })) {
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                Text(sub).font(.caption).foregroundStyle(.secondary)
            }
        }
        .accessibilityIdentifier("directive-\(key)")
    }

    private func edit(_ change: (inout DefinitionDraft) -> Void) {
        guard var d = settings.draft else { return }
        change(&d)
        settings.draft = d
        settings.changed("directives")
    }

    private func load() async {
        async let o = model.api.owner()
        async let p = model.api.authority()
        // A cancelled load (the screen left, a reconnect's new load) says nothing: the next one will.
        do { owner = try await o; ownerError = nil } catch is CancellationError {
        } catch { ownerError = error.localizedDescription }
        do { presets = try await p; presetsError = nil } catch is CancellationError {
        } catch { presetsError = error.localizedDescription }
    }
}

/// What the agent would be told with the settings as they are in the form (POST …/directives/preview): each layer
/// and its state, then the text.
struct DirectivesPreview: View {
    let settings: AgentSettings
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var preview: Directives?
    @State private var error: String?

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    if let error { ErrorText(text: error) }
                    if preview == nil && error == nil { ProgressView().frame(maxWidth: .infinity).padding(.top, 40) }
                    if let p = preview {
                        if let e = p.error { ErrorText(text: e) }
                        FlowChips(items: p.layers.map { ($0.label, $0.state == "ok" ? Color.valet : .gray) })
                        if p.text.isEmpty {
                            Text("Nothing: it gets only its harness's own instructions.").foregroundStyle(.secondary)
                        } else {
                            Text(p.whereText + (settings.isDirty ? " With your unsaved changes, as if it started now."
                                                : " As if it started now."))
                                .font(.callout).foregroundStyle(.secondary)
                            CodeText(text: p.text, font: .system(.footnote, design: .monospaced), wraps: true)
                                .accessibilityIdentifier("directives-text")
                        }
                    }
                }
                .padding()
                .frame(maxWidth: 820)
                .frame(maxWidth: .infinity)
            }
            .background(Color.screen)
            .navigationTitle("What it's told")
            #if os(iOS)
            .navigationBarTitleDisplayMode(.inline)
            #endif
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } }
                ToolbarItem(placement: .automatic) {
                    Button { Task { await load() } } label: { Label("Refresh", systemImage: "arrow.clockwise") }
                }
            }
            .task { await load() }
        }
        #if os(macOS)
        .frame(minWidth: 560, minHeight: 520)
        #endif
    }

    private func load() async {
        guard let d = settings.draft else { return }
        error = nil
        do {
            preview = try await model.api.previewDirectives(settings.name, definition: d, persona: settings.persona)
        } catch {
            self.error = error.localizedDescription
        }
    }
}

/// Small chips that wrap onto more lines.
struct FlowChips: View {
    let items: [(String, Color)]

    var body: some View {
        FlowLayout(spacing: 6) {
            ForEach(Array(items.enumerated()), id: \.offset) { _, it in Chip(text: it.0, color: it.1) }
        }
    }
}

/// Lays its children out in rows, wrapping when a row is full.
struct FlowLayout: Layout {
    var spacing: CGFloat = 6

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let width = proposal.width ?? .infinity
        var x: CGFloat = 0, y: CGFloat = 0, row: CGFloat = 0, widest: CGFloat = 0
        for v in subviews {
            let s = v.sizeThatFits(.unspecified)
            if x > 0 && x + s.width > width { y += row + spacing; x = 0; row = 0 }
            x += s.width + spacing
            row = max(row, s.height)
            widest = max(widest, x - spacing)
        }
        return CGSize(width: proposal.width ?? widest, height: y + row)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var x = bounds.minX, y = bounds.minY, row: CGFloat = 0
        for v in subviews {
            let s = v.sizeThatFits(.unspecified)
            if x > bounds.minX && x + s.width > bounds.maxX { y += row + spacing; x = bounds.minX; row = 0 }
            v.place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(s))
            x += s.width + spacing
            row = max(row, s.height)
        }
    }
}
