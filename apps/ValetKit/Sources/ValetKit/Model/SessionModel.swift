import Foundation
import Observation

/// A prompt the server queued because a turn was running (M4: the prompt route answers `{"queued": id}`, and the
/// Session shape lists its `queue`). It shows until its turn starts.
public struct QueuedPrompt: Identifiable, Hashable, Sendable {
    public var id: String
    public var text: String
    /// Who sent it, when not the owner ("Schedule", "From scout").
    public var source: String?
    /// The server lists it in the session's queue, so it can be taken out.
    public var removable = false
    /// What goes with it (the owner's attachments).
    public var attachments: [Item] = []
    /// The folder's cursor when it was queued here: its user_message comes after.
    var after: Int = 0

    /// An attachment that goes with it: its name, and whether it's a picture.
    public struct Item: Hashable, Sendable {
        public var name: String
        public var image: Bool
    }
}

/// A file on its way to the next message: uploading, sent up (its attachment), or failed (and why).
public struct DraftAttachment: Identifiable, Sendable {
    public enum State: Sendable {
        case uploading
        case ready(Attachment)
        case failed(String)
    }

    public let id: UUID
    public var name: String
    public var size: Int?
    /// A small PNG of a picture, for its chip.
    public var thumbnail: Data?
    public var state: State

    public var attachment: Attachment? { if case .ready(let a) = state { a } else { nil } }
    public var isUploading: Bool { if case .uploading = state { true } else { false } }
    public var failure: String? { if case .failed(let why) = state { why } else { nil } }
}

/// One open session: its history from the events route, then its live events from the socket, folded into its
/// conversation. Live events that arrive while history is being fetched wait their turn, so nothing applies out
/// of order; after every reconnect (a hello) it fetches what came after the last event it applied. History is
/// folded off the main actor. (app.js's SessionView.)
@MainActor @Observable
public final class SessionModel {
    public let agent: String
    public let id: String
    public private(set) var folder = EventFolder()
    public private(set) var loaded = false
    /// The session doesn't exist (404).
    public private(set) var missing = false
    public var error: String?
    /// Prompts this screen sent that the server queued (for a server whose Session shape has no `queue`).
    private var sentQueued: [QueuedPrompt] = []
    public private(set) var sending = false
    public private(set) var cancelling = false
    /// What goes with the next message (the composer's chips).
    public private(set) var drafts: [DraftAttachment] = []
    /// The server's limit per message.
    public static let maxAttachments = 10
    @ObservationIgnored private var dropped: Set<UUID> = []
    /// Grows with every change to the conversation (the view follows it to the end).
    public private(set) var revision = 0

    @ObservationIgnored private let model: AppModel
    @ObservationIgnored private var subscription: UUID?
    @ObservationIgnored private var syncing = false
    @ObservationIgnored private var again = false
    @ObservationIgnored private var waiting: [Event] = []
    @ObservationIgnored private var active = false

    public init(agent: String, id: String, model: AppModel) {
        self.agent = agent
        self.id = id
        self.model = model
    }

    public var session: Session? { model.sessions[agent]?[id] }
    public var title: String { session?.title.flatMap { $0.isEmpty ? nil : $0 } ?? folder.title ?? "New session" }
    public var isWorking: Bool { session?.isWorking ?? false }

    /// What waits for the running turn to end: the session's own queue when the server lists it (prompts from
    /// schedules, webhooks and other agents too), else the prompts sent from here.
    public var queued: [QueuedPrompt] {
        guard let list = session?.extra["queue"]?.array else { return sentQueued }
        return list.compactMap { v in
            guard let id = v["id"]?.text, let text = v["text"]?.string else { return nil }
            let items = (v["attachments"]?.array ?? []).compactMap { a in
                a["name"]?.string.map { QueuedPrompt.Item(name: $0, image: a["image"]?.bool ?? false) }
            }
            return QueuedPrompt(id: id, text: text,
                                source: Event.sourceLabel(v["source"], label: v["label"]?.string,
                                                          retry: !(v["retry_of"]?.isNull ?? true)),
                                removable: true, attachments: items)
        }
    }

    public func start() {
        guard !active else { return }
        active = true
        subscription = model.subscribe { [weak self] frame in self?.handle(frame) }
        Task {
            await refreshSession()
            await sync()
        }
    }

    public func stop() {
        active = false
        if let s = subscription { model.unsubscribe(s) }
        subscription = nil
    }

    func handle(_ frame: Frame) {
        switch frame {
        case .event(let e) where e.agent == agent && e.sessionId == id:
            if syncing { waiting.append(e) } else { applyLive([e]) }
        case .hello:
            Task {
                await refreshSession()
                await sync()
            }
        default: break
        }
    }

    /// Fetches events after the cursor until there are no more, retrying with backoff; then the live events
    /// that waited. A sync asked for while one runs makes that one go round again.
    public func sync() async {
        if syncing { again = true; return }
        syncing = true
        var delay = 1.0, failed = false
        while true {
            do {
                repeat {
                    again = false
                    var more = true
                    while more {
                        let page = try await model.api.events(agent, id, after: folder.cursor, limit: 500)
                        await applyHistory(page.events)
                        more = page.more && !page.events.isEmpty
                    }
                } while again
                break
            } catch {
                let status = (error as? APIError)?.status
                if status == 401 || status == 404 || error is CancellationError || !active {
                    // Signed out or gone: drop what waited; the next sync fetches it again.
                    syncing = false
                    waiting = []
                    if status == 404 { missing = true }
                    loaded = true
                    return
                }
                failed = true
                self.error = error.localizedDescription
                try? await Task.sleep(for: .seconds(delay))
                delay = min(delay * 2, 30)
                if !active { syncing = false; waiting = []; return }
            }
        }
        syncing = false
        if failed { error = nil }
        loaded = true
        let live = waiting
        waiting = []
        applyLive(live)
    }

    private func applyHistory(_ events: [Event]) async {
        guard !events.isEmpty else { return }
        let before = folder
        folder = await Task.detached { var f = before; f.apply(events); return f }.value
        dequeue(events)
        revision += 1
    }

    private func applyLive(_ events: [Event]) {
        guard !events.isEmpty else { return }
        folder.apply(events)
        dequeue(events)
        revision += 1
    }

    private func dequeue(_ events: [Event]) {
        guard !sentQueued.isEmpty else { return }
        for e in events where e.kind == "user_message" {
            let text = blockText(e.data["content"])
            if let i = sentQueued.firstIndex(where: { $0.text == text && e.id > $0.after }) { sentQueued.remove(at: i) }
        }
    }

    public func refreshSession() async {
        do {
            model.put(try await model.api.session(agent, id))
        } catch let e as APIError where e.status == 404 {
            missing = true
        } catch {}
    }

    /// Whether there's something to send: text, or attachments.
    public func canSend(_ text: String) -> Bool {
        !sending && (!text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !drafts.isEmpty)
            && !drafts.contains(where: \.isUploading)
    }

    /// Sends a prompt with the attachments. True when it's on its way (or recorded with the turn's error), so the
    /// composer can clear; false when the text and the attachments should stay to be sent again (nothing was
    /// recorded: `"sent": false`, or an attachment that didn't upload).
    public func send(_ text: String) async -> Bool {
        guard canSend(text) else { return false }
        if drafts.contains(where: { $0.failure != nil }) {
            error = "An attachment didn't upload: take it off, or attach it again."
            return false
        }
        let carried = drafts.compactMap(\.attachment)
        sending = true
        error = nil
        defer { sending = false }
        do {
            let r = try await model.api.prompt(agent, id, text: text, attachments: carried.map(\.id))
            if let q = r.queued {
                sentQueued.append(QueuedPrompt(id: q, text: text,
                                               attachments: carried.map { .init(name: $0.name, image: $0.image) },
                                               after: folder.cursor))
            }
            drafts = []
            await refreshSession()
            return true
        } catch let e as APIError {
            if e.body?["sent"] == .bool(false) {
                error = e.localizedDescription
                return false
            }
            switch e.status {
            case 409: error = "The agent is still working on the last message."; return false
            case 502, 503: error = e.localizedDescription; drafts = []; await refreshSession(); return true
            default: error = e.localizedDescription; return false
            }
        } catch {
            return false
        }
    }

    // MARK: attachments

    /// Uploads files for the next message, one at a time; each shows as a chip at once. At most 10 on a message.
    public func attach(_ items: [UploadSource]) async {
        error = nil
        let room = Self.maxAttachments - drafts.count
        if items.count > room { error = "At most \(Self.maxAttachments) attachments on a message." }
        let taken = Array(items.prefix(max(0, room)))
        let ids = taken.map { item -> UUID in
            let d = DraftAttachment(id: UUID(), name: item.name, state: .uploading)
            drafts.append(d)
            return d.id
        }
        for (item, id) in zip(taken, ids) {
            guard !dropped.contains(id) else { continue }
            do {
                let data = try await item.data()
                let thumb = await Task.detached { Thumbnail.png(data) }.value
                update(id) {
                    $0.size = data.count
                    $0.thumbnail = thumb
                }
                let a = try await model.api.uploadAttachment(agent, name: item.name, data: data)
                if dropped.remove(id) != nil {
                    try? await model.api.deleteAttachment(agent, id: a.id)     // taken off while it uploaded
                    continue
                }
                update(id) { $0.state = .ready(a) }
            } catch {
                update(id) { $0.state = .failed(error.localizedDescription) }
            }
        }
    }

    /// Takes one off the message (an uploaded one is deleted on the server; one still uploading, once it's up).
    public func detach(_ id: UUID) {
        guard let i = drafts.firstIndex(where: { $0.id == id }) else { return }
        let d = drafts.remove(at: i)
        if let a = d.attachment {
            Task { try? await model.api.deleteAttachment(agent, id: a.id) }
        } else if d.isUploading {
            dropped.insert(id)
        }
        if drafts.isEmpty { error = nil }
    }

    private func update(_ id: UUID, _ change: (inout DraftAttachment) -> Void) {
        guard let i = drafts.firstIndex(where: { $0.id == id }) else { return }
        change(&drafts[i])
    }

    /// Sends an interrupted or failed turn's prompt again. Nil when it's on its way (its Retry goes once the new
    /// prompt arrives), else why not.
    public func retry(_ turn: String) async -> String? {
        do {
            let r = try await model.api.retryTurn(agent, id, turn: turn)
            if r.queued != nil || r.turnId != nil { await refreshSession() }
            return nil
        } catch let e as APIError {
            switch e.status {
            case 409: return "It was sent again already, or there's nothing to send."
            case 503: return "The agent isn't running: start it, then retry."
            default: return e.localizedDescription
            }
        } catch {
            return error.localizedDescription
        }
    }

    /// Takes a prompt out of the queue (gone already: the session's next frame says where it went).
    public func remove(_ q: QueuedPrompt) async {
        do {
            try await model.api.removeQueued(agent, id, id: q.id)
        } catch let e as APIError where e.status == 404 {
        } catch {
            self.error = error.localizedDescription
        }
        await refreshSession()
    }

    public func cancel() async {
        cancelling = true
        defer { cancelling = false }
        do {
            try await model.api.cancel(agent, id)
        } catch let e as APIError where e.status == 409 {
        } catch {
            self.error = error.localizedDescription
        }
    }
}
