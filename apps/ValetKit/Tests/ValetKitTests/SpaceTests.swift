import Foundation
import Testing
@testable import ValetKit

@MainActor
@Suite("The shared space and memory")
struct SpaceTests {
    nonisolated static let note = #"""
    {"id": 7, "agent": "scout", "kind": "note", "title": "Groceries", "mime": null, "size": null, "added_by": "scout",
     "updated_by": "owner", "created_at": 100.5, "updated_at": 200.25, "unseen": true, "preview": "eggs, milk",
     "text": "# Groceries\n\neggs, milk", "later": 1}
    """#

    func signedIn(_ stub: Stub) -> AppModel {
        let defaults = UserDefaults(suiteName: "valet-tests-\(UUID().uuidString)")!
        defaults.set("http://127.0.0.1:9", forKey: "server")
        return AppModel(store: MemoryTokenStore(["token": "vown_t"]), defaults: defaults, transport: FakeTransport(),
                        urlSession: stub.session)
    }

    @Test func itemsAndPagesDecode() throws {
        let n = try decode(SharedItem.self, Self.note)
        #expect(n.id == 7 && n.agent == "scout" && n.kind == .note && n.title == "Groceries")
        #expect(n.addedBy == "scout" && n.updatedBy == "owner")
        #expect(n.updatedAt == 200.25 && n.unseen)
        #expect(n.text?.hasPrefix("# Groceries") == true)
        let f = try decode(SharedItem.self, #"{"id": 8, "kind": "file", "title": "a.png", "mime": "image/png", "size": 2048}"#)
        #expect(f.isFile && f.isImage && !f.isText && f.agent == nil)
        #expect(f.addedBy == "owner" && f.updatedBy == "owner")
        #expect(try decode(SharedItem.self, #"{"id": 9, "kind": "file", "title": "NOTES.MD", "mime": null}"#).isMarkdown)
        let page = try decode(SharedPage.self, #"{"items": [\#(Self.note), {"title": "no id"}], "more": true, "unseen": 3}"#)
        #expect(page.items.map(\.id) == [7])
        #expect(page.more && page.unseen == 3)
        #expect(byteSize(820) == "820 B" && byteSize(12_400) == "12 KB" && byteSize(3_565_158) == "3.4 MB")
    }

    @Test func sharedFrames() {
        let counts = #""unseen": 2, "agents": {"scout": 1, "ada": 1}, "unassigned": 3"#
        guard case .shared(let added) = Frame.decode(#"{"type": "shared", "item": \#(Self.note), \#(counts)}"#) else {
            Issue.record("no frame"); return
        }
        #expect(added.item?.id == 7 && added.counts == SharedCounts(unseen: 2, agents: ["scout": 1, "ada": 1],
                                                                       unassigned: 3))
        guard case .shared(let gone) = Frame.decode(#"{"type": "shared", "deleted": 7, "agent": "scout", "unseen": 1}"#)
        else { return }
        #expect(gone.deleted == 7 && gone.item == nil && gone.counts == SharedCounts(unseen: 1))
        guard case .shared(let seen) = Frame.decode(#"{"type": "shared", "seen": [7, 8], "unseen": 0}"#) else { return }
        #expect(seen.seen == [7, 8] && !seen.seenAll)
        guard case .shared(let all) = Frame.decode(#"{"type": "shared", "seen": "all", "unseen": 0}"#) else { return }
        #expect(all.seenAll && all.seen == nil)
    }

    @Test func showingTheListSeesWhatItShowsButKeepsTheDots() async throws {
        let stub = Stub { r in
            switch (r.method, r.path) {
            case ("GET", "/api/agents/scout/shared"):
                return (200, body(#"{"items": [\#(Self.note)], "more": false, "unseen": 1}"#))
            case ("POST", "/api/shared/seen"): return (200, body(#"{"unseen": 0, "agents": {}, "unassigned": 0}"#))
            default: return (404, body("{}"))
            }
        }
        let m = signedIn(stub)
        let list = SharedList(model: m, agent: "scout")
        list.start()
        await list.reload()
        #expect(stub.recorded.last?.body == ["ids": [7]])
        #expect(m.sharedUnseen == 0)
        // The server's frame says it's seen: its dot stays for this visit, and goes once the screen is left.
        list.apply(SharedChange(seen: [7]))
        #expect(!list.items[0].unseen && list.showsDot(list.items[0]))
        list.stop()
        #expect(!list.showsDot(list.items[0]))
        // Not on show (the app in the background): nothing is marked.
        let hidden = SharedList(model: m, agent: "scout")
        hidden.visible = false
        let before = stub.recorded.count
        await hidden.reload()
        #expect(stub.recorded.count == before + 1)
    }

    @Test func theListFollowsFramesUnderItsFilters() async throws {
        let m = signedIn(Stub { _ in (200, body("{}")) })
        let list = SharedList(model: m, agent: "scout")
        func item(_ id: Int, _ at: Double, by: String = "owner", kind: SharedKind = .note, title: String = "x",
                  agent: String? = "scout") -> SharedItem {
            SharedItem(id: id, agent: agent, kind: kind, title: title, addedBy: by, createdAt: at, unseen: by != "owner")
        }
        list.put(item(1, 10))
        list.put(item(2, 30))
        list.put(item(3, 20, by: "scout"))
        #expect(list.items.map(\.id) == [2, 3, 1])
        // A change moves it to the top.
        list.apply(SharedChange(item: item(1, 40)))
        #expect(list.items.map(\.id) == [1, 2, 3])
        // Another agent's item never comes; one moved to another agent goes.
        list.apply(SharedChange(item: item(8, 45, agent: "ada")))
        list.apply(SharedChange(item: item(9, 46, agent: nil)))
        #expect(list.items.map(\.id) == [1, 2, 3])
        list.put(item(10, 47))
        list.apply(SharedChange(item: item(10, 48, agent: "ada")))
        #expect(list.items.map(\.id) == [1, 2, 3])
        list.apply(SharedChange(seen: [3]))
        #expect(!list.items[2].unseen)
        list.apply(SharedChange(deleted: 2))
        #expect(list.items.map(\.id) == [1, 3])
        // Under a filter, what doesn't pass goes (or never comes).
        list.query = SharedQuery(by: "scout")
        list.apply(SharedChange(item: item(1, 50)))
        list.apply(SharedChange(item: item(4, 60, by: "scout", title: "Plan")))
        #expect(list.items.map(\.id) == [4, 3])
        #expect(SharedList.matches(item(5, 1, by: "scout", kind: .file, title: "Report.PDF"),
                                   SharedQuery(q: "report", by: "scout", kind: "file")))
        #expect(!SharedList.matches(item(5, 1, by: "scout", kind: .file, title: "x"), SharedQuery(kind: "note")))
        #expect(!SharedList.matches(item(5, 1), SharedQuery(by: "scout")))
    }

    @Test func listingSeenNotesAndUploads() async throws {
        let stub = Stub { r in
            switch (r.method, r.path) {
            case ("GET", "/api/agents/scout/shared"), ("GET", "/api/shared/unassigned"):
                return (200, body(#"{"items": [\#(Self.note)], "more": false, "unseen": 1}"#))
            case ("GET", "/api/shared/unseen"):
                return (200, body(#"{"unseen": 2, "agents": {"scout": 2}, "unassigned": 1}"#))
            case ("POST", "/api/shared/seen"): return (200, body(#"{"unseen": 0, "agents": {}, "unassigned": 1}"#))
            case ("POST", "/api/agents/scout/shared/notes"): return (201, body(Self.note))
            case ("POST", "/api/agents/scout/shared/files"):
                return (201, body(#"[{"id": 9, "kind": "file", "title": "My file.txt", "mime": "text/plain", "size": 5}]"#))
            case ("PATCH", "/api/shared/7"): return (200, body(Self.note))
            case ("PUT", "/api/shared/9/file"): return (200, body(#"{"id": 9, "kind": "file", "title": "b.txt"}"#))
            case ("POST", "/api/shared/4/move"): return (200, body(#"{"id": 4, "agent": "scout", "title": "old"}"#))
            default: return (404, body(#"{"detail": "no"}"#))
            }
        }
        let api = APIClient(baseURL: URL(string: "http://h")!, token: "vown_t", urlSession: stub.session)
        let page = try await api.shared("scout", SharedQuery(q: "eggs", by: "scout", kind: "note"), before: 200.25)
        #expect(page.items.count == 1)
        #expect(stub.recorded[0].query == ["q": "eggs", "by": "scout", "kind": "note", "before": "200.25", "limit": "50"])
        #expect(try await api.markSeen([7]) == SharedCounts(unassigned: 1))
        #expect(stub.recorded[1].body == ["ids": [7]])
        _ = try await api.markSeen(nil)
        #expect(stub.recorded[2].body == [:])
        _ = try await api.addNote(agent: "scout", title: "  ", text: "hi")
        #expect(stub.recorded[3].body == ["text": "hi"])
        let up = try await api.upload(agent: "scout", name: "My file.txt", data: Data("hello".utf8), text: "for you")
        #expect(up.map(\.id) == [9])
        let r = stub.recorded[4]
        #expect(r.query == ["name": "My file.txt", "text": "for you"])
        #expect(r.raw == Data("hello".utf8))
        #expect(r.headers["Content-Type"] == "application/octet-stream")
        #expect(r.headers["Authorization"] == "Bearer vown_t")
        _ = try await api.updateShared(7, text: "new")
        #expect(stub.recorded[5].body == ["text": "new"])
        _ = try await api.replaceSharedFile(9, data: Data("b".utf8), name: "b.txt")
        #expect(stub.recorded[6].query == ["name": "b.txt"] && stub.recorded[6].raw == Data("b".utf8))
        // The unassigned items, the counts, and moving one to an agent.
        _ = try await api.shared(nil)
        #expect(stub.recorded[7].path == "/api/shared/unassigned")
        #expect(try await api.sharedCounts() == SharedCounts(unseen: 2, agents: ["scout": 2], unassigned: 1))
        #expect(try await api.moveShared(4, to: "scout").agent == "scout")
        #expect(stub.recorded[9].body == ["agent": "scout"])
    }

    // MARK: memory

    @Test func memoryDecodesAndFollowsFrames() throws {
        var mem = try decode(AgentMemory.self, #"""
        {"agent": "scout", "enabled": true, "layer": false, "size": 30,
         "files": [{"name": "people", "title": "People", "size": 10, "updated_at": 5},
                   {"name": "projects", "title": "Projects", "size": 20, "updated_at": 6}, {"title": "no name"}],
         "limits": {"file": 100, "total": 1000, "files": 3, "layer": 50}}
        """#)
        #expect(mem.files.map(\.name) == ["people", "projects"])
        #expect(mem.enabled == true && mem.layer == false)
        #expect(mem.limits == AgentMemory.Limits(file: 100, total: 1000, files: 3, layer: 50))
        guard case .memory(let c) = Frame.decode(#"{"type": "memory", "agent": "scout", "name": "home", "file": {"name": "home", "title": "Home", "size": 7}}"#) else {
            Issue.record("no frame"); return
        }
        mem.apply(c)
        #expect(mem.files.map(\.name) == ["home", "people", "projects"])
        #expect(mem.size == 37)
        guard case .memory(let d) = Frame.decode(#"{"type": "memory", "agent": "scout", "name": "people", "file": null}"#) else { return }
        mem.apply(d)
        #expect(mem.files.map(\.name) == ["home", "projects"])
        #expect(Frame.decode(#"{"type": "memory", "name": "x"}"#).map { if case .unknown = $0 { true } else { false } } == true)
    }

    @Test func memoryNames() {
        #expect(MemoryFile.slug("Project Notes.md") == "project-notes")
        #expect(MemoryFile.slug("  Ünïcode & stuff!! ") == "n-code-stuff")
        #expect(MemoryFile.isValidName("project-notes"))
        #expect(!MemoryFile.isValidName("Project"))
        #expect(!MemoryFile.isValidName("a--b"))
        #expect(!MemoryFile.isValidName(""))
    }

    @Test func memoryRoutes() async throws {
        let stub = Stub { r in
            switch (r.method, r.path) {
            case ("GET", "/api/agents/scout/memory/people"):
                return (200, body(#"{"name": "people", "commit": "abc", "text": "old"}"#))
            case ("PUT", "/api/agents/scout/memory/people"):
                return (200, body(#"{"file": {"name": "people", "title": "People", "size": 3}}"#))
            case ("GET", "/api/agents/scout/memory/people/history"):
                return (200, body(#"{"history": [{"commit": "abcdef1234", "message": "Memory: people", "at": 1}]}"#))
            default: return (404, body("{}"))
            }
        }
        let api = APIClient(baseURL: URL(string: "http://h")!, urlSession: stub.session)
        let old = try await api.memoryFile("scout", "people", at: "abc")
        #expect(old.text == "old" && old.commit == "abc" && old.file == nil)
        #expect(stub.recorded[0].query == ["at": "abc"])
        let f = try await api.writeMemory("scout", "people", text: "new")
        #expect(f?.size == 3)
        #expect(stub.recorded[1].body == ["text": "new"])
        let h = try await api.memoryHistory("scout", "people")
        #expect(h.first?.short == "abcdef1")
    }
}
