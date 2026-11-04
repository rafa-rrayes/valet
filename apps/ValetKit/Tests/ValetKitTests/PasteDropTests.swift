import Foundation
import Testing
import UniformTypeIdentifiers
@testable import ValetKit
#if os(macOS)
import AppKit
#endif

/// What the owner hands the app to upload from a drop or ⌘V (PickedFile, PasteWatcher), and Shared's uploads taking
/// the same sources as the composer and Files.
@MainActor
@Suite("Drops, pastes and Shared's uploads")
struct PasteDropTests {
    nonisolated static let png = Data([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 1, 2, 3])

    func file(_ name: String, _ text: String) throws -> URL {
        let dir = FileManager.default.temporaryDirectory.appending(path: "valet-tests-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let url = dir.appending(path: name)
        try Data(text.utf8).write(to: url)
        return url
    }

    func picture(_ type: UTType, named name: String? = nil) -> NSItemProvider {
        let p = NSItemProvider()
        p.registerDataRepresentation(for: type, visibility: .all) { done in
            done(Self.png, nil)
            return nil
        }
        p.suggestedName = name
        return p
    }

    @Test func dropsOfFilesAndPictures() async throws {
        let report = try file("report.txt", "hello")
        let providers = [
            NSItemProvider(object: report as NSURL),            // a file from the Finder
            picture(.png, named: "chart"),                      // a picture dragged out of a browser
            picture(.jpeg),                                     // one with no name
            NSItemProvider(object: "just text" as NSString),    // neither: skipped
            picture(.png, named: "Screenshot 2026-11-02 at 21.14.59"),     // a dot, but no extension
            picture(.png, named: "diagram.png"),
        ]
        let sources = await PickedFile.sources(dropped: providers)
        #expect(sources.count == 5)
        #expect(sources[0].name == "report.txt")
        #expect(try await sources[0].data() == Data("hello".utf8))
        #expect(sources[1].name == "chart.png")
        #expect(try await sources[1].data() == Self.png)
        let ext = UTType.jpeg.preferredFilenameExtension!
        #expect(sources[2].name.hasPrefix("Dropped image ") && sources[2].name.hasSuffix("-3.\(ext)"))
        #expect(sources[3].name == "Screenshot 2026-11-02 at 21.14.59.png")
        #expect(sources[4].name == "diagram.png")
        // One picture alone has no number.
        let one = await PickedFile.sources(dropped: [picture(.png)])
        #expect(one.map(\.name).first?.hasSuffix("-1.png") == false)
    }

    @Test func sharedUploadsTakeTheSameSources() async throws {
        let stub = Stub { r in
            guard r.method == "POST", r.path == "/api/agents/scout/shared/files" else { return (404, body("{}")) }
            let name = r.query["name"] ?? ""
            return (201, body(#"{"id": \#(name.count), "agent": "scout", "kind": "file", "title": "\#(name)", "created_at": 5}"#))
        }
        let defaults = UserDefaults(suiteName: "valet-tests-\(UUID().uuidString)")!
        defaults.set("http://127.0.0.1:9", forKey: "server")
        let m = AppModel(store: MemoryTokenStore(["token": "vown_t"]), defaults: defaults, transport: FakeTransport(),
                         urlSession: stub.session)
        let list = SharedList(model: m, agent: "scout")
        let failed = await list.upload([
            UploadSource(name: "a.txt") { Data("a".utf8) },
            UploadSource(name: "gone.bin") { throw CocoaError(.fileNoSuchFile) },
            UploadSource(name: "Pasted image.png") { Self.png },
        ])
        #expect(list.items.map(\.title).sorted() == ["Pasted image.png", "a.txt"])
        #expect(stub.recorded.map(\.query).compactMap { $0["name"] } == ["a.txt", "Pasted image.png"])
        #expect(stub.recorded.last?.raw == Self.png)
        #expect(failed?.hasPrefix("gone.bin: ") == true)
        #expect(!(failed ?? "").contains("a.txt"))
        #expect(list.uploading == 0)
        #expect(await list.upload([]) == nil)
    }

    #if os(macOS)
    func pasteboard() -> NSPasteboard {
        let pb = NSPasteboard(name: .init("valet-tests-\(UUID().uuidString)"))
        pb.clearContents()
        return pb
    }

    @Test func pastesOfFilesAndPictures() async throws {
        let pb = pasteboard()
        defer { pb.releaseGlobally() }
        // Files copied in the Finder (which also puts their names as text): the files.
        let a = try file("a.txt", "A"), b = try file("b.md", "B")
        pb.writeObjects([a as NSURL, b as NSURL])
        let files = try #require(PickedFile.sources(pasteboard: pb))
        #expect(files.map(\.name) == ["a.txt", "b.md"])
        #expect(try await files[1].data() == Data("B".utf8))
        // A picture alone: a PNG.
        pb.clearContents()
        pb.setData(Self.png, forType: .png)
        let shot = try #require(PickedFile.sources(pasteboard: pb))
        #expect(shot.count == 1 && shot[0].name.hasPrefix("Pasted image ") && shot[0].name.hasSuffix(".png"))
        #expect(try await shot[0].data() == Self.png)
        // A TIFF (what Preview copies) becomes a PNG.
        pb.clearContents()
        let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: 2, pixelsHigh: 2, bitsPerSample: 8,
                                   samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                                   bytesPerRow: 0, bitsPerPixel: 0)!
        pb.setData(rep.tiffRepresentation, forType: .tiff)
        let tiff = try #require(PickedFile.sources(pasteboard: pb))
        #expect(try await tiff[0].data().starts(with: [0x89, 0x50, 0x4E, 0x47]))
        // A picture with text: the composer pastes the text; Shared (nowhere to type) takes the picture.
        pb.clearContents()
        pb.setString("a caption", forType: .string)
        pb.setData(Self.png, forType: .png)
        #expect(PickedFile.sources(pasteboard: pb) == nil)
        #expect(PickedFile.sources(pasteboard: pb, orText: false)?.count == 1)
        // Text alone: nothing to upload.
        pb.clearContents()
        pb.setString("hello", forType: .string)
        #expect(PickedFile.sources(pasteboard: pb, orText: false) == nil)
    }

    @Test func onlyCommandVInItsOwnWindowUploads() throws {
        _ = NSApplication.shared
        let pb = pasteboard()
        defer { pb.releaseGlobally() }
        pb.setData(Self.png, forType: .png)
        let mine = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 200, height: 100), styleMask: [.titled],
                            backing: .buffered, defer: false)
        let other = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 200, height: 100), styleMask: [.titled],
                             backing: .buffered, defer: false)
        func key(_ chars: String, _ flags: NSEvent.ModifierFlags, in w: NSWindow) -> NSEvent {
            NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: flags, timestamp: 0,
                             windowNumber: w.windowNumber, context: nil, characters: chars,
                             charactersIgnoringModifiers: chars, isARepeat: false, keyCode: 9)!
        }
        let watcher = PasteWatcher(orText: true)
        watcher.window = mine
        #expect(watcher.sources(for: key("v", .command, in: mine), pasteboard: pb) == nil)     // not enabled
        watcher.enabled = true
        #expect(watcher.sources(for: key("v", .command, in: mine), pasteboard: pb)?.count == 1)
        #expect(watcher.sources(for: key("v", .command, in: other), pasteboard: pb) == nil)    // another window
        #expect(watcher.sources(for: key("c", .command, in: mine), pasteboard: pb) == nil)
        #expect(watcher.sources(for: key("v", [.command, .shift], in: mine), pasteboard: pb) == nil)
        #expect(watcher.sources(for: key("v", [], in: mine), pasteboard: pb) == nil)
        pb.clearContents()
        pb.setString("words", forType: .string)
        #expect(watcher.sources(for: key("v", .command, in: mine), pasteboard: pb) == nil)     // text pastes as text
        // Shared (nowhere to type): a picture with text uploads, unless a text field (its search) is being edited.
        let shared = PasteWatcher(orText: false)
        shared.window = mine
        shared.enabled = true
        pb.setData(Self.png, forType: .png)
        #expect(shared.sources(for: key("v", .command, in: mine), pasteboard: pb)?.count == 1)
        let search = NSTextField(frame: NSRect(x: 0, y: 0, width: 100, height: 20))
        mine.contentView?.addSubview(search)
        #expect(mine.makeFirstResponder(search))
        #expect(mine.firstResponder is NSText)
        #expect(shared.sources(for: key("v", .command, in: mine), pasteboard: pb) == nil)
    }
    #endif
}
