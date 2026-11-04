import Foundation
import Testing
import ValetPushCategories
@testable import ValetKit

@MainActor
@Suite("The app's models, against a stub server")
struct ModelTests {
    /// A signed-in AppModel whose requests the stub answers.
    func signedIn(_ stub: Stub) -> AppModel {
        let defaults = UserDefaults(suiteName: "valet-tests-\(UUID().uuidString)")!
        defaults.set("http://127.0.0.1:9", forKey: "server")
        return AppModel(store: MemoryTokenStore(["token": "vown_t"]), defaults: defaults, transport: FakeTransport(),
                        urlSession: stub.session)
    }

    @Test func aSessionLoadsItsHistoryThenFollowsLiveEvents() async throws {
        let events = try fixture("events.json")
        let stub = Stub { r in
            if r.path.hasSuffix("/events") {
                return r.query["after"] == "0" ? (200, events) : (200, body(#"{"events": [], "more": false}"#))
            }
            return (200, body(#"{"agent": "sample", "id": "s", "state": "idle", "title": "hello"}"#))
        }
        let m = signedIn(stub)
        let s = SessionModel(agent: "sample", id: "s", model: m)
        s.start()
        #expect(await eventually { s.loaded })
        #expect(s.folder.cursor == 21)
        #expect(s.folder.items.count == 12)
        #expect(s.title == "hello")
        // Live: the next event applies; an old one doesn't.
        m.apply(.event(ev(22, "agent_message", turn: "t9", agent: "sample", text("live"))))
        m.apply(.event(ev(3, "agent_message", agent: "sample", text("old"))))
        m.apply(.event(ev(23, "agent_message", agent: "other", text("not mine"))))
        #expect(s.folder.cursor == 22)
        guard case .message(let msg) = s.folder.items.last else { Issue.record("no message"); return }
        #expect(msg.text == "live")
        // A reconnect refetches from the cursor.
        m.apply(.hello(Hello()))
        #expect(await eventually { stub.recorded.contains { $0.query["after"] == "22" } })
        s.stop()
    }

    @Test func aSyncRetriesAndLiveEventsWaitForIt() async throws {
        let fails = Counter()
        let stub = Stub { r in
            if r.path.hasSuffix("/events") {
                if fails.value == 0 { fails.add(); return (500, body(#"{"detail": "boom"}"#)) }
                return (200, body(#"{"events": [{"id": 1, "agent": "a", "session_id": "s", "kind": "agent_message", "turn_id": "t", "data": {"content": {"type": "text", "text": "one "}}}], "more": false}"#))
            }
            return (200, body(#"{"agent": "a", "id": "s", "state": "working"}"#))
        }
        let m = signedIn(stub)
        let s = SessionModel(agent: "a", id: "s", model: m)
        s.start()
        #expect(await eventually { s.error != nil })      // the first fetch failed; it says so and retries
        m.apply(.event(ev(2, "agent_message", turn: "t", agent: "a", text("two"))))   // waits for the history
        #expect(await eventually(4) { s.loaded })
        #expect(s.error == nil)
        guard case .message(let msg) = s.folder.items.first else { Issue.record("no message"); return }
        #expect(msg.text == "one two")
        #expect(s.isWorking)
        s.stop()
    }

    @Test func sendingQueuedAndRefused() async throws {
        let answers = Counter()
        let stub = Stub { r in
            if r.path.hasSuffix("/prompt") {
                answers.add()
                switch answers.value {
                case 1: return (202, body(#"{"queued": "q1"}"#))
                case 2: return (409, body(#"{"detail": "a turn is already running in this session"}"#))
                default: return (503, body(#"{"detail": "agent sample is not running"}"#))
                }
            }
            if r.path.hasSuffix("/events") { return (200, body(#"{"events": [], "more": false}"#)) }
            return (200, body(#"{"agent": "sample", "id": "s", "state": "working"}"#))
        }
        let m = signedIn(stub)
        let s = SessionModel(agent: "sample", id: "s", model: m)
        s.start()
        #expect(await eventually { s.loaded })
        #expect(await s.send("later please"))
        #expect(s.queued.map(\.text) == ["later please"])
        #expect(await s.send("again") == false)
        #expect(s.error == "The agent is still working on the last message.")
        #expect(await s.send("down"))              // recorded with the turn's error: the composer clears
        #expect(s.error == "Agent sample is not running")
        // The queued prompt's user_message arrives: it leaves the queue.
        m.apply(.event(ev(1, "user_message", agent: "sample", text("later please"))))
        #expect(s.queued.isEmpty)
        #expect(await s.send("   ") == false)
        s.stop()
    }

    @Test func theSessionsOwnQueueWins() async throws {
        let stub = Stub { r in
            if r.path.hasSuffix("/events") { return (200, body(#"{"events": [], "more": false}"#)) }
            return (200, body(#"{"agent": "a", "id": "s", "state": "working", "queued": 2, "queue": [{"id": 7, "session_id": "s", "text": "check the deploy", "source": "schedule", "label": "every day at 8:00"}, {"id": 8, "text": "and you", "source": "owner", "label": "You"}, {"bad": true}]}"#))
        }
        let m = signedIn(stub)
        let s = SessionModel(agent: "a", id: "s", model: m)
        s.start()
        #expect(await eventually { s.loaded })
        #expect(s.queued.map(\.id) == ["7", "8"])
        #expect(s.queued.map(\.source) == ["Schedule · every day at 8:00", nil])
        s.stop()
    }

    @Test func answeringAGoneOrAnsweredAsk() async throws {
        let item = String(decoding: try fixture("inbox-all.json").firstItem(), as: UTF8.self)
        let stub = Stub { r in
            switch r.path {
            case "/api/inbox/gone/answer": return (410, body(#"{"detail": "gone", "item": "# + item + "}"))
            case "/api/inbox/twice/answer": return (409, body(#"{"detail": "this request is already answered"}"#))
            case "/api/inbox/twice": return (200, body(item.replacingOccurrences(of: #""id": "#, with: #""was": "#)
                .replacingOccurrences(of: "{", with: #"{"id": "twice", "#, options: [], range: item.range(of: "{"))))
            default: return (503, body(#"{"detail": "runner mac is not connected"}"#))
            }
        }
        let m = signedIn(stub)
        try await m.answer("gone", optionId: "allow")
        #expect(m.inbox.values.contains { $0.agent == "sample" })
        try await m.answer("twice", optionId: "allow")
        #expect(m.inbox["twice"] != nil)
        await #expect(throws: APIError.self) { try await m.answer("other", optionId: "allow") }
    }

    @Test func aRefusedStartFetchesTheAgentAgain() async throws {
        let refused = try JSONDecoder().decode(JSONValue.self, from: fixture("start-missing-secrets.json"))["body"]!
        let stub = Stub { r in
            r.path.hasSuffix("/start") ? (422, try JSONEncoder().encode(refused))
                : (200, body(#"{"name": "keeper", "missing_secrets": ["KEEPER_TOKEN"]}"#))
        }
        let m = signedIn(stub)
        do {
            try await m.start("keeper")
            Issue.record("expected a refusal")
        } catch let e as APIError {
            #expect(e.missingSecrets == ["KEEPER_TOKEN"])
        }
        #expect(m.agents["keeper"]?.missingSecrets == ["KEEPER_TOKEN"])
    }

    @Test func pushRegistrationToleratesAServerWithoutIt() async {
        let stub = Stub { _ in (404, body(#"{"detail": "Not Found"}"#)) }
        let m = signedIn(stub)
        await m.didRegisterForPush(deviceToken: Data([0xab, 0x01]))
        #expect(m.push == .unsupported)
        #expect(m.pushToken == "ab01")
        let ok = Stub { _ in (201, body(#"{"id": "d7"}"#)) }
        let m2 = signedIn(ok)
        await m2.didRegisterForPush(deviceToken: Data([0x01]))
        #expect(m2.push == .registered)
        #expect(m2.deviceID == "d7")
    }

    @Test func signingInKeepsTheTokenOrSaysItCouldnt() async throws {
        let me = try fixture("me.json")
        let stub = Stub { r in
            switch r.path {
            case "/api/me": return (200, me)
            case "/api/login": return (200, body(#"{"ok": true, "token": "vown_new", "expires_in": 60}"#))
            default: return (200, body("[]"))
            }
        }
        let store = MemoryTokenStore()
        let defaults = UserDefaults(suiteName: "valet-tests-\(UUID().uuidString)")!
        let m = AppModel(store: store, defaults: defaults, transport: FakeTransport(), urlSession: stub.session)
        try await m.signIn(server: URL(string: "http://127.0.0.1:9")!, password: "pw")
        #expect(m.signedIn && store.get("token") == "vown_new" && m.notice == nil)
        #expect(m.rememberedServer == "http://127.0.0.1:9")
        m.signOut()

        let broken = AppModel(store: RefusingStore(), defaults: defaults, transport: FakeTransport(), urlSession: stub.session)
        try await broken.signIn(server: URL(string: "http://127.0.0.1:9")!, password: "pw")
        #expect(broken.signedIn)
        #expect(broken.notice?.contains("Keychain") == true)
        broken.signOut()
    }

    @Test func signingOutForgetsEverything() {
        let stub = Stub { _ in (200, body("{}")) }
        let m = signedIn(stub)
        m.apply(.hello(Hello(agents: [Agent(name: "a")], inbox: [InboxItem(id: "i")])))
        #expect(m.signedIn)
        m.signOut(expired: true)
        #expect(!m.signedIn)
        #expect(m.agents.isEmpty && m.inbox.isEmpty)
        #expect(m.api.currentToken == nil)
        #expect(m.notice == "Your session expired. Sign in again.")
    }
}

@Suite("Push, sign-in addresses and Markdown")
struct PushAndMiscTests {
    let gateway = [PermissionOption(optionId: "allow_once", name: "Allow once", kind: .allowOnce),
                   PermissionOption(optionId: "allow_hour", name: "Allow for an hour", kind: .allowOnce),
                   PermissionOption(optionId: "allow_always", name: "Always allow", kind: .allowAlways),
                   PermissionOption(optionId: "reject_once", name: "Reject", kind: .rejectOnce)]
    let harness = [PermissionOption(optionId: "allow", name: "Allow", kind: .allowOnce),
                   PermissionOption(optionId: "allow-always", name: "Always allow", kind: .allowAlways),
                   PermissionOption(optionId: "reject", name: "Reject", kind: .rejectOnce)]

    @Test func actionsMapOntoWhatTheItemOffers() {
        let tool = InboxItem(id: "g", kind: .tool, options: gateway)
        #expect(Push.option(for: .allowOnce, in: tool) == "allow_once")
        #expect(Push.option(for: .allowHour, in: tool) == "allow_hour")
        #expect(Push.option(for: .reject, in: tool) == "reject_once")
        let perm = InboxItem(id: "p", kind: .permission, options: harness)
        #expect(Push.option(for: .allowOnce, in: perm) == "allow")
        #expect(Push.option(for: .allowHour, in: perm) == "allow")       // an hour not offered: once, never more
        #expect(Push.option(for: .reject, in: perm) == "reject")
        let none = InboxItem(id: "m", kind: .message, options: [])
        #expect(Push.option(for: .allowOnce, in: none) == nil)
        #expect(Push.option(for: .reply, in: perm) == nil)
        #expect(Push.option(for: .dismiss, in: perm) == nil)
        let question = InboxItem(id: "q", kind: .question, options: [PermissionOption(optionId: "option-1", name: "main"),
                                                                    PermissionOption(optionId: "dismiss", name: "Dismiss")])
        #expect(Push.option(for: .dismiss, in: question) == "dismiss")
        #expect(Push.option(for: .allowOnce, in: question) == nil)
    }

    @Test func categoriesByKindOrOptions() {
        #expect(PushCategories.pick(kind: "tool", options: nil) == "VALET_INBOX")
        #expect(PushCategories.pick(kind: "browser", options: nil) == "VALET_INBOX")
        #expect(PushCategories.pick(kind: "permission", options: nil) == "VALET_INBOX_ONCE")
        #expect(PushCategories.pick(kind: "question", options: nil) == "VALET_INBOX_QUESTION")
        #expect(PushCategories.pick(kind: "message", options: nil) == "VALET_INBOX_MESSAGE")
        #expect(PushCategories.pick(kind: "anything", options: nil) == "VALET_INBOX")
        #expect(PushCategories.pick(kind: "tool", options: ["allow_once", "reject_once"]) == "VALET_INBOX_ONCE")
        #expect(PushCategories.pick(kind: "browser", options: ["allow_once", "allow_hour", "reject_once"]) == "VALET_INBOX")
        #expect(PushCategories.pick(kind: "message", options: []) == "VALET_INBOX_MESSAGE")
        #expect(Set(Push.categories.map(\.identifier)) == Set(PushCategories.all))
    }

    final class Content: UNMutableNotificationContentLike {
        var userInfo: [AnyHashable: Any]
        var categoryIdentifier: String
        init(_ info: [AnyHashable: Any], _ category: String) { userInfo = info; categoryIdentifier = category }
    }

    @Test func theExtensionNarrowsOnlyValetsPushes() {
        let c = Content(["item_id": "i", "kind": "permission"], "VALET_INBOX")
        #expect(PushCategories.narrow(c) == "VALET_INBOX_ONCE")
        let withOptions = Content(["item_id": "i", "kind": "tool", "options": [["optionId": "allow_once"],
                                                                                ["optionId": "reject_once"]]], "")
        #expect(PushCategories.narrow(withOptions) == "VALET_INBOX_ONCE")
        let other = Content(["kind": "permission"], "SOMETHING_ELSE")
        #expect(PushCategories.narrow(other) == "SOMETHING_ELSE")
    }

    @Test func itemIDsFromAPayload() {
        #expect(Push.itemID(["item_id": "abc"]) == "abc")
        #expect(Push.itemID(["item_id": NSNumber(value: 12)]) == "12")
        #expect(Push.itemID([:]) == nil)
    }

    @MainActor @Test func serverAddresses() {
        #expect(SignInView.url("valet.example.com")?.absoluteString == "https://valet.example.com")
        #expect(SignInView.url("127.0.0.1:8790/")?.absoluteString == "http://127.0.0.1:8790")
        #expect(SignInView.url("localhost:8780")?.absoluteString == "http://localhost:8780")
        #expect(SignInView.url("mac.local:8780")?.absoluteString == "http://mac.local:8780")
        #expect(SignInView.url(" https://v.example.com/valet ")?.absoluteString == "https://v.example.com/valet")
        #expect(SignInView.url("ftp://x") == nil)
        #expect(SignInView.url("") == nil)
    }

    @Test func markdownBlocksAndLinks() {
        let blocks = MarkdownBlock.parse("# Title\n\nSome *text*.\n\n- one\n- two\n\n```console\n$ ls\n```\n\n| a | b |\n|---|---|\n| 1 | 2 |")
        #expect(blocks.count == 5)
        #expect(blocks[0] == .heading(level: 1, text: "Title"))
        #expect(blocks[3] == .code(language: "console", code: "$ ls"))
        #expect(blocks[4] == .table(header: ["a", "b"], rows: [["1", "2"]]))
        #expect(MarkdownBlock.safeLink(URL(string: "https://x.com")!))
        #expect(!MarkdownBlock.safeLink(URL(string: "javascript:alert(1)")!))
        #expect(!MarkdownBlock.safeLink(URL(string: "file:///etc/passwd")!))
        // An unclosed fence while streaming is still a code block.
        #expect(MarkdownBlock.parse("```\npartial").first == .code(language: nil, code: "partial"))
    }

    @Test func secretValuesAreTrimmedWhenOneLine() {
        #expect(SecretsSheet.cleaned("  ghp_abc\n") == "ghp_abc")
        #expect(SecretsSheet.cleaned("line one\nline two\n") == "line one\nline two\n")
    }
}

/// A Keychain that refuses everything (an unsigned build).
final class RefusingStore: TokenStore {
    func get(_ account: String) -> String? { nil }
    func set(_ value: String?, for account: String) -> Bool { value == nil }
}
