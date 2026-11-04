import Foundation
import Testing
@testable import ValetKit

/// A terminal socket the test drives: frames it pushes come out of receive(); what the session sends is recorded.
final class FakeTerminalConnection: TerminalConnection, @unchecked Sendable {
    private let lock = NSLock()
    private var queue: [Result<TerminalFrame, Error>] = []
    private var waiter: CheckedContinuation<TerminalFrame, Error>?
    private var code = 0
    private var log: [TerminalFrame] = []
    private(set) var closed = false
    let size: TerminalSize

    init(size: TerminalSize) { self.size = size }

    func push(_ frame: TerminalFrame) { deliver(.success(frame)) }
    func pushText(_ text: String) { push(.text(text)) }

    /// The server closes it with a code.
    func drop(code: Int) {
        lock.withLock { self.code = code }
        deliver(.failure(URLError(.networkConnectionLost)))
    }

    private func deliver(_ r: Result<TerminalFrame, Error>) {
        let w: CheckedContinuation<TerminalFrame, Error>? = lock.withLock {
            if let w = waiter { waiter = nil; return w }
            queue.append(r)
            return nil
        }
        w?.resume(with: r)
    }

    func receive() async throws -> TerminalFrame {
        try await withCheckedThrowingContinuation { (k: CheckedContinuation<TerminalFrame, Error>) in
            let next: Result<TerminalFrame, Error>? = lock.withLock {
                if closed { return .failure(URLError(.cancelled)) }
                if !queue.isEmpty { return queue.removeFirst() }
                waiter = k
                return nil
            }
            if let next { k.resume(with: next) }
        }
    }

    func send(_ frame: TerminalFrame) { lock.withLock { log.append(frame) } }

    func ping() async throws {}

    func close() {
        let w: CheckedContinuation<TerminalFrame, Error>? = lock.withLock {
            closed = true
            let w = waiter
            waiter = nil
            return w
        }
        w?.resume(throwing: URLError(.cancelled))
    }

    var closeCode: Int { lock.withLock { code } }
    var sent: [TerminalFrame] { lock.withLock { log } }
    var sentTexts: [String] { sent.compactMap { if case .text(let t) = $0 { return t } else { return nil } } }
    var sentBytes: [Data] { sent.compactMap { if case .binary(let d) = $0 { return d } else { return nil } } }
    var isClosed: Bool { lock.withLock { closed } }
}

@MainActor
final class TerminalHarness {
    var connections: [FakeTerminalConnection] = []
    lazy var session: TerminalSession = {
        let s = TerminalSession(id: 1, agent: "helper") { [unowned self] size in
            let c = FakeTerminalConnection(size: size)
            self.connections.append(c)
            return c
        }
        s.pingEvery = 0
        return s
    }()
    var last: FakeTerminalConnection? { connections.last }
}

@Suite("The agent's terminal")
struct TerminalTests {
    @Test func serverMessages() {
        #expect(TerminalMessage.decode(#"{"type": "ready", "terminal": "t1", "agent": "helper"}"#)
                == .ready(terminal: "t1", agent: "helper"))
        #expect(TerminalMessage.decode(#"{"type": "exit", "code": 0, "reason": null}"#) == .exit(code: 0, reason: nil))
        #expect(TerminalMessage.decode(#"{"type": "exit", "code": null, "reason": "the agent stopped"}"#)
                == .exit(code: nil, reason: "the agent stopped"))
        #expect(TerminalMessage.decode(#"{"type": "error", "message": "helper is not running"}"#)
                == .error("helper is not running"))
        #expect(TerminalMessage.decode(#"{"type": "later", "x": 1}"#) == .unknown)
        #expect(TerminalMessage.decode("not json") == .unknown)
        // What the screen says once it's over (terminal.js's words).
        #expect(TerminalMessage.exit(code: 0, reason: nil).endText == "The shell exited (0).")
        #expect(TerminalMessage.exit(code: nil, reason: "the agent stopped").endText == "Closed: the agent stopped.")
        #expect(TerminalMessage.error("Too many terminals").endText == "Too many terminals")
    }

    @Test func resizeMessagesAndLimits() throws {
        let m = json(TerminalSize(cols: 120, rows: 40).message)
        #expect(m == ["type": "resize", "cols": 120, "rows": 40])
        // The server takes 2..1000 each.
        #expect(TerminalSize(cols: 1, rows: 5000) == TerminalSize(cols: 2, rows: 1000))
    }

    @Test func theSocketsAddress() {
        let api = APIClient(baseURL: URL(string: "https://valet.example.com/base")!, token: "vown_x")
        let req = api.terminalRequest(agent: "my agent", size: TerminalSize(cols: 91, rows: 33))
        #expect(req.url?.absoluteString == "wss://valet.example.com/base/ws/terminal/my%20agent?cols=91&rows=33")
        #expect(req.value(forHTTPHeaderField: "Authorization") == "Bearer vown_x")
        let local = APIClient(baseURL: URL(string: "http://127.0.0.1:8790")!)
        #expect(local.terminalURL(agent: "a", size: TerminalSize(cols: 80, rows: 24)).absoluteString
                == "ws://127.0.0.1:8790/ws/terminal/a?cols=80&rows=24")
    }

    @Test func closeCodes() {
        #expect(TerminalSession.closeText(code: 4401, connecting: true, agent: "a") == "Sign in again.")
        #expect(TerminalSession.closeText(code: 4404, connecting: true, agent: "a") == "There's no agent named a.")
        #expect(TerminalSession.closeText(code: 4403, connecting: false, agent: "a").contains("refused"))
        #expect(TerminalSession.closeText(code: 1006, connecting: true, agent: "a") == "Couldn't connect.")
        #expect(TerminalSession.closeText(code: 1006, connecting: false, agent: "a") == "Disconnected.")
    }

    @MainActor @Test func itConnectsAtItsViewsFirstSizeAndRunsOnReady() async {
        let h = TerminalHarness()
        let s = h.session
        #expect(s.state == .waiting)
        #expect(h.connections.isEmpty)
        s.resize(cols: 100, rows: 30)
        #expect(s.state == .connecting)
        #expect(h.last?.size == TerminalSize(cols: 100, rows: 30))
        // Keystrokes before the shell runs go nowhere.
        s.send("ls\r")
        #expect(h.last?.sent.isEmpty == true)
        h.last?.pushText(#"{"type": "ready", "terminal": "t9", "agent": "helper"}"#)
        #expect(await eventually { s.state == .open })
        #expect(s.terminalID == "t9")
        #expect(s.statusText == "")
        s.send("echo hi\r")
        #expect(h.last?.sentBytes == [Data("echo hi\r".utf8)])
        #expect(h.last?.sentTexts.isEmpty == true)     // the size it opened with isn't sent again
    }

    @MainActor @Test func resizesAreSentOnlyWhenTheyChangeAndTheNewestAfterReady() async {
        let h = TerminalHarness()
        let s = h.session
        s.resize(cols: 80, rows: 24)
        // Sizes while it connects: only the newest counts, sent once it's ready.
        s.resize(cols: 90, rows: 24)
        s.resize(cols: 100, rows: 40)
        #expect(h.connections.count == 1)
        h.last?.pushText(#"{"type": "ready", "terminal": "t1", "agent": "helper"}"#)
        #expect(await eventually { s.state == .open })
        #expect(h.last?.sentTexts.map(json) == [["type": "resize", "cols": 100, "rows": 40]])
        // The same size again (every layout reports it): nothing.
        s.resize(cols: 100, rows: 40)
        #expect(h.last?.sentTexts.count == 1)
        // A rotation.
        s.resize(cols: 52, rows: 70)
        #expect(h.last?.sentTexts.last.map(json) == ["type": "resize", "cols": 52, "rows": 70])
        #expect(s.size == TerminalSize(cols: 52, rows: 70))

        // A size that came back to the opening one while connecting isn't sent.
        let h2 = TerminalHarness()
        h2.session.resize(cols: 80, rows: 24)
        h2.session.resize(cols: 70, rows: 24)
        h2.session.resize(cols: 80, rows: 24)
        h2.last?.pushText(#"{"type": "ready", "terminal": "t2", "agent": "helper"}"#)
        #expect(await eventually { h2.session.state == .open })
        #expect(h2.last?.sentTexts.isEmpty == true)
    }

    @MainActor @Test func outputGoesToTheViewInOrderAndWaitsForIt() async {
        let h = TerminalHarness()
        let s = h.session
        s.resize(cols: 80, rows: 24)
        let c = h.last!
        c.pushText(#"{"type": "ready", "terminal": "t1", "agent": "helper"}"#)
        c.push(.binary(Data("one ".utf8)))
        c.push(.binary(Data("two ".utf8)))
        #expect(await eventually { s.state == .open })
        try? await Task.sleep(for: .milliseconds(50))
        // No view yet: kept, then handed over at once, in order.
        var seen = ""
        s.onOutput = { seen += String(decoding: $0, as: UTF8.self) }
        #expect(seen == "one two ")
        c.push(.binary(Data("three".utf8)))
        #expect(await eventually { seen == "one two three" })
    }

    @MainActor @Test func theShellExits() async {
        let h = TerminalHarness()
        let s = h.session
        s.resize(cols: 80, rows: 24)
        let c = h.last!
        c.pushText(#"{"type": "ready", "terminal": "t1", "agent": "helper"}"#)
        c.pushText(#"{"type": "exit", "code": 0, "reason": null}"#)
        #expect(await eventually { s.isEnded })
        #expect(s.state == .ended("The shell exited (0)."))
        #expect(c.isClosed)
        // The server's close after it changes nothing; keystrokes and sizes go nowhere.
        c.drop(code: 1000)
        s.send("x")
        s.resize(cols: 10, rows: 10)
        try? await Task.sleep(for: .milliseconds(30))
        #expect(s.state == .ended("The shell exited (0)."))
        #expect(c.sent.isEmpty)
    }

    @MainActor @Test func anErrorOrACloseCodeSaysWhy() async {
        let h = TerminalHarness()
        h.session.resize(cols: 80, rows: 24)
        h.last?.pushText(#"{"type": "error", "message": "helper is not running"}"#)
        h.last?.drop(code: 1000)
        #expect(await eventually { h.session.isEnded })
        #expect(h.session.statusText == "helper is not running")

        let h2 = TerminalHarness()
        h2.session.resize(cols: 80, rows: 24)
        h2.last?.drop(code: 4401)
        #expect(await eventually { h2.session.isEnded })
        #expect(h2.session.statusText == "Sign in again.")

        let h3 = TerminalHarness()
        h3.session.resize(cols: 80, rows: 24)
        h3.last?.pushText(#"{"type": "ready", "terminal": "t1", "agent": "helper"}"#)
        #expect(await eventually { h3.session.state == .open })
        h3.last?.drop(code: 1006)
        #expect(await eventually { h3.session.isEnded })
        #expect(h3.session.statusText == "Disconnected.")
    }

    @MainActor @Test func closingEndsItsShell() async {
        let h = TerminalHarness()
        h.session.resize(cols: 80, rows: 24)
        h.last?.pushText(#"{"type": "ready", "terminal": "t1", "agent": "helper"}"#)
        #expect(await eventually { h.session.state == .open })
        h.session.close()
        #expect(h.last?.isClosed == true)
        #expect(h.session.state == .ended("Closed."))
    }

    @MainActor @Test func tabs() async {
        var made: [TerminalSession] = []
        let tabs = TerminalTabs(agent: "helper") { n in
            let s = TerminalSession(id: n, agent: "helper") { FakeTerminalConnection(size: $0) }
            s.pingEvery = 0
            made.append(s)
            return s
        }
        #expect(tabs.shells.isEmpty && tabs.current == nil)
        let one = tabs.open()
        let two = tabs.open()
        let three = tabs.open()
        #expect(tabs.shells.map(\.id) == [1, 2, 3])
        #expect(tabs.current == 3)
        // Closing the one shown shows the next (or the one before, at the end).
        tabs.current = 2
        tabs.close(two!)
        #expect(tabs.current == 3)
        #expect(two?.isEnded == true)
        tabs.close(three!)
        #expect(tabs.current == 1)
        // At most 8 running; an ended one doesn't count.
        for _ in 0..<7 { tabs.open() }
        #expect(tabs.live == 8)
        #expect(!tabs.canOpen)
        #expect(tabs.open() == nil)
        made.last?.close()
        #expect(tabs.canOpen)
        // Numbers go on from the highest.
        #expect(tabs.open()?.id == 11)
        tabs.closeAll()
        #expect(tabs.shells.isEmpty && tabs.current == nil)
        #expect(made.allSatisfy { $0.isEnded })
        _ = one
    }
}
