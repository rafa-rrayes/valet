import Foundation
import Testing
@testable import ValetKit

@Suite("Decoding the API's shapes")
struct DecodingTests {
    // MARK: recorded answers

    @Test func agentsFromTheServer() throws {
        let agents = try JSONDecoder().decode([Agent].self, from: fixture("agents.json"))
        let sample = try #require(agents.first { $0.name == "sample" })
        #expect(sample.title == "Sample")
        #expect(sample.emoji == "🧪")
        #expect(sample.harness == "claude")
        #expect(sample.level == "host")
        #expect(sample.permissions == "ask")
        #expect(sample.missingSecrets.isEmpty)
        let keeper = try #require(agents.first { $0.name == "keeper" })
        #expect(keeper.title == "keeper")            // no display name: the name
        #expect(keeper.missingSecrets == ["KEEPER_TOKEN"])
    }

    @Test func agentDetailKeepsItsFilesOutOfExtra() throws {
        let a = try JSONDecoder().decode(Agent.self, from: fixture("agent-detail.json"))
        #expect(a.name == "sample")
        #expect(a.extra["files"] == nil && a.extra["definition"] == nil && a.extra["commit"] == nil)
    }

    @Test func sessionsEventsInboxRunnersCatalog() throws {
        let sessions = try JSONDecoder().decode([Session].self, from: fixture("sessions.json"))
        #expect(sessions.count == 1)
        #expect(sessions[0].displayTitle == "hello")
        #expect(sessions[0].state == .idle)

        let page = try JSONDecoder().decode(EventPage.self, from: fixture("events.json"))
        #expect(page.events.count == 21)
        #expect(page.more == false)
        #expect(page.events.map(\.kind).contains("permission_request"))

        let items = try JSONDecoder().decode([InboxItem].self, from: fixture("inbox-pending.json"))
        let item = try #require(items.first)
        #expect(item.kind == .permission)
        #expect(item.options.map(\.optionId) == ["allow", "allow-always", "reject"])
        #expect(item.shownOptions.map(\.optionId) == ["allow", "reject"])     // the web UI's two
        #expect(item.toolCall["toolCallId"]?.string == "call-77df1057")
        #expect(item.tool == nil)
        #expect(item.isPending)

        let runners = try JSONDecoder().decode([Runner].self, from: fixture("runners.json"))
        #expect(runners.first?.name == "mac")
        #expect(runners.first?.harnesses == ["claude"])

        let catalog = try JSONDecoder().decode(Catalog.self, from: fixture("catalog.json"))
        #expect(catalog.harnesses.map(\.id).contains("claude"))
        #expect(catalog.permissions.map(\.id) == ["locked", "ask", "trusted", "unrestricted"])
        #expect(!catalog.templates.isEmpty)
        #expect(catalog.authority["locked"] == "strict")

        let me = try JSONDecoder().decode(Me.self, from: fixture("me.json"))
        #expect(me.owner && me.setup && me.api == 1)
    }

    @Test func everyRecordedFrameDecodes() throws {
        let lines = String(decoding: try fixture("frames.jsonl"), as: UTF8.self).split(separator: "\n")
        #expect(lines.count > 10)
        var types: Set<String> = []
        for line in lines {
            let f = try #require(Frame.decode(String(line)))
            if case .unknown = f { Issue.record("not decoded: \(line.prefix(120))") }
            types.insert(f.type)
        }
        #expect(types.isSuperset(of: ["hello", "agent", "session", "event", "inbox"]))
        guard case .hello(let h) = Frame.decode(String(lines[0])) else { Issue.record("first frame isn't hello"); return }
        #expect(h.runners.first?.connected == true)
    }

    // MARK: tolerance

    @Test func unknownFieldsAndTheM4AdditionsOnAgents() throws {
        let a = try decode(Agent.self, """
            {"name": "scout", "status": "working", "state": "running", "desired": "running", "connected": true,
             "activity": {"text": "Running the tests", "tool": "bash"}, "queued": 2,
             "next_trigger": {"at": 1800000000, "name": "morning"}, "brand_new": [1, 2], "missing_secrets": null}
            """)
        #expect(a.activityText == "Running the tests")
        #expect(a.queuedCount == 2)
        #expect(a.nextTriggerInfo?.name == "morning")
        #expect(a.nextTriggerInfo?.at == Date(timeIntervalSince1970: 1_800_000_000))
        #expect(a.extra["brand_new"] == [1, 2])
        #expect(a.missingSecrets.isEmpty)

        let b = try decode(Agent.self, #"{"name": "b", "status": "waiting", "activity": "Reading mail", "queued": [{"id": "q1"}]}"#)
        #expect(b.activityText == "Reading mail")
        // Only while it works or waits (a stopped agent's leftover activity isn't shown), the harness's words as
        // the web says them.
        var stale = b
        stale.status = .stopped
        #expect(stale.activityText == nil)
        stale.status = .working
        stale.activity = "thinking"
        #expect(stale.activityText == "Thinking…")
        stale.activity = "writing"
        #expect(stale.activityText == "Writing…")
        #expect(b.queuedCount == 1)
        #expect(b.nextTriggerInfo == nil)
        let c = try decode(Agent.self, #"{"name": "c", "activity": null, "next_trigger": 1800000000.5}"#)
        #expect(c.activityText == nil)
        #expect(c.nextTriggerInfo?.at != nil)
        // The shape m4-triggers sends.
        let d = try decode(Agent.self, #"{"name": "d", "activity": "thinking", "queued": 0, "main_session": "s1", "next_trigger": {"at": 1800000000, "kind": "schedule", "trigger": "morning", "label": "every day at 8:00"}}"#)
        #expect(d.nextTriggerInfo?.name == "every day at 8:00")
        #expect(d.queuedCount == 0)
        #expect(d.extra["main_session"] == "s1")
    }

    @Test func wrongTypesBecomeDefaultsNotErrors() throws {
        let a = try decode(Agent.self, """
            {"name": "x", "connected": "yes", "restart_needed": 1, "status": 7, "missing_secrets": ["A", 3, "B"]}
            """)
        #expect(a.connected == false)
        #expect(a.restartNeeded == false)
        #expect(a.status == AgentStatus(rawValue: "stopped"))     // falls back to the state
        #expect(a.missingSecrets == ["A", "B"])
    }

    @Test func unknownStatusesAndKindsAreKept() throws {
        let a = try decode(Agent.self, #"{"name": "x", "state": "running", "status": "sleeping"}"#)
        #expect(a.status.rawValue == "sleeping")
        let s = try decode(Session.self, #"{"agent": "a", "id": "s", "state": "queued", "main": true}"#)
        #expect(s.state.rawValue == "queued")
        #expect(s.extra["main"] == true)
    }

    @Test func aListDropsWhatDoesntDecode() throws {
        struct Holder: Decodable {
            let agents: [Agent]
            init(from decoder: Decoder) throws {
                agents = try decoder.container(keyedBy: AnyKey.self).list("agents")
            }
        }
        let h = try decode(Holder.self, #"{"agents": [{"name": "ok"}, {"display_name": "no name"}, 3]}"#)
        #expect(h.agents.map(\.name) == ["ok"])
        #expect(throws: DecodingError.self) { try decode(Agent.self, #"{"display_name": "no name"}"#) }
    }

    @Test func inboxKindsOfTonight() throws {
        let items = try decode([InboxItem].self, """
            [{"id": "g1", "kind": "tool", "agent": "scout", "status": "pending", "title": "github: Create issue",
              "tool_call": {"toolCallId": "gateway-g1", "title": "github: Create issue", "kind": "other",
                            "rawInput": {"title": "Bug"}},
              "options": [{"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"},
                          {"optionId": "allow_hour", "name": "Allow for an hour", "kind": "allow_once"},
                          {"optionId": "allow_always", "name": "Always allow", "kind": "allow_always"},
                          {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"}],
              "tool": {"server": "github", "name": "create_issue", "title": "Create issue",
                       "arguments": {"title": "Bug"}, "annotations": null},
              "answer": null, "created_at": 10},
             {"id": "b1", "kind": "browser", "agent": "scout", "status": "pending", "title": "Submit the form",
              "browser": {"site": "github.com", "action": "commit", "screenshot": "abc.png"},
              "options": [{"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"}], "created_at": 11},
             {"id": "q1", "kind": "question", "agent": "scout", "status": "pending", "title": "A question",
              "text": "Which branch should I use?", "options": [], "created_at": 12},
             {"id": "m1", "kind": "message", "agent": "scout", "status": "pending", "title": "Done with the report",
              "body": {"text": "It's in shared/report.md"}, "created_at": 13},
             {"id": "z1", "kind": "teleport", "agent": "scout", "status": "beamed", "title": "?", "created_at": 14}]
            """)
        #expect(items.count == 5)
        let tool = items[0]
        #expect(tool.isToolAsk)
        #expect(tool.shownOptions.count == 4)          // every option a gateway ask carries, in order
        #expect(tool.shownOptions.map(\.optionId) == ["allow_once", "allow_hour", "allow_always", "reject_once"])
        #expect(tool.tool?.label == "Create issue")
        #expect(tool.tool?.arguments?["title"] == "Bug")
        let browser = items[1]
        #expect(browser.kind == .browser)
        #expect(browser.extra["browser"]?["site"] == "github.com")
        let question = items[2]
        #expect(question.takesText)
        #expect(question.bodyText == "Which branch should I use?")
        #expect(items[3].bodyText == "It's in shared/report.md")
        #expect(items[4].kind.rawValue == "teleport")
        #expect(items[4].status.rawValue == "beamed")
        #expect(!items[4].isPending)
    }

    @Test func eventsAndTheirSource() throws {
        let e = try decode(Event.self, """
            {"id": 9, "agent": "a", "session_id": "s", "turn_id": null, "ts": 1.5, "kind": "user_message",
             "data": {"content": {"type": "text", "text": "wake up"}, "source": {"kind": "schedule", "name": "morning"}}}
            """)
        #expect(e.source == "Schedule morning")
        #expect(e.turnId == nil)
        let owner = try decode(Event.self, #"{"id": 1, "kind": "user_message", "data": {"source": "owner", "label": "You"}}"#)
        #expect(owner.source == nil)
        // M4's shape: a source string and the server's label.
        let hook = try decode(Event.self, #"{"id": 3, "kind": "user_message", "data": {"source": "webhook:deploy"}}"#)
        #expect(hook.source == "Webhook deploy")
        let peer = try decode(Event.self, #"{"id": 4, "kind": "user_message", "data": {"source": "agent:scout", "label": "From Scout"}}"#)
        #expect(peer.source == "From Scout")
        #expect(Event.sourceLabel("wakeup", label: nil) == "Wake-up")
        #expect(Event.sourceLabel("carrier-pigeon", label: nil) == "carrier-pigeon")
        #expect(Event.sourceLabel(nil, label: "x") == nil)
        let odd = try decode(Event.self, #"{"id": 2, "kind": "teleport", "data": 5, "extra": true}"#)
        #expect(odd.data.isEmpty)
        #expect(odd.extra["extra"] == true)
    }

    @Test func framesOfUnknownTypesOrBrokenPayloads() {
        guard case .unknown(let t, let raw) = Frame.decode(#"{"type": "browser", "frame": "abc"}"#) else {
            Issue.record("expected unknown"); return
        }
        #expect(t == "browser")
        #expect(raw["frame"] == "abc")
        guard case .unknown(let t2, _) = Frame.decode(#"{"type": "agent", "agent": {"no": "name"}}"#) else {
            Issue.record("expected unknown"); return
        }
        #expect(t2 == "agent")
        #expect(Frame.decode("not json") == nil)
        #expect(Frame.decode("[1, 2]") == nil)
        guard case .hello(let h) = Frame.decode(#"{"type": "hello"}"#) else { Issue.record("expected hello"); return }
        #expect(h == Hello())
    }

    @Test func promptResults() throws {
        #expect(try decode(PromptResult.self, #"{"turn_id": "t1"}"#) == PromptResult(turnId: "t1"))
        #expect(try decode(PromptResult.self, #"{"queued": "q7"}"#) == PromptResult(queued: "q7"))
        #expect(try decode(PromptResult.self, #"{"queued": 7}"#).queued == "7")
        #expect(try decode(PromptResult.self, #"{"queued": {"id": "q8", "position": 1}}"#).queued == "q8")
        #expect(try decode(PromptResult.self, #"{"turn_id": "t", "queued": false}"#).queued == nil)
    }

    @Test func devicesAsAListOrAnObject() throws {
        let a = try decode(DeviceList.self, #"[{"id": 3, "platform": "ios", "name": "iPhone", "created_at": 5}]"#)
        #expect(a.devices.first?.id.value == "3")
        let b = try decode(DeviceList.self, #"{"push_configured": true, "devices": [{"id": "d1", "platform": "macos"}]}"#)
        #expect(b.devices.first?.id.value == "d1")
        #expect(b.devices.first?.name == nil)
    }

    @Test func optionsWithSnakeCaseOrMissingNames() throws {
        let o = try decode([PermissionOption].self, #"[{"option_id": "x", "kind": "allow_once"}]"#)
        #expect(o.first?.name == "x")
        #expect(o.first?.kind.allows == true)
    }

    @Test func jsonValueBasics() {
        let v = json(#"{"a": [1, 2.5, "x", true, null], "b": {"c": ""}}"#)
        #expect(v["a"]?.array?.count == 5)
        #expect(v["a"]?.array?[0].int == 1)
        #expect(v["a"]?.array?[1].int == nil)
        #expect(v["a"]?.array?[1].text == "2.5")
        #expect(v["b"]?["c"]?.isEmpty == true)
        #expect(JSONValue.string("hi").display == "hi")
        #expect(json(#"{"k": 1}"#).display.contains("\"k\" : 1"))
    }
}
