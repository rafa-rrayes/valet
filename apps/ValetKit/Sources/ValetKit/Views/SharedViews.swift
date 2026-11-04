import PhotosUI
import QuickLook
import SwiftUI
import UniformTypeIdentifiers

public struct SharedRef: Hashable, Sendable {
    public let id: Int
    public init(_ id: Int) { self.id = id }
}

/// One agent's shared space (nil: the unassigned items).
public struct SharedListRef: Hashable, Sendable {
    public let agent: String?
    public init(_ agent: String?) { self.agent = agent }
}

extension SharedItem {
    var icon: String {
        if kind == .note { return "note.text" }
        if isImage { return "photo" }
        if mime?.hasPrefix("video/") == true { return "film" }
        if mime?.hasPrefix("audio/") == true { return "waveform" }
        if mime == "application/pdf" { return "doc.richtext" }
        if isText { return "doc.plaintext" }
        if mime == "application/zip" || mime?.contains("compressed") == true { return "doc.zipper" }
        return "doc"
    }

    var tint: Color { kind == .note ? .yellow : isImage ? .teal : .blue }
}

/// Who did it, as a sentence starts: "You", or the agent's title.
@MainActor
private func who(_ name: String, _ model: AppModel) -> String { name == "owner" ? "You" : model.title(of: name) }

// MARK: the unassigned

/// Each agent's Shared is on its page, and only that agent sees it. The items with no agent (from when Shared was
/// one list for every agent) are a row on the Agents screen while there are any, as the web UI's card.
struct SharedUnassignedSection: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        let n = model.sharedCounts.unassigned
        if n > 0 {
            Section {
                NavigationLink(value: SharedListRef(nil)) {
                    Label {
                        VStack(alignment: .leading, spacing: 3) {
                            Text("Shared items with no agent").font(.headline)
                            Text("\(n) from when Shared was one list for every agent: move each to its agent, or "
                                 + "delete it.")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                    } icon: {
                        Image(systemName: "rectangle.stack").foregroundStyle(.tint)
                    }
                    .padding(.vertical, 2)
                }
                .accessibilityIdentifier("shared-unassigned")
            }
        }
    }
}

/// "3 new", as an agent's page shows it by its Shared.
struct NewCount: View {
    let n: Int
    var body: some View {
        Text("\(n) new").font(.caption.weight(.semibold)).foregroundStyle(.tint)
    }
}

// MARK: the list

/// One agent's shared space (or the unassigned items: agent nil): notes and files between the owner and that agent,
/// most recently changed first, a dot on what the agent changed since the owner last looked. Search, who added it,
/// notes or files; a new note, files from Files (or dropped on it) and from Photos.
public struct SharedView: View {
    public let agent: String?
    @Environment(AppModel.self) private var model
    @Environment(\.scenePhase) private var phase
    @State private var list: SharedList?
    @State private var search = ""
    @State private var by = ""
    @State private var kind = ""
    @State private var newNote = false
    @State private var importing = false
    @State private var choosingPhotos = false
    @State private var photos: [PhotosPickerItem] = []
    @State private var error: String?
    #if os(macOS)
    @State private var paste = PasteWatcher(orText: false)
    #endif

    public init(agent: String?) { self.agent = agent }

    public var body: some View {
        Group {
            if let list { content(list) } else { ProgressView() }
        }
        .navigationTitle(agent.map { "Shared with \(model.title(of: $0))" } ?? "Shared, no agent")
        .readableWidth()
        .onAppear {
            if list == nil { list = SharedList(model: model, agent: agent) }
            list?.start()
            #if os(macOS)
            paste.enabled = agent != nil
            paste.start { upload($0) }
            #endif
        }
        .onDisappear {
            list?.stop()
            #if os(macOS)
            paste.stop()
            #endif
        }
        .onChange(of: phase, initial: true) { _, p in list?.visible = p == .active }
        #if os(macOS)
        .background(WindowReader { paste.window = $0 })
        #endif
        .task(id: SharedQuery(q: search, by: by, kind: kind)) {
            guard let list else { return }
            let q = SharedQuery(q: search.trimmingCharacters(in: .whitespaces), by: by, kind: kind)
            if list.loaded && q.q != list.query.q {
                try? await Task.sleep(for: .milliseconds(300))      // typing: wait for a pause
                if Task.isCancelled { return }
            }
            list.query = q
            await list.reload()
        }
        .searchable(text: $search, prompt: "Search notes and files")
        .toolbar { toolbar }
        .sheet(isPresented: $newNote) {
            if let agent { SharedEditor(mode: .newNote(agent)) { item in list?.put(item) } }
        }
        .fileImporter(isPresented: $importing, allowedContentTypes: [.item], allowsMultipleSelection: true) { r in
            if case .success(let urls) = r { upload(PickedFile.sources(urls)) }
        }
        .photosPicker(isPresented: $choosingPhotos, selection: $photos, maxSelectionCount: 20,
                      matching: .any(of: [.images, .videos]))
        .onChange(of: photos) { _, picked in
            guard !picked.isEmpty else { return }
            photos = []
            upload(PickedFile.sources(picked))
        }
        .onDrop(of: PickedFile.dropTypes, isTargeted: nil) { providers in
            guard agent != nil else { return false }
            Task { upload(await PickedFile.sources(dropped: providers)) }
            return true
        }
        .errorAlert($error)
    }

    @ViewBuilder private func content(_ list: SharedList) -> some View {
        List {
            if list.uploading > 0 {
                HStack(spacing: 10) {
                    ProgressView().controlSize(.small)
                    Text(list.uploading == 1 ? "Uploading a file…" : "Uploading \(list.uploading) files…")
                        .foregroundStyle(.secondary)
                }
            }
            if let e = list.error { ErrorText(text: e) }
            ForEach(list.items) { item in
                NavigationLink(value: SharedRef(item.id)) { SharedRow(item: item, dot: list.showsDot(item)) }
                    .accessibilityIdentifier("shared-row")
            }
            if list.more {
                Button {
                    Task { await list.loadOlder() }
                } label: {
                    HStack {
                        Text("Older")
                        if list.loading { ProgressView().controlSize(.small) }
                    }
                }
                .accessibilityIdentifier("shared-older")
            }
        }
        .overlay {
            if list.loaded && list.items.isEmpty && list.error == nil {
                if list.query.isFiltered {
                    ContentUnavailableView.search(text: search)
                } else {
                    if let agent {
                        ContentUnavailableView {
                            Label("Nothing shared yet", systemImage: "rectangle.stack")
                        } description: {
                            Text("Leave a note or a file here for \(model.title(of: agent)), and it leaves its own "
                                 + "here for you. No other agent sees them.")
                        } actions: {
                            Button("New note") { newNote = true }.buttonStyle(.borderedProminent)
                        }
                    } else {
                        ContentUnavailableView("Every shared item is with its agent", systemImage: "rectangle.stack")
                    }
                }
            } else if !list.loaded && list.loading {
                ProgressView()
            }
        }
        .refreshable { await list.reload() }
    }

    @ToolbarContentBuilder private var toolbar: some ToolbarContent {
        ToolbarItemGroup(placement: .primaryAction) {
            Menu {
                Section("Added by") {
                    Picker("Added by", selection: $by) {
                        Text("Anyone").tag("")
                        Text("You").tag("owner")
                        if let agent { Text(model.title(of: agent)).tag(agent) }
                    }
                    .pickerStyle(.inline)
                }
                Section("Kind") {
                    Picker("Kind", selection: $kind) {
                        Text("Notes and files").tag("")
                        Text("Notes").tag("note")
                        Text("Files").tag("file")
                    }
                    .pickerStyle(.inline)
                }
            } label: {
                Label("Filter", systemImage: by.isEmpty && kind.isEmpty
                      ? "line.3.horizontal.decrease.circle" : "line.3.horizontal.decrease.circle.fill")
            }
            .accessibilityIdentifier("shared-filter")
            if agent != nil {
                Menu {
                    Button { newNote = true } label: { Label("New note", systemImage: "square.and.pencil") }
                        .accessibilityIdentifier("shared-new-note")
                    Button { importing = true } label: { Label("Upload files", systemImage: "doc.badge.plus") }
                        .accessibilityIdentifier("shared-upload-files")
                    Button { choosingPhotos = true } label: { Label("From Photos", systemImage: "photo.on.rectangle") }
                        .accessibilityIdentifier("shared-upload-photos")
                } label: {
                    Label("Add", systemImage: "plus")
                }
                .accessibilityIdentifier("shared-add")
            }
        }
    }

    // MARK: uploads

    /// Files from anywhere (Files, Photos, a drop, ⌘V on the Mac), one after another; what failed is said once.
    private func upload(_ sources: [UploadSource]) {
        guard !sources.isEmpty, let list else { return }
        Task {
            if let failed = await list.upload(sources) { error = failed }
        }
    }
}

struct SharedRow: View {
    let item: SharedItem
    /// Unseen, or unseen when this visit showed it.
    var dot = false
    @Environment(AppModel.self) private var model

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: item.icon)
                .font(.title3)
                .foregroundStyle(item.tint)
                .frame(width: 28)
                .padding(.top, 2)
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Text(item.title.isEmpty ? "Untitled" : item.title)
                        .font(.body.weight(dot || item.unseen ? .semibold : .regular))
                        .lineLimit(1)
                    Spacer(minLength: 4)
                    if dot || item.unseen {
                        Circle().fill(.tint).frame(width: 8, height: 8)
                            .accessibilityLabel("New")
                    }
                }
                if !item.preview.isEmpty {
                    Text(item.preview).font(.callout).foregroundStyle(.secondary).lineLimit(2)
                }
                Text(meta).font(.caption).foregroundStyle(.secondary).lineLimit(1)
            }
        }
        .padding(.vertical, 2)
    }

    private var meta: String {
        var parts = [who(item.updatedBy, model), Date(timeIntervalSince1970: item.updatedAt).ago]
        if let s = item.size, item.isFile { parts.append(byteSize(s)) }
        return parts.joined(separator: " · ")
    }
}

// MARK: one item

/// A note (its Markdown) or a file (shown in place when it's a picture or text, else with Quick Look), with Share or
/// Save, Edit, Replace and Delete. Opening it marks it seen.
public struct SharedItemView: View {
    let id: Int
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var item: SharedItem?
    @State private var gone = false
    @State private var loadError: String?
    @State private var error: String?
    @State private var editing = false
    @State private var replacing = false
    @State private var confirmDelete = false
    @State private var busy: String?
    @State private var fileData: Data?
    @State private var lookURL: URL?
    #if os(iOS)
    @State private var shareFile: ShareFile?
    #endif
    @State private var subscription: UUID?

    public init(id: Int) { self.id = id }

    public var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                if let item {
                    header(item)
                    if item.isFile { fileBody(item) } else { MarkdownView(item.text ?? "").font(.body) }
                } else if gone {
                    ContentUnavailableView("This was deleted", systemImage: "trash")
                } else if let loadError {
                    ErrorText(text: loadError)
                } else {
                    ProgressView().frame(maxWidth: .infinity)
                }
            }
            .padding()
            .frame(maxWidth: 820, alignment: .leading)
            .frame(maxWidth: .infinity)
        }
        .background(Color.screen)
        .navigationTitle(item?.title ?? "Shared")
        .readableWidth()
        #if os(iOS)
        .navigationBarTitleDisplayMode(.inline)
        #endif
        .toolbar { if let item { toolbar(item) } }
        .task { await load() }
        .onAppear {
            subscription = model.subscribe { frame in
                guard case .shared(let c) = frame else { return }
                if c.deleted == id { gone = true; item = nil }
                if let i = c.item, i.id == id {
                    if i.isFile, let old = item, old.updatedAt != i.updatedAt { fileData = nil }
                    item = SharedItem(merging: i, into: item)
                }
            }
        }
        .onDisappear { if let s = subscription { model.unsubscribe(s) } }
        .sheet(isPresented: $editing) {
            if let item {
                SharedEditor(mode: .edit(item)) { self.item = $0 }
            }
        }
        .fileImporter(isPresented: $replacing, allowedContentTypes: [.item]) { r in
            if case .success(let url) = r { Task { await replace(url) } }
        }
        .confirmationDialog("Delete \(item?.title ?? "this")?", isPresented: $confirmDelete, titleVisibility: .visible) {
            Button("Delete", role: .destructive) { Task { await delete() } }
        } message: {
            Text(item?.agent.map { "For you and \(model.title(of: $0))." } ?? "It's gone for good.")
        }
        .quickLookPreview($lookURL)
        #if os(iOS)
        .sheet(item: $shareFile) { f in ActivitySheet(items: [f.url]).ignoresSafeArea() }
        #endif
        .errorAlert($error)
    }

    @ViewBuilder private func header(_ item: SharedItem) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Image(systemName: item.icon).foregroundStyle(item.tint)
                Text(item.title.isEmpty ? "Untitled" : item.title).font(.title2.bold()).textSelection(.enabled)
            }
            Group {
                Text("Added by \(who(item.addedBy, model)) \(Date(timeIntervalSince1970: item.createdAt).ago)")
                if item.updatedAt > item.createdAt + 1 {
                    Text("Changed by \(who(item.updatedBy, model)) \(Date(timeIntervalSince1970: item.updatedAt).ago)")
                }
                if item.isFile {
                    Text([item.mime, item.size.map(byteSize)].compactMap { $0 }.joined(separator: " · "))
                }
                Text(item.agent.map { "Between you and \(model.title(of: $0)) only" } ?? "No agent: only you see it")
            }
            .font(.caption)
            .foregroundStyle(.secondary)
            if let busy {
                HStack(spacing: 8) { ProgressView().controlSize(.small); Text(busy).font(.caption) }
            }
        }
    }

    // MARK: a file

    @ViewBuilder private func fileBody(_ item: SharedItem) -> some View {
        if let t = item.text, !t.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            MarkdownView(t).font(.callout)
        }
        if item.isImage || (item.isText && (item.size ?? 0) <= 512 * 1024) {
            Group {
                if let data = fileData {
                    if item.isImage, let img = PlatformImage(data: data) {
                        ImagePreview(image: img)
                    } else if let text = String(data: data, encoding: .utf8) {
                        if item.isMarkdown { MarkdownView(text) } else { CodeText(text: clip(text, 100_000)) }
                    } else {
                        Text("It can't be shown here.").foregroundStyle(.secondary)
                    }
                } else {
                    RoundedRectangle(cornerRadius: 10).fill(.fill.quaternary).frame(height: 160)
                        .overlay(ProgressView())
                }
            }
            .task(id: item.updatedAt) {
                if fileData == nil { fileData = try? await model.api.sharedFile(id) }
            }
            .accessibilityIdentifier("shared-preview")
        }
        HStack(spacing: 10) {
            if !item.isImage {
                Button { Task { await look(item) } } label: { Label("Quick Look", systemImage: "eye") }
                    .buttonStyle(.bordered)
            }
            #if os(iOS)
            Button { Task { await share(item) } } label: { Label("Share or save", systemImage: "square.and.arrow.up") }
                .buttonStyle(.borderedProminent)
                .accessibilityIdentifier("shared-share")
            #else
            Button { Task { await save(item) } } label: { Label("Save…", systemImage: "square.and.arrow.down") }
                .buttonStyle(.borderedProminent)
                .accessibilityIdentifier("shared-save")
            Button { Task { await open(item) } } label: { Label("Open", systemImage: "arrow.up.forward.app") }
                .buttonStyle(.bordered)
            #endif
        }
        .disabled(busy != nil)
    }

    @ToolbarContentBuilder private func toolbar(_ item: SharedItem) -> some ToolbarContent {
        ToolbarItemGroup(placement: .primaryAction) {
            if item.kind == .note {
                ShareLink(item: (item.title.isEmpty ? "" : "# \(item.title)\n\n") + (item.text ?? "")) {
                    Label("Share", systemImage: "square.and.arrow.up")
                }
            }
            Menu {
                Button { editing = true } label: {
                    Label(item.isFile ? "Rename or describe" : "Edit", systemImage: "pencil")
                }
                .accessibilityIdentifier("shared-edit")
                if item.isFile {
                    Button { replacing = true } label: { Label("Replace the file", systemImage: "arrow.triangle.2.circlepath") }
                }
                if item.agent == nil, !model.agentList.isEmpty {
                    Menu {
                        ForEach(model.agentList) { a in
                            Button(a.title) { Task { await move(to: a.name) } }
                        }
                    } label: { Label("Move to an agent", systemImage: "arrow.right.circle") }
                }
                Divider()
                Button(role: .destructive) { confirmDelete = true } label: { Label("Delete", systemImage: "trash") }
            } label: {
                Label("More", systemImage: "ellipsis.circle")
            }
            .accessibilityIdentifier("shared-more")
        }
    }

    // MARK: loading and actions

    private func load() async {
        do {
            let i = try await model.api.sharedItem(id)
            item = i
            if i.unseen {
                model.setSharedCounts(try await model.api.markSeen([id]))
                item?.unseen = false
            }
        } catch let e as APIError where e.status == 404 {
            gone = true
        } catch is CancellationError {
        } catch {
            loadError = error.localizedDescription
        }
    }

    /// An unassigned item into an agent's space: only that agent sees it then, and the owner.
    private func move(to agent: String) async {
        do { item = SharedItem(merging: try await model.api.moveShared(id, to: agent), into: item) } catch {
            self.error = error.localizedDescription
        }
    }

    /// The file in a temporary folder of its own, under its own name (for Quick Look, sharing, saving).
    private func download(_ item: SharedItem) async -> URL? {
        busy = "Downloading…"
        defer { busy = nil }
        do {
            let data: Data
            if let d = fileData { data = d } else { data = try await model.api.sharedFile(id) }
            return try FileStash.write(data, name: item.title, id: item.id)
        } catch {
            self.error = error.localizedDescription
            return nil
        }
    }

    private func look(_ item: SharedItem) async { lookURL = await download(item) }

    #if os(iOS)
    private func share(_ item: SharedItem) async { shareFile = await download(item).map(ShareFile.init) }
    #else
    private func save(_ item: SharedItem) async {
        guard let url = await download(item) else { return }
        let panel = NSSavePanel()
        panel.nameFieldStringValue = url.lastPathComponent
        panel.canCreateDirectories = true
        guard panel.runModal() == .OK, let dest = panel.url else { return }
        do {
            if FileManager.default.fileExists(atPath: dest.path) { try FileManager.default.removeItem(at: dest) }
            try FileManager.default.copyItem(at: url, to: dest)
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func open(_ item: SharedItem) async {
        guard let url = await download(item) else { return }
        NSWorkspace.shared.open(url)
    }
    #endif

    private func replace(_ url: URL) async {
        busy = "Uploading…"
        defer { busy = nil }
        do {
            let data = try PickedFile.read(url)
            let updated = try await model.api.replaceSharedFile(id, data: data, name: url.lastPathComponent)
            fileData = nil
            item = SharedItem(merging: updated, into: item)
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func delete() async {
        do {
            try await model.api.deleteShared(id)
            gone = true
            dismiss()
        } catch {
            self.error = error.localizedDescription
        }
    }
}

extension SharedItem {
    /// A frame's or a write's item over the one shown: lists and frames may leave `text` out.
    init(merging new: SharedItem, into old: SharedItem?) {
        self = new
        if text == nil { text = old?.text }
    }
}

/// A picture, fitted, tap to enlarge.
struct ImagePreview: View {
    let image: PlatformImage
    @State private var enlarged = false

    var body: some View {
        Button { enlarged = true } label: {
            Image(platform: image).resizable().aspectRatio(contentMode: .fit)
                .frame(maxWidth: .infinity, maxHeight: 520)
                .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
        }
        .buttonStyle(.plain)
        .accessibilityLabel("The picture")
        .sheet(isPresented: $enlarged) { EnlargedImage(image: image) }
    }
}

/// Downloads kept for Quick Look and the share sheet: `tmp/shared/<id>/<name>` (an agent's file:
/// `tmp/computer/<agent>/<name>`), replaced on the next download.
enum FileStash {
    static func write(_ data: Data, name: String, id: Int) throws -> URL {
        try write(data, name: name, folder: "shared/\(id)", fallback: "file-\(id)")
    }

    static func write(_ data: Data, name: String, folder: String, fallback: String = "file") throws -> URL {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(folder, isDirectory: true)
        try? FileManager.default.removeItem(at: dir)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let url = dir.appendingPathComponent(safeName(name, fallback: fallback))
        try data.write(to: url, options: .atomic)
        return url
    }

    static func safeName(_ name: String, fallback: String) -> String {
        let s = name.replacingOccurrences(of: "/", with: "-").replacingOccurrences(of: ":", with: "-")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return s.isEmpty || s.hasPrefix(".") ? fallback : s
    }
}

#if os(iOS)
/// A downloaded file for the share sheet.
struct ShareFile: Identifiable {
    let url: URL
    var id: URL { url }
}

/// The system share sheet (save to Files, AirDrop, another app).
struct ActivitySheet: UIViewControllerRepresentable {
    let items: [Any]

    func makeUIViewController(context: Context) -> UIActivityViewController {
        UIActivityViewController(activityItems: items, applicationActivities: nil)
    }

    func updateUIViewController(_ vc: UIActivityViewController, context: Context) {}
}
#endif

// MARK: writing

/// A new note, or an item's edit: a note's title and Markdown, a file's name and description.
struct SharedEditor: View {
    enum Mode {
        case newNote(String)            // in this agent's space
        case edit(SharedItem)
    }

    let mode: Mode
    let done: (SharedItem) -> Void
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var title = ""
    @State private var text = ""
    @State private var saving = false
    @State private var error: String?
    @State private var confirmDiscard = false

    /// Something typed that Save hasn't kept: Cancel and a swipe down ask first.
    private var dirty: Bool {
        guard !saving else { return false }
        if let i = editing { return title != i.title || text != (i.text ?? "") }
        return !title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            || !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    private var editing: SharedItem? { if case .edit(let i) = mode { return i } else { return nil } }
    private var isFile: Bool { editing?.isFile ?? false }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField(isFile ? "Name" : "Title (else the first line)", text: $title)
                        .accessibilityIdentifier("shared-title")
                }
                Section(isFile ? "Description" : "Text (Markdown)") {
                    TextEditor(text: $text)
                        .font(.body.monospaced())
                        .frame(minHeight: isFile ? 120 : 260)
                        .accessibilityIdentifier("shared-text")
                }
                if let error { Section { ErrorText(text: error) } }
            }
            .formStyle(.grouped)
            .navigationTitle(editing == nil ? "New note" : isFile ? "Edit file" : "Edit note")
            .interactiveDismissDisabled(dirty)
            #if os(iOS)
            .navigationBarTitleDisplayMode(.inline)
            #endif
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { if dirty { confirmDiscard = true } else { dismiss() } }
                        .confirmationDialog("Discard changes?", isPresented: $confirmDiscard, titleVisibility: .visible) {
                            Button("Discard", role: .destructive) { dismiss() }
                            Button("Keep editing", role: .cancel) {}
                        }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button { Task { await save() } } label: {
                        if saving { ProgressView().controlSize(.small) } else { Text("Save") }
                    }
                    .disabled(saving || (!isFile && text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                              || (isFile && title.trimmingCharacters(in: .whitespaces).isEmpty))
                    .accessibilityIdentifier("shared-save-note")
                }
            }
        }
        .onAppear {
            if let i = editing {
                title = i.title
                text = i.text ?? ""
            }
        }
        #if os(macOS)
        .frame(minWidth: 520, minHeight: 460)
        #endif
    }

    private func save() async {
        saving = true
        error = nil
        defer { saving = false }
        do {
            let item: SharedItem
            if let i = editing {
                // Only what changed (the server refuses an edit with nothing in it); an emptied title stays.
                let t = title.trimmingCharacters(in: .whitespacesAndNewlines)
                let newTitle = t.isEmpty || t == i.title ? nil : t
                let newText = text == (i.text ?? "") ? nil : text
                if newTitle == nil && newText == nil { dismiss(); return }
                item = try await model.api.updateShared(i.id, title: newTitle, text: newText)
            } else {
                guard case .newNote(let agent) = mode else { return }
                item = try await model.api.addNote(agent: agent, title: title, text: text)
            }
            done(item)
            dismiss()
        } catch {
            self.error = error.localizedDescription
        }
    }
}
