import Foundation
import Observation

// The agent's terminal (docs/API.md, "The terminal"): one shell per `/ws/terminal/{agent}?cols=&rows=` socket.
// Binary frames both ways are the terminal's bytes (its output; the owner's keystrokes); text frames are JSON: the
// server's ready, exit and error, the owner's resize. The web UI's terminal.js is the reference.

/// What the server says on a terminal's socket (text frames).
public enum TerminalMessage: Equatable, Sendable {
    /// The shell runs.
    case ready(terminal: String, agent: String)
    /// It ended: the shell exited (`code`), or `reason` ("closed", "the agent stopped", "the runner disconnected"...).
    case exit(code: Int?, reason: String?)
    /// It couldn't open (not running, no computer, too many, runner too old).
    case error(String)
    case unknown

    public static func decode(_ text: String) -> TerminalMessage {
        guard let v = try? JSONDecoder().decode(JSONValue.self, from: Data(text.utf8)) else { return .unknown }
        switch v["type"]?.string {
        case "ready": return .ready(terminal: v["terminal"]?.text ?? "", agent: v["agent"]?.string ?? "")
        case "exit":
            let reason = v["reason"]?.string.flatMap { $0.isEmpty ? nil : $0 }
            return .exit(code: v["code"]?.int, reason: reason)
        case "error": return .error(v["message"]?.string ?? "The terminal couldn't open.")
        default: return .unknown
        }
    }

    /// The line under the tabs once the shell is over (terminal.js's words).
    var endText: String? {
        switch self {
        case .exit(let code, let reason):
            if let reason { return "Closed: \(reason)." }
            return code.map { "The shell exited (\($0))." } ?? "The shell exited."
        case .error(let message): return message
        default: return nil
        }
    }
}

/// A terminal's size: what the server takes (2..1000 each).
public struct TerminalSize: Equatable, Sendable {
    public var cols: Int
    public var rows: Int

    public init(cols: Int, rows: Int) {
        self.cols = min(1000, max(2, cols))
        self.rows = min(1000, max(2, rows))
    }

    /// `{"type": "resize", "cols", "rows"}`.
    public var message: String { #"{"cols":\#(cols),"rows":\#(rows),"type":"resize"}"# }
}

/// One frame on a terminal's socket.
public enum TerminalFrame: Equatable, Sendable {
    case text(String)
    case binary(Data)
}

/// One terminal socket, as `TerminalSession` uses it: URLSessionWebSocketTask in the apps, a script in the tests.
public protocol TerminalConnection: AnyObject, Sendable {
    /// The next frame. Throws once the connection is gone.
    func receive() async throws -> TerminalFrame
    func send(_ frame: TerminalFrame)
    /// A ping, so a quiet shell's connection isn't taken for dead (and a dead one is found).
    func ping() async throws
    func close()
    /// The close code the server sent (4401: not the owner), once receive has thrown; 0 if none.
    var closeCode: Int { get }
}

final class WebSocketTerminalConnection: TerminalConnection {
    let task: URLSessionWebSocketTask

    init(request: URLRequest, session: URLSession = .shared) {
        task = session.webSocketTask(with: request)
        task.maximumMessageSize = 4 << 20
        task.resume()
    }

    func receive() async throws -> TerminalFrame {
        switch try await task.receive() {
        case .data(let d): return .binary(d)
        case .string(let s): return .text(s)
        @unknown default: return .text("")
        }
    }

    func send(_ frame: TerminalFrame) {
        switch frame {
        case .text(let s): task.send(.string(s)) { _ in }
        case .binary(let d): task.send(.data(d)) { _ in }
        }
    }

    func ping() async throws { try await firstAnswer(task.sendPing) }

    func close() { task.cancel(with: .normalClosure, reason: nil) }

    var closeCode: Int { task.closeCode.rawValue }
}

/// One shell in an agent's computer. It connects once its view knows how big it is (the size goes in the URL), and
/// then tells the server only real changes of size (the newest one once the shell is ready, if it changed while it
/// connected). Output goes to the view in order (kept until a view takes it); keystrokes go out as binary frames once
/// the shell runs. A shell that ends stays ended: there's no reconnecting to it (the web UI's way); a new one is a
/// new session.
@MainActor @Observable
public final class TerminalSession: Identifiable {
    public enum State: Equatable, Sendable {
        /// Not connected yet: it waits for its view's size.
        case waiting
        case connecting
        case open
        /// Over, and why (the line to show).
        case ended(String)
    }

    /// Its number in the screen's tabs ("Shell 2").
    public let id: Int
    public let agent: String
    public private(set) var state: State = .waiting
    /// The server's id for it, once it's ready.
    public private(set) var terminalID: String?

    /// Where the output goes (the view): output that came before it's set is handed over at once.
    @ObservationIgnored public var onOutput: ((Data) -> Void)? {
        didSet { flushOutput() }
    }
    /// The size the shell has, or is being opened with.
    @ObservationIgnored public private(set) var size: TerminalSize?
    @ObservationIgnored private var wanted: TerminalSize?
    @ObservationIgnored private var pending: [Data] = []
    @ObservationIgnored private var connection: (any TerminalConnection)?
    @ObservationIgnored private var receiveLoop: Task<Void, Never>?
    @ObservationIgnored private var pingLoop: Task<Void, Never>?
    /// Seconds between pings (0: none).
    @ObservationIgnored var pingEvery: Double = 20
    @ObservationIgnored private let connect: @MainActor (TerminalSize) -> any TerminalConnection

    public init(id: Int, agent: String, connect: @escaping @MainActor (TerminalSize) -> any TerminalConnection) {
        self.id = id
        self.agent = agent
        self.connect = connect
    }

    public convenience init(id: Int, agent: String, api: APIClient) {
        self.init(id: id, agent: agent) { size in
            WebSocketTerminalConnection(request: api.terminalRequest(agent: agent, size: size))
        }
    }

    deinit {
        // The screen's backstop: a session freed without close() (its view gone) still ends its shell.
        connection?.close()
    }

    public var isEnded: Bool { if case .ended = state { return true } else { return false } }

    /// The line under the tabs: "Connecting…", nothing while it runs, why it ended.
    public var statusText: String {
        switch state {
        case .waiting, .connecting: return "Connecting…"
        case .open: return ""
        case .ended(let why): return why
        }
    }

    // MARK: the view's side

    /// The view's size in cells, after every layout: the first one connects, later ones resize the shell.
    public func resize(cols: Int, rows: Int) {
        let s = TerminalSize(cols: cols, rows: rows)
        switch state {
        case .waiting:
            size = s
            open(s)
        case .connecting:
            wanted = s
        case .open:
            guard s != size else { return }
            size = s
            connection?.send(.text(s.message))
        case .ended:
            break
        }
    }

    /// Keystrokes (bytes for the PTY), once the shell runs.
    public func send(_ bytes: Data) {
        guard state == .open, !bytes.isEmpty else { return }
        connection?.send(.binary(bytes))
    }

    public func send(_ text: String) { send(Data(text.utf8)) }

    /// Ends the shell (closing the socket ends it and everything it started).
    public func close() {
        receiveLoop?.cancel()
        receiveLoop = nil
        pingLoop?.cancel()
        pingLoop = nil
        connection?.close()
        connection = nil
        if !isEnded { state = .ended("Closed.") }
    }

    // MARK: the socket

    private func open(_ s: TerminalSize) {
        state = .connecting
        let conn = connect(s)
        connection = conn
        receiveLoop = Task { [weak self] in
            do {
                while !Task.isCancelled {
                    let frame = try await conn.receive()
                    guard let self, self.connection === conn else { return }
                    self.handle(frame)
                }
            } catch {
                guard let self, self.connection === conn else { return }
                self.dropped(code: conn.closeCode)
            }
        }
        let every = pingEvery
        guard every > 0 else { return }
        pingLoop = Task {
            while !Task.isCancelled {
                do {
                    try await Task.sleep(for: .seconds(every))
                    try await conn.ping()
                } catch {
                    if !Task.isCancelled { conn.close() }     // receive() then throws: Disconnected
                    return
                }
            }
        }
    }

    func handle(_ frame: TerminalFrame) {
        switch frame {
        case .binary(let data):
            guard !data.isEmpty else { return }
            if let onOutput, pending.isEmpty { onOutput(data) } else { pending.append(data) }
        case .text(let text):
            let m = TerminalMessage.decode(text)
            switch m {
            case .ready(let id, _):
                guard state == .connecting else { return }
                terminalID = id
                state = .open
                if let w = wanted, w != size {
                    size = w
                    connection?.send(.text(w.message))
                }
                wanted = nil
            case .exit, .error:
                end(m.endText ?? "Closed.")
            case .unknown:
                break
            }
        }
    }

    private func dropped(code: Int) {
        connection = nil
        receiveLoop = nil
        pingLoop?.cancel()
        pingLoop = nil
        guard !isEnded else { return }
        end(Self.closeText(code: code, connecting: state == .connecting, agent: agent))
    }

    private func end(_ why: String) {
        guard !isEnded else { return }
        state = .ended(why)
        pingLoop?.cancel()
        pingLoop = nil
        connection?.close()
    }

    /// Why the socket closed, when no message said.
    nonisolated static func closeText(code: Int, connecting: Bool, agent: String) -> String {
        switch code {
        case 4401: return "Sign in again."
        case 4403: return "Valet refused the terminal from here."
        case 4404: return "There's no agent named \(agent)."
        default: return connecting ? "Couldn't connect." : "Disconnected."
        }
    }

    private func flushOutput() {
        guard let onOutput, !pending.isEmpty else { return }
        let out = pending
        pending = []
        for d in out { onOutput(d) }
    }
}

/// An agent's shells, as the terminal screen's tabs: open one (at most 8, the runner's limit), switch, close one,
/// close them all when the screen goes away.
@MainActor @Observable
public final class TerminalTabs {
    public static let max = 8

    public let agent: String
    public private(set) var shells: [TerminalSession] = []
    public var current: Int?
    @ObservationIgnored private var counter = 0
    @ObservationIgnored private let make: @MainActor (Int) -> TerminalSession

    public init(agent: String, make: @escaping @MainActor (Int) -> TerminalSession) {
        self.agent = agent
        self.make = make
    }

    public convenience init(agent: String, api: APIClient) {
        self.init(agent: agent) { TerminalSession(id: $0, agent: agent, api: api) }
    }

    public var live: Int { shells.filter { !$0.isEnded }.count }
    public var canOpen: Bool { live < Self.max }
    public var shown: TerminalSession? { shells.first { $0.id == current } }

    @discardableResult
    public func open() -> TerminalSession? {
        guard canOpen else { return nil }
        counter += 1
        let s = make(counter)
        shells.append(s)
        current = s.id
        return s
    }

    /// Ends the shell and takes its tab away; the next tab (or the one before) shows.
    public func close(_ s: TerminalSession) {
        s.close()
        guard let i = shells.firstIndex(where: { $0 === s }) else { return }
        shells.remove(at: i)
        if current == s.id { current = shells.isEmpty ? nil : shells[min(i, shells.count - 1)].id }
    }

    public func closeAll() {
        for s in shells { s.close() }
        shells = []
        current = nil
    }
}
