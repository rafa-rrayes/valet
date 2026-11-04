import PhotosUI
import QuickLook
import SwiftUI
import UniformTypeIdentifiers

/// An agent's files while it runs (a container's /work, /home/agent and shared folders; a host agent's working
/// folder): one screen per folder or file, Back is up a folder. The first screen lists the places when there's more
/// than one (and a container agent's volumes); with one, it's that folder. Where a screen is shows as crumbs under
/// its title. A folder lists its entries, takes uploads (Files, Photos, drops) and new folders unless it's read-only;
/// a file shows its text or picture, with Quick Look and Share (iOS) or Save… and Open (Mac). The web UI's files.js.
public struct FilesView: View {
    let ref: FilesRef
    @Environment(AppModel.self) private var model
    @Environment(\.popScreens) private var popScreens
    @State private var files: FilesModel?
    @State private var actions = AgentActions()
    @State private var importing = false
    @State private var choosingPhotos = false
    @State private var photos: [PhotosPickerItem] = []
    @State private var naming = false
    @State private var newName = ""
    @State private var confirmVolumes = false
    @State private var lookURL: URL?
    #if os(iOS)
    @State private var shareFile: ShareFile?
    #endif
    @State private var error: String?

    public init(ref: FilesRef) { self.ref = ref }

    private var agent: Agent? { model.agents[ref.agent] }
    private var up: Bool { agent?.computerUp ?? false }
    private var isFirst: Bool { ref.path == nil }
    private var isContainer: Bool { agent?.level == "container" }
    /// It runs (or is about to): its volumes can't go.
    private var running: Bool { agent?.state == "running" || agent?.state == "starting" }

    /// Delete volumes can't be pressed: it runs (stop it first), or they're being deleted.
    private func volumesLocked(_ files: FilesModel) -> Bool { running || files.deletingVolumes }

    public var body: some View {
        content
            .navigationTitle(title)
            #if os(iOS)
            .navigationBarTitleDisplayMode(.inline)
            #endif
            .toolbar { toolbar }
            .onAppear { ensure() }
            .onDisappear { files?.choose(nil) }
            .task(id: LoadKey(up: up, hellos: model.hellos)) {
                ensure()
                if up { await files?.load() }
            }
            .task(id: VolumesKey(state: agent?.state, hellos: model.hellos)) {
                ensure()
                if isFirst && isContainer { await files?.loadVolumes() }
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
            .alert("New folder", isPresented: $naming) {
                TextField("Name", text: $newName)
                    .autocorrectionDisabled()
                    #if os(iOS)
                    .textInputAutocapitalization(.never)
                    #endif
                    .accessibilityIdentifier("files-new-name")
                Button("Cancel", role: .cancel) { newName = "" }
                Button("Create") {
                    let n = newName
                    newName = ""
                    Task { await files?.makeFolder(n) }
                }
            } message: {
                Text("In \(files?.listing.map { FilePaths.name($0.path) } ?? "this folder").")
            }
            // An alert, not a dialog: nothing on the screen asked (an upload found the name taken).
            .alert("\(files?.clash?.name ?? "It") is here already.", isPresented: clashShown,
                   presenting: files?.clash) { c in
                Button("Replace", role: .destructive) { files?.choose(.replace, ask: c.id) }
                Button("Keep both") { files?.choose(.rename, ask: c.id) }
                Button("Cancel", role: .cancel) { files?.choose(nil, ask: c.id) }
            } message: { _ in
                Text("Keep both gives the new one another name.")
            }
            .quickLookPreview($lookURL)
            #if os(iOS)
            .sheet(item: $shareFile) { f in ActivitySheet(items: [f.url]).ignoresSafeArea() }
            #endif
            .errorAlert($error)
            .agentActions(actions)
    }

    private struct LoadKey: Equatable { let up: Bool; let hellos: Int }
    private struct VolumesKey: Equatable { let state: String?; let hellos: Int }

    private func ensure() {
        if files == nil { files = FilesModel(agent: ref.agent, path: ref.path, api: model.api) }
    }

    /// The dialog about a taken name: gone without an answer (tapped outside) is Cancel, for that question only.
    private var clashShown: Binding<Bool> {
        Binding(get: { files?.clash != nil }, set: { shown in
            guard !shown, let id = files?.clash?.id else { return }
            DispatchQueue.main.async { files?.choose(nil, ask: id) }
        })
    }

    private var title: String {
        guard let p = ref.path else { return "Files" }
        if let l = files?.listing, let root = l.root, root.path == l.path { return root.label }
        return FilePaths.name(p)
    }

    /// This screen in the trail of the ones it opens.
    private var here: String {
        guard let l = files?.listing else { return ref.path ?? FilesRef.places }
        return showsPlaces(l) ? FilesRef.places : l.path
    }

    private func showsPlaces(_ l: FileListing) -> Bool { isFirst && l.roots.count >= 2 }

    // MARK: the screen

    @ViewBuilder private var content: some View {
        if let a = agent {
            if !a.computerUp {
                List {
                    Section {
                        ComputerNotice(agent: a, action: "look at its files", actions: actions)
                            .listRowInsets(EdgeInsets())
                            .listRowBackground(Color.clear)
                    }
                    if isFirst && isContainer, let files { volumesSection(files) }
                }
            } else if let files, let l = files.listing {
                if showsPlaces(l) {
                    places(files, l)
                } else if l.type == .dir {
                    folder(files, l)
                } else if l.type == .file {
                    file(files, l)
                } else {
                    List { Section { Text("Not a file or folder.").foregroundStyle(.secondary) } header: { crumbs(l) } }
                }
            } else if let e = files?.loadError {
                List { Section { ErrorText(text: e).accessibilityIdentifier("files-error") } }
                    .refreshable { await files?.load() }
            } else {
                ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        } else {
            ContentUnavailableView("There's no agent named \(ref.agent)", systemImage: "questionmark.circle")
        }
    }

    private func places(_ files: FilesModel, _ l: FileListing) -> some View {
        List {
            Section("Places") {
                ForEach(l.roots, id: \.path) { r in
                    NavigationLink(value: FilesRef(ref.agent, path: r.path, trail: ref.trail + [FilesRef.places])) {
                        placeRow(r)
                    }
                    .accessibilityIdentifier("files-place-\(r.label)")
                }
            }
            if isContainer { volumesSection(files) }
        }
        .refreshable {
            await files.load()
            if isContainer { await files.loadVolumes() }
        }
    }

    private func placeRow(_ r: FileRoot) -> some View {
        HStack(spacing: 12) {
            Image(systemName: r.path == "/home/agent" ? "house" : r.path == "/work" ? "folder" : "folder.badge.person.crop")
                .foregroundStyle(Color.accentColor)
                .frame(width: 26)
            VStack(alignment: .leading, spacing: 2) {
                Text(r.label)
                Text(r.path).font(.caption.monospaced()).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
            }
            Spacer()
            if !r.writable { Text("Read-only").font(.caption).foregroundStyle(.secondary) }
        }
    }

    // MARK: a folder

    private func folder(_ files: FilesModel, _ l: FileListing) -> some View {
        List {
            if files.busy != nil || files.message != nil {
                Section {
                    if let b = files.busy {
                        HStack(spacing: 10) {
                            ProgressView().controlSize(.small)
                            Text(b).foregroundStyle(.secondary)
                        }
                        .accessibilityIdentifier("files-busy")
                    }
                    if let m = files.message { ErrorText(text: m).accessibilityIdentifier("files-message") }
                }
            }
            Section {
                ForEach(l.entries) { e in entryRow(l, e) }
                if l.entries.isEmpty {
                    Text("Nothing here yet.").foregroundStyle(.secondary).accessibilityIdentifier("files-empty")
                }
            } header: {
                crumbs(l)
            } footer: {
                if l.truncated { Text("Showing the first \(l.entries.count).") }
            }
        }
        .refreshable { await files.load() }
        .onDrop(of: PickedFile.dropTypes, isTargeted: nil) { providers in
            guard l.writable, files.busy == nil else { return false }
            Task { upload(await PickedFile.sources(dropped: providers)) }
            return true
        }
    }

    @ViewBuilder private func entryRow(_ l: FileListing, _ e: FileEntry) -> some View {
        if e.type == .other {
            entryLabel(e).foregroundStyle(.secondary).accessibilityIdentifier("file-\(e.name)")
        } else {
            NavigationLink(value: FilesRef(ref.agent, path: FilePaths.join(l.path, e.name), trail: ref.trail + [here])) {
                entryLabel(e)
            }
            .accessibilityIdentifier("file-\(e.name)")
        }
    }

    private func entryLabel(_ e: FileEntry) -> some View {
        HStack(spacing: 12) {
            Image(systemName: e.type == .dir ? "folder.fill" : e.type == .other ? "questionmark.square.dashed"
                  : FilePaths.isImage(e.name) ? "photo" : "doc")
                .foregroundStyle(e.type == .dir ? Color.accentColor : Color.secondary)
                .frame(width: 26)
            VStack(alignment: .leading, spacing: 2) {
                Text(e.name).lineLimit(1).truncationMode(.middle)
                Text(entrySub(e)).font(.caption).foregroundStyle(.secondary).lineLimit(1)
            }
        }
    }

    private func entrySub(_ e: FileEntry) -> String {
        var parts: [String] = []
        switch e.type {
        case .dir: parts.append("Folder")
        case .other: parts.append(e.link ? "A link that leads nowhere it may go" : "Not a file or folder")
        default: parts.append(byteSize(e.size))
        }
        if e.link && e.type != .other { parts.append("link") }
        if e.mtime > 0 { parts.append(Date(timeIntervalSince1970: e.mtime).ago) }
        return parts.joined(separator: " · ")
    }

    /// Where it is: its place, then each folder; an earlier screen of this stack is gone back to, another pushed.
    private func crumbs(_ l: FileListing) -> some View {
        let cs = l.crumbs
        return ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 4) {
                ForEach(Array(cs.enumerated()), id: \.offset) { i, c in
                    if i > 0 {
                        Image(systemName: "chevron.right").font(.caption2.weight(.semibold)).foregroundStyle(.tertiary)
                    }
                    if i == cs.count - 1 {
                        Text(c.label).fontWeight(.semibold).foregroundStyle(.primary)
                    } else if let at = ref.trail.lastIndex(of: c.path), let popScreens {
                        Button(c.label) { popScreens.pop(ref.trail.count - at) }.buttonStyle(.borderless)
                    } else {
                        NavigationLink(c.label, value: FilesRef(ref.agent, path: c.path, trail: ref.trail + [here]))
                            .buttonStyle(.borderless)
                    }
                }
                if l.type == .dir && !l.writable {
                    Label("Read-only", systemImage: "lock").labelStyle(.titleAndIcon).foregroundStyle(.secondary)
                        .padding(.leading, 8)
                }
            }
            .font(.footnote)
            .lineLimit(1)
        }
        .textCase(nil)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("files-crumbs")
    }

    // MARK: a file

    private func file(_ files: FilesModel, _ l: FileListing) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                crumbs(l)
                VStack(alignment: .leading, spacing: 4) {
                    Text([byteSize(l.size), l.mtime > 0 ? "changed " + Date(timeIntervalSince1970: l.mtime).ago : nil]
                        .compactMap { $0 }.joined(separator: " · "))
                        .font(.caption).foregroundStyle(.secondary)
                    if let b = files.busy {
                        HStack(spacing: 8) { ProgressView().controlSize(.small); Text(b).font(.caption) }
                    }
                }
                fileActions(files)
                filePreview(files, l)
            }
            .padding()
            .frame(maxWidth: 820, alignment: .leading)
            .frame(maxWidth: .infinity)
        }
        .background(Color.screen)
        .refreshable { await files.load() }
    }

    private func fileActions(_ files: FilesModel) -> some View {
        HStack(spacing: 10) {
            Button { Task { lookURL = await fetch(files) } } label: { Label("Quick Look", systemImage: "eye") }
                .buttonStyle(.bordered)
                .accessibilityIdentifier("files-look")
            #if os(iOS)
            Button {
                Task { shareFile = await fetch(files).map(ShareFile.init) }
            } label: { Label("Share or save", systemImage: "square.and.arrow.up") }
                .buttonStyle(.borderedProminent)
                .accessibilityIdentifier("files-share")
            #else
            Button { Task { await save(files) } } label: { Label("Save…", systemImage: "square.and.arrow.down") }
                .buttonStyle(.borderedProminent)
                .accessibilityIdentifier("files-save")
            Button {
                Task { if let url = await fetch(files) { NSWorkspace.shared.open(url) } }
            } label: { Label("Open", systemImage: "arrow.up.forward.app") }
                .buttonStyle(.bordered)
            #endif
        }
        .disabled(files.busy != nil)
    }

    @ViewBuilder private func filePreview(_ files: FilesModel, _ l: FileListing) -> some View {
        switch files.preview {
        case .image(let data):
            if let img = PlatformImage(data: data) {
                ImagePreview(image: img).accessibilityIdentifier("files-image")
            } else {
                Text("It can't be shown here.").foregroundStyle(.secondary)
            }
        case .text(let t):
            if t.binary || t.text == nil {
                Text("No preview: it isn't text.").foregroundStyle(.secondary)
            } else {
                let text = t.text ?? ""
                VStack(alignment: .leading, spacing: 8) {
                    CodeText(text: clip(text.hasSuffix("\n") ? String(text.dropLast()) : text, 100_000))
                        .accessibilityIdentifier("files-text")
                    if t.truncated || text.count > 100_000 {
                        Text("Showing the start of it: Quick Look or share it for the rest.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
        case .tooBig:
            Text("Too big to show here: Quick Look or share it.").foregroundStyle(.secondary)
        case .failed(let message):
            ErrorText(text: message)
        case nil:
            RoundedRectangle(cornerRadius: 10).fill(.fill.quaternary).frame(height: 160).overlay(ProgressView())
        }
    }

    private func fetch(_ files: FilesModel) async -> URL? {
        do {
            return try await files.download()
        } catch is CancellationError {
            return nil
        } catch {
            self.error = error.localizedDescription
            return nil
        }
    }

    #if os(macOS)
    private func save(_ files: FilesModel) async {
        guard let url = await fetch(files) else { return }
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
    #endif

    // MARK: volumes

    @ViewBuilder private func volumesSection(_ files: FilesModel) -> some View {
        Section {
            if let v = files.volumes {
                if v.volumes.isEmpty {
                    Text("It has none on its runner.").foregroundStyle(.secondary).accessibilityIdentifier("volumes-none")
                }
                ForEach(v.volumes, id: \.name) { vol in
                    VStack(alignment: .leading, spacing: 2) {
                        Text(vol.name).font(.callout.monospaced()).textSelection(.enabled)
                        Text(vol.mount).font(.caption).foregroundStyle(.secondary)
                    }
                    .accessibilityIdentifier("volume-\(vol.name)")
                }
                if !v.volumes.isEmpty {
                    Button(role: .destructive) {
                        confirmVolumes = true
                    } label: {
                        HStack {
                            #if os(iOS)
                            // The trash red too, not only the words (a form row's icon takes the app's tint).
                            Label {
                                Text("Delete volumes")
                            } icon: {
                                Image(systemName: "trash").foregroundStyle(.red.opacity(volumesLocked(files) ? 0.4 : 1))
                            }
                            #else
                            Label("Delete volumes", systemImage: "trash")
                            #endif
                            if files.deletingVolumes { Spacer(); ProgressView().controlSize(.small) }
                        }
                    }
                    .disabled(volumesLocked(files))
                    .accessibilityIdentifier("volumes-delete")
                    .confirmationDialog("Delete its volumes?", isPresented: $confirmVolumes, titleVisibility: .visible) {
                        Button("Delete for good", role: .destructive) { Task { await files.deleteVolumes() } }
                        Button("Cancel", role: .cancel) {}
                    } message: {
                        Text(files.deleteVolumesQuestion)
                    }
                }
            } else if files.volumesError == nil {
                ProgressView().frame(maxWidth: .infinity)
            }
            if let d = files.volumesDone { Text(d).foregroundStyle(.secondary) }
            if let e = files.volumesError { ErrorText(text: e) }
        } header: {
            Text("Volumes")
        } footer: {
            Text("Its home (/home/agent) and workspace (/work) are Docker volumes"
                 + (files.volumes?.runner.map { " on \($0)" } ?? "")
                 + ": they outlast restarts and stopping it."
                 + (running && !(files.volumes?.volumes.isEmpty ?? true) ? " Stop it to delete them." : ""))
        }
    }

    // MARK: adding

    @ToolbarContentBuilder private var toolbar: some ToolbarContent {
        if up, let files, let l = files.listing, l.type == .dir, !showsPlaces(l) {
            ToolbarItem(placement: .primaryAction) {
                Menu {
                    Button { importing = true } label: { Label("Upload files", systemImage: "doc.badge.plus") }
                        .accessibilityIdentifier("files-upload")
                    Button { choosingPhotos = true } label: {
                        Label("Upload from Photos", systemImage: "photo.on.rectangle")
                    }
                    .accessibilityIdentifier("files-photos")
                    Button { naming = true } label: { Label("New folder", systemImage: "folder.badge.plus") }
                        .accessibilityIdentifier("files-new-folder")
                } label: {
                    Label("Add", systemImage: "plus")
                }
                .disabled(!l.writable || files.busy != nil)
                .help(l.writable ? "Upload, or make a folder" : "Read-only")
                .accessibilityIdentifier("files-add")
            }
        }
    }

    private func upload(_ items: [UploadSource]) {
        guard let files else { return }
        Task { await files.upload(items) }
    }
}
