import Foundation
import Testing
@testable import ValetKit

@MainActor
@Suite("New agents: the form's draft, its checks, the request and a refusal's errors")
struct NewAgentTests {
    nonisolated static let catalog = #"""
    {"harnesses": [
       {"id": "claude", "name": "Claude", "by": "Anthropic", "key": "CLAUDE_CODE_OAUTH_TOKEN", "ready": false,
        "models": [{"id": "claude-opus-5-5", "name": "Opus 5.5"}]},
       {"id": "deepseek", "name": "DeepSeek", "by": "DeepSeek Harness", "key": "DEEPSEEK_API_KEY", "ready": true,
        "models": []}],
     "permissions": [], "levels": [], "networks": [],
     "templates": [{"id": "assistant", "name": "Assistant", "emoji": "🧭", "definition": {"permissions": "ask"},
                    "persona": "You are {who}, one of Alice's agents."},
                   {"id": "blank", "name": "Blank", "emoji": null, "definition": {}, "persona": "You are {who}."}]}
    """#

    nonisolated static let created = #"""
    {"name": "my-scout", "display_name": "My Scout", "emoji": "🧭", "state": "stopped", "desired": "stopped",
     "harness": "deepseek", "definition": {"name": "my-scout", "display_name": "My Scout", "harness": "deepseek"},
     "files": {"agent.yaml": "name: my-scout\n", "persona.md": "You are My Scout.\n"}}
    """#

    @Test func theIDFollowsTheNameUntilTyped() {
        #expect(NewAgentDraft.slug("Scout") == "scout")
        #expect(NewAgentDraft.slug("  Café Bot_2 ") == "cafe-bot-2")
        #expect(NewAgentDraft.slug("Ünïcödé — and more!!") == "unicode-and-more")
        #expect(NewAgentDraft.slug("--a  b--") == "a-b")
        #expect(NewAgentDraft.slug("🔭") == "")
        #expect(NewAgentDraft.slug(String(repeating: "abc ", count: 20)).count <= 31)
        #expect(NewAgentDraft.slug("a" + String(repeating: " ", count: 29) + "b").hasSuffix("-") == false)

        var d = NewAgentDraft()
        d.setName("My Scout")
        #expect(d.id == "my-scout" && d.displayName == "My Scout")
        d.setID(" scout-2 ")
        #expect(d.id == "scout-2" && d.idEdited)
        d.setName("Something else")
        #expect(d.id == "scout-2")       // typed: it no longer follows
    }

    @Test func aNewFormStartsFromTheCatalog() throws {
        let c = try decode(Catalog.self, Self.catalog)
        let d = NewAgentDraft(catalog: c)
        #expect(d.harness == "deepseek")     // the first one a runner can run
        #expect(d.template == "assistant" && d.copyFrom == nil)
        #expect(NewAgentDraft(catalog: nil).harness == "claude")
        #expect(NewAgentDraft(catalog: nil).template == "blank")
    }

    @Test func checksBeforeSending() {
        let taken: (String) -> Bool = { $0 == "scout" }
        var d = NewAgentDraft()
        #expect(d.check(taken: taken)[NewAgentDraft.Field.displayName] == "Give it a name.")
        d.setName("🔭")                        // a name that gives no ID
        #expect(d.check(taken: taken)[NewAgentDraft.Field.name] == "Pick an ID.")
        d.setName("Scout")
        #expect(d.liveError(taken: taken) == "There's already an agent with the ID scout.")
        #expect(d.check(taken: taken)[NewAgentDraft.Field.name] == "There's already an agent with the ID scout.")
        d.setID("-scout")
        #expect(d.check(taken: taken)[NewAgentDraft.Field.name]?.hasPrefix("The ID takes lowercase") == true)
        d.setID("Scout")
        #expect(d.check(taken: taken)[NewAgentDraft.Field.name] != nil)
        d.setID(String(repeating: "a", count: 32))
        #expect(d.check(taken: taken)[NewAgentDraft.Field.name] != nil)
        d.setID(String(repeating: "a", count: 31))
        #expect(d.check(taken: taken).isEmpty)
        d.setID("owner")
        #expect(d.check(taken: taken)[NewAgentDraft.Field.name]?.contains("stands for you") == true)
        d.setID("scout-2")
        #expect(d.check(taken: taken).isEmpty && d.liveError(taken: taken) == nil)
        d.setName(String(repeating: "x", count: 41))
        #expect(d.check(taken: taken)[NewAgentDraft.Field.displayName] == "At most 40 characters.")
    }

    @Test func theBodyFromATemplate() {
        var d = NewAgentDraft(start: .template("assistant"))
        d.setName("  My Scout ")
        d.harness = "claude"
        #expect(d.body == json(#"""
        {"name": "my-scout", "template": "assistant",
         "definition": {"display_name": "My Scout", "harness": "claude", "model": null}}
        """#))
        d.emoji = " 🔭 "
        d.model = "claude-opus-5-5"
        d.start = .template("")
        #expect(d.body == json(#"""
        {"name": "my-scout", "template": "blank",
         "definition": {"display_name": "My Scout", "emoji": "🔭", "harness": "claude", "model": "claude-opus-5-5"}}
        """#))
        d.pickHarness("deepseek")
        #expect(d.model == nil && d.harness == "deepseek")
    }

    @Test func aCopyTakesTheOriginalsBrain() throws {
        let a = try decode(Agent.self, #"{"name": "scout", "harness": "claude", "model": "claude-sonnet-5-5"}"#)
        var d = NewAgentDraft(start: .template("blank"))
        d.harness = "deepseek"
        d.copy(a)
        d.setName("Scout 2")
        #expect(d.copyFrom == "scout" && d.template == nil)
        #expect(d.body == json(#"""
        {"name": "scout-2", "copy_from": "scout",
         "definition": {"display_name": "Scout 2", "harness": "claude", "model": "claude-sonnet-5-5"}}
        """#))
        // An original whose definition doesn't load (no harness) leaves the brain as it was.
        let broken = try decode(Agent.self, #"{"name": "broken"}"#)
        d.copy(broken)
        #expect(d.copyFrom == "broken" && d.harness == "claude")
    }

    @Test func aRefusalsErrorsGoUnderTheirFields() {
        var d = NewAgentDraft()
        d.setName("Scout")
        func refused(_ status: Int, _ body: String) -> APIError {
            let b = json(body)
            return .http(status: status, detail: APIError.detail(of: b), body: b)
        }
        let F = NewAgentDraft.Field.self
        #expect(d.errors(from: refused(409, #"{"detail": "agent scout already exists"}"#))[F.name]
                == "There's already an agent with the ID scout.")
        let invalid = d.errors(from: refused(422, #"""
        {"detail": "invalid", "errors": [{"field": "emoji", "msg": "one emoji, no spaces"},
          {"field": "name", "msg": "owner stands for Alice"}, {"field": "directives", "msg": "authority/x: no such"}]}
        """#))
        #expect(invalid[F.emoji] == "one emoji, no spaces")
        #expect(invalid[F.name] == "owner stands for Alice")
        #expect(invalid.general == ["directives: authority/x: no such"])
        #expect(d.errors(from: refused(422, #"{"detail": "names are lowercase letters, digits and dashes"}"#))[F.name]
                == "Names are lowercase letters, digits and dashes")
        #expect(d.errors(from: refused(422, #"{"detail": "no template named x"}"#))[F.start] == "No template named x")
        #expect(d.errors(from: refused(404, #"{"detail": "no agent named gone"}"#))[F.start] == "No agent named gone")
        #expect(d.errors(from: refused(500, #"{"detail": "boom"}"#)).general == ["Boom"])
        #expect(d.errors(from: APIError.offline("x")).general == ["Can't reach Valet. Check the connection."])
    }

    @Test func createAndStart() async throws {
        let stub = Stub { r in
            switch (r.method, r.path) {
            case ("POST", "/api/agents"): return (201, body(Self.created))
            case ("POST", "/api/agents/my-scout/start"):
                return (503, body(#"{"detail": "no runner is connected; the agent starts when one connects"}"#))
            case ("GET", "/api/agents/my-scout"): return (200, body(Self.created))
            default: return (404, body("{}"))
            }
        }
        let defaults = UserDefaults(suiteName: "valet-tests-\(UUID().uuidString)")!
        defaults.set("http://127.0.0.1:9", forKey: "server")
        let m = AppModel(store: MemoryTokenStore(["token": "vown_t"]), defaults: defaults, transport: FakeTransport(),
                         urlSession: stub.session)
        var d = NewAgentDraft(start: .template("assistant"))
        d.setName("My Scout")
        d.harness = "deepseek"
        let a = try await m.createAgent(d)
        #expect(a.name == "my-scout" && m.agents["my-scout"]?.title == "My Scout")
        let post = try #require(stub.recorded.first)
        #expect(post.method == "POST" && post.path == "/api/agents")
        #expect(post.body == d.body)
        let note = await m.startNew("my-scout")
        #expect(note == "Created. No runner is connected; the agent starts when one connects.")
        #expect(m.agents["my-scout"] != nil)          // fetched again after the refused start
    }
}
