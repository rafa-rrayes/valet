import Foundation
import ImageIO
import Testing
import UniformTypeIdentifiers
@testable import ValetKit

@MainActor
@Suite("Attachments in a conversation")
struct AttachmentTests {
    func signedIn(_ stub: Stub) -> AppModel {
        let defaults = UserDefaults(suiteName: "valet-tests-\(UUID().uuidString)")!
        defaults.set("http://127.0.0.1:9", forKey: "server")
        return AppModel(store: MemoryTokenStore(["token": "vown_t"]), defaults: defaults, transport: FakeTransport(),
                        urlSession: stub.session)
    }

    nonisolated static func attachment(_ id: String, _ name: String, image: Bool = false) -> Data {
        body(#"{"id": "\#(id)", "agent": "a", "name": "\#(name)", "mime": "\#(image ? "image/png" : "text/plain")", "#
             + #""size": 5, "image": \#(image), "sent": false, "path": null, "url": "/api/agents/a/attachments/\#(id)", "#
             + #""created_at": 1.5}"#)
    }

    /// A 4x2 PNG.
    nonisolated static func png() -> Data {
        let ctx = CGContext(data: nil, width: 4, height: 2, bitsPerComponent: 8, bytesPerRow: 0,
                            space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
        ctx.setFillColor(red: 0, green: 0.5, blue: 0.3, alpha: 1)
        ctx.fill(CGRect(x: 0, y: 0, width: 4, height: 2))
        let out = NSMutableData()
        let dest = CGImageDestinationCreateWithData(out, UTType.png.identifier as CFString, 1, nil)!
        CGImageDestinationAddImage(dest, ctx.makeImage()!, nil)
        CGImageDestinationFinalize(dest)
        return out as Data
    }

    @Test func theRoutes() async throws {
        let stub = Stub { r in
            switch (r.method, r.path) {
            case ("POST", "/api/agents/a/attachments"): return (201, Self.attachment("ab12", r.query["name"] ?? ""))
            case ("DELETE", "/api/agents/a/attachments/ab12"): return (200, body(#"{"ok": true}"#))
            case ("GET", "/api/agents/a/attachments/ab12"): return (200, Data([7, 7]))
            case ("POST", "/api/agents/a/sessions/s/prompt"): return (202, body(#"{"turn_id": "t9"}"#))
            default: return (404, body(#"{"detail": "no"}"#))
            }
        }
        let api = APIClient(baseURL: URL(string: "http://h")!, token: "vown_t", urlSession: stub.session)
        let a = try await api.uploadAttachment("a", name: "my notes.txt", data: Data("hello".utf8))
        #expect(a == Attachment(id: "ab12", agent: "a", name: "my notes.txt", mime: "text/plain", size: 5,
                                url: "/api/agents/a/attachments/ab12"))
        #expect(stub.recorded[0].query == ["name": "my notes.txt"])
        #expect(stub.recorded[0].raw == Data("hello".utf8))
        #expect(stub.recorded[0].headers["Authorization"] == "Bearer vown_t")
        try await api.deleteAttachment("a", id: "ab12")
        #expect(try await api.attachmentData("/api/agents/a/attachments/ab12") == Data([7, 7]))
        #expect(stub.recorded[2].headers["Authorization"] == "Bearer vown_t")

        // The prompt's body: text and the ids; without attachments, as before.
        _ = try await api.prompt("a", "s", text: "look", attachments: ["ab12", "cd34"])
        #expect(stub.recorded[3].body == ["text": "look", "attachments": ["ab12", "cd34"]])
        _ = try await api.prompt("a", "s", text: "", attachments: ["ab12"])
        #expect(stub.recorded[4].body == ["text": "", "attachments": ["ab12"]])
        _ = try await api.prompt("a", "s", text: "plain")
        #expect(stub.recorded[5].body == ["text": "plain"])

        // The token goes only to paths on this server.
        for bad in ["https://evil.example/x", "//evil.example/x", "/api/../x", "/api/x?y=1", "/other/x", "/api/a b"] {
            await #expect(throws: APIError.invalidResponse) { try await api.attachmentData(bad) }
        }
        #expect(stub.recorded.count == 6)
        #expect(APIClient.isServerPath("/api/agents/a/attachments/ab12"))
        #expect(APIClient.isServerPath("/api/agents/a/attachments/a%20b"))
    }

    @Test func draftsUploadAndGoWithThePrompt() async throws {
        let uploads = Counter()
        let stub = Stub { r in
            switch (r.method, r.path) {
            case ("POST", "/api/agents/a/attachments"):
                uploads.add()
                let name = r.query["name"] ?? ""
                if name == "broken.bin" { return (413, body(#"{"detail": "too big"}"#)) }
                return (201, Self.attachment("id\(uploads.value)", name, image: name.hasSuffix(".png")))
            case ("DELETE", _): return (200, body(#"{"ok": true}"#))
            case ("POST", "/api/agents/a/sessions/s/prompt"): return (202, body(#"{"turn_id": "t1"}"#))
            case (_, "/api/agents/a/sessions/s/events"): return (200, body(#"{"events": [], "more": false}"#))
            default: return (200, body(#"{"agent": "a", "id": "s", "state": "idle"}"#))
            }
        }
        let m = signedIn(stub)
        let s = SessionModel(agent: "a", id: "s", model: m)
        s.start()
        #expect(await eventually { s.loaded })
        let png = Self.png()

        await s.attach([UploadSource(name: "shot.png") { png }, UploadSource(name: "notes.txt") { Data("hi".utf8) }])
        #expect(s.drafts.map(\.name) == ["shot.png", "notes.txt"])
        #expect(s.drafts.map { $0.attachment?.id } == ["id1", "id2"])
        #expect(s.drafts[0].thumbnail != nil && s.drafts[1].thumbnail == nil)
        #expect(s.drafts.map(\.size) == [png.count, 2])
        #expect(s.canSend(""))                       // attachments alone are a message

        // Taken off: deleted on the server.
        s.detach(s.drafts[1].id)
        #expect(await eventually { stub.recorded.contains { $0.method == "DELETE" && $0.path.hasSuffix("/id2") } })

        // One that didn't upload stops the send (and says why) until it's taken off.
        await s.attach([UploadSource(name: "broken.bin") { Data([1]) }])
        #expect(s.drafts.last?.failure == "Too big")
        #expect(await s.send("look") == false)
        #expect(s.error == "An attachment didn't upload: take it off, or attach it again.")
        s.detach(s.drafts[1].id)

        #expect(await s.send("look"))
        let prompt = try #require(stub.recorded.last { $0.path.hasSuffix("/prompt") })
        #expect(prompt.body == ["text": "look", "attachments": ["id1"]])
        #expect(s.drafts.isEmpty)
        s.stop()
    }

    @Test func aRefusedPromptKeepsItsTextAndAttachments() async throws {
        let stub = Stub { r in
            switch (r.method, r.path) {
            case ("POST", "/api/agents/a/attachments"): return (201, Self.attachment("id1", "a.txt"))
            case ("POST", "/api/agents/a/sessions/s/prompt"):
                return (503, body(#"{"detail": "agent a is not running", "sent": false}"#))
            case (_, "/api/agents/a/sessions/s/events"): return (200, body(#"{"events": [], "more": false}"#))
            default: return (200, body(#"{"agent": "a", "id": "s", "state": "idle"}"#))
            }
        }
        let m = signedIn(stub)
        let s = SessionModel(agent: "a", id: "s", model: m)
        s.start()
        #expect(await eventually { s.loaded })
        await s.attach([UploadSource(name: "a.txt") { Data("x".utf8) }])
        // Nothing recorded: the composer keeps the text, the chips stay.
        #expect(await s.send("with this") == false)
        #expect(s.error == "Agent a is not running")
        #expect(s.drafts.count == 1)
        s.stop()
    }

    @Test func atMostTenOnAMessageAndOneTakenOffWhileItUploads() async throws {
        let gate = Gate()
        let stub = Stub { r in
            switch (r.method, r.path) {
            case ("POST", "/api/agents/a/attachments"): return (201, Self.attachment("up-\(r.query["name"]!)", r.query["name"]!))
            case ("DELETE", _): return (200, body(#"{"ok": true}"#))
            default: return (200, body(#"{"agent": "a", "id": "s", "state": "idle"}"#))
            }
        }
        let m = signedIn(stub)
        let s = SessionModel(agent: "a", id: "s", model: m)
        await s.attach((1...12).map { i in UploadSource(name: "f\(i).txt") { Data([UInt8(i)]) } })
        #expect(s.drafts.count == 10)
        #expect(s.error == "At most 10 attachments on a message.")
        for d in s.drafts { s.detach(d.id) }

        // Taken off while its bytes are still being read: it's deleted once it's up, and no chip comes back.
        let run = Task { await s.attach([UploadSource(name: "slow.txt") { await gate.wait(); return Data([1]) }]) }
        #expect(await eventually { s.drafts.first?.isUploading == true })
        s.detach(s.drafts[0].id)
        await gate.open()
        await run.value
        #expect(s.drafts.isEmpty)
        #expect(stub.recorded.contains { $0.method == "DELETE" && $0.path.hasSuffix("/up-slow.txt") })
    }

    @Test func aQueuedPromptSaysWhatItCarries() async throws {
        let stub = Stub { r in
            if r.path.hasSuffix("/events") { return (200, body(#"{"events": [], "more": false}"#)) }
            return (200, body(#"{"agent": "a", "id": "s", "state": "working", "queued": 1, "queue": [{"id": 3, "text": "", "source": "owner", "label": "You", "attachments": [{"id": "x1", "name": "shot.png", "mime": "image/png", "image": true, "url": "/u"}, {"id": "x2", "name": "r.txt", "mime": "text/plain", "image": false, "url": "/v"}]}]}"#))
        }
        let m = signedIn(stub)
        let s = SessionModel(agent: "a", id: "s", model: m)
        s.start()
        #expect(await eventually { s.loaded && !s.queued.isEmpty })
        #expect(s.queued[0].attachments == [.init(name: "shot.png", image: true), .init(name: "r.txt", image: false)])
        s.stop()
    }

    @Test nonisolated func thumbnails() throws {
        let t = try #require(Thumbnail.png(Self.png(), maxPixel: 2))
        let src = try #require(CGImageSourceCreateWithData(t as CFData, nil))
        let props = CGImageSourceCopyPropertiesAtIndex(src, 0, nil) as? [CFString: Any]
        #expect(props?[kCGImagePropertyPixelWidth] as? Int == 2)
        #expect(Thumbnail.png(Data("not a picture".utf8)) == nil)
    }
}

/// Holds an async step until the test opens it.
actor Gate {
    private var opened = false
    private var waiters: [CheckedContinuation<Void, Never>] = []

    func wait() async {
        if opened { return }
        await withCheckedContinuation { waiters.append($0) }
    }

    func open() {
        opened = true
        for w in waiters { w.resume() }
        waiters = []
    }
}
