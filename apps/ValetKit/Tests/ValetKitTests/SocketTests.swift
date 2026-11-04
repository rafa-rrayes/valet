import Foundation
import Testing
@testable import ValetKit

/// Records the waits the socket asks for and lets each one finish at once.
final class Sleeps: @unchecked Sendable {
    private let lock = NSLock()
    private var waits: [Double] = []
    var hold: Bool = false

    func sleep(_ s: Double) async throws {
        if s == ClientSocket.pingEvery {           // the ping loop: park it
            try await Task.sleep(for: .seconds(3600))
            return
        }
        lock.withLock { waits.append(s) }
    }

    var recorded: [Double] { lock.withLock { waits } }
}

@MainActor
@Suite("The client socket and the live state")
struct SocketTests {
    let hello = #"{"type": "hello", "agents": [{"name": "a", "state": "running", "desired": "running", "status": "idle"}], "runners": [], "inbox": [], "last_event_id": 4}"#

    func socket(_ t: FakeTransport, _ sleeps: Sleeps) -> (ClientSocket, Box) {
        let box = Box()
        let s = ClientSocket(transport: t, request: { URLRequest(url: URL(string: "ws://x/ws/client")!) },
                             sleep: { try await sleeps.sleep($0) })
        s.onFrame = { box.frames.append($0) }
        s.onState = { box.states.append($0) }
        return (s, box)
    }

    @MainActor final class Box {
        var frames: [Frame] = []
        var states: [ClientSocket.State] = []
    }

    @Test func helloThenFramesInOrder() async throws {
        let t = FakeTransport(), sleeps = Sleeps()
        let (s, box) = socket(t, sleeps)
        s.start()
        #expect(s.state == .connecting)
        let c = try #require(t.last)
        c.push(hello)
        c.push("garbage")
        c.push(#"{"type": "agent", "agent": {"name": "a", "status": "working"}}"#)
        c.push(#"{"type": "future", "x": 1}"#)
        c.push(#"{"type": "event", "event": {"id": 5, "agent": "a", "session_id": "s", "kind": "agent_message", "data": {}}}"#)
        #expect(await eventually { box.frames.count == 4 })
        #expect(box.frames.map(\.type) == ["hello", "agent", "future", "event"])
        #expect(s.state == .live)
        s.stop()
        #expect(s.state == .idle)
    }

    @Test func aDropRedialsWithBackoffResetByHello() async throws {
        let t = FakeTransport(), sleeps = Sleeps()
        let (s, _) = socket(t, sleeps)
        s.start()
        t.connections[0].fail()
        #expect(await eventually { t.connections.count == 2 })
        t.connections[1].fail()
        #expect(await eventually { t.connections.count == 3 })
        t.connections[2].fail()
        #expect(await eventually { t.connections.count == 4 })
        #expect(sleeps.recorded == [1, 2, 4])
        t.connections[3].push(hello)
        #expect(await eventually { s.state == .live })
        t.connections[3].fail()
        #expect(await eventually { t.connections.count == 5 })
        #expect(sleeps.recorded == [1, 2, 4, 1])
        s.stop()
    }

    @Test func backoffStopsAtThirtySeconds() async throws {
        let t = FakeTransport(), sleeps = Sleeps()
        let (s, _) = socket(t, sleeps)
        s.start()
        for i in 0..<7 {
            t.connections[i].fail()
            #expect(await eventually { t.connections.count == i + 2 })
        }
        #expect(sleeps.recorded == [1, 2, 4, 8, 16, 30, 30])
        s.stop()
    }

    @Test func notSignedInAsksFirst() async throws {
        let t = FakeTransport(), sleeps = Sleeps()
        let (s, _) = socket(t, sleeps)
        var asked = 0
        s.onUnauthorized = { asked += 1; return asked == 1 }    // still signed in the first time, not the second
        s.start()
        t.connections[0].fail(code: 4401)
        #expect(await eventually { t.connections.count == 2 })
        t.connections[1].fail(code: 4401)
        #expect(await eventually { s.state == .idle })
        #expect(asked == 2)
        try await Task.sleep(for: .milliseconds(50))
        #expect(t.connections.count == 2)
    }

    @Test func stopMeansNoRedial() async throws {
        let t = FakeTransport(), sleeps = Sleeps()
        let (s, _) = socket(t, sleeps)
        s.start()
        let c = t.connections[0]
        s.stop()
        #expect(c.closed)
        c.fail()
        try await Task.sleep(for: .milliseconds(50))
        #expect(t.connections.count == 1)
        #expect(s.state == .idle)
    }

    @Test func startAgainReplacesTheConnection() async throws {
        let t = FakeTransport(), sleeps = Sleeps()
        let (s, box) = socket(t, sleeps)
        s.start()
        s.start()
        #expect(t.connections.count == 2)
        #expect(t.connections[0].closed)
        t.connections[0].push(hello)        // the old one's frames are dropped
        t.connections[1].push(hello)
        #expect(await eventually { box.frames.count == 1 })
        s.stop()
    }

    // MARK: AppModel's state from frames

    func model() -> AppModel {
        AppModel(store: MemoryTokenStore(), defaults: UserDefaults(suiteName: "valet-tests-\(UUID().uuidString)")!,
                 transport: FakeTransport())
    }

    @Test func framesKeepTheLiveState() throws {
        let m = model()
        var seen: [String] = []
        let sub = m.subscribe { seen.append($0.type) }
        let lines = String(decoding: try fixture("frames.jsonl"), as: UTF8.self).split(separator: "\n")
        for line in lines { m.apply(try #require(Frame.decode(String(line)))) }
        #expect(seen.count == lines.count)
        #expect(m.agents["sample"]?.title == "Sample")
        #expect(m.sessionList("sample").count == 1)
        #expect(m.pendingCount == 0)           // the recorded ask was answered
        #expect(m.lastEventId == 21)
        #expect(m.hellos == 1)
        m.unsubscribe(sub)
        m.apply(.runner(Runner(name: "mac", connected: false)))
        #expect(seen.count == lines.count)
        #expect(m.runners["mac"]?.connected == false)
    }

    @Test func helloReplacesEverythingAndThePendingAreNewestFirst() throws {
        let m = model()
        m.apply(.inbox(InboxItem(id: "old", createdAt: 1)))
        m.apply(.inbox(InboxItem(id: "gone", createdAt: 2)))
        m.apply(.hello(Hello(agents: [Agent(name: "b"), Agent(name: "a")],
                             inbox: [InboxItem(id: "x", createdAt: 5), InboxItem(id: "y", createdAt: 9),
                                     InboxItem(id: "z", status: .answered, createdAt: 10)], lastEventId: 3)))
        #expect(m.agentList.map(\.name) == ["a", "b"])
        #expect(m.pending.map(\.id) == ["y", "x"])
        #expect(m.pendingCount == 2)
        m.apply(.inbox(InboxItem(id: "y", status: .answered, createdAt: 9)))
        #expect(m.pending.map(\.id) == ["x"])
        #expect(m.inbox["gone"] == nil)
    }

    @Test func aDeletedAgentLeavesTheListsWithItsSessionsAndAsks() throws {
        let m = model()
        m.apply(.hello(Hello(agents: [Agent(name: "a"), Agent(name: "b")],
                             inbox: [InboxItem(id: "x", agent: "a"), InboxItem(id: "y", agent: "b")])))
        m.apply(.session(Session(agent: "a", id: "1", updatedAt: 5)))
        let frame = try #require(Frame.decode(#"{"type": "agent_deleted", "agent": "a"}"#))
        #expect(frame == .agentDeleted("a") && frame.type == "agent_deleted")
        m.apply(frame)
        #expect(m.agentList.map(\.name) == ["b"])
        #expect(m.sessionList("a").isEmpty)
        #expect(m.pending.map(\.id) == ["y"] && m.pendingCount == 1)
        if case .unknown = Frame.decode(#"{"type": "agent_deleted"}"#) {} else { Issue.record("no name: unknown") }
    }

    @Test func sessionsNewestFirst() {
        let m = model()
        m.apply(.session(Session(agent: "a", id: "1", updatedAt: 5)))
        m.apply(.session(Session(agent: "a", id: "2", updatedAt: 9)))
        m.apply(.session(Session(agent: "a", id: "1", state: .working, updatedAt: 12)))
        #expect(m.sessionList("a").map(\.id) == ["1", "2"])
        #expect(m.sessionList("a").first?.isWorking == true)
    }
}

/// sendPing can answer more than once when a socket dies; only the first answer counts, and a second must not trap.
@Test func pingTakesOnlyTheFirstAnswer() async throws {
    struct Gone: Error {}
    try await firstAnswer { handler in handler(nil); handler(Gone()) }
    await #expect(throws: Gone.self) {
        try await firstAnswer { handler in handler(Gone()); handler(nil); handler(Gone()) }
    }
    try await firstAnswer { handler in
        DispatchQueue.global().async { handler(nil) }
        DispatchQueue.global().async { handler(Gone()) }
    }
}
