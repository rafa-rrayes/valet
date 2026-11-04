import Foundation
@testable import ValetKit

/// A recorded answer from Fixtures/ (a real control plane with the fake agent).
func fixture(_ name: String) throws -> Data {
    guard let url = Bundle.module.url(forResource: name, withExtension: nil, subdirectory: "Fixtures") else {
        throw NSError(domain: "fixture", code: 1, userInfo: [NSLocalizedDescriptionKey: "no fixture \(name)"])
    }
    return try Data(contentsOf: url)
}

func decode<T: Decodable>(_ type: T.Type, _ json: String) throws -> T {
    try JSONDecoder().decode(T.self, from: Data(json.utf8))
}

func json(_ text: String) -> JSONValue { try! JSONDecoder().decode(JSONValue.self, from: Data(text.utf8)) }

/// An event, the way the server sends it.
func ev(_ id: Int, _ kind: String, turn: String? = "t1", agent: String = "a", session: String = "s",
        _ data: JSONValue = [:]) -> Event {
    Event(id: id, agent: agent, sessionId: session, turnId: turn, ts: Double(id), kind: kind, data: data.object ?? [:])
}

func text(_ s: String) -> JSONValue { ["content": ["type": "text", "text": .string(s)]] }

// MARK: a stub URLProtocol

/// Answers a URLSession's requests from a handler and records them. Each `Stub` has its own session (a header
/// names it), so tests can run in parallel.
final class Stub: @unchecked Sendable {
    struct Recorded: Sendable {
        let method: String
        let url: URL
        let headers: [String: String]
        let body: JSONValue?
        var raw: Data? = nil

        var path: String { url.path(percentEncoded: true) }
        var query: [String: String] {
            Dictionary((URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? [])
                .map { ($0.name, $0.value ?? "") }, uniquingKeysWith: { _, b in b })
        }
    }

    typealias Handler = @Sendable (Recorded) throws -> (Int, Data)

    let id = UUID().uuidString
    let handler: Handler
    private let lock = NSLock()
    private var log: [Recorded] = []
    let session: URLSession

    nonisolated(unsafe) static var all: [String: Stub] = [:]
    static let lock = NSLock()

    init(_ handler: @escaping Handler) {
        self.handler = handler
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [StubProtocol.self]
        config.httpAdditionalHeaders = ["X-Stub": id]
        session = URLSession(configuration: config)
        Stub.lock.withLock { Stub.all[id] = self }
    }

    var recorded: [Recorded] { lock.withLock { log } }
    func record(_ r: Recorded) { lock.withLock { log.append(r) } }
}

final class StubProtocol: URLProtocol, @unchecked Sendable {
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        var bodyData = request.httpBody
        if bodyData == nil, let stream = request.httpBodyStream {
            stream.open()
            var d = Data()
            let buf = UnsafeMutablePointer<UInt8>.allocate(capacity: 4096)
            while stream.hasBytesAvailable {
                let n = stream.read(buf, maxLength: 4096)
                if n <= 0 { break }
                d.append(buf, count: n)
            }
            buf.deallocate()
            stream.close()
            bodyData = d
        }
        let body = bodyData.flatMap { try? JSONDecoder().decode(JSONValue.self, from: $0) }
        let rec = Stub.Recorded(method: request.httpMethod ?? "GET", url: request.url!,
                                headers: request.allHTTPHeaderFields ?? [:], body: body, raw: bodyData)
        let id = request.value(forHTTPHeaderField: "X-Stub") ?? ""
        guard let stub = Stub.lock.withLock({ Stub.all[id] }) else {
            client?.urlProtocol(self, didFailWithError: URLError(.unsupportedURL))
            return
        }
        stub.record(rec)
        do {
            let (status, data) = try stub.handler(rec)
            let resp = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: "HTTP/1.1",
                                       headerFields: ["Content-Type": "application/json"])!
            client?.urlProtocol(self, didReceive: resp, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        } catch {
            client?.urlProtocol(self, didFailWithError: error)
        }
    }

    override func stopLoading() {}
}

func body(_ s: String) -> Data { Data(s.utf8) }

// MARK: a scripted socket

/// A connection whose frames the test pushes; `fail(code:)` drops it as the server would.
final class FakeConnection: SocketConnection, @unchecked Sendable {
    private let lock = NSLock()
    private var queue: [Result<String, Error>] = []
    private var waiter: CheckedContinuation<String, Error>?
    private var code = 0
    private(set) var closed = false
    let request: URLRequest

    init(request: URLRequest) { self.request = request }

    func push(_ text: String) { deliver(.success(text)) }

    func fail(code: Int = 1006) {
        lock.withLock { self.code = code }
        deliver(.failure(URLError(.networkConnectionLost)))
    }

    private func deliver(_ r: Result<String, Error>) {
        let w: CheckedContinuation<String, Error>? = lock.withLock {
            if let w = waiter { waiter = nil; return w }
            queue.append(r)
            return nil
        }
        w?.resume(with: r)
    }

    func receive() async throws -> String {
        try await withCheckedThrowingContinuation { (k: CheckedContinuation<String, Error>) in
            let next: Result<String, Error>? = lock.withLock {
                if closed { return .failure(URLError(.cancelled)) }
                if !queue.isEmpty { return queue.removeFirst() }
                waiter = k
                return nil
            }
            if let next { k.resume(with: next) }
        }
    }

    func ping() async throws {}

    func close() {
        let w: CheckedContinuation<String, Error>? = lock.withLock {
            closed = true
            let w = waiter
            waiter = nil
            return w
        }
        w?.resume(throwing: URLError(.cancelled))
    }

    var closeCode: Int { lock.withLock { code } }
}

final class FakeTransport: SocketTransport, @unchecked Sendable {
    private let lock = NSLock()
    private var made: [FakeConnection] = []

    func connect(_ request: URLRequest) -> any SocketConnection {
        let c = FakeConnection(request: request)
        lock.withLock { made.append(c) }
        return c
    }

    var connections: [FakeConnection] { lock.withLock { made } }
    var last: FakeConnection? { connections.last }
}

/// Waits (polling the main actor) until a condition holds, up to a timeout.
@MainActor
func eventually(_ timeout: Double = 2, _ condition: @MainActor () -> Bool) async -> Bool {
    let end = Date().addingTimeInterval(timeout)
    while Date() < end {
        if condition() { return true }
        try? await Task.sleep(for: .milliseconds(10))
    }
    return condition()
}
