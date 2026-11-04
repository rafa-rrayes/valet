import Foundation
import Testing
@testable import ValetKit

@Suite("Browser asks and the live view")
struct BrowserTests {
    /// A browser item as m3-browser's Hub.ask_item stores it (docs/API.md, InboxItem and BrowserAsk).
    static let askItem = #"""
    {"id": "f3a9", "kind": "browser", "agent": "scout", "session_id": "s1", "turn_id": "t1", "status": "pending",
     "title": "github.com: click \"Save\"",
     "tool_call": {"toolCallId": "browser-f3a9", "title": "github.com: click \"Save\"", "kind": "other",
                   "rawInput": {"url": "https://github.com/x", "ref": "e7"}},
     "options": [{"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"},
                 {"optionId": "allow_hour", "name": "Allow for an hour", "kind": "allow_once"},
                 {"optionId": "allow_always", "name": "Always allow", "kind": "allow_always"},
                 {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"}],
     "tool": null,
     "browser": {"profile": "web", "tab": "T1", "url": "https://github.com/x", "host": "github.com",
                 "kind": "commit", "summary": "click \"Save\"",
                 "detail": {"url": "https://github.com/x", "ref": "e7",
                            "element": {"tag": "button", "type": "submit", "name": "Save"}},
                 "shot": "0123456789abcdef.jpg"},
     "answer": null, "created_at": 1700000000.5, "answered_at": null}
    """#

    @Test func aBrowserItemDecodesWithItsAsk() throws {
        let item = try decode(InboxItem.self, Self.askItem)
        #expect(item.kind == .browser)
        let ask = try #require(item.browser)
        #expect(ask.profile == "web")
        #expect(ask.tab == "T1")
        #expect(ask.host == "github.com")
        #expect(ask.kind == .commit)
        #expect(ask.summary == "click \"Save\"")
        #expect(ask.shot == "0123456789abcdef.jpg")
        #expect(ask.detailLines.first?.label == "Element")
        #expect(ask.detailLines.first?.text == "\u{201C}Save\u{201D} (button submit)")
        // Every option, in order: a browser ask isn't a harness's permission.
        #expect(item.shownOptions.map(\.optionId) == ["allow_once", "allow_hour", "allow_always", "reject_once"])
        #expect(!item.isToolAsk)
        // Still in extra, for anything that reads it there.
        #expect(item.extra["browser"]?["host"] == "github.com")
    }

    @Test func aTakeOverSaysWhy() throws {
        let item = try decode(InboxItem.self, #"""
        {"id": "h1", "kind": "browser", "agent": "scout", "status": "pending", "title": "bank.com: take over",
         "options": [{"optionId": "done", "name": "Done", "kind": "allow_once"},
                     {"optionId": "cant", "name": "I can't", "kind": "reject_once"}],
         "browser": {"profile": "web", "tab": null, "url": "https://bank.com/", "host": "bank.com", "kind": "human",
                     "summary": "take over", "detail": {"reason": "Solve the captcha"}, "shot": null},
         "created_at": 1}
        """#)
        let ask = try #require(item.browser)
        #expect(ask.isTakeOver)
        #expect(ask.reason == "Solve the captcha")
        #expect(ask.kind.label == "take over")
        #expect(item.shownOptions.map(\.name) == ["Done", "I can't"])
    }

    @Test func anOddBrowserObjectStillDecodes() throws {
        let item = try decode(InboxItem.self, #"{"id": "b", "kind": "browser", "browser": "nope"}"#)
        #expect(item.browser == nil)
        let other = try decode(InboxItem.self, #"{"id": "b", "kind": "browser", "browser": {"site": "x"}}"#)
        #expect(other.browser?.host == "")
        #expect(other.browser?.kind == .interact)
    }

    @Test func theConversationCarriesTheAskOnItsOwn() {
        var f = EventFolder()
        f.apply(ev(1, "user_message", text("open it")))
        f.apply(ev(2, "permission_request", json(#"""
        {"requestId": "f3a9", "kind": "browser",
         "toolCall": {"toolCallId": "browser-f3a9", "title": "github.com: click", "kind": "other", "rawInput": {}},
         "options": [{"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"},
                     {"optionId": "allow_hour", "name": "Allow for an hour", "kind": "allow_once"},
                     {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"}],
         "browser": {"profile": "web", "url": "https://github.com", "host": "github.com", "kind": "interact",
                     "summary": "click", "detail": {}, "shot": "a.jpg"}}
        """#)))
        guard case .permission(let p) = f.items.last else { Issue.record("no permission item"); return }
        #expect(p.isBrowserAsk)
        #expect(!p.isToolAsk)
        #expect(p.kind == "browser")
        #expect(p.browser?.shot == "a.jpg")
        // Without its inbox item, every option still (not the harness's two).
        #expect(p.shownOptions(item: nil).count == 3)
        f.apply(ev(3, "permission_answer", json(#"{"requestId": "f3a9", "status": "answered", "outcome": {"outcome": "selected", "optionId": "allow_hour"}}"#)))
        guard case .permission(let answered) = f.items.last else { return }
        #expect(answered.state(item: nil).result(options: answered.options)?.text == "Allowed for an hour")
    }

    // MARK: the live view

    @Test func inputMessages() {
        #expect(LiveInput.tap(x: 0.25, y: 0.5).message == #"{"type":"tap","x":0.25,"y":0.5}"#)
        #expect(LiveInput.tap(x: -1, y: 3).message == #"{"type":"tap","x":0,"y":1}"#)
        #expect(LiveInput.mouse(action: "down", x: 0.5, y: 0.5, clicks: 2).message
                == #"{"action":"down","clicks":2,"type":"mouse","x":0.5,"y":0.5}"#)
        #expect(LiveInput.mouse(action: "move", x: 0.5, y: 0.5, clicks: nil).message
                == #"{"action":"move","type":"mouse","x":0.5,"y":0.5}"#)
        #expect(LiveInput.wheel(x: 0.5, y: 0.5, dy: 120.4).message == #"{"dy":120,"type":"wheel","x":0.5,"y":0.5}"#)
        #expect(LiveInput.wheel(x: 0.5, y: 0.5, dy: -9000).message.contains(#""dy":-5000"#))
        #expect(LiveInput.key(.enter).message == #"{"key":"Enter","type":"key"}"#)
        #expect(LiveInput.text("héllo").message == #"{"text":"héllo","type":"text"}"#)
        #expect(LiveInput.nav("github.com/x").message == #"{"type":"nav","url":"github.com/x"}"#)
        #expect(LiveInput.back.message == #"{"type":"back"}"#)
        #expect(LiveInput.reload.message == #"{"type":"reload"}"#)
        #expect(LiveInput.tab("T2").message == #"{"tab":"T2","type":"tab"}"#)
        #expect(LiveInput.open(nil).message == #"{"type":"open"}"#)
        #expect(LiveInput.open("https://a.b").message == #"{"type":"open","url":"https://a.b"}"#)
    }

    @Test func longTextGoesInPieces() {
        let parts = LiveInput.texts(String(repeating: "a", count: 4500))
        #expect(parts.count == 3)
        #expect(parts.last == .text(String(repeating: "a", count: 500)))
        #expect(LiveInput.texts("").isEmpty)
    }

    @Test func serverMessages() {
        #expect(LiveMessage.decode(#"{"type": "tabs", "tabs": [{"tab": "A", "url": "https://x", "title": "X"}], "tab": "A"}"#)
                == .tabs([BrowserTab(tab: "A", url: "https://x", title: "X")], current: "A"))
        #expect(LiveMessage.decode(#"{"type": "page", "tab": "A", "url": "u", "title": "t"}"#)
                == .page(tab: "A", url: "u", title: "t"))
        #expect(LiveMessage.decode(#"{"type": "busy", "busy": true}"#) == .busy(true, dropped: nil))
        #expect(LiveMessage.decode(#"{"type": "busy", "busy": false, "dropped": "tap"}"#) == .busy(false, dropped: "tap"))
        #expect(LiveMessage.decode(#"{"type": "error", "error": "the browser stopped"}"#)
                == .error("the browser stopped", fatal: true))
        #expect(LiveMessage.decode(#"{"type": "error", "error": "no such tab", "fatal": false}"#)
                == .error("no such tab", fatal: false))
        #expect(LiveMessage.decode(#"{"type": "later"}"#) == .unknown)
        #expect(LiveMessage.decode("not json") == .unknown)
    }

    @MainActor @Test func theSessionFollowsTabsPagesAndBusy() {
        let s = LiveSession(profile: "web", tab: nil, api: APIClient(baseURL: URL(string: "http://h")!))
        s.handle(.tabs([BrowserTab(tab: "B", url: "https://b", title: "Bee"), BrowserTab(tab: "A", url: "https://a", title: "")],
                       current: "A"))
        #expect(s.tab == "A")
        #expect(s.url == "https://a")
        s.handle(.page(tab: "A", url: "https://a/2", title: "Two"))
        #expect(s.title == "Two")
        #expect(s.tabs.last?.title == "Two")
        s.handle(.page(tab: "B", url: "https://b/2", title: "Bee 2"))
        #expect(s.url == "https://a/2")     // another tab's page doesn't change the one shown
        s.handle(.busy(true, dropped: "text"))
        #expect(s.busy)
        #expect(s.notice?.contains("typing") == true)
        s.select(tab: "B")
        #expect(s.tab == "B")
        #expect(s.sent.last == #"{"tab":"B","type":"tab"}"#)
    }

    @MainActor @Test func wheelDeltasAreAddedUp() async {
        let s = LiveSession(profile: "web", tab: nil, api: APIClient(baseURL: URL(string: "http://h")!))
        s.scroll(x: 0.5, y: 0.5, dy: 10)
        s.scroll(x: 0.5, y: 0.5, dy: 15)
        s.scroll(x: 0.4, y: 0.5, dy: 5)
        #expect(s.sent.isEmpty)
        #expect(await eventually { !s.sent.isEmpty })
        #expect(s.sent == [#"{"dy":30,"type":"wheel","x":0.4,"y":0.5}"#])
        // A click sends what's pending first.
        s.scroll(x: 0.5, y: 0.5, dy: 7)
        s.tap(x: 0.1, y: 0.1)
        #expect(s.sent.suffix(2) == [#"{"dy":7,"type":"wheel","x":0.5,"y":0.5}"#, #"{"type":"tap","x":0.1,"y":0.1}"#])
    }

    @MainActor @Test func theViewSaysItsSize() async {
        #expect(LiveInput.viewport(width: 393, height: 652, scale: 2).message
                == #"{"height":652,"scale":2,"type":"viewport","width":393}"#)
        let s = LiveSession(profile: "web", tab: nil, api: APIClient(baseURL: URL(string: "http://h")!))
        s.viewport(.zero)                                       // before the first layout: nothing
        s.viewport(CGSize(width: CGFloat.infinity, height: 10))
        #expect(s.sent.isEmpty)
        s.viewport(CGSize(width: 393.4, height: 651.6), scale: 3)   // the first size: at once, in whole points; 3x is 2
        #expect(s.sent == [#"{"height":652,"scale":2,"type":"viewport","width":393}"#])
        s.viewport(CGSize(width: 393, height: 652), scale: 3)       // the same: nothing
        // A rotation or a window being resized: only the size it settles at, a moment later.
        s.viewport(CGSize(width: 700, height: 300), scale: 3)
        s.viewport(CGSize(width: 852, height: 300), scale: 3)
        #expect(s.sent.count == 1)
        #expect(await eventually { s.sent.count == 2 })
        #expect(s.sent.last == #"{"height":300,"scale":2,"type":"viewport","width":852}"#)
        // Back and forth before it settles: the size already sent isn't sent again.
        s.viewport(CGSize(width: 900, height: 300), scale: 3)
        s.viewport(CGSize(width: 852, height: 300), scale: 3)
        try? await Task.sleep(for: .milliseconds(400))
        #expect(s.sent.count == 2)
        // The window moved to a non-Retina screen: the same size at 1x.
        s.viewport(CGSize(width: 852, height: 300), scale: 1)
        #expect(await eventually { s.sent.count == 3 })
        #expect(s.sent.last == #"{"height":300,"scale":1,"type":"viewport","width":852}"#)
        #expect(LiveViewport(CGSize(width: 10, height: 10), scale: .nan)?.scale == 1)
    }

    @Test func geometry() {
        let bounds = CGRect(x: 0, y: 0, width: 400, height: 400)
        let image = CGSize(width: 1600, height: 800)      // drawn 400 x 200, from y 100
        #expect(LiveGeometry.fitted(image, in: bounds) == CGRect(x: 0, y: 100, width: 400, height: 200))
        #expect(LiveGeometry.normalized(CGPoint(x: 200, y: 200), image: image, bounds: bounds) == CGPoint(x: 0.5, y: 0.5))
        #expect(LiveGeometry.normalized(CGPoint(x: 200, y: 50), image: image, bounds: bounds) == nil)
        #expect(LiveGeometry.clamped(CGPoint(x: 500, y: 50), image: image, bounds: bounds) == CGPoint(x: 1, y: 0))
        #expect(LiveGeometry.pixelsPerPoint(image: image, bounds: bounds) == 4)
        // Frames in the view's shape (the page laid out at its size): no bars, the corners are the page's.
        let filled = CGSize(width: 800, height: 800)
        #expect(LiveGeometry.fitted(filled, in: bounds) == bounds)
        #expect(LiveGeometry.normalized(CGPoint(x: 400, y: 400), image: filled, bounds: bounds) == CGPoint(x: 1, y: 1))
        #expect(LiveGeometry.normalized(CGPoint(x: 0, y: 0), image: filled, bounds: bounds) == CGPoint(x: 0, y: 0))
    }

    @Test func liveURLs() {
        let api = APIClient(baseURL: URL(string: "https://example.com/valet")!, token: "vown_t")
        #expect(api.liveURL(profile: "web", tab: nil).absoluteString == "wss://example.com/valet/ws/browser/web")
        #expect(api.liveURL(profile: "my web", tab: "T 1").absoluteString
                == "wss://example.com/valet/ws/browser/my%20web?tab=T%201")
        #expect(api.liveRequest(profile: "web", tab: nil).value(forHTTPHeaderField: "Authorization") == "Bearer vown_t")
        let local = APIClient(baseURL: URL(string: "http://127.0.0.1:8790")!)
        #expect(local.liveURL(profile: "web", tab: "x").absoluteString == "ws://127.0.0.1:8790/ws/browser/web?tab=x")
    }

    @Test func shotsAreFetchedWithTheToken() async throws {
        let stub = Stub { r in
            r.path == "/api/browser/shots/0123456789abcdef.jpg" ? (200, Data([0xFF, 0xD8, 0xFF])) : (404, body("{}"))
        }
        let api = APIClient(baseURL: URL(string: "http://h")!, token: "vown_t", urlSession: stub.session)
        let data = try await api.shot("0123456789abcdef.jpg")
        #expect(data == Data([0xFF, 0xD8, 0xFF]))
        #expect(stub.recorded[0].headers["Authorization"] == "Bearer vown_t")
    }
}
