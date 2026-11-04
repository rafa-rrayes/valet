import SwiftUI

public struct MemoryRef: Hashable, Sendable {
    public let agent: String
    public init(_ agent: String) { self.agent = agent }
}

public struct MemoryFileRef: Hashable, Sendable {
    public let agent: String
    public let name: String
    /// A file that doesn't exist yet: the screen opens in the editor.
    public var creating = false

    public init(agent: String, name: String, creating: Bool = false) {
        self.agent = agent
        self.name = name
        self.creating = creating
    }
}

/// An agent's memory: whether it's on, its files (Markdown, one per subject) with their size against the limits,
/// and New file. Memory itself is switched on and off in the agent's settings.
public struct MemoryView: View {
    let agent: String
    @Environment(AppModel.self) private var model
    @State private var memory: AgentMemory?
    @State private var error: String?
    @State private var naming = false
    @State private var newName = ""
    @State private var newError: String?
    @State private var opened: MemoryFileRef?
    @State private var subscription: UUID?

    public init(agent: String) { self.agent = agent }

    public var body: some View {
        List {
            Section {
                Text("Notes it keeps for itself across sessions and restarts, as Markdown files, one per subject. "
                     + "You can read and change them too.")
                    .font(.callout).foregroundStyle(.secondary)
                if let m = memory { status(m) }
            }
            if let error { Section { ErrorText(text: error) } }
            if let m = memory {
                Section {
                    ForEach(m.files) { f in
                        NavigationLink(value: MemoryFileRef(agent: agent, name: f.name)) { row(f) }
                            .accessibilityIdentifier("memory-row")
                    }
                } header: {
                    Text(m.files.isEmpty ? "No memory files yet"
                         : "\(m.files.count) file\(m.files.count == 1 ? "" : "s"), \(byteSize(m.size)) of \(byteSize(m.limits.total))")
                } footer: {
                    if m.files.count >= m.limits.files { Text("That's as many files as an agent can keep.") }
                }
            } else if error == nil {
                ProgressView().frame(maxWidth: .infinity)
            }
        }
        .navigationTitle("Memory")
        .readableWidth()
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button { naming = true } label: { Label("New file", systemImage: "plus") }
                    .accessibilityIdentifier("memory-new")
            }
        }
        .navigationDestination(item: $opened) { MemoryFileView(ref: $0) }
        .refreshable { await load() }
        .task(id: model.hellos) { await load() }
        .onAppear {
            subscription = model.subscribe { frame in
                if case .memory(let c) = frame, c.agent == agent { memory?.apply(c) }
            }
        }
        .onDisappear { if let s = subscription { model.unsubscribe(s) } }
        .alert("New memory file", isPresented: $naming) {
            TextField("project-notes", text: $newName)
                .autocorrectionDisabled()
                #if os(iOS)
                .textInputAutocapitalization(.never)
                #endif
                .accessibilityIdentifier("memory-new-name")
            Button("Cancel", role: .cancel) { newName = "" }
            Button("Create") { create() }
        } message: {
            Text("Lowercase letters and digits, words joined by dashes.")
        }
        .errorAlert($newError)
    }

    @ViewBuilder private func status(_ m: AgentMemory) -> some View {
        let (icon, color, text): (String, Color, String) = switch (m.enabled, m.layer) {
        case (.some(true), .some(true)): ("brain", .green, "On: it reads and writes these files with its memory tools, and every "
                            + "session starts with them in what it's told.")
        case (.some(true), _): ("brain", .orange, "On, but what it's told at the start leaves them out: it has to look them up.")
        case (.some(false), _): ("brain", .secondary, "Off: no memory tools, and nothing about memory in what it's told. "
                          + "Its settings turn it on.")
        default: ("exclamationmark.triangle", .orange, "Its agent.yaml doesn't validate, so whether memory is on isn't known.")
        }
        Label { Text(text).font(.callout) } icon: { Image(systemName: icon).foregroundStyle(color) }
            .accessibilityIdentifier("memory-status")
    }

    private func row(_ f: MemoryFile) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(f.name).font(.body.monospaced())
            Text([f.title.isEmpty ? nil : f.title, byteSize(f.size),
                  f.updatedAt > 0 ? "updated " + Date(timeIntervalSince1970: f.updatedAt).ago : nil]
                .compactMap { $0 }.joined(separator: " · "))
                .font(.caption).foregroundStyle(.secondary).lineLimit(1)
        }
    }

    private func load() async {
        do {
            memory = try await model.api.memory(agent)
            error = nil
        } catch is CancellationError {
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func create() {
        let n = MemoryFile.slug(newName)
        newName = ""
        guard MemoryFile.isValidName(n) else {
            newError = "A memory file's name is lowercase letters and digits, words joined by dashes (project-notes)."
            return
        }
        if memory?.files.contains(where: { $0.name == n }) == true {
            opened = MemoryFileRef(agent: agent, name: n)
        } else {
            opened = MemoryFileRef(agent: agent, name: n, creating: true)
        }
    }
}

/// One memory file: its Markdown, Edit (with its size against the limit), Delete, and its history (an older version
/// shown, then Restore puts it in the editor).
public struct MemoryFileView: View {
    let ref: MemoryFileRef
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var text: String?
    @State private var file: MemoryFile?
    @State private var exists = true
    @State private var editing = false
    @State private var draft = ""
    @State private var saving = false
    @State private var news: String?
    @State private var error: String?
    @State private var confirmDelete = false
    @State private var history: [HistoryEntry]?
    @State private var showHistory = false
    @State private var older: OlderVersion?
    @State private var subscription: UUID?

    struct OlderVersion: Identifiable {
        let entry: HistoryEntry
        let text: String
        var id: String { entry.commit }
    }

    public init(ref: MemoryFileRef) { self.ref = ref }

    private static let fileLimit = 65536

    public var body: some View {
        Group {
            if editing { editor } else { reader }
        }
        .navigationTitle(ref.name + ".md")
        #if os(iOS)
        .navigationBarTitleDisplayMode(.inline)
        #endif
        .toolbar { toolbar }
        .task {
            if ref.creating {
                exists = false
                text = ""
                editing = true
            } else {
                await load()
            }
        }
        .onAppear {
            subscription = model.subscribe { frame in
                guard case .memory(let c) = frame, c.agent == ref.agent, c.name == ref.name else { return }
                changedElsewhere(c)
            }
        }
        .onDisappear { if let s = subscription { model.unsubscribe(s) } }
        .confirmationDialog("Delete \(ref.name).md?", isPresented: $confirmDelete, titleVisibility: .visible) {
            Button("Delete", role: .destructive) { Task { await delete() } }
        } message: {
            Text("The agent forgets what's in it. Its history keeps it.")
        }
        .sheet(isPresented: $showHistory) { historySheet }
        .sheet(item: $older) { o in olderSheet(o) }
        .errorAlert($error)
    }

    // MARK: reading

    private var reader: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                if let news { NoteBox(text: news) }
                if let file {
                    Text([file.title.isEmpty ? nil : file.title, byteSize(file.size),
                          file.updatedAt > 0 ? "updated " + Date(timeIntervalSince1970: file.updatedAt).ago : nil]
                        .compactMap { $0 }.joined(separator: " · "))
                        .font(.caption).foregroundStyle(.secondary)
                }
                if let text {
                    if exists {
                        MarkdownView(text).accessibilityIdentifier("memory-text")
                    } else {
                        ContentUnavailableView {
                            Label("There's no such memory file", systemImage: "doc.questionmark")
                        } actions: {
                            Button("Write it") { startEditing("") }.buttonStyle(.borderedProminent)
                        }
                    }
                } else {
                    ProgressView().frame(maxWidth: .infinity)
                }
            }
            .padding()
            .frame(maxWidth: 820, alignment: .leading)
            .frame(maxWidth: .infinity)
        }
        .background(Color.screen)
    }

    // MARK: editing

    private var editor: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let news { NoteBox(text: news) }
            TextEditor(text: $draft)
                .font(.body.monospaced())
                .scrollContentBackground(.hidden)
                .padding(8)
                .background(Color.card, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(.separator))
                .accessibilityIdentifier("memory-editor")
            let n = draft.utf8.count
            Text("\(byteSize(n)) of \(byteSize(Self.fileLimit))")
                .font(.caption).foregroundStyle(n > Self.fileLimit ? .red : .secondary)
        }
        .padding()
        .background(Color.screen)
    }

    @ToolbarContentBuilder private var toolbar: some ToolbarContent {
        if editing {
            ToolbarItem(placement: .cancellationAction) {
                Button("Cancel") {
                    if ref.creating && !exists { dismiss() }
                    editing = false
                    news = nil
                }
            }
            ToolbarItem(placement: .confirmationAction) {
                Button { Task { await save() } } label: {
                    if saving { ProgressView().controlSize(.small) } else { Text(exists ? "Save" : "Create") }
                }
                .disabled(saving || draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                          || draft.utf8.count > Self.fileLimit)
                .accessibilityIdentifier("memory-save")
            }
        } else if exists && text != nil {
            ToolbarItemGroup(placement: .primaryAction) {
                Button { startEditing(text ?? "") } label: { Label("Edit", systemImage: "pencil") }
                    .accessibilityIdentifier("memory-edit")
                Menu {
                    Button { Task { await openHistory() } } label: { Label("History", systemImage: "clock.arrow.circlepath") }
                    Button(role: .destructive) { confirmDelete = true } label: { Label("Delete", systemImage: "trash") }
                } label: { Label("More", systemImage: "ellipsis.circle") }
            }
        }
    }

    private func startEditing(_ text: String) {
        draft = text
        news = nil
        editing = true
    }

    // MARK: history

    private var historySheet: some View {
        NavigationStack {
            List {
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
                            } else if !c.message.contains(" deleted ") {
                                Button("View") { Task { await view(c) } }.buttonStyle(.bordered).controlSize(.small)
                            }
                        }
                    }
                } else {
                    ProgressView()
                }
            }
            .navigationTitle("History")
            #if os(iOS)
            .navigationBarTitleDisplayMode(.inline)
            #endif
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { showHistory = false } } }
        }
        #if os(macOS)
        .frame(minWidth: 480, minHeight: 420)
        #endif
    }

    private func olderSheet(_ o: OlderVersion) -> some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 10) {
                    Text("\(o.entry.message) · \(Date(timeIntervalSince1970: o.entry.at).ago)")
                        .font(.caption).foregroundStyle(.secondary)
                    CodeText(text: o.text)
                }
                .padding()
            }
            .navigationTitle(o.entry.short)
            #if os(iOS)
            .navigationBarTitleDisplayMode(.inline)
            #endif
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Close") { older = nil } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Restore this version") {
                        older = nil
                        startEditing(o.text)
                        news = "Restored into the editor: Save to keep it."
                    }
                }
            }
        }
        #if os(macOS)
        .frame(minWidth: 560, minHeight: 480)
        #endif
    }

    // MARK: loading and writing

    private func load() async {
        do {
            let r = try await model.api.memoryFile(ref.agent, ref.name)
            text = r.text
            file = r.file
            exists = true
        } catch let e as APIError where e.status == 404 {
            exists = false
            text = ""
        } catch is CancellationError {
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func save() async {
        saving = true
        defer { saving = false }
        do {
            file = try await model.api.writeMemory(ref.agent, ref.name, text: draft)
            text = draft.hasSuffix("\n") ? draft : draft + "\n"
            exists = true
            editing = false
            news = nil
            history = nil
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func delete() async {
        do {
            try await model.api.deleteMemory(ref.agent, ref.name)
            dismiss()
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func openHistory() async {
        showHistory = true
        do { history = try await model.api.memoryHistory(ref.agent, ref.name) } catch {
            showHistory = false
            self.error = error.localizedDescription
        }
    }

    private func view(_ c: HistoryEntry) async {
        do {
            let r = try await model.api.memoryFile(ref.agent, ref.name, at: c.commit)
            showHistory = false
            try? await Task.sleep(for: .milliseconds(400))      // one sheet at a time
            older = OlderVersion(entry: c, text: r.text)
        } catch {
            self.error = error.localizedDescription
        }
    }

    /// The agent (or another client) wrote or deleted this file.
    private func changedElsewhere(_ c: MemoryChange) {
        if editing {
            if exists {
                news = c.file != nil ? "It was changed elsewhere since you started editing: saving replaces that."
                    : "It was deleted elsewhere: saving writes it again."
                if c.file == nil { exists = false }
            }
            return
        }
        if c.file != nil {
            news = nil
            Task { await load() }
        } else {
            news = "It was just deleted."
            exists = false
            text = ""
            file = nil
        }
    }
}
