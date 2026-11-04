import Foundation
import Observation

/// Something to upload: its name, and its bytes when its turn comes (a picked file, a photo, a drop).
public struct UploadSource: Sendable {
    public var name: String
    public var data: @Sendable () async throws -> Data

    public init(name: String, data: @escaping @Sendable () async throws -> Data) {
        self.name = name
        self.data = data
    }
}

/// A name an upload found taken, while the owner decides (each ask its own id).
public struct UploadClash: Identifiable, Equatable, Sendable {
    public let id: Int
    public let name: String
}

/// How a file shows on its screen.
public enum FilePreview: Sendable {
    case text(FileText)
    case image(Data)
    /// A picture too big to fetch just to show it.
    case tooBig
    case failed(String)
}

/// One screen of the agent's files: a folder (or, with no path, its first screen: the first root, and the roots),
/// or a file and its preview. Uploads go one at a time; a name that's taken (in the listing, or a 409 from the
/// server) waits on the owner's answer: Replace, Keep both, or Cancel. A container agent's first screen also has
/// its volumes. The web UI's FilesView and filesVolumes.
@MainActor @Observable
public final class FilesModel {
    public let agent: String
    /// nil: the first screen.
    public let path: String?
    @ObservationIgnored let api: APIClient

    public private(set) var listing: FileListing?
    public private(set) var loadError: String?
    public private(set) var preview: FilePreview?
    /// "Uploading notes.txt…", "Making the folder…", "Downloading…".
    public private(set) var busy: String?
    /// What went wrong with the last uploads or the new folder (the listing still shows).
    public var message: String?
    /// The name an upload found taken, while the owner decides.
    public private(set) var clash: UploadClash?
    @ObservationIgnored private var answer: CheckedContinuation<UploadChoice?, Never>?
    @ObservationIgnored private var seq = 0
    @ObservationIgnored private var asks = 0

    public private(set) var volumes: AgentVolumes?
    public private(set) var volumesError: String?
    /// "Deleted valet-agent-x-home and valet-agent-x-work."
    public private(set) var volumesDone: String?
    public private(set) var deletingVolumes = false

    /// Pictures bigger than this aren't fetched for their preview (Quick Look or Share fetches them).
    static let imagePreviewLimit = 20 << 20

    public init(agent: String, path: String?, api: APIClient) {
        self.agent = agent
        self.path = path
        self.api = api
    }

    // MARK: loading

    public func load() async {
        seq += 1
        let mine = seq
        do {
            let l = try await api.files(agent, path: path)
            guard mine == seq else { return }
            let pv = l.type == .file ? await loadPreview(l) : nil
            guard mine == seq else { return }
            listing = l
            preview = pv
            loadError = nil
        } catch is CancellationError {
        } catch {
            guard mine == seq else { return }
            listing = nil
            loadError = error.localizedDescription
        }
    }

    private func loadPreview(_ l: FileListing) async -> FilePreview {
        do {
            if FilePaths.isImage(l.path) {
                if l.size > Self.imagePreviewLimit { return .tooBig }
                return .image(try await api.downloadFile(agent, path: l.path))
            }
            return .text(try await api.fileText(agent, path: l.path))
        } catch {
            return .failed(error.localizedDescription)
        }
    }

    // MARK: uploads and folders

    /// Uploads into the folder shown, one by one, asking about each name that's taken; then reads it again.
    public func upload(_ items: [UploadSource]) async {
        guard let dir = listing, dir.type == .dir, dir.writable, !items.isEmpty, busy == nil else { return }
        message = nil
        var failed: [String] = []
        for item in items {
            var choice = UploadChoice.fail
            if listing?.has(item.name) == true {
                guard let c = await ask(item.name) else { continue }
                choice = c
            }
            busy = "Uploading \(item.name)…"
            do {
                let data = try await item.data()
                do {
                    _ = try await api.uploadFile(agent, dir: dir.path, name: item.name, data: data, exists: choice)
                } catch let e as APIError where e.status == 409 && choice == .fail {
                    // Taken since the listing was read.
                    busy = nil
                    guard let c = await ask(item.name) else { continue }
                    busy = "Uploading \(item.name)…"
                    _ = try await api.uploadFile(agent, dir: dir.path, name: item.name, data: data, exists: c)
                }
            } catch is CancellationError {
                break
            } catch {
                failed.append("\(item.name): \(error.localizedDescription)")
            }
        }
        busy = nil
        message = failed.isEmpty ? nil : failed.joined(separator: "\n")
        await load()
    }

    private func ask(_ name: String) async -> UploadChoice? {
        choose(nil)
        asks += 1
        clash = UploadClash(id: asks, name: name)
        return await withCheckedContinuation { answer = $0 }
    }

    /// The answer about a taken name: `.replace`, `.rename` (Keep both), or nil (Cancel: that file isn't sent).
    /// With `ask`, only that question is answered (a dialog that goes away late doesn't cancel the next one).
    public func choose(_ choice: UploadChoice?, ask: Int? = nil) {
        guard let k = answer, ask == nil || ask == clash?.id else { return }
        answer = nil
        clash = nil
        k.resume(returning: choice)
    }

    /// Makes a folder in the one shown; false (and why, in `message`) when it couldn't.
    @discardableResult
    public func makeFolder(_ name: String) async -> Bool {
        if let problem = FilePaths.folderNameProblem(name) {
            message = problem
            return false
        }
        guard let l = listing, l.type == .dir, busy == nil else { return false }
        busy = "Making the folder…"
        defer { busy = nil }
        do {
            _ = try await api.makeFolder(agent, path: FilePaths.join(l.path, name.trimmingCharacters(in: .whitespaces)))
            message = nil
            await load()
            return true
        } catch {
            message = error.localizedDescription
            return false
        }
    }

    // MARK: the file shown

    /// The file shown, downloaded into a temporary folder under its own name (Quick Look, Share, Save, Open).
    public func download() async throws -> URL {
        guard let l = listing, l.type == .file else { throw CancellationError() }
        busy = "Downloading…"
        defer { busy = nil }
        let data: Data
        if case .image(let d) = preview { data = d } else { data = try await api.downloadFile(agent, path: l.path) }
        return try FileStash.write(data, name: FilePaths.name(l.path), folder: "computer/" + FileStash.safeName(agent, fallback: "agent"))
    }

    // MARK: volumes

    public func loadVolumes() async {
        do {
            volumes = try await api.volumes(agent)
            volumesError = nil
        } catch is CancellationError {
        } catch {
            volumesError = error.localizedDescription
        }
    }

    /// Deletes them for good (the server refuses while the agent runs).
    public func deleteVolumes() async {
        deletingVolumes = true
        defer { deletingVolumes = false }
        var done: String?, failed: String?
        do {
            let removed = try await api.deleteVolumes(agent)
            done = removed.isEmpty ? "There were none to delete." : "Deleted \(removed.formatted(.list(type: .and)))."
        } catch {
            failed = error.localizedDescription
        }
        await loadVolumes()
        volumesDone = done
        if let failed { volumesError = failed }
    }

    /// The confirmation's question: every volume by name, and where it's mounted.
    public var deleteVolumesQuestion: String {
        let names = (volumes?.volumes ?? []).map { "\($0.name) (\($0.mount))" }
        return "Delete \(names.formatted(.list(type: .and)))? Everything in its home and workspace goes, for good."
    }
}
