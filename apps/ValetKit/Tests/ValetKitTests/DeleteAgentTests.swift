import Foundation
import Testing
import UserNotifications
@testable import ValetKit

/// Delete agent (DELETE /api/agents/{name}) and a shared item's push, against a stub server.
@MainActor
@Suite("Deleting an agent, and a shared item's push")
struct DeleteAgentTests {
    func signedIn(_ stub: Stub) -> AppModel {
        let defaults = UserDefaults(suiteName: "valet-tests-\(UUID().uuidString)")!
        defaults.set("http://127.0.0.1:9", forKey: "server")
        return AppModel(store: MemoryTokenStore(["token": "vown_t"]), defaults: defaults, transport: FakeTransport(),
                        urlSession: stub.session)
    }

    @Test func theRequestAndWhatItAnswers() async throws {
        let stub = Stub { r in
            switch (r.method, r.path) {
            case ("DELETE", "/api/agents/box"):
                return (200, body(#"{"ok": true, "volumes": ["valet-box-home"], "kept": null}"#))
            case ("DELETE", "/api/agents/host"):
                return (200, body(#"{"ok": true, "volumes": [], "kept": "/data/agents/host/work"}"#))
            case ("DELETE", "/api/agents/far"):
                return (503, body(#"{"detail": "runner mac is not connected"}"#))
            default: return (404, body(#"{"detail": "no agent named x"}"#))
            }
        }
        let api = APIClient(baseURL: URL(string: "http://h")!, token: "vown_t", urlSession: stub.session)
        #expect(try await api.deleteAgent("box", volumes: true) == AgentDeleted(volumes: ["valet-box-home"]))
        #expect(stub.recorded[0].query == ["volumes": "1"])
        #expect(try await api.deleteAgent("host").kept == "/data/agents/host/work")
        #expect(stub.recorded[1].query.isEmpty)
        await #expect(throws: APIError.self) { try await api.deleteAgent("far") }
        do { try await api.deleteAgent("far") } catch let e as APIError {
            #expect(e.status == 503 && e.errorDescription?.localizedCaseInsensitiveContains("runner mac is not connected") == true)
        }
        #expect(try decode(AgentDeleted.self, "{}") == AgentDeleted())
    }

    @Test func aDeletedAgentLeavesAtOnceAndItsCountsAreFetchedAgain() async throws {
        let stub = Stub { r in
            switch (r.method, r.path) {
            case ("DELETE", "/api/agents/ada"): return (200, body(#"{"ok": true, "volumes": [], "kept": null}"#))
            case ("GET", "/api/shared/unseen"): return (200, body(#"{"unseen": 1, "agents": {"bob": 1}, "unassigned": 0}"#))
            default: return (404, body(#"{"detail": "no"}"#))
            }
        }
        let m = signedIn(stub)
        m.apply(.hello(Hello(agents: [Agent(name: "ada"), Agent(name: "bob")],
                             inbox: [InboxItem(id: "x", agent: "ada"), InboxItem(id: "y", agent: "bob")])))
        m.apply(.shared(SharedChange(counts: SharedCounts(unseen: 3, agents: ["ada": 2, "bob": 1]))))
        #expect(m.sharedCounts.agents["ada"] == 2)
        var frames: [Frame] = []
        let sub = m.subscribe { frames.append($0) }
        defer { m.unsubscribe(sub) }

        try await m.deleteAgent("ada")
        #expect(m.agentList.map(\.name) == ["bob"])
        #expect(m.pending.map(\.id) == ["y"])
        #expect(m.deletedAgents == ["ada"])                              // its open screens close
        #expect(frames == [.agentDeleted("ada")])                       // the screens hear it as from the server
        #expect(await eventually { m.sharedCounts == SharedCounts(unseen: 1, agents: ["bob": 1]) })
        // The server's own frame after it changes nothing more.
        m.apply(.agentDeleted("ada"))
        #expect(m.deletedAgents == ["ada"] && m.agentList.map(\.name) == ["bob"])
        // Made again with that name: it's back, and its screens stay open.
        m.put(Agent(name: "ada"))
        #expect(m.deletedAgents.isEmpty && m.agentList.map(\.name) == ["ada", "bob"])
    }

    @Test func aRefusedDeleteKeepsTheAgent() async throws {
        let stub = Stub { _ in (502, body(#"{"detail": "Docker didn't delete them: busy"}"#)) }
        let m = signedIn(stub)
        m.apply(.hello(Hello(agents: [Agent(name: "ada")])))
        await #expect(throws: APIError.self) { try await m.deleteAgent("ada") }
        #expect(m.agentList.map(\.name) == ["ada"] && m.deletedAgents.isEmpty)
    }

    @Test func oneDeletedWhileTheSocketWasDownIsGoneAtTheNextHello() {
        let m = signedIn(Stub { _ in (404, body("{}")) })
        m.apply(.hello(Hello(agents: [Agent(name: "ada"), Agent(name: "bob")])))
        #expect(m.deletedAgents.isEmpty)
        m.apply(.hello(Hello(agents: [Agent(name: "bob")])))
        #expect(m.deletedAgents == ["ada"])
        m.apply(.hello(Hello(agents: [Agent(name: "ada"), Agent(name: "bob")])))
        #expect(m.deletedAgents.isEmpty)
    }

    @Test func aSharedPushOpensItsItem() async {
        let info: [AnyHashable: Any] = ["aps": ["alert": ["title": "Scout added to Shared"]], "kind": "shared",
                                        "shared_id": 12, "agent": "scout"]
        #expect(Push.sharedTarget(info) == SharedPushTarget(agent: "scout", id: 12))
        #expect(Push.sharedTarget(["kind": "shared", "shared_id": "7"]) == SharedPushTarget(agent: nil, id: 7))
        #expect(Push.sharedTarget(["kind": "shared", "shared_id": 7, "agent": "owner"]) == SharedPushTarget(agent: nil, id: 7))
        #expect(Push.sharedTarget(["kind": "shared"]) == nil)
        #expect(Push.sharedTarget(["item_id": "abc", "kind": "permission", "shared_id": 3]) == nil)
        #expect(Push.itemID(info) == nil)

        let m = signedIn(Stub { _ in (404, body("{}")) })
        let target = Push.sharedTarget(info)
        await Push.handle(action: UNNotificationDismissActionIdentifier, itemID: nil, shared: target, text: nil, model: m)
        #expect(m.openShared == nil)                                    // dismissed: nothing opens
        await Push.handle(action: UNNotificationDefaultActionIdentifier, itemID: nil, shared: target, text: nil, model: m)
        #expect(m.openShared == SharedPushTarget(agent: "scout", id: 12) && m.openItemID == nil)
    }
}
