import SwiftUI

// Skills (app.js's SkillsView, SkillView and the settings form's drawSkills): instructions an agent loads when a
// task calls for them. The shared library, an agent's own, and which shared ones an agent picks.

/// Settings › Skills (navigation value).
public struct SkillsRef: Hashable, Sendable { public init() {} }

/// One skill: the library's (`agent` nil) or an agent's own.
public struct SkillRef: Hashable, Sendable {
    public let name: String
    public let agent: String?
    public init(_ name: String, agent: String? = nil) {
        self.name = name
        self.agent = agent
    }
}

/// The shared library: each skill with what it's for, who uses it and its files; New skill.
public struct SkillsView: View {
    @Environment(AppModel.self) private var model
    @State private var list: [SkillInfo]?
    @State private var error: String?
    @State private var creating = false

    public init() {}

    public var body: some View {
        List {
            Section {
                if let error { ErrorText(text: error) }
                if list == nil && error == nil { ProgressView() }
                if let l = list, l.isEmpty {
                    ContentUnavailableView("No skills yet", systemImage: "book.closed",
                                           description: Text("New skill writes one; then give it to an agent in its settings."))
                }
                ForEach(list ?? []) { s in
                    NavigationLink(value: SkillRef(s.name)) { SkillRow(skill: s, shared: true) }
                        .accessibilityIdentifier("skill-\(s.name)")
                }
            } footer: {
                Text("Instructions your agents load when a task calls for them. Give one to an agent in its settings, "
                     + "under Skills; changes reach an agent when it restarts.")
            }
        }
        .readableWidth()
        .navigationTitle("Skills")
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button { creating = true } label: { Label("New skill", systemImage: "plus") }
                    .accessibilityIdentifier("skill-new")
            }
        }
        .task(id: model.hellos) { await load() }
        .onAppear { if list != nil { Task { await load() } } }
        .refreshable { await load() }
        .sheet(isPresented: $creating) { NewSkillSheet(agent: nil) { _ in await load() } }
    }

    private func load() async {
        do {
            list = try await model.api.skills()
            error = nil
        } catch is CancellationError {
        } catch {
            self.error = error.localizedDescription
        }
    }
}

/// A skill in a list: its name, what it's for, a problem, who uses it.
struct SkillRow: View {
    let skill: SkillInfo
    let shared: Bool
    @Environment(AppModel.self) private var model

    var body: some View {
        let s = skill
        VStack(alignment: .leading, spacing: 3) {
            Text(s.name).font(.body.monospaced())
            if let d = s.description, !d.isEmpty { Text(brief(d, 160)).font(.callout).foregroundStyle(.secondary) }
            if let e = s.error {
                Text((shared ? "It has a problem, so no agent gets it: " : "It has a problem, so it's left out: ") + e)
                    .font(.caption).foregroundStyle(.orange)
            }
            if s.replacesShared {
                Text("Used instead of the shared skill of this name.").font(.caption).foregroundStyle(.secondary)
            }
            if shared {
                let used = s.usedBy.map(model.title)
                let more = s.moreFiles
                Text([used.isEmpty ? "No agent uses it" : "Used by " + andList(used),
                      more > 0 ? (more == 1 ? "1 more file" : "\(more) more files") : nil]
                    .compactMap { $0 }.joined(separator: " · "))
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 2)
    }
}

/// A new skill: its name, when to use it, and what to do.
struct NewSkillSheet: View {
    let agent: String?
    let done: (Skill) async -> Void
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var description = ""
    @State private var instructions = ""
    @State private var busy = false
    @State private var error: String?

    var body: some View {
        NavigationStack {
            Form {
                Section("Name") {
                    TextField("Name", text: $name, prompt: Text(verbatim: "code-review"))
                        .font(.body.monospaced())
                        .autocorrectionDisabled()
                        #if os(iOS)
                        .textInputAutocapitalization(.never)
                        #endif
                        .accessibilityIdentifier("skill-name")
                }
                Section {
                    TextField("When to use it", text: $description, prompt: Text("Use when reviewing a pull request."),
                              axis: .vertical)
                        .lineLimit(2...4)
                        .accessibilityIdentifier("skill-description")
                } header: { Text("When to use it") } footer: {
                    Text("The agent sees only this until it uses the skill.")
                }
                Section("Instructions") {
                    TextEditor(text: $instructions)
                        .font(.body.monospaced())
                        .frame(minHeight: 180)
                        .accessibilityIdentifier("skill-instructions")
                }
                if let error { Section { ErrorText(text: error) } }
            }
            .formStyle(.grouped)
            .navigationTitle(agent == nil ? "New skill" : "New skill for \(model.title(of: agent ?? ""))")
            #if os(iOS)
            .navigationBarTitleDisplayMode(.inline)
            #endif
            .interactiveDismissDisabled(!name.isEmpty || !description.isEmpty || !instructions.isEmpty)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button { Task { await create() } } label: {
                        if busy { ProgressView().controlSize(.small) } else { Text("Create") }
                    }
                    .disabled(busy)
                    .accessibilityIdentifier("skill-create")
                }
            }
        }
        #if os(macOS)
        .frame(minWidth: 520, minHeight: 520)
        #endif
    }

    private func create() async {
        let n = Skill.name(from: name)
        let d = description.trimmingCharacters(in: .whitespacesAndNewlines)
        error = nil
        guard !n.isEmpty else { error = "Give it a name, like code-review."; return }
        guard Skill.isValidName(n) else {
            error = "Use lowercase letters and digits, with single dashes between words, like code-review."
            return
        }
        guard !d.isEmpty else { error = "Say when to use it: that's what the agent goes by."; return }
        busy = true
        defer { busy = false }
        do {
            let s = try await model.api.createSkill(n, description: d, instructions: instructions, agent: agent)
            await done(s)
            dismiss()
        } catch {
            self.error = error.localizedDescription
        }
    }
}

/// One skill: when to use it, the instructions and its other files, saved together; Delete. A SKILL.md that can't
/// be read as name / description / instructions is edited as a file.
struct SkillView: View {
    let ref: SkillRef
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var skill: Skill?
    @State private var saved = SkillDraft()
    @State private var draft = SkillDraft()
    @State private var loadError: String?
    @State private var notFound = false
    @State private var busy = false
    @State private var flash: String?
    @State private var error: String?
    @State private var newPath = ""
    @State private var pathError: String?
    @State private var open: Set<String> = []
    @State private var confirmDelete = false
    @State private var leaving = false

    private var raw: Bool { skill?.raw ?? false }
    private var dirty: Bool { skill != nil && draft != saved }

    var body: some View {
        Form {
            if notFound {
                ContentUnavailableView(ref.agent.map { "\(model.title(of: $0)) has no skill named \(ref.name)" }
                                       ?? "No skill named \(ref.name)", systemImage: "questionmark.circle")
            } else if let e = loadError, skill == nil {
                Section { ErrorText(text: e); Button("Try again") { Task { await load() } } }
            } else if skill == nil {
                Section { ProgressView() }
            } else if let s = skill {
                Section {
                    Text(ref.agent.map { "Only for " + model.title(of: $0) }
                         ?? "Shared skill · " + (s.info.usedBy.isEmpty ? "no agent uses it"
                                                 : "used by " + andList(s.info.usedBy.map(model.title))))
                        .font(.callout).foregroundStyle(.secondary)
                    if let e = s.info.error {
                        NoteBox(text: (ref.agent == nil ? "It has a problem, so no agent gets it: "
                                       : "It has a problem, so the agent doesn't get it: ") + e,
                                systemImage: "exclamationmark.triangle")
                    }
                }
                if raw {
                    Section {
                        NoteBox(text: "Its SKILL.md doesn't start with a name and description, so it's shown as a file "
                                + "below: fix it there.")
                    }
                } else {
                    Section {
                        TextField("When to use it", text: $draft.description, axis: .vertical)
                            .lineLimit(2...5)
                            .accessibilityIdentifier("skill-description")
                    } header: { Text("When to use it") } footer: {
                        Text("The agent sees only this until it uses the skill.")
                    }
                    Section {
                        TextEditor(text: $draft.instructions)
                            .font(.body.monospaced())
                            .frame(minHeight: 220)
                            .accessibilityIdentifier("skill-instructions")
                    } header: { Text("Instructions") } footer: {
                        let extra = s.extra.keys.sorted()
                        Text("What to do, in Markdown. It reads this when it uses the skill."
                             + (extra.isEmpty ? "" : " Its SKILL.md also sets \(andList(extra)): kept as they are."))
                    }
                }
                filesSection
                Section {
                    Button("Delete skill", role: .destructive) { confirmDelete = true }
                        .accessibilityIdentifier("skill-delete")
                }
            }
        }
        .formStyle(.grouped)
        .readableWidth()
        .navigationTitle(ref.name)
        #if os(iOS)
        .navigationBarTitleDisplayMode(.inline)
        #endif
        .navigationBarBackButtonHidden(dirty)
        .toolbar {
            if dirty {
                ToolbarItem(placement: .cancellationAction) {
                    Button { leaving = true } label: { Label("Back", systemImage: "chevron.backward") }
                }
            }
            ToolbarItem(placement: .confirmationAction) {
                Button { Task { await save() } } label: {
                    if busy { ProgressView().controlSize(.small) } else { Text("Save") }
                }
                .disabled(busy || !dirty)
                .accessibilityIdentifier("skill-save")
            }
        }
        .safeAreaInset(edge: .bottom, spacing: 0) {
            if error != nil || flash != nil {
                VStack(alignment: .leading) {
                    if let error { ErrorText(text: error) }
                    if let flash { Label(flash, systemImage: "checkmark.circle.fill").foregroundStyle(.green) }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(12)
                .background(.bar)
            }
        }
        .task { if skill == nil { await load() } }
        .onChange(of: draft) { flash = nil }
        .confirmationDialog("You have unsaved changes", isPresented: $leaving, titleVisibility: .visible) {
            Button("Save") { Task { if await save() { dismiss() } } }
            Button("Discard & leave", role: .destructive) { draft = saved; dismiss() }
            Button("Stay", role: .cancel) {}
        }
        .confirmationDialog(deleteQuestion, isPresented: $confirmDelete, titleVisibility: .visible) {
            Button("Delete", role: .destructive) { Task { await delete() } }
        }
    }

    private var filesSection: some View {
        let paths = draft.files.keys.sorted { a, b in
            (a == Skill.file ? 0 : 1, a.lowercased()) < (b == Skill.file ? 0 : 1, b.lowercased())
        }
        return Section {
            if paths.isEmpty { Text("None.").foregroundStyle(.secondary) }
            ForEach(paths, id: \.self) { p in fileRow(p) }
            HStack {
                TextField("Add a file", text: $newPath, prompt: Text(verbatim: "reference.md"))
                    .font(.body.monospaced())
                    .autocorrectionDisabled()
                    #if os(iOS)
                    .textInputAutocapitalization(.never)
                    #endif
                    .onSubmit(addFile)
                    .accessibilityIdentifier("skill-file-new")
                Button("Add", action: addFile).disabled(newPath.trimmingCharacters(in: .whitespaces).isEmpty)
            }
            FieldError(text: pathError)
        } header: {
            Text("Files")
        } footer: {
            Text(raw ? "Its files." : "Other files the instructions point to, like a reference or a script. Text only.")
        }
    }

    private func fileRow(_ p: String) -> some View {
        let text = draft.files[p] ?? ""
        let lines = text.isEmpty ? 0 : text.split(separator: "\n", omittingEmptySubsequences: false).count
            - (text.hasSuffix("\n") ? 1 : 0)
        return VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(p).font(.body.monospaced()).lineLimit(1).truncationMode(.middle)
                if saved.files[p] == nil { Chip(text: "new", color: .valet) }
                Spacer()
                Text(lines == 1 ? "1 line" : "\(lines) lines").font(.caption).foregroundStyle(.secondary)
            }
            if open.contains(p) {
                TextEditor(text: Binding(get: { draft.files[p] ?? "" }, set: { draft.files[p] = $0 }))
                    .font(.system(.callout, design: .monospaced))
                    .autocorrectionDisabled()
                    #if os(iOS)
                    .textInputAutocapitalization(.never)
                    #endif
                    .frame(minHeight: 160)
                    .accessibilityIdentifier("skill-file-\(p)")
            }
            HStack {
                Button(open.contains(p) ? "Done" : "Edit") {
                    if open.contains(p) { open.remove(p) } else { open.insert(p) }
                }
                .buttonStyle(.bordered)
                if !(raw && p == Skill.file) {
                    Button("Remove", role: .destructive) {
                        draft.files[p] = nil
                        open.remove(p)
                    }
                    .buttonStyle(.bordered).tint(.red)
                }
            }
            .controlSize(.small)
        }
    }

    private func addFile() {
        let p = newPath.trimmingCharacters(in: .whitespacesAndNewlines)
        pathError = nil
        if p.isEmpty { pathError = "Type its path, like reference.md."; return }
        if p == Skill.file { pathError = "SKILL.md is the description and instructions above."; return }
        if draft.files[p] != nil { pathError = "It already has \(p)."; return }
        guard Skill.isValidPath(p) else {
            pathError = "Use a relative path like reference.md or scripts/check.sh: letters, digits, . _ - and /."
            return
        }
        draft.files[p] = ""
        open.insert(p)
        newPath = ""
    }

    private var deleteQuestion: String {
        if let a = ref.agent { return "Delete \(model.title(of: a))'s skill \(ref.name)? It keeps it until it restarts." }
        let used = (skill?.info.usedBy ?? []).map(model.title)
        let many = used.count > 1
        return used.isEmpty ? "Delete this skill? It stays in the definitions repo's history."
            : "\(andList(used)) \(many ? "use" : "uses") it. \(many ? "They keep" : "It keeps") it until "
                + "\(many ? "they restart" : "it restarts"). Delete it?"
    }

    private func take(_ s: Skill) {
        skill = s
        saved = SkillDraft(s)
        draft = saved
        open = open.filter { saved.files[$0] != nil }
        if s.raw { open.insert(Skill.file) }
    }

    private func load() async {
        do {
            take(try await model.api.skill(ref.name, agent: ref.agent))
            loadError = nil
        } catch let e as APIError where e.status == 404 {
            notFound = true
        } catch is CancellationError {
        } catch {
            loadError = error.localizedDescription
        }
    }

    @discardableResult
    private func save() async -> Bool {
        guard dirty, !busy else { return false }
        busy = true
        error = nil
        defer { busy = false }
        do {
            take(try await model.api.updateSkill(ref.name, agent: ref.agent,
                                                 body: Skill.editBody(raw: raw, saved: saved, draft: draft)))
            flash = "Saved."
            endEditing()
            return true
        } catch {
            self.error = error.localizedDescription
            return false
        }
    }

    private func delete() async {
        do {
            try await model.api.deleteSkill(ref.name, agent: ref.agent)
            draft = saved
            dismiss()
        } catch {
            self.error = error.localizedDescription
        }
    }
}

// MARK: the settings form's "Skills"

/// Which shared skills an agent gets (tools.skills: by name, or all), what's left out, and its own skills.
struct SkillsSection: View {
    @Bindable var settings: AgentSettings
    @Environment(AppModel.self) private var model
    @State private var library: [SkillInfo]?
    @State private var libraryError: String?
    @State private var mine: AgentSkills?
    @State private var mineError: String?
    @State private var creating = false

    var body: some View {
        let s = settings
        let picked = s.draft?.skills ?? []
        let all = picked.contains("*")
        let known = library ?? []
        let own = Set((mine?.own ?? []).map(\.name))
        let names = known.map(\.name) + picked.filter { n in n != "*" && !known.contains { $0.name == n } }
        Section {
            if let e = libraryError { ErrorText(text: e) }
            if library == nil && libraryError == nil { ProgressView() }
            if library != nil {
                Toggle(isOn: Binding(get: { all }, set: { on in
                    s.draft?.setAllSkills(on, saved: s.saved?.skills ?? [], library: known.map(\.name))
                    s.changed("tools.skills")
                })) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("All shared skills")
                        Text("Every skill in the library, and any added later").font(.caption).foregroundStyle(.secondary)
                    }
                }
                .accessibilityIdentifier("skills-all")
                ForEach(names, id: \.self) { n in
                    sharedRow(n, info: known.first { $0.name == n }, on: all || picked.contains(n), locked: all,
                              shadowed: own.contains(n))
                }
                if known.isEmpty { Text("The library has no skills yet.").foregroundStyle(.secondary) }
            }
            FieldError(text: s.errors["tools.skills"])
            if let p = mine?.problems, !p.isEmpty {
                NoteBox(text: "Left out, until fixed:\n" + p.joined(separator: "\n"), systemImage: "exclamationmark.triangle")
            }
            NavigationLink(value: SkillsRef()) { Text("Manage the skill library") }
        } header: {
            Text("Skills")
        } footer: {
            Text("Instructions it loads when a task calls for them, like how you want code reviewed. It gets the shared "
                 + "skills you switch on, and all of its own.")
        }
        Section {
            if let e = mineError { ErrorText(text: e) }
            if mine == nil && mineError == nil { ProgressView() }
            if let m = mine, m.own.isEmpty {
                Text("None yet. Its own skills are only for it.").foregroundStyle(.secondary)
            }
            ForEach(mine?.own ?? []) { x in
                NavigationLink(value: SkillRef(x.name, agent: s.name)) { SkillRow(skill: x, shared: false) }
            }
            Button { creating = true } label: { Label("New skill", systemImage: "plus") }
                .accessibilityIdentifier("agent-skill-new")
                // On the row: a sheet hung on a Form's Section never shows.
                .sheet(isPresented: $creating) { NewSkillSheet(agent: s.name) { _ in await load() } }
        } header: {
            Text("Its own skills")
        }
        .task(id: model.hellos) { await load() }
        .onAppear { if mine != nil { Task { await load() } } }
    }

    private func sharedRow(_ n: String, info: SkillInfo?, on: Bool, locked: Bool, shadowed: Bool) -> some View {
        let s = settings
        return Toggle(isOn: Binding(get: { on }, set: { v in
            s.draft?.toggle("tools.skills", n, on: v, savedOrder: s.saved?.skills ?? [])
            s.changed("tools.skills")
        })) {
            VStack(alignment: .leading, spacing: 2) {
                Text(n).font(.body.monospaced())
                if let d = info?.description, !d.isEmpty {
                    Text(brief(d, 160)).font(.caption).foregroundStyle(.secondary)
                }
                if info == nil {
                    Text("The library has no skill by this name.").font(.caption).foregroundStyle(.orange)
                }
                if let e = info?.error {
                    Text("It has a problem, so it's left out: " + e).font(.caption).foregroundStyle(.orange)
                }
                if shadowed {
                    Text("Its own skill of this name is used instead.").font(.caption).foregroundStyle(.secondary)
                }
            }
        }
        .disabled(locked)
        .accessibilityIdentifier("skill-toggle-\(n)")
    }

    private func load() async {
        async let lib = model.api.skills()
        async let own = model.api.agentSkills(settings.name)
        // A cancelled load (the screen left, a reconnect's new load) says nothing: the next one will.
        do { library = try await lib; libraryError = nil } catch is CancellationError {
        } catch { libraryError = error.localizedDescription }
        do { mine = try await own; mineError = nil } catch is CancellationError {
        } catch { mineError = error.localizedDescription }
    }
}
