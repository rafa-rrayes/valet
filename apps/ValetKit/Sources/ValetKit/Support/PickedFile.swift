import Foundation
import PhotosUI
import SwiftUI
import UniformTypeIdentifiers

/// What the owner hands the app to upload, from wherever it comes (the Files picker, Photos, a drop, ⌘V on the
/// Mac), as `UploadSource`s: a name now, the bytes when its turn comes. The composer's attachments, Shared and the
/// agent's Files all take them.
enum PickedFile {
    static func read(_ url: URL) throws -> Data {
        let scoped = url.startAccessingSecurityScopedResource()
        defer { if scoped { url.stopAccessingSecurityScopedResource() } }
        return try Data(contentsOf: url, options: .mappedIfSafe)
    }

    /// "2026-11-02 14.05.01", for a photo's name.
    static func stamp(_ date: Date = Date()) -> String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd HH.mm.ss"
        return f.string(from: date)
    }

    /// Picked or dropped files, read when their turn comes.
    static func sources(_ urls: [URL]) -> [UploadSource] {
        urls.map { url in UploadSource(name: url.lastPathComponent) { try read(url) } }
    }

    /// Photos, named "Photo <stamp>.<ext>" (-1, -2... for several).
    static func sources(_ picked: [PhotosPickerItem]) -> [UploadSource] {
        let now = stamp()
        return picked.enumerated().map { i, p in
            let ext = p.supportedContentTypes.first?.preferredFilenameExtension ?? "jpg"
            let name = picked.count > 1 ? "Photo \(now)-\(i + 1).\(ext)" : "Photo \(now).\(ext)"
            return UploadSource(name: name) {
                guard let data = try await p.loadTransferable(type: Data.self) else { throw CocoaError(.fileReadUnknown) }
                return data
            }
        }
    }

    // MARK: drops

    /// What a drop takes: files, and pictures that aren't files (dragged out of a browser, or Photos).
    static let dropTypes: [UTType] = [.fileURL, .image]

    /// Dropped items, in order: a file (from the Finder, the Files app) read when its turn comes; a picture that
    /// isn't a file loaded now, named as the drag names it or "Dropped image <stamp>". Anything else is skipped.
    @MainActor static func sources(dropped providers: [NSItemProvider]) async -> [UploadSource] {
        let now = stamp()
        var out: [UploadSource] = []
        for (i, p) in providers.enumerated() {
            if p.hasItemConformingToTypeIdentifier(UTType.fileURL.identifier), let url = await fileURL(p) {
                out += sources([url])
            } else if let type = p.registeredContentTypes.first(where: { $0.conforms(to: .image) }),
                      let data = await data(p, type) {
                let ext = type.preferredFilenameExtension ?? "png"
                var name = p.suggestedName?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
                if name.isEmpty { name = providers.count > 1 ? "Dropped image \(now)-\(i + 1)" : "Dropped image \(now)" }
                // "Screenshot 2026-11-02 at 21.14.59" has no extension, though it has a dot.
                if UTType(filenameExtension: (name as NSString).pathExtension)?.isDeclared != true { name += ".\(ext)" }
                out.append(UploadSource(name: name) { data })
            }
        }
        return out
    }

    @MainActor private static func fileURL(_ p: NSItemProvider) async -> URL? {
        await withCheckedContinuation { c in
            _ = p.loadObject(ofClass: URL.self) { url, _ in c.resume(returning: url?.isFileURL == true ? url : nil) }
        }
    }

    @MainActor private static func data(_ p: NSItemProvider, _ type: UTType) async -> Data? {
        await withCheckedContinuation { c in
            _ = p.loadDataRepresentation(for: type) { data, _ in c.resume(returning: data) }
        }
    }

    // MARK: pastes

    #if os(macOS)
    /// What ⌘V would upload: the files copied in the Finder, or a picture as a PNG. `orText`: where text can be pasted
    /// (the composer), a picture that comes with text pastes the text instead. nil: nothing to upload.
    static func sources(pasteboard pb: NSPasteboard, orText: Bool = true) -> [UploadSource]? {
        if let urls = pb.readObjects(forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true]) as? [URL],
           !urls.isEmpty {
            return sources(urls)
        }
        guard !(orText && pb.string(forType: .string) != nil), pb.availableType(from: [.png, .tiff]) != nil,
              let png = pb.data(forType: .png)
                ?? pb.data(forType: .tiff).flatMap({ NSBitmapImageRep(data: $0)?.representation(using: .png, properties: [:]) })
        else { return nil }
        return [UploadSource(name: "Pasted image \(stamp()).png") { png }]
    }
    #endif
}

#if os(macOS)
/// ⌘V with files (copied in the Finder) or a picture on the pasteboard uploads them (`PickedFile.sources(pasteboard:)`);
/// anything else pastes as it always does. A key monitor, because a text field's editor takes Paste first. Only in
/// its own window (`window`: not in a sheet over it, nor the menu bar's), and only while `enabled`.
@MainActor
final class PasteWatcher {
    var enabled = false
    /// Where the composer or the list is (WindowReader).
    weak var window: NSWindow?
    /// Whether text can be pasted there instead (the composer: a picture that comes with text pastes the text).
    /// Anywhere, a text field being edited (Shared's search) counts too.
    let orText: Bool
    private var monitor: Any?

    init(orText: Bool) { self.orText = orText }

    func start(_ take: @escaping ([UploadSource]) -> Void) {
        stop()
        monitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] e in
            guard let self, let sources = self.sources(for: e, pasteboard: .general) else { return e }
            take(sources)
            return nil
        }
    }

    func stop() {
        if let monitor { NSEvent.removeMonitor(monitor) }
        monitor = nil
    }

    /// What this key event uploads: ⌘V, here, with something to upload; nil lets it through.
    func sources(for e: NSEvent, pasteboard: NSPasteboard) -> [UploadSource]? {
        guard enabled, e.type == .keyDown,
              e.modifierFlags.intersection(.deviceIndependentFlagsMask) == .command,
              e.charactersIgnoringModifiers?.lowercased() == "v",
              let window, e.windowNumber == window.windowNumber, window.attachedSheet == nil else { return nil }
        return PickedFile.sources(pasteboard: pasteboard, orText: orText || window.firstResponder is NSText)
    }
}

/// The window a SwiftUI view is in, as it changes.
struct WindowReader: NSViewRepresentable {
    let found: (NSWindow?) -> Void

    func makeNSView(context: Context) -> ReaderView {
        let v = ReaderView()
        v.found = found
        return v
    }

    func updateNSView(_ v: ReaderView, context: Context) { v.found = found }

    final class ReaderView: NSView {
        var found: ((NSWindow?) -> Void)?

        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            found?(window)
        }
    }
}
#endif
