import PhotosUI
import SwiftUI
import UniformTypeIdentifiers

/// A session: the live conversation and the composer. Follows the end while the reader is there; "Jump to latest"
/// when they've scrolled up.
public struct SessionView: View {
    let agent: String
    let id: String
    @Environment(AppModel.self) private var model
    @State private var session: SessionModel?
    @State private var draft = ""
    @State private var atBottom = true
    @State private var actions = AgentActions()
    @FocusState private var composing: Bool
    @State private var importing = false
    @State private var choosingPhotos = false
    @State private var photos: [PhotosPickerItem] = []
    #if os(macOS)
    @State private var paste = PasteWatcher(orText: true)
    #endif

    public init(agent: String, id: String) {
        self.agent = agent
        self.id = id
    }

    public var body: some View {
        Group {
            if let session {
                content(session)
            } else {
                ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .navigationTitle(session?.title ?? "Session")
        #if os(iOS)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            // The agent's main session says so under its title: schedules, webhooks and wake-ups come here.
            if session?.session?.isMain == true {
                ToolbarItem(placement: .principal) {
                    VStack(spacing: 0) {
                        Text(session?.title ?? "Session").font(.headline).lineLimit(1)
                        Text("Main session").font(.caption2).foregroundStyle(.secondary)
                    }
                    .accessibilityElement(children: .combine)
                    .accessibilityIdentifier("main-session")
                }
            }
        }
        #else
        .navigationSubtitle(session?.session?.isMain == true ? "Main session" : "")
        #endif
        .background(Color.screen)
        .onAppear {
            if session == nil { session = SessionModel(agent: agent, id: id, model: model) }
            session?.start()
        }
        .onDisappear { session?.stop() }
        .agentActions(actions)
    }

    @ViewBuilder private func content(_ s: SessionModel) -> some View {
        if s.missing {
            ContentUnavailableView("No such session", systemImage: "questionmark.bubble")
        } else {
            VStack(spacing: 0) {
                notice
                conversation(s)
                composer(s)
            }
            // Attachments: the composer's paperclip (Files, Photos), drops on the session, ⌘V on the Mac.
            .fileImporter(isPresented: $importing, allowedContentTypes: [.item], allowsMultipleSelection: true) { r in
                if case .success(let urls) = r { attach(s, urls) }
            }
            .photosPicker(isPresented: $choosingPhotos, selection: $photos, maxSelectionCount: SessionModel.maxAttachments,
                          matching: .any(of: [.images, .videos]))
            .onChange(of: photos) { _, picked in
                guard !picked.isEmpty else { return }
                photos = []
                Task { await s.attach(PickedFile.sources(picked)) }
            }
            .onDrop(of: PickedFile.dropTypes, isTargeted: nil) { providers in
                Task { await s.attach(await PickedFile.sources(dropped: providers)) }
                return true
            }
            #if os(macOS)
            .background(WindowReader { paste.window = $0 })
            .onAppear { paste.start { sources in Task { await s.attach(sources) } } }
            .onDisappear { paste.stop() }
            .onChange(of: composing) { _, now in paste.enabled = now }
            #endif
        }
    }

    private func attach(_ s: SessionModel, _ urls: [URL]) {
        guard !urls.isEmpty else { return }
        Task { await s.attach(PickedFile.sources(urls)) }
    }

    /// The agent isn't running (or its runner is offline): say so, with Start.
    @ViewBuilder private var notice: some View {
        if let a = model.agents[agent], !(a.state == "running" && a.connected) {
            let what = a.desired == "running" && !a.connected ? "Its runner is offline."
                : a.state == "starting" ? "The agent is starting." : a.state == "failed" ? "The agent failed."
                : "The agent is stopped."
            VStack(alignment: .leading, spacing: 6) {
                ViewThatFits(in: .horizontal) {
                    HStack {
                        Text(what).font(.callout)
                        Spacer()
                        if a.state != "starting" { AgentButtons(agent: a, actions: actions, size: .small) }
                    }
                    VStack(alignment: .leading, spacing: 6) {
                        Text(what).font(.callout)
                        if a.state != "starting" { AgentButtons(agent: a, actions: actions, size: .small) }
                    }
                }
                if a.state == "failed", let e = a.error { Text(e).font(.caption).foregroundStyle(.red).lineLimit(3) }
                if let needs = a.needsLine {
                    HStack {
                        Text(needs).font(.caption).foregroundStyle(.orange)
                        Spacer()
                        Button("Set") { actions.offer(a.missingSecrets, for: agent) }
                            .buttonStyle(.bordered).controlSize(.small)
                            .accessibilityIdentifier("notice-set-secrets")
                    }
                }
            }
            .padding(.horizontal, 14).padding(.vertical, 8)
            .background(Color.orange.opacity(0.12))
        }
    }

    private func conversation(_ s: SessionModel) -> some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(spacing: 0) {
                LazyVStack(alignment: .leading, spacing: 14) {
                    if !s.loaded {
                        ProgressView().frame(maxWidth: .infinity).padding(.top, 40)
                    } else if s.folder.items.isEmpty && s.queued.isEmpty {
                        Text("Say something to start.")
                            .foregroundStyle(.secondary).frame(maxWidth: .infinity).padding(.top, 40)
                    }
                    ForEach(s.folder.items) { item in
                        Group {
                            if case .turnEnd(let e) = item, e.offersRetry {
                                RetryRow(entry: e, session: s)
                            } else {
                                ConversationRow(item: item)
                            }
                        }
                        .id(item.id)
                    }
                    ForEach(s.queued) { q in
                        QueuedRow(prompt: q, session: s)
                    }
                }
                .padding(.horizontal, 14)
                .padding(.top, 12)
                .frame(maxWidth: 820)
                .frame(maxWidth: .infinity)
                // Outside the lazy stack, so it always exists to scroll to.
                Color.clear.frame(height: 12).id(Self.bottom)
                }
                .background(GeometryReader { g in
                    let f = g.frame(in: .named(Self.space))
                    Color.clear.preference(key: ContentKey.self, value: ContentFrame(top: f.minY, bottom: f.maxY))
                })
            }
            .coordinateSpace(name: Self.space)
            .background(GeometryReader { g in Color.clear.preference(key: HeightKey.self, value: g.size.height) })
            // Whether the reader is at the end is decided only when they scroll (the content's top moves): content
            // growing below them, or the keyboard coming up, keeps them following it.
            .onPreferenceChange(ContentKey.self) { f in
                let scrolled = abs(f.top - content.top) > 0.5
                content = f
                if scrolled { updateAtBottom() }
            }
            .onPreferenceChange(HeightKey.self) { h in
                height = h
                if atBottom { follow(proxy) }
            }
            .defaultScrollAnchor(.bottom)
            .scrollDismissesKeyboard(.interactively)
            .onChange(of: s.revision) { _, _ in
                if atBottom { follow(proxy) }
            }
            .onChange(of: s.loaded) { _, loaded in
                if loaded { atBottom = true; follow(proxy) }
            }
            .onChange(of: s.isWorking) { _, _ in
                if atBottom { follow(proxy) }
            }
            .overlay(alignment: .bottomTrailing) {
                if !atBottom {
                    Button {
                        atBottom = true
                        withAnimation { proxy.scrollTo(Self.bottom, anchor: .bottom) }
                    } label: {
                        Label("Latest", systemImage: "arrow.down")
                            .font(.callout.weight(.medium))
                            .padding(.horizontal, 12).padding(.vertical, 7)
                            .background(.regularMaterial, in: Capsule())
                            .shadow(radius: 3, y: 1)
                    }
                    .buttonStyle(.plain)
                    .padding(.trailing, 14).padding(.bottom, 10)
                    .accessibilityLabel("Jump to latest")
                    .transition(.opacity)
                }
            }
        }
    }

    @State private var content = ContentFrame(top: 0, bottom: 0)
    @State private var height: CGFloat = 0

    private func updateAtBottom() {
        let now = content.bottom <= height + 80
        if now != atBottom { atBottom = now }
    }

    /// To the end, once what just changed is laid out.
    private func follow(_ proxy: ScrollViewProxy) {
        DispatchQueue.main.async { proxy.scrollTo(Self.bottom, anchor: .bottom) }
    }

    private func composer(_ s: SessionModel) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            if let e = s.error { ErrorText(text: e) }
            if s.isWorking {
                HStack {
                    ProgressView().controlSize(.small)
                    Text("Working…").font(.callout).foregroundStyle(.secondary)
                    Spacer()
                    Button {
                        Task { await s.cancel() }
                    } label: { Label("Cancel", systemImage: "stop.circle") }
                    .buttonStyle(.bordered).controlSize(.small)
                    .disabled(s.cancelling)
                    .accessibilityIdentifier("cancel-turn")
                }
            }
            if !s.drafts.isEmpty {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 8) {
                        ForEach(s.drafts) { d in AttachmentChip(draft: d) { s.detach(d.id) } }
                    }
                    .padding(.vertical, 2)
                }
                .accessibilityIdentifier("attachments")
            }
            HStack(alignment: .bottom, spacing: 8) {
                Menu {
                    Button { choosingPhotos = true } label: { Label("Photos", systemImage: "photo.on.rectangle") }
                        .accessibilityIdentifier("attach-photos")
                    Button { importing = true } label: { Label("Files", systemImage: "folder") }
                        .accessibilityIdentifier("attach-files")
                } label: {
                    Image(systemName: "paperclip")
                        .font(.system(size: 19, weight: .medium))
                        .frame(width: 32, height: 36)
                        .contentShape(Rectangle())
                }
                .menuStyle(.button)
                .buttonStyle(.plain)
                .foregroundStyle(.secondary)
                .disabled(s.drafts.count >= SessionModel.maxAttachments)
                .help("Attach photos or files")
                .accessibilityLabel("Attach")
                .accessibilityIdentifier("attach")
                TextField(s.isWorking ? "Message (sent when it's done)" : "Message", text: $draft, axis: .vertical)
                    .lineLimit(1...8)
                    .focused($composing)
                    .padding(.horizontal, 12).padding(.vertical, 8)
                    .background(Color.card, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
                    .overlay(RoundedRectangle(cornerRadius: 18, style: .continuous).strokeBorder(.separator))
                    .onSubmit { send(s) }
                    .accessibilityIdentifier("composer")
                Button { send(s) } label: {
                    Image(systemName: "arrow.up.circle.fill").font(.system(size: 30))
                }
                .buttonStyle(.plain)
                .foregroundStyle(canSend(s) ? Color.valet : Color.secondary)
                .disabled(!canSend(s))
                .keyboardShortcut(.return, modifiers: .command)
                .accessibilityLabel("Send")
                .accessibilityIdentifier("send")
            }
        }
        .padding(.horizontal, 12).padding(.vertical, 8)
        .background(.bar)
    }

    private func canSend(_ s: SessionModel) -> Bool { s.canSend(draft) }

    private func send(_ s: SessionModel) {
        let text = draft
        guard canSend(s) else { return }
        Task {
            if await s.send(text) {
                if draft == text { draft = "" }
            }
        }
    }

    static let bottom = "bottom"
    static let space = "conversation"
}

/// A turn that failed or was interrupted, with Retry: its prompt goes again as a new turn (gone once it has).
private struct RetryRow: View {
    let entry: TurnEndEntry
    let session: SessionModel
    @State private var busy = false
    /// Sent: it stays off until the new prompt arrives (it may wait in the queue) and the row goes.
    @State private var sent = false
    @State private var error: String?

    var body: some View {
        VStack(spacing: 6) {
            ConversationRow(item: .turnEnd(entry))
            Button {
                guard let turn = entry.turnId else { return }
                busy = true
                error = nil
                Task {
                    error = await session.retry(turn)
                    sent = error == nil
                    busy = false
                }
            } label: {
                if busy { ProgressView().controlSize(.small) } else { Label("Retry", systemImage: "arrow.clockwise") }
            }
            .buttonStyle(.bordered).controlSize(.small)
            .disabled(busy || sent)
            .accessibilityIdentifier("retry-turn")
            if let error { ErrorText(text: error) }
        }
        .frame(maxWidth: .infinity)
    }
}

/// A prompt waiting for the running turn, with where it came from; one the server lists can be taken out.
private struct QueuedRow: View {
    let prompt: QueuedPrompt
    let session: SessionModel
    @State private var removing = false

    var body: some View {
        VStack(alignment: .trailing, spacing: 3) {
            if !prompt.text.isEmpty {
                Text(prompt.text)
                    .padding(.horizontal, 12).padding(.vertical, 8)
                    .background(Color.ownerBubble.opacity(0.35), in: RoundedRectangle(cornerRadius: 16, style: .continuous))
            }
            if !prompt.attachments.isEmpty {
                HStack(spacing: 10) {
                    ForEach(Array(prompt.attachments.enumerated()), id: \.offset) { _, a in
                        Label(a.name, systemImage: a.image ? "photo" : "doc").lineLimit(1).truncationMode(.middle)
                    }
                }
                .font(.caption)
                .foregroundStyle(.secondary)
                .accessibilityIdentifier("queued-attachments")
            }
            HStack(spacing: 10) {
                Label(prompt.source.map { "Queued from \($0): sent when this turn ends" }
                      ?? "Queued: sent when this turn ends", systemImage: "clock")
                    .font(.caption).foregroundStyle(.secondary)
                if prompt.removable {
                    Button("Remove", role: .destructive) {
                        removing = true
                        Task {
                            await session.remove(prompt)
                            removing = false
                        }
                    }
                    .font(.caption)
                    .buttonStyle(.borderless).tint(.red)
                    .disabled(removing)
                    .accessibilityIdentifier("queue-remove")
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .trailing)
        .padding(.leading, 40)
    }
}

private struct ContentFrame: Equatable {
    var top: CGFloat
    var bottom: CGFloat
}

private struct ContentKey: PreferenceKey {
    static let defaultValue = ContentFrame(top: 0, bottom: 0)
    static func reduce(value: inout ContentFrame, nextValue: () -> ContentFrame) { value = nextValue() }
}

private struct HeightKey: PreferenceKey {
    static let defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = nextValue() }
}

/// A file on its way to the next message, above the text box: its picture or icon, its name, its size (or
/// "Uploading…", or why it failed), and × to take it off.
struct AttachmentChip: View {
    let draft: DraftAttachment
    let remove: () -> Void

    var body: some View {
        HStack(spacing: 8) {
            Group {
                if let t = draft.thumbnail, let img = PlatformImage(data: t) {
                    Image(platform: img).resizable().aspectRatio(contentMode: .fill)
                } else {
                    Image(systemName: draft.failure != nil ? "exclamationmark.triangle" : "doc")
                        .foregroundStyle(draft.failure != nil ? Color.red : Color.secondary)
                }
            }
            .frame(width: 34, height: 34)
            .background(.fill.quaternary)
            .clipShape(RoundedRectangle(cornerRadius: 7, style: .continuous))
            VStack(alignment: .leading, spacing: 1) {
                Text(draft.name).font(.caption.weight(.medium)).lineLimit(1).truncationMode(.middle)
                Group {
                    if draft.isUploading {
                        Text("Uploading…")
                    } else if let why = draft.failure {
                        Text(why).foregroundStyle(.red)
                    } else {
                        Text(draft.size.map(byteSize) ?? "")
                    }
                }
                .font(.caption2).foregroundStyle(.secondary).lineLimit(1)
            }
            .frame(maxWidth: 150, alignment: .leading)
            if draft.isUploading { ProgressView().controlSize(.small) }
            Button(action: remove) {
                Image(systemName: "xmark.circle.fill").font(.system(size: 17)).foregroundStyle(.secondary)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Remove \(draft.name)")
            .accessibilityIdentifier("attachment-remove")
        }
        .padding(5).padding(.trailing, 2)
        .background(Color.card, in: RoundedRectangle(cornerRadius: 11, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 11, style: .continuous)
            .strokeBorder(draft.failure != nil ? Color.red.opacity(0.6) : Color.secondary.opacity(0.25)))
        .accessibilityElement(children: .contain)
        .accessibilityValue(draft.isUploading ? "uploading" : draft.failure != nil ? "failed" : "ready")
        .accessibilityIdentifier("attachment-chip")
    }
}
