import Foundation

/// One agent's shared space (or the unassigned items: agent nil) as the list screen shows it: a page of items under
/// the filters, older pages on demand, and the `shared` frames applied while it's open (an item added or changed in
/// this space moves to the top if it passes the filters, one deleted or moved to another agent goes, seen ones lose
/// their dot).
@MainActor @Observable
public final class SharedList {
    /// Whose space: an agent's name, or nil for the unassigned items.
    public let agent: String?
    public var query = SharedQuery()
    public private(set) var items: [SharedItem] = []
    public private(set) var more = false
    public private(set) var loading = false
    public private(set) var loaded = false
    /// Files on their way up.
    public private(set) var uploading = 0
    public var error: String?
    /// Items that were unseen when shown: marked seen on the server at once (so the tab's badge goes), their
    /// dots kept until the screen is left (shared.js's seeShown).
    public private(set) var dots: Set<Int> = []
    /// The screen is on show (the scene active): only then does showing an item count as seeing it.
    public var visible = true {
        didSet { if visible && !oldValue { Task { await seeShown(items) } } }
    }

    @ObservationIgnored private let model: AppModel
    @ObservationIgnored private var subscription: UUID?
    /// Bumped by each load, so a slow answer for old filters doesn't land over a newer one.
    @ObservationIgnored private var generation = 0

    public init(model: AppModel, agent: String?) {
        self.model = model
        self.agent = agent
    }

    public func start() {
        guard subscription == nil else { return }
        subscription = model.subscribe { [weak self] frame in
            switch frame {
            case .shared(let c): self?.apply(c)
            case .hello: Task { await self?.reload() }
            default: break
            }
        }
    }

    public func stop() {
        if let s = subscription { model.unsubscribe(s) }
        subscription = nil
        dots = []
    }

    /// Whether an item shows its dot: unseen, or unseen when this visit showed it.
    public func showsDot(_ item: SharedItem) -> Bool { item.unseen || dots.contains(item.id) }

    /// Marks the unseen ones of `shown` seen, keeping their dots for this visit.
    func seeShown(_ shown: [SharedItem]) async {
        let ids = shown.filter(\.unseen).map(\.id)
        guard visible, !ids.isEmpty else { return }
        dots.formUnion(ids)
        if let n = try? await model.api.markSeen(ids) { model.setSharedCounts(n) }   // else the next look marks them
    }

    public func reload() async {
        generation += 1
        let g = generation
        loading = true
        defer { if g == generation { loading = false } }
        do {
            let page = try await model.api.shared(agent, query)
            guard g == generation else { return }
            items = page.items
            more = page.more
            loaded = true
            error = nil
            await seeShown(page.items)
        } catch is CancellationError {
        } catch let e as URLError where e.code == .cancelled {
        } catch {
            guard g == generation else { return }
            self.error = error.localizedDescription
        }
    }

    public func loadOlder() async {
        guard more, !loading, let last = items.last else { return }
        let g = generation
        loading = true
        defer { loading = false }
        do {
            let page = try await model.api.shared(agent, query, before: last.updatedAt)
            guard g == generation else { return }
            let have = Set(items.map(\.id))
            items += page.items.filter { !have.contains($0.id) }
            more = page.more
            await seeShown(page.items)
        } catch {
            self.error = error.localizedDescription
        }
    }

    /// Files the owner added (picked in Files or Photos, dropped, pasted), uploaded one after another, each shown as
    /// soon as it's in, into this agent's space (the unassigned list takes none). What failed, one line per file
    /// ("name: why"), or nil.
    @discardableResult
    public func upload(_ sources: [UploadSource]) async -> String? {
        guard let agent else { return sources.isEmpty ? nil : "Upload to an agent's Shared, on its page." }
        uploading += sources.count
        var failed: [String] = []
        for source in sources {
            defer { uploading -= 1 }
            do {
                let data = try await source.data()
                for item in try await model.api.upload(agent: agent, name: source.name, data: data) { put(item) }
            } catch {
                failed.append("\(source.name): \(error.localizedDescription)")
            }
        }
        return failed.isEmpty ? nil : failed.joined(separator: "\n")
    }

    /// An item the app changed itself (added, edited): shown at once, before its frame arrives.
    public func put(_ item: SharedItem) { apply(SharedChange(item: item)) }

    public func remove(_ id: Int) { items.removeAll { $0.id == id } }

    func apply(_ c: SharedChange) {
        if let id = c.deleted { items.removeAll { $0.id == id } }
        if c.seenAll {
            for i in items.indices { items[i].unseen = false }
        } else if let ids = c.seen {
            let set = Set(ids)
            for i in items.indices where set.contains(items[i].id) { items[i].unseen = false }
        }
        if let item = c.item {
            items.removeAll { $0.id == item.id }
            if item.agent == agent && Self.matches(item, query) {
                let at = items.firstIndex { $0.updatedAt < item.updatedAt } ?? items.endIndex
                // Past the last one loaded, an older page will bring it.
                if at < items.endIndex || !more { items.insert(item, at: at) }
                if item.unseen, subscription != nil { Task { await seeShown([item]) } }
            }
        }
    }

    /// Whether an item passes the filters, as the server's list would decide (space.py: who added it, its kind,
    /// the words in its title or text, ASCII case ignored).
    static func matches(_ item: SharedItem, _ q: SharedQuery) -> Bool {
        if !q.kind.isEmpty && item.kind.rawValue != q.kind { return false }
        switch q.by {
        case "": break
        case "owner": if item.addedBy != "owner" { return false }
        case "agents": if item.addedBy == "owner" { return false }
        default: if item.addedBy != q.by { return false }
        }
        let words = q.q.trimmingCharacters(in: .whitespaces)
        if !words.isEmpty {
            let hay = [item.title, item.text ?? item.preview].joined(separator: "\n")
            if hay.range(of: words, options: .caseInsensitive) == nil { return false }
        }
        return true
    }
}
