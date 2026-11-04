import Foundation
import os

/// One WebSocket connection, as the socket uses it. URLSessionWebSocketTask in the apps; a script in the tests.
public protocol SocketConnection: AnyObject, Sendable {
    /// The next text frame (a binary one is read as UTF-8). Throws once the connection is gone.
    func receive() async throws -> String
    func ping() async throws
    func close()
    /// The close code the server sent (4401: not signed in), once receive has thrown; 0 if none.
    var closeCode: Int { get }
}

public protocol SocketTransport: Sendable {
    func connect(_ request: URLRequest) -> any SocketConnection
}

/// `GET /ws/client` (docs/API.md, "Live updates"): a `hello` snapshot, then a frame per change. Frames are decoded
/// off the main actor and delivered in order. A dropped connection redials after 1 s, doubling to 30 s, back to
/// 1 s once a hello arrives; a 4401 close asks `onUnauthorized` first (the token may be gone). A ping every 25 s
/// finds a connection that died quietly (a phone that slept). Clients refetch what they missed after every hello
/// (events after the last id they saw), since frames sent while the socket was down are gone.
@MainActor
public final class ClientSocket {
    public enum State: Equatable, Sendable {
        case idle, connecting, live
        /// Down; the next attempt is in `retryIn` seconds.
        case offline(retryIn: Double)
    }

    public private(set) var state: State = .idle {
        didSet { if state != oldValue { onState(state) } }
    }

    public var onFrame: (Frame) -> Void = { _ in }
    public var onState: (State) -> Void = { _ in }
    /// Closed with 4401. Return true to keep redialing (still signed in: a hiccup), false to stop.
    public var onUnauthorized: () async -> Bool = { false }

    private let transport: any SocketTransport
    private let request: @MainActor () -> URLRequest
    private let sleep: @Sendable (Double) async throws -> Void
    private var connection: (any SocketConnection)?
    private var tasks: [Task<Void, Never>] = []
    private var retry: Task<Void, Never>?
    private var generation = 0
    private(set) var delay: Double = ClientSocket.firstDelay

    nonisolated static let firstDelay = 1.0
    nonisolated static let maxDelay = 30.0
    nonisolated static let pingEvery = 25.0

    public init(transport: any SocketTransport = URLSessionTransport(),
                request: @escaping @MainActor () -> URLRequest,
                sleep: @escaping @Sendable (Double) async throws -> Void = { try await Task.sleep(for: .seconds($0)) }) {
        self.transport = transport
        self.request = request
        self.sleep = sleep
    }

    /// Connects (or reconnects now, with the backoff reset).
    public func start() {
        delay = Self.firstDelay
        connect()
    }

    public func stop() {
        generation += 1
        teardown()
        state = .idle
    }

    public var isRunning: Bool { state != .idle }

    private func teardown() {
        retry?.cancel()
        retry = nil
        tasks.forEach { $0.cancel() }
        tasks = []
        connection?.close()
        connection = nil
    }

    private func connect() {
        generation += 1
        let gen = generation
        teardown()
        state = .connecting
        let conn = transport.connect(request())
        connection = conn
        tasks.append(Task.detached { [weak self] in
            do {
                while !Task.isCancelled {
                    let text = try await conn.receive()
                    guard let frame = Frame.decode(text) else { continue }
                    guard let self else { return }
                    await self.deliver(frame, gen)
                }
            } catch {
                await self?.dropped(gen, code: conn.closeCode)
            }
        })
        let every = Self.pingEvery
        let sleep = self.sleep
        tasks.append(Task.detached {
            while !Task.isCancelled {
                do {
                    try await sleep(every)
                    try await conn.ping()
                } catch {
                    if !Task.isCancelled { conn.close() }  // receive() then throws, and the socket redials
                    return
                }
            }
        })
    }

    private func deliver(_ frame: Frame, _ gen: Int) {
        guard gen == generation else { return }
        if case .hello = frame {
            delay = Self.firstDelay
            state = .live
        }
        onFrame(frame)
    }

    private func dropped(_ gen: Int, code: Int) async {
        guard gen == generation else { return }
        connection = nil
        tasks.forEach { $0.cancel() }
        tasks = []
        if code == 4401 {
            let keepGoing = await onUnauthorized()
            guard gen == generation, keepGoing else {
                if gen == generation { stop() }
                return
            }
        }
        let wait = delay
        delay = min(delay * 2, Self.maxDelay)
        state = .offline(retryIn: wait)
        let sleep = self.sleep
        retry = Task { [weak self] in
            do { try await sleep(wait) } catch { return }
            guard let self, gen == self.generation else { return }
            self.connect()
        }
    }
}

// MARK: URLSession

public struct URLSessionTransport: SocketTransport {
    let session: URLSession

    public init(session: URLSession = .shared) { self.session = session }

    public func connect(_ request: URLRequest) -> any SocketConnection {
        let task = session.webSocketTask(with: request)
        task.maximumMessageSize = 64 << 20      // a hello carries every agent and pending item
        task.resume()
        return URLSessionConnection(task: task)
    }
}

final class URLSessionConnection: SocketConnection {
    let task: URLSessionWebSocketTask

    init(task: URLSessionWebSocketTask) { self.task = task }

    func receive() async throws -> String {
        switch try await task.receive() {
        case .string(let s): return s
        case .data(let d): return String(decoding: d, as: UTF8.self)
        @unknown default: return ""
        }
    }

    func ping() async throws { try await firstAnswer(task.sendPing) }

    func close() { task.cancel(with: .normalClosure, reason: nil) }

    var closeCode: Int { task.closeCode.rawValue }
}

/// Waits for `send`'s handler and takes only its first answer. URLSessionWebSocketTask.sendPing can call its handler
/// more than once (a socket that dies, after the Mac sleeps, say), and a continuation resumed twice traps the app.
func firstAnswer(_ send: (@escaping @Sendable (Error?) -> Void) -> Void) async throws {
    let answered = OSAllocatedUnfairLock(initialState: false)
    try await withCheckedThrowingContinuation { (k: CheckedContinuation<Void, Error>) in
        send { error in
            let first = answered.withLock { was in defer { was = true }; return !was }
            guard first else { return }
            if let error { k.resume(throwing: error) } else { k.resume() }
        }
    }
}
