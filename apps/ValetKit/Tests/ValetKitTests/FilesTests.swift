import Foundation
import Testing
@testable import ValetKit

@Suite("The agent's files and volumes")
struct FilesTests {
    static let roots = #"[{"path": "/work", "label": "Workspace", "writable": true},"#
        + #" {"path": "/home/agent", "label": "Home", "writable": true},"#
        + #" {"path": "/work/shared/docs", "label": "docs", "writable": false}]"#

    static func listing(_ path: String = "/work", entries: String = "[]", type: String = "dir") -> Data {
        body(#"{"roots": \#(roots), "path": "\#(path)", "type": "\#(type)", "size": 64, "mtime": 1700000000.5, "#
             + #""entries": \#(entries), "truncated": false}"#)
    }

    // MARK: decoding and paths

    @Test func aListingDecodes() throws {
        let l = try decode(FileListing.self, #"""
        {"roots": [{"path": "/work", "label": "Workspace", "writable": true}, {"label": "no path"}],
         "path": "/work", "type": "dir", "size": 4096, "mtime": 1.5, "truncated": true,
         "entries": [{"name": "src", "type": "dir", "link": false, "size": 0, "mtime": 2},
                     {"name": "notes.txt", "type": "file", "link": true, "size": 12, "mtime": 3},
                     {"name": "away", "type": "other", "link": true, "size": 0, "mtime": 0},
                     {"name": "odd", "type": "socket"}, {"type": "file"}]}
        """#)
        #expect(l.roots == [FileRoot(path: "/work", label: "Workspace", writable: true)])
        #expect(l.entries.map(\.name) == ["src", "notes.txt", "away", "odd"])
        #expect(l.entries[1].link && l.entries[1].size == 12)
        #expect(l.entries[2].type == .other && l.entries[3].type.rawValue == "socket")
        #expect(l.truncated && l.writable && l.has("src") && !l.has("nope"))

        let t = try decode(FileText.self, #"{"path": "/work/a.bin", "size": 9, "text": null, "binary": true, "truncated": false}"#)
        #expect(t.binary && t.text == nil)
        let t2 = try decode(FileText.self, #"{"path": "/work/a.txt", "size": 2, "text": "hi", "truncated": true}"#)
        #expect(t2.text == "hi" && !t2.binary && t2.truncated)

        let v = try decode(AgentVolumes.self, #"""
        {"runner": "mac", "running": false, "volumes": [{"name": "valet-agent-a-home", "mount": "/home/agent"},
         {"name": "valet-agent-a-work", "mount": "/work"}, {"mount": "/x"}]}
        """#)
        #expect(v.runner == "mac" && !v.running && v.volumes.map(\.name) == ["valet-agent-a-home", "valet-agent-a-work"])
    }

    @Test func paths() throws {
        #expect(FilePaths.join("/", "a") == "/a")
        #expect(FilePaths.join("/work", "a b") == "/work/a b")
        #expect(FilePaths.join("/work/", "a") == "/work/a")
        #expect(FilePaths.parent("/work/a/b.txt") == "/work/a")
        #expect(FilePaths.parent("/work") == "/")
        #expect(FilePaths.parent("/") == "/")
        #expect(FilePaths.name("/work/a/b.txt") == "b.txt")

        let roots = try decode([FileRoot].self, Self.roots)
        // The deepest root that holds it; a name that only starts the same isn't inside.
        #expect(FilePaths.root(holding: "/work/shared/docs/x.md", in: roots)?.label == "docs")
        #expect(FilePaths.root(holding: "/work/shared", in: roots)?.label == "Workspace")
        #expect(FilePaths.root(holding: "/workshop", in: roots) == nil)
        #expect(FilePaths.root(holding: "/home/agent", in: roots)?.label == "Home")

        #expect(FilePaths.crumbs("/work/src/app", roots: roots).map(\.label) == ["Workspace", "src", "app"])
        #expect(FilePaths.crumbs("/work/src/app", roots: roots).map(\.path) == ["/work", "/work/src", "/work/src/app"])
        #expect(FilePaths.crumbs("/work/shared/docs/a", roots: roots).map(\.label) == ["docs", "a"])
        #expect(FilePaths.crumbs("/etc", roots: roots).map(\.label) == ["/etc"])

        let ro = FileListing(roots: roots, path: "/work/shared/docs/a")
        #expect(!ro.writable)
        #expect(FileListing(roots: roots, path: "/home/agent/.config").writable)

        #expect(FilePaths.folderNameProblem("New folder") == nil)
        for bad in ["", "  ", "a/b", ".", ".."] { #expect(FilePaths.folderNameProblem(bad) != nil) }
        #expect(FilePaths.isImage("shot.PNG") && FilePaths.isImage("a.b.jpeg") && FilePaths.isImage("x.heic"))
        #expect(!FilePaths.isImage("notes.txt") && !FilePaths.isImage("png"))
    }

    // MARK: the routes

    @Test func theRequests() async throws {
        let stub = Stub { r in
            switch (r.method, r.path) {
            case ("GET", "/api/agents/my%20agent/computer/files"): return (200, Self.listing())
            case ("GET", "/api/agents/my%20agent/computer/files/text"):
                return (200, body(#"{"path": "/work/a.txt", "size": 2, "text": "hi", "binary": false, "truncated": false}"#))
            case ("GET", "/api/agents/my%20agent/computer/files/download"): return (200, Data([0, 1, 2]))
            case ("POST", "/api/agents/my%20agent/computer/files/upload"):
                return (201, body(#"{"path": "/work/a-2.txt", "size": 5}"#))
            case ("POST", "/api/agents/my%20agent/computer/files/folder"): return (201, body(#"{"path": "/work/new"}"#))
            case ("GET", "/api/agents/my%20agent/computer/volumes"):
                return (200, body(#"{"runner": "mac", "running": true, "volumes": []}"#))
            case ("DELETE", "/api/agents/my%20agent/computer/volumes"):
                return (200, body(#"{"removed": ["valet-agent-a-home", "valet-agent-a-work"]}"#))
            default: return (404, body(#"{"detail": "no"}"#))
            }
        }
        let api = APIClient(baseURL: URL(string: "http://h")!, token: "vown_t", urlSession: stub.session)
        let agent = "my agent"

        _ = try await api.files(agent)
        #expect(stub.recorded[0].query == [:])
        _ = try await api.files(agent, path: "/work/a b&c")
        #expect(stub.recorded[1].query == ["path": "/work/a b&c"])
        #expect(try await api.fileText(agent, path: "/work/a.txt").text == "hi")
        #expect(stub.recorded[2].query == ["path": "/work/a.txt"])
        #expect(try await api.downloadFile(agent, path: "/work/x.png") == Data([0, 1, 2]))
        #expect(stub.recorded[3].query == ["path": "/work/x.png"])

        let up = try await api.uploadFile(agent, dir: "/work", name: "a.txt", data: Data("hello".utf8), exists: .rename)
        #expect(up == UploadedFile(path: "/work/a-2.txt", size: 5))
        let r = stub.recorded[4]
        #expect(r.query == ["dir": "/work", "name": "a.txt", "exists": "rename"])
        #expect(r.raw == Data("hello".utf8))
        #expect(r.headers["Content-Type"] == "application/octet-stream")
        #expect(r.headers["Authorization"] == "Bearer vown_t")

        #expect(try await api.makeFolder(agent, path: "/work/new") == "/work/new")
        #expect(stub.recorded[5].body == ["path": "/work/new"])
        #expect(try await api.volumes(agent).running)
        #expect(try await api.deleteVolumes(agent) == ["valet-agent-a-home", "valet-agent-a-work"])
    }

    // MARK: the screen's model

    /// A folder with a.txt in it; uploads answer 409 for `taken` with exists=fail.
    final class Server: @unchecked Sendable {
        let lock = NSLock()
        var taken: Set<String> = []
        var failing: Set<String> = []
        lazy var stub = Stub { [unowned self] r in
            switch (r.method, r.path) {
            case ("GET", "/api/agents/a/computer/files"):
                return (200, FilesTests.listing(r.query["path"] ?? "/work",
                                                entries: #"[{"name": "a.txt", "type": "file", "size": 1, "mtime": 1}]"#))
            case ("POST", "/api/agents/a/computer/files/upload"):
                let name = r.query["name"] ?? ""
                if self.lock.withLock({ self.failing.contains(name) }) { return (413, body(#"{"detail": "too big"}"#)) }
                if r.query["exists"] == "fail" && self.lock.withLock({ self.taken.contains(name) }) {
                    return (409, body(#"{"detail": "exists"}"#))
                }
                return (201, body(#"{"path": "/work/\#(name)", "size": 1}"#))
            case ("POST", "/api/agents/a/computer/files/folder"):
                return (201, body(#"{"path": "\#(r.body?["path"]?.string ?? "")"}"#))
            case ("GET", "/api/agents/a/computer/volumes"):
                return (200, body(#"{"runner": "mac", "running": false, "volumes": [{"name": "valet-agent-a-home", "mount": "/home/agent"}, {"name": "valet-agent-a-work", "mount": "/work"}]}"#))
            case ("DELETE", "/api/agents/a/computer/volumes"):
                return (409, body(#"{"detail": "a is running: stop it first"}"#))
            default: return (404, body(#"{"detail": "no such file or folder"}"#))
            }
        }
        var uploads: [[String: String]] { stub.recorded.filter { $0.path.hasSuffix("/upload") }.map(\.query) }
        var api: APIClient { APIClient(baseURL: URL(string: "http://h")!, urlSession: stub.session) }
    }

    static func source(_ name: String) -> UploadSource { UploadSource(name: name) { Data(name.utf8) } }

    @MainActor @Test func uploadsAskAboutATakenName() async throws {
        let server = Server()
        server.taken = ["a.txt", "c.txt"]
        let files = FilesModel(agent: "a", path: "/work", api: server.api)
        await files.load()
        #expect(files.listing?.has("a.txt") == true)

        let run = Task { await files.upload(["a.txt", "b.txt", "c.txt", "d.txt"].map(Self.source)) }
        // a.txt is in the listing: it asks before sending anything.
        #expect(await eventually { files.clash?.name == "a.txt" })
        #expect(server.uploads.isEmpty)
        let first = try #require(files.clash)
        files.choose(.replace, ask: first.id - 1)        // a stale dialog's answer: ignored
        #expect(files.clash == first)
        files.choose(.rename, ask: first.id)
        // c.txt isn't listed but the server has it (409): it asks then.
        #expect(await eventually { files.clash?.name == "c.txt" })
        files.choose(nil)                                // Cancel: c.txt isn't sent
        await run.value
        #expect(server.uploads.map { $0["name"]! + ":" + $0["exists"]! }
                == ["a.txt:rename", "b.txt:fail", "c.txt:fail", "d.txt:fail"])
        #expect(files.clash == nil && files.busy == nil && files.message == nil)
        // Read again afterwards.
        #expect(server.stub.recorded.last?.path == "/api/agents/a/computer/files")
    }

    @MainActor @Test func aTakenNameCanBeReplacedAndFailuresAreSaid() async throws {
        let server = Server()
        server.taken = ["x.txt"]
        server.failing = ["big.iso"]
        let files = FilesModel(agent: "a", path: "/work", api: server.api)
        await files.load()
        let run = Task { await files.upload(["x.txt", "big.iso"].map(Self.source)) }
        #expect(await eventually { files.clash?.name == "x.txt" })
        files.choose(.replace)
        await run.value
        #expect(server.uploads.map { $0["exists"]! } == ["fail", "replace", "fail"])
        #expect(files.message == "big.iso: Too big")
    }

    @MainActor @Test func leavingCancelsTheQuestion() async throws {
        let server = Server()
        let files = FilesModel(agent: "a", path: "/work", api: server.api)
        await files.load()
        let run = Task { await files.upload([Self.source("a.txt")]) }
        #expect(await eventually { files.clash != nil })
        files.choose(nil)
        await run.value
        #expect(server.uploads.isEmpty)
    }

    @MainActor @Test func newFolders() async throws {
        let server = Server()
        let files = FilesModel(agent: "a", path: "/work", api: server.api)
        await files.load()
        #expect(await files.makeFolder("a/b") == false)
        #expect(files.message == "A folder's name, without /.")
        #expect(server.stub.recorded.count == 1)
        #expect(await files.makeFolder(" reports "))
        #expect(server.stub.recorded[1].body == ["path": "/work/reports"])
        #expect(files.message == nil && files.busy == nil)
    }

    @MainActor @Test func aFileLoadsItsPreview() async throws {
        let stub = Stub { r in
            switch r.path {
            case "/api/agents/a/computer/files":
                let p = r.query["path"]!
                return (200, FilesTests.listing(p, type: "file"))
            case "/api/agents/a/computer/files/text":
                return (200, body(#"{"path": "/work/a.txt", "size": 2, "text": "hi", "binary": false, "truncated": false}"#))
            case "/api/agents/a/computer/files/download": return (200, Data([1, 2, 3]))
            default: return (404, body(#"{"detail": "no such file or folder"}"#))
            }
        }
        let api = APIClient(baseURL: URL(string: "http://h")!, urlSession: stub.session)
        let text = FilesModel(agent: "a", path: "/work/a.txt", api: api)
        await text.load()
        guard case .text(let t) = text.preview else { Issue.record("no text"); return }
        #expect(t.text == "hi")
        let image = FilesModel(agent: "a", path: "/work/shot.png", api: api)
        await image.load()
        guard case .image(let d) = image.preview else { Issue.record("no image"); return }
        #expect(d == Data([1, 2, 3]))
        // A 404 is the load's error.
        let missing = FilesModel(agent: "b", path: "/work", api: api)
        await missing.load()
        #expect(missing.listing == nil && missing.loadError == "No such file or folder")
    }

    @MainActor @Test func volumes() async throws {
        let server = Server()
        let files = FilesModel(agent: "a", path: nil, api: server.api)
        await files.loadVolumes()
        #expect(files.volumes?.volumes.count == 2)
        #expect(files.deleteVolumesQuestion == "Delete valet-agent-a-home (/home/agent) and valet-agent-a-work (/work)? "
                + "Everything in its home and workspace goes, for good.")
        await files.deleteVolumes()
        #expect(files.volumesError == "A is running: stop it first")
        #expect(files.volumesDone == nil && !files.deletingVolumes)
    }
}
