import SwiftUI

/// The New agent form (the web UI's New agent card): a name (the ID follows it), an emoji, what it starts from (a
/// template, or a copy of an agent), and its brain. Create & start creates it, starts it and opens its page. The
/// root presents it for `AppModel.newAgent`.
struct NewAgentView: View {
    let request: NewAgentRequest
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var draft = NewAgentDraft()
    @State private var catalog: Catalog?
    @State private var loading = true
    @State private var errors = FieldErrors()
    @State private var busy: String?
    @State private var editingID = false
    @State private var otherModel = false
    @FocusState private var focus: Focus?

    enum Focus: Hashable { case name, id, model }

    private static let otherTag = "\u{1}other"

    var body: some View {
        NavigationStack {
            Group {
                if let catalog {
                    form(catalog)
                } else if loading {
                    ProgressView()
                } else {
                    ContentUnavailableView {
                        Label("Can't load what agents start from", systemImage: "exclamationmark.triangle")
                    } description: {
                        Text("Valet didn't answer with its templates and harnesses.")
                    } actions: {
                        Button("Try again") { Task { await load() } }
                    }
                }
            }
            .navigationTitle(request.copyFrom == nil ? "New agent" : "Copy of \(model.title(of: request.copyFrom!))")
            #if os(iOS)
            .navigationBarTitleDisplayMode(.inline)
            #endif
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }.disabled(busy != nil)
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button(busy ?? "Create & start") { Task { await create() } }
                        .disabled(busy != nil || catalog == nil)
                        .keyboardShortcut(.defaultAction)
                        .accessibilityIdentifier("new-agent-create")
                }
            }
        }
        .interactiveDismissDisabled(busy != nil)
        .tint(.valet)       // a sheet on the Mac doesn't take the window's
        #if os(macOS)
        .frame(minWidth: 500, idealWidth: 540, minHeight: 600, idealHeight: 860)
        #endif
        .task { await load() }
    }

    private func load() async {
        loading = true
        defer { loading = false }
        let c = await model.loadCatalog(force: true)
        guard let c else { return }
        if catalog == nil {
            var d = NewAgentDraft(catalog: c)
            if let src = request.copyFrom, let a = model.agents[src] { d.copy(a) }
            draft = d
            #if DEBUG
            let script = DebugLaunch.takeNewAgentScript()
            if let n = script.name { draft.setName(n) }
            #endif
            catalog = c
            try? await Task.sleep(for: .milliseconds(350))
            focus = .name
            #if DEBUG
            if script.create { await create() }
            #endif
        } else {
            catalog = c
        }
    }

    // MARK: the form

    @ViewBuilder private func form(_ c: Catalog) -> some View {
        Form {
            nameSection(c)
            startSection(c)
            brainSection(c)
            if !errors.general.isEmpty {
                Section { ErrorText(text: errors.general.joined(separator: "\n")) }
            }
        }
        .formStyle(.grouped)
        .scrollDismissesKeyboard(.interactively)
        .disabled(busy != nil)
    }

    @ViewBuilder private func nameSection(_ c: Catalog) -> some View {
        Section {
            TextField("Name", text: Binding(get: { draft.displayName }, set: { draft.setName($0); clear(F.name, F.displayName) }),
                      prompt: Text("e.g. Scout"))
                .focused($focus, equals: .name)
                .submitLabel(.done)
                .accessibilityIdentifier("new-agent-name")
            FieldError(text: errors[F.displayName])
            if editingID {
                TextField("ID", text: Binding(get: { draft.id }, set: { draft.setID($0); clear(F.name) }),
                          prompt: Text(verbatim: "scout"))
                    .font(.body.monospaced())
                    .autocorrectionDisabled()
                    #if os(iOS)
                    .textInputAutocapitalization(.never)
                    .keyboardType(.asciiCapable)
                    #endif
                    .focused($focus, equals: .id)
                    .accessibilityIdentifier("new-agent-id")
            } else {
                HStack(spacing: 4) {
                    Text("ID").foregroundStyle(.secondary)
                    Text(verbatim: draft.id.isEmpty ? "…" : draft.id).font(.callout.monospaced())
                        .accessibilityIdentifier("new-agent-id-shown")
                    Spacer()
                    Button("Change") {
                        editingID = true
                        focus = .id
                    }
                    .buttonStyle(.borderless)
                    .accessibilityIdentifier("new-agent-id-change")
                }
                .font(.callout)
            }
            FieldError(text: errors[F.name]
                       ?? draft.liveError(taken: { model.agents[$0] != nil }))
            LabeledContent("Emoji") {
                TextField("Emoji", text: $draft.emoji, prompt: Text(emojiPlaceholder(c)))
                    .labelsHidden()
                    .multilineTextAlignment(.trailing)
                    .frame(maxWidth: 120)
                    .onChange(of: draft.emoji) { clear(F.emoji) }
                    .accessibilityIdentifier("new-agent-emoji")
            }
            FieldError(text: errors[F.emoji])
        } header: {
            Text("Name")
        } footer: {
            Text("The ID is how Valet knows it, in addresses and files. It can't change later.")
        }
    }

    /// Empty, it gets the template's emoji, or the copied agent's: shown as the placeholder.
    private func emojiPlaceholder(_ c: Catalog) -> String {
        switch draft.start {
        case .copy(let n): return model.agents[n]?.emoji ?? "🙂"
        case .template(let t): return c.templates.first { $0.id == t }?.emoji ?? "🙂"
        }
    }

    @ViewBuilder private func startSection(_ c: Catalog) -> some View {
        let agents = model.agentList
        Section {
            ChoiceList(items: c.templates.map { t in
                ChoiceList.Item(id: "template:" + t.id, name: (t.emoji.map { $0 + " " } ?? "") + t.name)
            } + (agents.isEmpty ? [] : [ChoiceList.Item(id: "copy", name: "Copy of…",
                                                       summary: "Its personality and settings, with a computer and "
                                                       + "sessions of its own.")]),
                       selection: draft.copyFrom != nil ? "copy" : "template:" + (draft.template ?? ""),
                       field: "start") { id in
                clear(F.start)
                if id == "copy" {
                    if draft.copyFrom == nil, let a = agents.first { draft.copy(a); syncOther(c) }
                } else {
                    draft.start = .template(String(id.dropFirst("template:".count)))
                }
            }
            if let src = draft.copyFrom {
                Picker("Agent to copy", selection: Binding(get: { src }, set: { n in
                    if let a = model.agents[n] { draft.copy(a); syncOther(c) }
                })) {
                    ForEach(agents) { a in
                        Text((a.emoji.map { $0 + " " } ?? "") + a.title + (a.title != a.name ? " (\(a.name))" : ""))
                            .tag(a.name)
                    }
                }
                .accessibilityIdentifier("new-agent-copy-from")
            } else if let t = c.templates.first(where: { $0.id == draft.template }) {
                Text(t.persona.replacingOccurrences(of: "{who}", with: draft.trimmedName.isEmpty ? "…" : draft.trimmedName))
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(10)
                    .background(Color.secondary.opacity(0.1), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                    .accessibilityIdentifier("new-agent-preview")
            }
            FieldError(text: errors[F.start])
        } header: {
            Text("Start from")
        } footer: {
            if draft.copyFrom == nil {
                Text("Its personality, which you can change in its settings.")
            }
        }
    }

    @ViewBuilder private func brainSection(_ c: Catalog) -> some View {
        let harness = draft.harness ?? "claude"
        let known = c.harnesses.contains { $0.id == harness }
        let models = c.harnesses.first { $0.id == harness }?.models ?? []
        let custom = otherModel || (draft.model != nil && !models.contains { $0.id == draft.model })
        Section {
            ChoiceList(items: c.harnesses.map(ChoiceList.Item.init(harness:))
                       + (known ? [] : [ChoiceList.Item(id: harness, name: harness, summary: "Set in the copied agent")]),
                       selection: harness, field: "harness") { id in
                draft.pickHarness(id)
                otherModel = false
                clear(F.harness, F.model)
            }
            FieldError(text: errors[F.harness])
            Picker("Model", selection: Binding(get: { custom ? Self.otherTag : (draft.model ?? "") }, set: { v in
                clear(F.model)
                if v == Self.otherTag {
                    otherModel = true
                    focus = .model
                } else {
                    otherModel = false
                    draft.model = v.isEmpty ? nil : v
                }
            })) {
                Text("Default").tag("")
                ForEach(models, id: \.id) { m in
                    Text(m.name + (m.note.map { " — " + $0 } ?? "")).tag(m.id)
                }
                Text("Other…").tag(Self.otherTag)
            }
            .accessibilityIdentifier("new-agent-model")
            if custom {
                TextField("Model id", text: Binding(get: { draft.model ?? "" }, set: { t in
                    let v = t.trimmingCharacters(in: .whitespacesAndNewlines)
                    draft.model = v.isEmpty ? nil : v
                    clear(F.model)
                }), prompt: Text("the model's id"))
                    .font(.body.monospaced())
                    .autocorrectionDisabled()
                    #if os(iOS)
                    .textInputAutocapitalization(.never)
                    #endif
                    .focused($focus, equals: .model)
                    .accessibilityIdentifier("new-agent-model-id")
            }
            FieldError(text: errors[F.model])
        } header: {
            Text("Brain")
        } footer: {
            Text("The AI that does the thinking.")
        }
    }

    /// A copy's model that the harness doesn't list shows in the Other… field.
    private func syncOther(_ c: Catalog) {
        let models = c.harnesses.first { $0.id == draft.harness }?.models ?? []
        otherModel = draft.model.map { m in !models.contains { $0.id == m } } ?? false
    }

    private typealias F = NewAgentDraft.Field

    /// An edit clears the errors of the fields it touches.
    private func clear(_ keys: String...) {
        for k in keys { errors.clear(k) }
    }

    // MARK: Create & start

    private func create() async {
        // Return and the button can both fire before the disabled button is drawn: one create at a time.
        guard busy == nil else { return }
        errors = draft.check(taken: { model.agents[$0] != nil })
        if errors[F.name] != nil && !editingID && !draft.id.isEmpty { editingID = true }
        guard errors.isEmpty else { return }
        busy = "Creating…"
        do {
            _ = try await model.createAgent(draft)
        } catch {
            errors = draft.errors(from: error)
            if errors[F.name] != nil { editingID = true }
            busy = nil
            return
        }
        busy = "Starting…"
        let name = draft.id
        if let note = await model.startNew(name) { model.agentNotes[name] = note }
        busy = nil
        dismiss()
        model.openAgent = name
    }
}
