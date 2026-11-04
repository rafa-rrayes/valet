import Foundation
import Testing
@testable import ValetKit

@Suite("The event folder")
struct EventFolderTests {
    func messages(_ f: EventFolder) -> [MessageEntry] {
        f.items.compactMap { if case .message(let m) = $0 { return m } else { return nil } }
    }

    func tool(_ f: EventFolder, _ id: String) -> ToolEntry? {
        if case .tool(let t) = f.item("t:" + id) { return t }
        return nil
    }

    // MARK: messages

    @Test func chunksOfOneMessageJoinPerKindAndTurn() {
        let f = EventFolder([
            ev(1, "user_message", text("hello")),
            ev(2, "agent_thought", text("think")),
            ev(3, "agent_thought", text("ing")),
            ev(4, "agent_message", text("echo: ")),
            ev(5, "agent_message", text("hello")),
            ev(6, "turn_end", ["stopReason": "end_turn", "error": nil]),
            ev(7, "user_message", turn: "t2", text("again")),
            ev(8, "agent_message", turn: "t2", text("echo: again")),
        ])
        let m = messages(f)
        #expect(m.map(\.role) == [.user, .thought, .agent, .user, .agent])
        #expect(m.map(\.text) == ["hello", "thinking", "echo: hello", "again", "echo: again"])
        #expect(f.items.count == 5)          // an ordinary end adds nothing
        #expect(f.cursor == 8)
    }

    @Test func aNewTurnStartsANewMessageEvenOfTheSameKind() {
        let f = EventFolder([ev(1, "agent_message", turn: "a", text("one")), ev(2, "agent_message", turn: "b", text("two"))])
        #expect(messages(f).map(\.text) == ["one", "two"])
    }

    @Test func picturesAndLinksAreTheMessagesAttachmentsOtherBlocksText() {
        let f = EventFolder([
            ev(1, "agent_message", ["content": ["type": "image", "data": "iVBO", "mimeType": "image/png"]]),
            ev(2, "agent_message", ["content": ["type": "resource_link", "uri": "file:///a", "name": "a.txt"]]),
            ev(3, "agent_message", ["content": ["type": "resource", "resource": ["uri": "file:///b", "text": " body"]]]),
            ev(4, "agent_message", ["content": ["type": "audio", "data": "…"]]),
        ])
        #expect(messages(f).map(\.text) == [" body[audio]"])
        let atts = messages(f)[0].attachments
        #expect(atts.map(\.kind) == [.image(uri: nil, data: "iVBO"), .file(path: "/a", uri: "file:///a")])
        #expect(atts.map(\.name) == ["Image", "a.txt"])
    }

    /// The owner's prompt with attachments: its text, then a user_message per attachment, in the same turn.
    @Test func theOwnersAttachmentsFollowTheText() {
        let f = EventFolder([
            ev(1, "user_message", agent: "scout", text("look at these")),
            ev(2, "user_message", agent: "scout", ["content": ["type": "image", "mimeType": "image/png",
                                                               "uri": "/api/agents/scout/attachments/ab12", "name": "shot.png",
                                                               "size": 2048]]),
            ev(3, "user_message", agent: "scout", ["content": ["type": "resource_link", "uri": "file:///work/attachments/r b.txt",
                                                               "name": "r b.txt", "mimeType": "text/plain", "size": 26]]),
            ev(4, "agent_message", agent: "scout", text("ok")),
            // Attachments alone (no text): a message of their own.
            ev(5, "user_message", turn: "t2", agent: "scout", ["content": ["type": "resource_link", "uri": "https://example.com/x"]]),
        ])
        let m = messages(f)
        #expect(m.map(\.text) == ["look at these", "ok", ""])
        #expect(m[0].attachments.count == 2)
        let image = m[0].attachments[0], file = m[0].attachments[1]
        #expect(image.kind == .image(uri: "/api/agents/scout/attachments/ab12", data: nil))
        #expect(image.name == "shot.png" && image.size == 2048 && image.mime == "image/png" && image.agent == "scout")
        #expect(file.kind == .file(path: "/work/attachments/r b.txt", uri: "file:///work/attachments/r b.txt"))
        #expect(file.name == "r b.txt" && file.size == 26)
        #expect(m[2].attachments.map(\.kind) == [.file(path: nil, uri: "https://example.com/x")])
        #expect(m[2].attachments[0].name == "https://example.com/x")
        // A block that isn't one stays text; one without a uri or data isn't an attachment.
        #expect(MessageAttachment(block: ["type": "text", "text": "x"], agent: "a") == nil)
        #expect(MessageAttachment(block: ["type": "image"], agent: "a") == nil)
    }

    @Test func aToolBetweenChunksSplitsTheMessage() {
        let f = EventFolder([
            ev(1, "agent_message", text("before")),
            ev(2, "tool_call", ["toolCallId": "c1", "title": "Run ls", "kind": "execute", "status": "in_progress"]),
            ev(3, "agent_message", text("after")),
            ev(4, "tool_call_update", ["toolCallId": "c1", "status": "completed"]),
            ev(5, "agent_message", text(" more")),
        ])
        #expect(messages(f).map(\.text) == ["before", "after more"])   // an update isn't a new item
    }

    @Test func historyAndLiveOverlapApplyOnce() {
        var f = EventFolder([ev(1, "agent_message", text("a")), ev(2, "agent_message", text("b"))])
        f.apply([ev(2, "agent_message", text("b")), ev(1, "agent_message", text("a")), ev(3, "agent_message", text("c"))])
        #expect(messages(f).map(\.text) == ["abc"])
    }

    @Test func theOwnersMessageKeepsItsSource() {
        let f = EventFolder([ev(1, "user_message", ["content": ["type": "text", "text": "wake"], "source": "schedule"])])
        #expect(messages(f).first?.source == "Schedule")
    }

    // MARK: tools

    @Test func toolCallsMergeTheirUpdates() throws {
        let f = EventFolder([
            ev(1, "tool_call", ["toolCallId": "c1", "title": "Run ls", "kind": "execute", "status": "in_progress",
                                "rawInput": ["command": "ls"], "locations": [["path": "/a"]]]),
            ev(2, "tool_call_update", ["toolCallId": "c1", "content": [["type": "content",
                                                                         "content": ["type": "text", "text": "out"]]],
                                       "title": nil]),
            ev(3, "tool_call_update", ["toolCallId": "c1", "status": "completed", "locations": [["path": "/b", "line": 3]]]),
        ])
        let t = try #require(tool(f, "c1"))
        #expect(t.title == "Run ls")                 // a null field doesn't erase
        #expect(t.kind == "execute")
        #expect(t.status == "completed")
        #expect(t.isDone)
        #expect(t.command == "ls")
        #expect(t.locations == ["/b:3"])             // lists replace
        #expect(t.content == [.block(["type": "text", "text": "out"])])
        #expect(!t.isOpen(asking: false))
        #expect(f.items.count == 1)
    }

    @Test func anUpdateForAnUnknownCallStartsAnEntry() throws {
        let f = EventFolder([ev(1, "tool_call_update", ["toolCallId": "c9", "status": "in_progress", "title": "Late"])])
        let t = try #require(tool(f, "c9"))
        #expect(t.title == "Late")
        #expect(t.isOpen(asking: false))
    }

    @Test func diffsAndTerminals() throws {
        let f = EventFolder([
            ev(1, "tool_call", ["toolCallId": "e", "title": "Edit a.py", "kind": "edit", "content": [
                ["type": "diff", "path": "/w/a.py", "oldText": "a\nb\nc", "newText": "a\nB\nc"],
                ["type": "terminal", "terminalId": "term-1"],
                ["type": "mystery"],
            ]]),
        ])
        let t = try #require(tool(f, "e"))
        #expect(t.content == [.diff(path: "/w/a.py", oldText: "a\nb\nc", newText: "a\nB\nc"), .terminal("term-1")])
    }

    @Test func dshTitlesGetTheirDescription() {
        let t = ToolEntry(id: "x", data: ["title": "bash", "rawInput": ["description": "List files", "command": "ls"]])
        #expect(t.displayName == "bash · List files")
        let same = ToolEntry(id: "y", data: ["title": "Run ls", "rawInput": ["description": "Run ls"]])
        #expect(same.displayName == "Run ls")
        #expect(ToolEntry(id: "z", data: [:]).displayName == "Tool call")
    }

    @Test func aTurnsEndClosesItsToolsOnly() throws {
        let f = EventFolder([
            ev(1, "tool_call", turn: "t1", ["toolCallId": "a", "title": "A", "status": "in_progress"]),
            ev(2, "tool_call", turn: "t2", ["toolCallId": "b", "title": "B", "status": "in_progress"]),
            ev(3, "turn_end", turn: "t1", ["stopReason": "cancelled", "error": nil]),
        ])
        #expect(try #require(tool(f, "a")).ended)
        #expect(!(try #require(tool(f, "a")).isOpen(asking: false)))
        #expect(!(try #require(tool(f, "b")).ended))
        guard case .turnEnd(let e) = f.items.last else { Issue.record("no turn end"); return }
        #expect(e.text == "Cancelled")
        #expect(!e.bad)
    }

    @Test func turnEndReasons() {
        #expect(TurnEndEntry.text(stopReason: "end_turn", error: nil) == nil)
        #expect(TurnEndEntry.text(stopReason: nil, error: "interrupted")! == ("Interrupted", true))
        #expect(TurnEndEntry.text(stopReason: nil, error: "boom")! == ("Error: boom", true))
        #expect(TurnEndEntry.text(stopReason: "refusal", error: nil)! == ("The agent refused", true))
        #expect(TurnEndEntry.text(stopReason: "max_tokens", error: nil)!.0 == "Stopped: output limit reached")
        #expect(TurnEndEntry.text(stopReason: "max_turn_requests", error: nil)!.0 == "Stopped: too many steps")
        #expect(TurnEndEntry.text(stopReason: "", error: "") == nil)
    }

    // MARK: permissions

    @Test func aPermissionSitsOnItsToolAndItsAnswerResolvesIt() throws {
        var f = EventFolder([
            ev(1, "tool_call", ["toolCallId": "c1", "title": "Write", "kind": "edit", "status": "pending",
                                "rawInput": ["path": "hello.txt"]]),
            ev(2, "permission_request", ["requestId": "r1",
                                         "toolCall": ["toolCallId": "c1", "title": "Write hello.txt", "status": "failed",
                                                      "content": [["type": "diff", "path": "hello.txt", "newText": "hi"]]],
                                         "options": [["optionId": "allow", "name": "Allow", "kind": "allow_once"],
                                                     ["optionId": "always", "name": "Always allow", "kind": "allow_always"],
                                                     ["optionId": "reject", "name": "Reject", "kind": "reject_once"]]]),
        ])
        var t = try #require(tool(f, "c1"))
        #expect(f.items.count == 1)                   // no card of its own
        #expect(t.title == "Write hello.txt")         // the request's ToolCallUpdate merges in,
        #expect(t.status == "pending")                // but not its status
        #expect(t.content.count == 1)
        let p = try #require(t.permission)
        let pending = InboxItem(id: "r1", kind: .permission, options: p.options)
        #expect(p.state(item: pending).isPending)
        #expect(t.isOpen(asking: true))
        #expect(p.shownOptions(item: pending).map(\.optionId) == ["allow", "reject"])
        #expect(p.state(item: nil).status.rawValue == "closed")

        f.apply(ev(3, "permission_answer", ["requestId": "r1", "status": "answered",
                                            "outcome": ["outcome": "selected", "optionId": "allow"]]))
        t = try #require(tool(f, "c1"))
        let state = try #require(t.permission).state(item: pending)    // the event wins over a stale item
        #expect(!state.isPending)
        #expect(state.result(options: p.options)! == (.allowed, "Allowed"))
    }

    @Test func aGatewayAskStandsAlone() throws {
        let f = EventFolder([
            ev(1, "permission_request", ["requestId": "g1",
                                         "toolCall": ["toolCallId": "gateway-g1", "title": "github: Create issue",
                                                      "kind": "other", "rawInput": ["title": "Bug"]],
                                         "options": [["optionId": "allow_once", "name": "Allow once", "kind": "allow_once"],
                                                     ["optionId": "allow_hour", "name": "Allow for an hour", "kind": "allow_once"],
                                                     ["optionId": "reject_once", "name": "Reject", "kind": "reject_once"]],
                                         "tool": ["server": "github", "name": "create_issue", "title": "Create issue",
                                                  "arguments": ["title": "Bug"]]]),
            ev(2, "tool_call", ["toolCallId": "toolu_1", "title": "mcp__valet__github__create_issue", "status": "pending"]),
        ])
        guard case .permission(let p) = f.items.first else { Issue.record("no ask card"); return }
        #expect(p.isToolAsk)
        #expect(p.tool?.server == "github")
        #expect(p.shownOptions(item: nil).count == 3)                   // all of a gateway ask's options
        #expect(f.items.count == 2)
        let hour = PermissionState(status: .answered, outcome: ["outcome": "selected", "optionId": "allow_hour"])
        #expect(hour.result(options: p.options)! == (.allowed, "Allowed for an hour"))
    }

    @Test func aRequestBeforeItsToolStandsAloneAndAnswersFindIt() {
        var f = EventFolder([ev(1, "permission_request", ["requestId": "r", "toolCall": ["toolCallId": "c", "title": "X"],
                                                          "options": []])])
        f.apply(ev(2, "permission_answer", ["requestId": "r", "status": "expired", "outcome": nil]))
        guard case .permission(let p) = f.items.first else { Issue.record("no card"); return }
        #expect(p.answer?.status == .expired)
        #expect(p.state(item: nil).result(options: [])! == (.expired, "Expired"))
        f.apply(ev(3, "permission_answer", ["requestId": "unknown", "status": "answered"]))   // ignored
        #expect(f.items.count == 1)
    }

    @Test func settledResults() {
        let opts = [PermissionOption(optionId: "a", name: "Allow", kind: .allowOnce),
                    PermissionOption(optionId: "aa", name: "Always", kind: .allowAlways),
                    PermissionOption(optionId: "r", name: "Reject", kind: .rejectOnce),
                    PermissionOption(optionId: "m", name: "Maybe", kind: "other")]
        func r(_ outcome: JSONValue?) -> String? { PermissionState(status: .answered, outcome: outcome).result(options: opts)?.text }
        #expect(r(["outcome": "selected", "optionId": "a"]) == "Allowed")
        #expect(r(["outcome": "selected", "optionId": "aa"]) == "Always allowed")
        #expect(r(["outcome": "selected", "optionId": "r"]) == "Rejected")
        #expect(r(["outcome": "selected", "optionId": "m"]) == "Maybe")
        #expect(r(["outcome": "cancelled"]) == "Cancelled")
        #expect(r(nil) == "Cancelled")
        #expect(r(["text": "main"]) == "main")
        #expect(PermissionState(status: .pending, outcome: nil).result(options: opts) == nil)
    }

    // MARK: plans, titles, the rest

    @Test func aPlanReplacesTheLastAndMovesToTheEnd() throws {
        let f = EventFolder([
            ev(1, "plan", ["entries": [["content": "one", "status": "pending"]]]),
            ev(2, "agent_message", text("working")),
            ev(3, "plan", ["entries": [["content": "one", "status": "completed"], ["content": "two", "status": "in_progress"]]]),
            ev(4, "agent_message", text("more")),
        ])
        #expect(f.items.map(\.id) == ["m:2", "plan", "m:4"])
        guard case .plan(let p) = f.item("plan") else { Issue.record("no plan"); return }
        #expect(p.steps.map(\.status) == ["completed", "in_progress"])
        #expect(messages(f).map(\.text) == ["working", "more"])    // a plan ends the streaming message
    }

    @Test func sessionInfoTitlesAndUnknownKinds() {
        let f = EventFolder([
            ev(1, "session_info", ["title": "  Fixing the build  "]),
            ev(2, "session_info", ["title": ""]),
            ev(3, "usage", ["used": 5]),
            ev(4, "commands", ["availableCommands": []]),
            ev(5, "teleport", ["anything": true]),
        ])
        #expect(f.title == "Fixing the build")
        #expect(f.items.isEmpty)
        #expect(f.cursor == 5)
    }

    @Test func theRecordedSession() throws {
        let page = try JSONDecoder().decode(EventPage.self, from: fixture("events.json"))
        let f = EventFolder(page.events)
        let kinds = f.items.map { item -> String in
            switch item {
            case .message(let m): return m.role.rawValue + ":" + m.text
            case .tool(let t): return "tool:" + t.title + ":" + t.status + (t.permission != nil ? ":asked" : "")
            case .permission: return "permission"
            case .plan: return "plan"
            case .turnEnd(let e): return "end:" + e.text
            case .handoff(let h): return "handoff:" + h.label
            }
        }
        #expect(kinds == [
            "user:hello", "thought:thinking", "agent:echo: hello",
            "user:permission", "tool:Write hello.txt:completed:asked", "agent:allowed",
            "user:tool", "tool:Run ls:completed", "agent:done",
            "user:slow", "agent:working", "end:Cancelled",
        ])
        #expect(f.openTurns.isEmpty)
    }

    @Test func aHandOffIsFoldedAndItsNoteStandsAlone() {
        let condensed = "[User] fix the build\n[Assistant] done\n--- End of the earlier conversation ---\n\nMy note"
        var data = text(condensed).object ?? [:]
        data["handoff"] = ["source": "Claude Code session in ~/code/x", "entries": 2, "omitted": 0, "note": "My note"]
        let f = EventFolder([ev(1, "user_message", .object(data)), ev(2, "agent_message", text("On it."))])
        #expect(f.items.count == 3)
        guard case .handoff(let h) = f.items[0] else { Issue.record("no hand-off"); return }
        #expect(h.label == "Handed off: Claude Code session in ~/code/x")
        #expect(h.text == condensed)
        guard case .message(let note) = f.items[1] else { Issue.record("no note"); return }
        #expect(note.role == .user && note.text == "My note")
        #expect(f.openTurns == ["t1"])
        // Without a source or a note: "an earlier conversation", nothing else.
        var bare = text("[User] hi").object ?? [:]
        bare["handoff"] = ["entries": 1]
        let g = EventFolder([ev(1, "user_message", .object(bare))])
        #expect(g.items.count == 1)
        guard case .handoff(let h2) = g.items[0] else { Issue.record("no hand-off"); return }
        #expect(h2.label == "Handed off: an earlier conversation")
    }

    @Test func anOwnersQuestionKeepsItsTextAndReadsAsTheWebSaysIt() {
        let options: JSONValue = [["optionId": "option-1", "name": "Yes, tonight", "kind": "allow_once"],
                                  ["optionId": "dismiss", "name": "Dismiss", "kind": "reject_once"]]
        var f = EventFolder([ev(1, "permission_request", [
            "requestId": "q1", "kind": "question", "text": "Should I deploy tonight?\n\nThe build is green.",
            "toolCall": ["toolCallId": "owner-q1", "title": "Should I deploy tonight?"], "options": options,
        ])])
        guard case .permission(let p) = f.items[0] else { Issue.record("no ask"); return }
        #expect(p.isOwnerAsk && !p.isHarnessAsk)
        #expect(p.text == "Should I deploy tonight?\n\nThe build is green.")
        #expect(p.state(item: nil).result(options: p.options, owner: true)?.text == "Closed")
        f.apply(ev(2, "permission_answer", ["requestId": "q1", "status": "answered",
                                            "outcome": ["outcome": "selected", "optionId": "option-1", "text": "after 6"]]))
        guard case .permission(let a) = f.items[0] else { Issue.record("no ask"); return }
        #expect(a.state(item: nil).result(options: a.options, owner: true)?.text == "You answered: Yes, tonight · after 6")
        // The harness's words for the same answer, without `owner`.
        #expect(a.state(item: nil).result(options: a.options)?.text == "Allowed")

        let dismissed = PermissionState(status: .answered, outcome: ["outcome": "selected", "optionId": "dismiss"])
        #expect(dismissed.result(options: a.options, owner: true)?.text == "Dismissed")
        let words = PermissionState(status: .answered, outcome: ["outcome": "text", "text": "Not tonight"])
        #expect(words.result(options: a.options, owner: true)?.text == "You answered: Not tonight")
        #expect(PermissionState(status: .expired, outcome: nil).result(options: [], owner: true)?.text == "Expired")
        #expect(PermissionState(status: .pending, outcome: nil).result(options: [], owner: true) == nil)
    }

    @Test func aChoiceCarriesTheWordsButDismissNever() {
        #expect(OwnerAnswer.body(option: "option-1", typed: "  after 6 ") == ("option-1", "after 6"))
        #expect(OwnerAnswer.body(option: "option-1", typed: " ") == ("option-1", nil))
        #expect(OwnerAnswer.body(option: "dismiss", typed: "ignored") == ("dismiss", nil))
    }

    @Test func onlyTheFirstAllowingOptionIsPrimary() {
        let gateway = [PermissionOption(optionId: "allow_once", name: "Allow once", kind: .allowOnce),
                       PermissionOption(optionId: "allow_hour", name: "Allow for an hour", kind: .allowOnce),
                       PermissionOption(optionId: "allow_always", name: "Always allow", kind: .allowAlways),
                       PermissionOption(optionId: "reject_once", name: "Reject", kind: .rejectOnce)]
        #expect(PermissionOption.primaryID(in: gateway) == "allow_once")
        #expect(PermissionOption.primaryID(in: [gateway[3]]) == nil)
    }

    @Test func diffLines() {
        let lines = DiffView.lines("a\nb\nc\nd\ne\nf\ng\nh", "a\nb\nc\nd\nE\nf\ng\nh")
        #expect(lines.map { "\($0.0.rawValue)\($0.1)" } == ["…", " b", " c", " d", "-e", "+E", " f", " g", " h"])
        #expect(DiffView.lines(nil, "new\nfile").map(\.0) == [.add, .add])
    }
}
