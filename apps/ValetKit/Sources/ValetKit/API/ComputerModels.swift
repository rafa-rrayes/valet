import Foundation

// The agent's files and volumes (docs/API.md, "The agent's computer"): a folder's listing, a file's text, an
// upload's answer, a container agent's Docker volumes. The web UI's files.js is the reference.

public struct FileKind: OpenEnum {
    public let rawValue: String
    public init(rawValue: String) { self.rawValue = rawValue }
    /// "other": not a file or folder, or a link that leads somewhere it may not go.
    public static let dir: Self = "dir", file: Self = "file", other: Self = "other"
}

/// One of the agent's folders the owner may look in: a container's /work (Workspace), /home/agent (Home) and its
/// shared folders; a host agent's working folder.
public struct FileRoot: Decodable, Hashable, Sendable {
    public var path: String
    public var label: String
    public var writable: Bool

    public init(path: String, label: String, writable: Bool = true) {
        self.path = path
        self.label = label
        self.writable = writable
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        guard let path: String = c.get("path") else { throw missing("path", in: decoder) }
        self.path = path
        label = c.get("label") ?? path
        writable = c.get("writable") ?? false
    }
}

/// What's in a folder: folders first, then by name (the server's order).
public struct FileEntry: Decodable, Hashable, Sendable, Identifiable {
    public var name: String
    public var type: FileKind
    /// A link: `type` is what it points to.
    public var link: Bool
    public var size: Int
    public var mtime: Double

    public var id: String { name }

    public init(name: String, type: FileKind = .file, link: Bool = false, size: Int = 0, mtime: Double = 0) {
        self.name = name
        self.type = type
        self.link = link
        self.size = size
        self.mtime = mtime
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        guard let name: String = c.get("name") else { throw missing("name", in: decoder) }
        self.name = name
        type = c.get("type") ?? .other
        link = c.get("link") ?? false
        size = c.get("size") ?? 0
        mtime = c.get("mtime") ?? 0
    }
}

/// `GET .../computer/files?path=`: what's at a path (a folder's entries, or a file's size), and the roots.
public struct FileListing: Decodable, Sendable {
    public var roots: [FileRoot]
    public var path: String
    public var type: FileKind
    public var size: Int
    public var mtime: Double
    public var entries: [FileEntry]
    /// More entries than the server lists (2000).
    public var truncated: Bool

    public init(roots: [FileRoot], path: String, type: FileKind = .dir, size: Int = 0, mtime: Double = 0,
                entries: [FileEntry] = [], truncated: Bool = false) {
        self.roots = roots
        self.path = path
        self.type = type
        self.size = size
        self.mtime = mtime
        self.entries = entries
        self.truncated = truncated
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        guard let path: String = c.get("path") else { throw missing("path", in: decoder) }
        self.path = path
        roots = c.list("roots")
        type = c.get("type") ?? .other
        size = c.get("size") ?? 0
        mtime = c.get("mtime") ?? 0
        entries = c.list("entries")
        truncated = c.get("truncated") ?? false
    }

    /// The root this is in (the deepest that holds it).
    public var root: FileRoot? { FilePaths.root(holding: path, in: roots) }
    /// Whether uploads and new folders may go here (not a read-only shared folder).
    public var writable: Bool { root?.writable ?? false }
    public var crumbs: [FilePaths.Crumb] { FilePaths.crumbs(path, roots: roots) }
    public func has(_ name: String) -> Bool { entries.contains { $0.name == name } }
}

/// `GET .../computer/files/text?path=`: the first MiB of a file as text, or that it isn't text.
public struct FileText: Decodable, Sendable, Equatable {
    public var path: String
    public var size: Int
    public var text: String?
    public var binary: Bool
    public var truncated: Bool

    public init(path: String, size: Int = 0, text: String? = nil, binary: Bool = false, truncated: Bool = false) {
        self.path = path
        self.size = size
        self.text = text
        self.binary = binary
        self.truncated = truncated
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        path = c.get("path") ?? ""
        size = c.get("size") ?? 0
        text = c.get("text")
        binary = c.get("binary") ?? (text == nil)
        truncated = c.get("truncated") ?? false
    }
}

/// An upload's answer (201): where it went (another name with Keep both) and its size.
public struct UploadedFile: Decodable, Sendable, Equatable {
    public var path: String
    public var size: Int
}

/// What an upload does when the name is taken: `exists=fail` (409), `replace`, `rename` ("Keep both": name-2.ext).
public enum UploadChoice: String, Sendable {
    case fail, replace, rename
}

public struct AgentVolume: Decodable, Hashable, Sendable {
    public var name: String
    public var mount: String
}

/// `GET .../computer/volumes`: a container agent's Docker volumes on the runner it was placed on.
public struct AgentVolumes: Decodable, Sendable {
    public var runner: String?
    public var running: Bool
    public var volumes: [AgentVolume]

    public init(runner: String? = nil, running: Bool = false, volumes: [AgentVolume] = []) {
        self.runner = runner
        self.running = running
        self.volumes = volumes
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        runner = c.get("runner")
        running = c.get("running") ?? false
        volumes = c.list("volumes")
    }
}

/// Paths in the agent's computer: absolute, "/"-separated (the server checks them; these only build them).
public enum FilePaths {
    /// One step of where a path is: its root (by label), then each folder below it.
    public struct Crumb: Hashable, Sendable {
        public var label: String
        public var path: String
    }

    public static func join(_ dir: String, _ name: String) -> String {
        var d = dir
        while d.count > 1 && d.hasSuffix("/") { d.removeLast() }
        return (d == "/" ? "" : d) + "/" + name
    }

    /// The folder a path is in ("/" for "/" and its children).
    public static func parent(_ path: String) -> String {
        guard let i = path.lastIndex(of: "/"), i != path.startIndex else { return "/" }
        return String(path[..<i])
    }

    public static func name(_ path: String) -> String {
        path.split(separator: "/").last.map(String.init) ?? path
    }

    /// The deepest root that holds `path` (a shared folder under /work over /work).
    public static func root(holding path: String, in roots: [FileRoot]) -> FileRoot? {
        roots.filter { r in path == r.path || path.hasPrefix(r.path == "/" ? "/" : r.path + "/") }
            .max { $0.path.count < $1.path.count }
    }

    /// Its root's label, then each folder down to it; outside every root, the path itself.
    public static func crumbs(_ path: String, roots: [FileRoot]) -> [Crumb] {
        guard let root = root(holding: path, in: roots) else { return [Crumb(label: path, path: path)] }
        var out = [Crumb(label: root.label, path: root.path)]
        var cur = root.path
        for seg in path.dropFirst(root.path.count).split(separator: "/") {
            cur = join(cur, String(seg))
            out.append(Crumb(label: String(seg), path: cur))
        }
        return out
    }

    /// Why a new folder's name won't do (nil: it will).
    public static func folderNameProblem(_ name: String) -> String? {
        let n = name.trimmingCharacters(in: .whitespacesAndNewlines)
        if n.isEmpty || n.contains("/") || n == "." || n == ".." { return "A folder's name, without /." }
        return nil
    }

    static let imageExtensions: Set<String> = ["png", "jpg", "jpeg", "gif", "webp", "avif", "bmp", "heic", "heif",
                                               "tif", "tiff"]

    /// A picture the apps can show (by its name, as the web UI does).
    public static func isImage(_ name: String) -> Bool {
        guard let dot = name.lastIndex(of: ".") else { return false }
        return imageExtensions.contains(name[name.index(after: dot)...].lowercased())
    }
}

// MARK: attachments

/// A file the owner attached to a message (docs/API.md, "Attachments"): uploaded first, kept on the control plane
/// until a prompt carries it (an image the harness takes goes as the image; anything else is put in the agent's
/// computer, and the message names the path).
public struct Attachment: Decodable, Hashable, Sendable, Identifiable {
    public var id: String
    public var agent: String
    public var name: String
    public var mime: String
    public var size: Int
    /// It goes as an image (the harness takes images).
    public var image: Bool
    public var sent: Bool
    /// Where a file was put in the agent's computer, once it was.
    public var path: String?
    /// Where to GET it (`/api/agents/<name>/attachments/<id>`).
    public var url: String

    public init(id: String, agent: String = "", name: String, mime: String = "application/octet-stream", size: Int = 0,
                image: Bool = false, sent: Bool = false, path: String? = nil, url: String = "") {
        self.id = id
        self.agent = agent
        self.name = name
        self.mime = mime
        self.size = size
        self.image = image
        self.sent = sent
        self.path = path
        self.url = url
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        guard let id: String = c.get("id") else { throw missing("id", in: decoder) }
        self.id = id
        agent = c.get("agent") ?? ""
        name = c.get("name") ?? id
        mime = c.get("mime") ?? "application/octet-stream"
        size = c.get("size") ?? 0
        image = c.get("image") ?? false
        sent = c.get("sent") ?? false
        path = c.get("path")
        url = c.get("url") ?? ""
    }
}
