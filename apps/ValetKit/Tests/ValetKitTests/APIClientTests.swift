import Foundation
import Testing
@testable import ValetKit

@Suite("The API client, against a stub server")
struct APIClientTests {
    let base = URL(string: "http://127.0.0.1:8790")!

    @Test func nativeLoginKeepsTheTokenAndSendsItAfter() async throws {
        let stub = Stub { r in
            switch r.path {
            case "/api/login": return (200, body(#"{"ok": true, "token": "vown_abc", "expires_in": 2592000}"#))
            case "/api/agents": return (200, try fixture("agents.json"))
            default: return (404, body(#"{"detail": "Not Found"}"#))
            }
        }
        let api = APIClient(baseURL: base, urlSession: stub.session)
        let token = try await api.login(password: "pw", device: "Alice's iPhone")
        #expect(token == "vown_abc")
        #expect(api.currentToken == "vown_abc")
        let agents = try await api.agents()
        #expect(agents.count == 3)
        let login = stub.recorded[0]
        #expect(login.method == "POST")
        #expect(login.body == ["password": "pw", "client": "native", "device": "Alice's iPhone"])
        #expect(login.headers["Authorization"] == nil)
        #expect(stub.recorded[1].headers["Authorization"] == "Bearer vown_abc")
        #expect(stub.recorded[1].headers["Accept"] == "application/json")
    }

    @Test func aLoginWithoutATokenIsRefused() async {
        let stub = Stub { _ in (200, body(#"{"ok": true}"#)) }
        let api = APIClient(baseURL: base, urlSession: stub.session)
        await #expect(throws: APIError.invalidResponse) { try await api.login(password: "pw", device: "d") }
    }

    @Test func routesPathsAndBodies() async throws {
        let stub = Stub { r in
            switch (r.method, r.path) {
            case ("GET", "/valet/api/agents/sample/sessions/fake%20a%2Fb/events"):
                return (200, try fixture("events.json"))
            case ("POST", "/valet/api/agents/sample/sessions/s1/prompt"): return (202, body(#"{"turn_id": "t9"}"#))
            case ("POST", "/valet/api/agents/sample/sessions/s1/cancel"): return (200, body(#"{"ok": true}"#))
            case ("POST", "/valet/api/inbox/i1/answer"): return (200, try fixture("inbox-all.json").firstItem())
            case ("PUT", "/valet/api/secrets/GITHUB_TOKEN"): return (200, body(#"{"name": "GITHUB_TOKEN", "set": true}"#))
            case ("POST", "/valet/api/agents/sample/sessions"): return (201, body(#"{"agent": "sample", "id": "s2"}"#))
            case ("GET", "/valet/api/inbox"): return (200, try fixture("inbox-pending.json"))
            default: return (404, body(#"{"detail": "Not Found"}"#))
            }
        }
        let api = APIClient(baseURL: URL(string: "https://example.com/valet/")!, token: "vown_t", urlSession: stub.session)
        let page = try await api.events("sample", "fake a/b", after: 7, limit: 50)
        #expect(page.events.count == 21)
        #expect(stub.recorded[0].query == ["after": "7", "limit": "50"])
        #expect(try await api.prompt("sample", "s1", text: "hi\nthere") == PromptResult(turnId: "t9"))
        #expect(stub.recorded[1].body == ["text": "hi\nthere"])
        try await api.cancel("sample", "s1")
        _ = try await api.answer("i1", optionId: "allow")
        #expect(stub.recorded[3].body == ["option_id": "allow"])
        _ = try await api.answer("i1", text: "main")
        #expect(stub.recorded[4].body == ["text": "main"])
        try await api.setSecret("GITHUB_TOKEN", value: "ghp_x")
        #expect(stub.recorded[5].body == ["value": "ghp_x"])
        #expect(try await api.newSession("sample").id == "s2")
        #expect(try await api.inbox().count == 1)
        #expect(stub.recorded[7].query == ["status": "pending"])
        _ = try? await api.inbox(all: true)
        #expect(stub.recorded[8].query == ["status": "all"])
    }

    @Test func unauthorizedSignsOutButAWrongPasswordDoesnt() async {
        let calls = Counter()
        let stub = Stub { _ in (401, body(#"{"detail": "sign in first"}"#)) }
        let api = APIClient(baseURL: base, token: "vown_old", urlSession: stub.session, onUnauthorized: { calls.add() })
        await #expect(throws: APIError.unauthorized) { try await api.agents() }
        #expect(calls.value == 1)
        do {
            _ = try await api.login(password: "wrong", device: "d")
            Issue.record("expected an error")
        } catch let e as APIError {
            #expect(e.status == 401)
            #expect(e.localizedDescription == "Sign in first")
        } catch { Issue.record("\(error)") }
        #expect(calls.value == 1)
    }

    @Test func aStartRefusedForSecretsNamesThem() async throws {
        let recorded = try JSONDecoder().decode(JSONValue.self, from: fixture("start-missing-secrets.json"))
        let answer = try JSONEncoder().encode(recorded["body"]!)
        let stub = Stub { _ in (422, answer) }
        let api = APIClient(baseURL: base, token: "t", urlSession: stub.session)
        do {
            _ = try await api.start("keeper")
            Issue.record("expected an error")
        } catch let e as APIError {
            #expect(e.missingSecrets == ["KEEPER_TOKEN"])
            #expect(e.localizedDescription == "Secret KEEPER_TOKEN isn't set: add it to Valet's secrets")
        }
    }

    @Test func errorDetailsOfEveryShape() {
        #expect(APIError.detail(of: ["detail": "plain"]) == "plain")
        #expect(APIError.detail(of: ["detail": [["msg": "a"], ["msg": "b"]]]) == "a\nb")
        #expect(APIError.detail(of: ["detail": ["detail": "nested"]]) == "nested")
        #expect(APIError.detail(of: nil) == "")
        #expect(APIError.http(status: 500, detail: "", body: nil).localizedDescription == "Valet answered 500.")
        // A full queue's 429 says so; only the sign-in screen words the login limit.
        #expect(APIError.http(status: 429, detail: "100 prompts are already waiting in this session", body: nil)
            .localizedDescription == "100 prompts are already waiting in this session")
    }

    @Test func aVaultEntrysPasswordGoesToTheVault() async throws {
        let stub = Stub { r in
            switch (r.method, r.path) {
            case ("PUT", "/api/vault/github/alice"): return (200, body(#"{"name": "github/alice", "has_password": true}"#))
            case ("PUT", "/api/secrets/GITHUB_TOKEN"): return (200, body(#"{"name": "GITHUB_TOKEN", "set": true}"#))
            default: return (404, body(#"{"detail": "no such vault entry"}"#))
            }
        }
        let api = APIClient(baseURL: base, token: "t", urlSession: stub.session)
        try await api.setSecret("vault:github/alice", value: "pw")
        #expect(stub.recorded[0].body == ["password": "pw"])
        try await api.setSecret("GITHUB_TOKEN", value: "ghp")
        #expect(stub.recorded[1].body == ["value": "ghp"])
        do {
            try await api.setSecret("vault:shop/a b", value: "pw")
            Issue.record("expected 404")
        } catch let e as APIError {
            #expect(stub.recorded[2].path == "/api/vault/shop/a%20b")
            #expect(e.localizedDescription == "There's no vault entry shop/a b: add it in the vault first.")
        }
        #expect(neededName("vault:github/alice") == "github/alice's password")
        #expect(neededName("GITHUB_TOKEN") == "GITHUB_TOKEN")
    }

    @Test func aGoneAskCarriesItsItem() async throws {
        let item = try fixture("inbox-all.json").firstItem()
        let gone = #"{"detail": "the agent is no longer waiting for this answer", "item": "# + String(decoding: item, as: UTF8.self) + "}"
        let stub = Stub { _ in (410, body(gone)) }
        let api = APIClient(baseURL: base, token: "t", urlSession: stub.session)
        do {
            _ = try await api.answer("x", optionId: "allow")
            Issue.record("expected an error")
        } catch let e as APIError {
            #expect(e.status == 410)
            #expect(e.item?.agent == "sample")
        }
    }

    @Test func offlineAndOddAnswers() async {
        let down = Stub { _ in throw URLError(.cannotConnectToHost) }
        let api = APIClient(baseURL: base, token: "t", urlSession: down.session)
        do {
            _ = try await api.me()
            Issue.record("expected an error")
        } catch let e as APIError {
            guard case .offline = e else { Issue.record("\(e)"); return }
        } catch { Issue.record("\(error)") }

        let html = Stub { _ in (200, body("<html>proxy</html>")) }
        let api2 = APIClient(baseURL: base, token: "t", urlSession: html.session)
        await #expect(throws: APIError.invalidResponse) { try await api2.me() }
    }

    @Test func devicesAreOptionalOnTodaysServer() async throws {
        let stub = Stub { r in
            r.path == "/api/devices" && r.method == "POST" ? (201, body(#"{"id": 12, "platform": "ios"}"#))
                : (404, body(#"{"detail": "Not Found"}"#))
        }
        let api = APIClient(baseURL: base, token: "t", urlSession: stub.session)
        #expect(try await api.registerDevice(token: "abcd", platform: "ios", name: "iPhone") == "12")
        #expect(stub.recorded[0].body == ["token": "abcd", "platform": "ios", "name": "iPhone"])
        do {
            _ = try await api.devices()
            Issue.record("expected 404")
        } catch let e as APIError {
            #expect(e.status == 404)
        }
    }

    @Test func socketAddress() {
        #expect(APIClient(baseURL: URL(string: "http://127.0.0.1:8790")!).socketURL.absoluteString
                == "ws://127.0.0.1:8790/ws/client")
        #expect(APIClient(baseURL: URL(string: "https://valet.example.com/v/")!).socketURL.absoluteString
                == "wss://valet.example.com/v/ws/client")
        let req = APIClient(baseURL: URL(string: "https://v.example.com")!, token: "vown_x").socketRequest()
        #expect(req.value(forHTTPHeaderField: "Authorization") == "Bearer vown_x")
        #expect(req.value(forHTTPHeaderField: "Origin") == nil)
    }
}

final class Counter: @unchecked Sendable {
    private let lock = NSLock()
    private var n = 0
    func add() { lock.withLock { n += 1 } }
    var value: Int { lock.withLock { n } }
}

extension Data {
    /// The first element of a JSON list, as JSON.
    func firstItem() throws -> Data {
        let v = try JSONDecoder().decode(JSONValue.self, from: self)
        return try JSONEncoder().encode(v.array?.first ?? .null)
    }
}
