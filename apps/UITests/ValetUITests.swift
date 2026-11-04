import UIKit
import XCTest

/// The phase-1 flow against a real control plane with the fake agent (tests/fake_agent.py) on a host-level agent:
/// sign in, the agent, start it, a session, `hello` streams, `permission` answered inline, another answered in the
/// inbox, a `slow` turn cancelled. Screenshots go to TEST_RUNNER_VALET_E2E_SHOTS (a directory) when it's set.
///
///   TEST_RUNNER_VALET_E2E_SERVER=http://127.0.0.1:PORT TEST_RUNNER_VALET_E2E_PASSWORD=... \
///   TEST_RUNNER_VALET_E2E_AGENT=helper xcodebuild test -scheme Valet-iOS -only-testing:Valet-iOSUITests ...
///
/// Skipped without a server.
@MainActor
final class ValetUITests: XCTestCase {
    var env: [String: String] { ProcessInfo.processInfo.environment }
    var shots: String? { env["VALET_E2E_SHOTS"] }
    let timeout: TimeInterval = 20

    func testPhaseOneFlow() throws {
        guard let server = env["VALET_E2E_SERVER"], let password = env["VALET_E2E_PASSWORD"] else {
            throw XCTSkip("Set TEST_RUNNER_VALET_E2E_SERVER and TEST_RUNNER_VALET_E2E_PASSWORD to run it.")
        }
        let agentTitle = env["VALET_E2E_AGENT_TITLE"] ?? "Helper"
        let app = XCUIApplication()
        app.launchEnvironment = ["VALET_DEBUG_RESET": "1", "VALET_DEBUG_NOLOCK": "1", "VALET_DEBUG_NOPUSH": "1"]
        app.launch()

        // Sign in.
        let serverField = app.textFields["server"]
        XCTAssertTrue(serverField.waitForExistence(timeout: timeout))
        shot("01-sign-in")
        serverField.tap()
        serverField.typeText(server)
        let pw = app.secureTextFields["password"]
        pw.tap()
        pw.typeText(password)
        app.buttons["sign-in"].tap()

        // The agent list.
        let row = app.staticTexts[agentTitle]
        XCTAssertTrue(row.waitForExistence(timeout: timeout), "the agent isn't listed")
        shot("02-agents")
        row.tap()

        // Its page: start it if it's stopped, then a new session.
        let start = app.buttons["Start"]
        if start.waitForExistence(timeout: 3) {
            shot("03-agent-stopped")
            start.tap()
        }
        let newSession = app.buttons["new-session"]
        XCTAssertTrue(newSession.waitForExistence(timeout: timeout))
        XCTAssertTrue(wait(newSession, "isEnabled == true"), "New session stays disabled")
        shot("04-agent-running")
        newSession.tap()

        // hello streams back.
        send(app, "hello")
        XCTAssertTrue(app.staticTexts["echo: hello"].waitForExistence(timeout: timeout), "no echo")
        shot("05-hello")

        // A permission, answered inline on its tool card.
        send(app, "permission")
        let allow = app.buttons["option-allow"]
        XCTAssertTrue(allow.waitForExistence(timeout: timeout), "no inline ask")
        shot("06-permission-inline")
        allow.tap()
        XCTAssertTrue(app.staticTexts["allowed"].waitForExistence(timeout: timeout), "the agent didn't go on")
        shot("07-permission-allowed")

        // Another, answered in the inbox (back to the agent first: the keyboard covers the tab bar).
        send(app, "permission please")
        XCTAssertTrue(app.buttons["option-allow"].waitForExistence(timeout: timeout))
        app.navigationBars.buttons.element(boundBy: 0).tap()
        XCTAssertTrue(newSession.waitForExistence(timeout: timeout))
        app.tabBars.buttons["Inbox"].tap()
        let inboxAllow = app.buttons["option-allow"]
        XCTAssertTrue(inboxAllow.waitForExistence(timeout: timeout), "not in the inbox")
        shot("08-inbox")
        inboxAllow.tap()
        XCTAssertTrue(app.staticTexts["Nothing is waiting on you"].waitForExistence(timeout: timeout))
        shot("09-inbox-empty")
        // The session again, from the agent's page: its history, with both asks settled.
        app.tabBars.buttons["Agents"].tap()
        let sessionRow = app.buttons["session-row"].firstMatch
        XCTAssertTrue(sessionRow.waitForExistence(timeout: timeout), "the session isn't listed")
        shot("10-agent-sessions")
        sessionRow.tap()
        let allowedTexts = app.staticTexts.matching(identifier: "allowed")
        XCTAssertTrue(wait(allowedTexts, "count >= 2"), "the inbox's answer didn't reach the session")

        // A slow turn, cancelled.
        send(app, "slow")
        let cancel = app.buttons["cancel-turn"]
        XCTAssertTrue(cancel.waitForExistence(timeout: timeout), "no Cancel while working")
        shot("11-working")
        cancel.tap()
        XCTAssertTrue(app.staticTexts["Cancelled"].waitForExistence(timeout: timeout), "no cancelled turn end")
        shot("12-cancelled")
    }

    /// New agents, against a control plane with a host-level fake agent (TEST_RUNNER_VALET_E2E_AGENT_TITLE, default
    /// Helper, ID TEST_RUNNER_VALET_E2E_AGENT, default helper): the form's own checks (no name, an ID that's taken), a
    /// refusal under its field (an emoji with a space), a copy of the agent ("Helper 37", helper-37: a new number each
    /// run, since agents aren't deleted) created and started, its page, and Copy… there.
    func testNewAgentFlow() throws {
        guard let server = env["VALET_E2E_SERVER"], let password = env["VALET_E2E_PASSWORD"] else {
            throw XCTSkip("Set TEST_RUNNER_VALET_E2E_SERVER and TEST_RUNNER_VALET_E2E_PASSWORD to run it.")
        }
        let title = env["VALET_E2E_AGENT_TITLE"] ?? "Helper"
        let id = env["VALET_E2E_AGENT"] ?? "helper"
        let n = Int.random(in: 2...9999)
        let newID = "\(id)-\(n)"
        let app = XCUIApplication()
        app.launchEnvironment = ["VALET_DEBUG_RESET": "1", "VALET_DEBUG_NOLOCK": "1", "VALET_DEBUG_NOPUSH": "1",
                                 "VALET_DEBUG_SERVER": server, "VALET_DEBUG_PASSWORD": password]
        app.launch()
        XCTAssertTrue(app.staticTexts[title].waitForExistence(timeout: timeout), "the agent isn't listed")
        app.buttons["new-agent"].tap()

        // Nothing typed: the form says what's missing.
        let name = app.textFields["new-agent-name"]
        XCTAssertTrue(name.waitForExistence(timeout: timeout), "no New agent form")
        let create = app.buttons["new-agent-create"]
        XCTAssertTrue(wait(create, "isEnabled == true"))
        create.tap()
        XCTAssertTrue(app.staticTexts["Give it a name."].waitForExistence(timeout: timeout))
        if app.buttons["Continue"].exists { app.buttons["Continue"].tap() }   // the keyboard's first-run tip
        shot("30-new-agent-empty")

        // A name whose ID is taken, then one that isn't: the ID follows the name.
        name.tap()
        name.typeText(title)
        XCTAssertTrue(app.staticTexts["There's already an agent with the ID \(id)."].waitForExistence(timeout: timeout))
        shot("31-new-agent-taken")
        name.typeText(" \(n)")
        XCTAssertTrue(wait(app.staticTexts["new-agent-id-shown"], "label == '\(newID)'"), "the ID doesn't follow the name")
        XCTAssertTrue(wait(app.staticTexts["There's already an agent with the ID \(id)."], "exists == false"))
        Thread.sleep(forTimeInterval: 0.5)     // the rows below move up as the error goes

        // An emoji the server refuses: its error under the field.
        let emoji = app.textFields["new-agent-emoji"]
        emoji.coordinate(withNormalizedOffset: CGVector(dx: 0.95, dy: 0.5)).tap()   // the field, right of its label
        emoji.typeText("a b")
        create.tap()
        let refused = app.staticTexts.containing(NSPredicate(format: "label CONTAINS[c] 'one emoji'")).firstMatch
        XCTAssertTrue(refused.waitForExistence(timeout: timeout), "the refusal isn't under the emoji")
        shot("32-new-agent-refused")
        // Emptied (a tap may put the caret inside the text: delete until it's gone).
        for _ in 0..<4 where (emoji.value as? String ?? "").contains(where: { $0 == "a" || $0 == "b" }) {
            emoji.coordinate(withNormalizedOffset: CGVector(dx: 0.99, dy: 0.5)).tap()
            emoji.typeText(String(repeating: XCUIKeyboardKey.delete.rawValue, count: 3))
        }
        XCTAssertFalse((emoji.value as? String ?? "").contains("b"), "the emoji wasn't emptied")

        // A copy of the agent: its brain comes along.
        let copy = reveal(app, app.buttons["start:copy"])
        copy.tap()
        XCTAssertTrue(app.buttons["new-agent-copy-from"].waitForExistence(timeout: timeout) ||
                      app.otherElements["new-agent-copy-from"].exists, "no agent to copy")
        reveal(app, app.buttons["harness:claude"])
        shot("33-new-agent-copy")

        // Create & start: its page, running.
        create.tap()
        XCTAssertTrue(app.staticTexts[newID].waitForExistence(timeout: timeout), "the new agent's page didn't open")
        XCTAssertTrue(app.buttons["Stop"].waitForExistence(timeout: timeout), "the new agent didn't start")
        shot("34-new-agent-created")

        // Copy… on its page: the form again, copying it.
        reveal(app, app.buttons["agent-copy"]).tap()
        XCTAssertTrue(app.navigationBars["Copy of \(title) \(n)"].waitForExistence(timeout: timeout), "Copy… didn't open the form")
        shot("35-copy-from-page")
        app.buttons["Cancel"].tap()
    }

    /// Shared per agent and Delete agent, pressed through: a new agent (the New agent form, a copy of the host-level
    /// fake agent TEST_RUNNER_VALET_E2E_AGENT, default helper, titled TEST_RUNNER_VALET_E2E_AGENT_TITLE; that agent
    /// needs valet.shared at `trusted`), a note and a photo in its Shared, the other agent's Shared without them (and
    /// with a note it left there itself: "1 new in Shared"), then the new agent deleted from its settings (its page
    /// closes, it leaves the list) and another from its row's context menu.
    func testSharedAndDeleteFlow() throws {
        guard let server = env["VALET_E2E_SERVER"], let password = env["VALET_E2E_PASSWORD"] else {
            throw XCTSkip("Set TEST_RUNNER_VALET_E2E_SERVER and TEST_RUNNER_VALET_E2E_PASSWORD to run it.")
        }
        let title = env["VALET_E2E_AGENT_TITLE"] ?? "Helper"
        let id = env["VALET_E2E_AGENT"] ?? "helper"
        let n = Int.random(in: 2...9999)
        let newTitle = "Notes \(n)", newID = "notes-\(n)", spare = "spare-\(n)"
        let api = try E2EAPI(server: server, password: password)
        try api.json("POST", "/api/agents",
                     ["name": spare, "agent_yaml": "name: \(spare)\nharness: claude\ncomputer:\n  level: host\n"])
        // The other agent leaves a note in its own Shared, through the gateway as an agent does.
        if try api.json("GET", "/api/agents/\(id)")["status"] as? String != "idle" {     // a start restarts it
            try api.json("POST", "/api/agents/\(id)/start")
        }
        XCTAssertTrue(api.waitFor { try api.json("GET", "/api/agents/\(id)")["status"] as? String == "idle" },
                      "\(id) didn't start")
        let sid = try XCTUnwrap(api.json("POST", "/api/agents/\(id)/sessions")["id"] as? String)
        try api.prompt(id, sid, "mcp valet_shared__shared_add_note "
                       + Self.args(["title": "From \(title) \(n)", "text": "Left here by the agent."]))
        XCTAssertTrue(api.waitFor { try api.turns(id, sid) >= 1 }, "the agent's note didn't finish")
        let said = try api.lastMessage(id, sid)
        XCTAssertTrue(said.hasPrefix("ok: "), said)

        let app = XCUIApplication()
        app.launchEnvironment = ["VALET_DEBUG_RESET": "1", "VALET_DEBUG_NOLOCK": "1", "VALET_DEBUG_NOPUSH": "1",
                                 "VALET_DEBUG_SERVER": server, "VALET_DEBUG_PASSWORD": password]
        app.launch()
        XCTAssertTrue(app.staticTexts[title].waitForExistence(timeout: timeout), "the agent isn't listed")
        XCTAssertFalse(app.tabBars.buttons["Shared"].exists, "there's still a Shared tab")
        XCTAssertTrue(app.staticTexts["1 new in Shared"].waitForExistence(timeout: timeout), "no '1 new in Shared'")
        shot("70-agents-new-in-shared")

        // A new agent, through the form: a copy of the other one.
        app.buttons["new-agent"].tap()
        let name = app.textFields["new-agent-name"]
        XCTAssertTrue(name.waitForExistence(timeout: timeout), "no New agent form")
        name.tap()
        name.typeText(newTitle)
        if app.buttons["Continue"].exists { app.buttons["Continue"].tap() }   // the keyboard's first-run tip
        reveal(app, app.buttons["start:copy"]).tap()
        app.buttons["new-agent-create"].tap()
        XCTAssertTrue(app.staticTexts[newID].waitForExistence(timeout: timeout), "the new agent's page didn't open")

        // Its Shared: empty, then a note and a photo.
        reveal(app, app.buttons["agent-shared"]).tap()
        XCTAssertTrue(app.staticTexts["Nothing shared yet"].waitForExistence(timeout: timeout), "a new agent's Shared isn't empty")
        XCTAssertTrue(app.navigationBars["Shared with \(newTitle)"].exists, "not the new agent's Shared")
        shot("71-shared-empty")
        app.buttons["shared-add"].tap()
        app.buttons["shared-new-note"].tap()
        let noteTitle = app.textFields["shared-title"]
        XCTAssertTrue(noteTitle.waitForExistence(timeout: timeout))
        noteTitle.tap()
        noteTitle.typeText("Plan \(n)")
        let noteText = app.textViews["shared-text"]
        noteText.tap()
        noteText.typeText("Only for \(newTitle).")
        app.buttons["shared-save-note"].tap()
        XCTAssertTrue(app.staticTexts["Plan \(n)"].firstMatch.waitForExistence(timeout: timeout), "the note isn't listed")
        app.buttons["shared-add"].tap()
        app.buttons["shared-upload-photos"].tap()
        XCTAssertTrue(pickPhoto(app), "couldn't pick a photo")
        let rows = app.buttons.matching(identifier: "shared-row")
        XCTAssertTrue(waitFor { rows.count == 2 }, "the photo isn't listed")
        sleep(1)
        shot("72-shared-note-and-photo")
        let photo = app.buttons.matching(NSPredicate(format: "identifier == 'shared-row' AND NOT label CONTAINS 'Plan'"))
            .firstMatch
        photo.tap()
        XCTAssertTrue(app.descendants(matching: .any)["shared-preview"].waitForExistence(timeout: timeout), "no preview")
        sleep(1)
        shot("73-shared-photo")
        app.navigationBars.buttons.element(boundBy: 0).tap()
        app.navigationBars.buttons.element(boundBy: 0).tap()        // its page
        app.navigationBars.buttons.element(boundBy: 0).tap()        // the agents

        // The other agent's Shared has its own note, not these.
        XCTAssertTrue(app.staticTexts[title].waitForExistence(timeout: timeout))
        app.staticTexts[title].tap()
        let otherShared = reveal(app, app.buttons["agent-shared"])
        XCTAssertTrue(otherShared.label.contains("1 new") || otherShared.staticTexts["1 new"].exists,
                      "no '1 new' by the other agent's Shared")
        otherShared.tap()
        XCTAssertTrue(app.staticTexts["From \(title) \(n)"].firstMatch.waitForExistence(timeout: timeout),
                      "the other agent's note isn't in its Shared")
        XCTAssertFalse(app.staticTexts["Plan \(n)"].exists, "the new agent's note shows in another agent's Shared")
        shot("74-other-agents-shared")
        app.navigationBars.buttons.element(boundBy: 0).tap()
        app.navigationBars.buttons.element(boundBy: 0).tap()

        // Delete agent… from its settings: the confirmation, then its page closes and it leaves the list.
        XCTAssertTrue(app.staticTexts[newTitle].waitForExistence(timeout: timeout))
        app.staticTexts[newTitle].tap()
        reveal(app, app.buttons["agent-settings"]).tap()
        reveal(app, app.buttons["agent-delete"]).tap()
        let confirm = app.buttons["delete-agent-confirm"]
        XCTAssertTrue(confirm.waitForExistence(timeout: timeout), "no confirmation")
        XCTAssertTrue(app.staticTexts.containing(NSPredicate(format: "label CONTAINS 'shared notes and files'")).firstMatch.exists)
        XCTAssertTrue(app.staticTexts.containing(NSPredicate(format: "label BEGINSWITH 'Its working folder'")).firstMatch.exists,
                      "a host agent's folder isn't said to stay")
        XCTAssertFalse(app.switches["delete-volumes"].exists, "a host agent has no volumes to offer")
        shot("75-delete-confirm")
        confirm.tap()
        XCTAssertTrue(app.buttons["new-agent"].waitForExistence(timeout: timeout), "the agent's screens didn't close")
        XCTAssertTrue(wait(app.staticTexts[newTitle], "exists == false"), "the deleted agent is still listed")
        XCTAssertThrowsError(try api.json("GET", "/api/agents/\(newID)"), "the server still has it")
        shot("76-deleted")

        // And from a row's context menu.
        let row = app.buttons["agent-row:\(spare)"].firstMatch
        XCTAssertTrue(reveal(app, row).exists, "no row for \(spare)")
        row.press(forDuration: 1.2)
        let menuDelete = app.buttons["agent-delete"].firstMatch
        XCTAssertTrue(menuDelete.waitForExistence(timeout: timeout), "no Delete agent… in the context menu")
        shot("77-row-menu")
        menuDelete.tap()
        XCTAssertTrue(confirm.waitForExistence(timeout: timeout))
        confirm.tap()
        XCTAssertTrue(wait(row, "exists == false"), "the agent deleted from its menu is still listed")
        shot("78-deleted-from-menu")
    }

    /// An agent whose start needs a secret that isn't set (TEST_RUNNER_VALET_E2E_SECRET_AGENT_TITLE, default
    /// "keeper", needing TEST_RUNNER_VALET_E2E_SECRET, default KEEPER_TOKEN): Start asks for it, Set & start
    /// stores it and starts the agent. Run it against a control plane where the secret isn't set yet.
    func testStartAsksForMissingSecrets() throws {
        guard let server = env["VALET_E2E_SERVER"], let password = env["VALET_E2E_PASSWORD"] else {
            throw XCTSkip("Set TEST_RUNNER_VALET_E2E_SERVER and TEST_RUNNER_VALET_E2E_PASSWORD to run it.")
        }
        let title = env["VALET_E2E_SECRET_AGENT_TITLE"] ?? "keeper"
        let secret = env["VALET_E2E_SECRET"] ?? "KEEPER_TOKEN"
        let app = XCUIApplication()
        app.launchEnvironment = ["VALET_DEBUG_RESET": "1", "VALET_DEBUG_NOLOCK": "1", "VALET_DEBUG_NOPUSH": "1",
                                 "VALET_DEBUG_SERVER": server, "VALET_DEBUG_PASSWORD": password]
        app.launch()
        let row = app.staticTexts[title]
        XCTAssertTrue(row.waitForExistence(timeout: timeout), "the agent isn't listed")
        row.tap()
        let start = app.buttons["Start"]
        XCTAssertTrue(start.waitForExistence(timeout: timeout), "no Start: is the agent already running?")
        start.tap()
        let field = app.secureTextFields["secret-\(secret)"]
        XCTAssertTrue(field.waitForExistence(timeout: timeout), "Start didn't ask for \(secret)")
        shot("20-secret-asked")
        field.tap()
        field.typeText("e2e-value\n")
        app.buttons["secrets-submit"].tap()
        XCTAssertTrue(app.buttons["Stop"].waitForExistence(timeout: timeout), "the agent didn't start")
        shot("21-secret-started")
        app.buttons["Stop"].tap()
        XCTAssertTrue(app.buttons["Start"].waitForExistence(timeout: timeout), "the agent didn't stop")
    }

    /// Phase 2 against TEST_RUNNER_VALET_E2E_PHASE2_AGENT (default scout): a host-level fake agent at `ask` with
    /// valet.browser and valet.shared, browser profile TEST_RUNNER_VALET_E2E_PROFILE (default web), memory on; and
    /// TEST_RUNNER_VALET_E2E_PAGE, an http page with a Save button and a text box. The test drives the agent through
    /// the API (its prompts are JSON, which the keyboard's smart quotes would bend): a browser ask answered in the
    /// inbox, a take-over done in the live view, a file the agent shares, a note, a memory file, a settings save.
    func testPhaseTwoFlow() throws {
        guard let server = env["VALET_E2E_SERVER"], let password = env["VALET_E2E_PASSWORD"],
              let page = env["VALET_E2E_PAGE"] else {
            throw XCTSkip("Set TEST_RUNNER_VALET_E2E_SERVER, _PASSWORD and _PAGE to run it.")
        }
        let agent = env["VALET_E2E_PHASE2_AGENT"] ?? "scout"
        let profile = env["VALET_E2E_PROFILE"] ?? "web"
        let api = try E2EAPI(server: server, password: password)
        let sid = try XCTUnwrap(api.json("POST", "/api/agents/\(agent)/sessions")["id"] as? String)
        let image = "e2e-swatch.png"
        try api.upload(agent, name: image, data: Self.swatch())

        let app = XCUIApplication()
        app.launchEnvironment = ["VALET_DEBUG_RESET": "1", "VALET_DEBUG_NOLOCK": "1", "VALET_DEBUG_NOPUSH": "1",
                                 "VALET_DEBUG_SERVER": server, "VALET_DEBUG_PASSWORD": password,
                                 "VALET_DEBUG_SESSION": "\(agent)/\(sid)"]
        app.launch()
        XCTAssertTrue(app.descendants(matching: .any)["composer"].waitForExistence(timeout: timeout),
                      "the session didn't open")

        // The agent opens the page, then clicks Save: a commit, asked with a screenshot, in the session.
        try api.prompt(agent, sid, "mcp valet_browser__open_tab " + Self.args(["profile": profile, "url": page]))
        XCTAssertTrue(api.waitFor(40) { try api.turns(agent, sid) >= 1 }, "the page didn't open")
        let opened = try api.lastMessage(agent, sid)
        let ref = try XCTUnwrap(opened.firstMatch(of: /button "Save" \[ref=(e\d+)\]/)?.1, opened)
        try api.prompt(agent, sid, "mcp valet_browser__click " + Self.args(["profile": profile, "ref": String(ref)]))
        XCTAssertTrue(app.buttons["browser-shot"].firstMatch.waitForExistence(timeout: timeout), "no ask in the session")
        XCTAssertTrue(app.buttons["option-allow_once"].firstMatch.waitForExistence(timeout: timeout))
        sleep(1)
        shot("30-browser-ask-session")

        // In the inbox, answered there.
        app.navigationBars.buttons.element(boundBy: 0).tap()
        app.tabBars.buttons["Inbox"].tap()
        let allowOnce = app.buttons["option-allow_once"].firstMatch
        XCTAssertTrue(allowOnce.waitForExistence(timeout: timeout), "the browser ask isn't in the inbox")
        XCTAssertTrue(app.buttons["browser-shot"].firstMatch.exists, "no screenshot in the inbox")
        shot("31-browser-ask-inbox")
        allowOnce.tap()
        XCTAssertTrue(app.staticTexts["Nothing is waiting on you"].waitForExistence(timeout: timeout))
        XCTAssertTrue(api.waitFor { try api.turns(agent, sid) >= 2 }, "the click didn't finish")
        XCTAssertTrue(try api.lastMessage(agent, sid).hasPrefix("ok: "), "the click didn't go on")

        // A take-over: the live view shows the page; a tap (the test page then focuses its box), typing and a key
        // reach it; Done hands it back.
        try api.prompt(agent, sid, "mcp valet_browser__request_human "
                       + Self.args(["profile": profile, "reason": "Type hello into the box, then say done."]))
        let takeOver = app.buttons["take-over"].firstMatch
        XCTAssertTrue(takeOver.waitForExistence(timeout: timeout), "no take-over in the inbox")
        shot("32-take-over-ask")
        takeOver.tap()
        let canvas = app.descendants(matching: .any)["live-canvas"]
        XCTAssertTrue(canvas.waitForExistence(timeout: timeout), "no live view")
        XCTAssertTrue(wait(canvas, "value == 'page shown'"), "no frame in the live view")
        sleep(1)
        shot("33-live-view")
        canvas.tap()
        let typing = app.textFields["live-type"]
        typing.tap()
        typing.typeText("hello!")
        app.buttons["live-send"].tap()
        app.buttons["Backspace"].tap()
        sleep(2)
        shot("34-live-typed")
        let done = app.buttons["option-done"].firstMatch
        XCTAssertTrue(done.waitForExistence(timeout: timeout), "no Done in the live view")
        done.tap()
        XCTAssertTrue(wait(canvas, "exists == false"), "the live view stayed open after Done")
        XCTAssertTrue(api.waitFor { try api.turns(agent, sid) >= 3 }, "the take-over didn't finish")
        XCTAssertTrue(try api.lastMessage(agent, sid).hasPrefix("ok: "), "the agent didn't go on after Done")
        try api.prompt(agent, sid, "mcp valet_browser__snapshot " + Self.args(["profile": profile]))
        XCTAssertTrue(api.waitFor { try api.turns(agent, sid) >= 4 })
        let typed = try api.lastMessage(agent, sid)
        XCTAssertTrue(typed.contains(#"textbox "something" [active] [ref=e4]: hello"#)
                      || typed.contains(": hello\n"), "the typing didn't reach the page: \(typed)")

        // The agent shares a file (an add asks first, at ask), allowed in the inbox.
        try api.prompt(agent, sid, "mcp valet_shared__shared_add_file "
                       + Self.args(["name": "findings.md", "content": "# Findings\n\n- The page has a Save button.\n"]))
        let allowAdd = app.buttons["option-allow_once"].firstMatch
        XCTAssertTrue(allowAdd.waitForExistence(timeout: timeout), "the share didn't ask")
        allowAdd.tap()
        XCTAssertTrue(api.waitFor { try api.turns(agent, sid) >= 5 }, "the share didn't finish")

        // Shared: the agent's space (on its page), both files, a new note, the previews.
        app.tabBars.buttons["Agents"].tap()
        let sharedAgent = app.buttons["agent-shared"]
        XCTAssertTrue(sharedAgent.waitForExistence(timeout: timeout), "no Shared on the agent's page")
        reveal(app, sharedAgent).tap()
        XCTAssertTrue(app.staticTexts["findings.md"].firstMatch.waitForExistence(timeout: timeout), "the agent's file isn't listed")
        XCTAssertTrue(app.staticTexts[image].firstMatch.exists, "the uploaded file isn't listed")
        shot("35-shared")
        app.buttons["shared-add"].tap()
        app.buttons["shared-new-note"].tap()
        let noteTitle = app.textFields["shared-title"]
        XCTAssertTrue(noteTitle.waitForExistence(timeout: timeout))
        noteTitle.tap()
        noteTitle.typeText("Plan for Friday")
        let noteText = app.textViews["shared-text"]
        noteText.tap()
        noteText.typeText("Ship phase two, then the triggers.")
        shot("36-shared-new-note")
        app.buttons["shared-save-note"].tap()
        let note = app.staticTexts["Plan for Friday"].firstMatch
        XCTAssertTrue(note.waitForExistence(timeout: timeout), "the note isn't listed")
        sleep(1)
        shot("37-shared-list")
        note.tap()
        XCTAssertTrue(app.staticTexts.containing(NSPredicate(format: "label CONTAINS 'then the triggers'")).firstMatch
            .waitForExistence(timeout: timeout), "the note doesn't open")
        shot("38-shared-note")
        app.navigationBars.buttons.element(boundBy: 0).tap()
        app.staticTexts[image].firstMatch.tap()
        XCTAssertTrue(app.descendants(matching: .any)["shared-preview"].waitForExistence(timeout: timeout), "no preview")
        sleep(1)
        shot("39-shared-picture")
        app.navigationBars.buttons.element(boundBy: 0).tap()
        app.staticTexts["findings.md"].firstMatch.tap()
        XCTAssertTrue(app.staticTexts.containing(NSPredicate(format: "label CONTAINS 'Save button'")).firstMatch
            .waitForExistence(timeout: timeout), "the text file's preview is missing")
        shot("40-shared-text-file")
        app.navigationBars.buttons.element(boundBy: 0).tap()        // back to the agent's page
        app.navigationBars.buttons.element(boundBy: 0).tap()

        // Memory: a new file, written here. Earlier runs' files go first, so the new one is on screen.
        for f in try api.json("GET", "/api/agents/\(agent)/memory")["files"] as? [[String: Any]] ?? [] {
            if let n = f["name"] as? String, n.hasPrefix("e2e-") {
                _ = try api.json("DELETE", "/api/agents/\(agent)/memory/\(n)")
            }
        }
        app.tabBars.buttons["Agents"].tap()
        let memory = app.buttons["agent-memory"]
        XCTAssertTrue(memory.waitForExistence(timeout: timeout), "no Memory on the agent's page")
        shot("41-agent-page")
        memory.tap()
        let newFile = app.buttons["memory-new"]
        XCTAssertTrue(newFile.waitForExistence(timeout: timeout))
        let fileName = "e2e-\(Int(Date().timeIntervalSince1970) % 100_000)"
        newFile.tap()
        let nameField = app.alerts.textFields.firstMatch
        XCTAssertTrue(nameField.waitForExistence(timeout: timeout))
        nameField.typeText(fileName)
        app.alerts.buttons["Create"].tap()
        let editor = app.textViews["memory-editor"]
        XCTAssertTrue(editor.waitForExistence(timeout: timeout), "no editor for the new file")
        editor.tap()
        editor.typeText("# Notes\n\nThe test page saves with its Save button.")
        app.buttons["memory-save"].tap()
        XCTAssertTrue(app.buttons["memory-edit"].waitForExistence(timeout: timeout), "the memory file didn't save")
        shot("42-memory-file")
        app.navigationBars.buttons.element(boundBy: 0).tap()
        XCTAssertTrue(app.staticTexts[fileName].waitForExistence(timeout: timeout), "the file isn't listed")
        shot("43-memory")
        app.navigationBars.buttons.element(boundBy: 0).tap()

        // Its settings: another permission level, saved through the same API as the web UI's form, then back.
        let settings = app.buttons["agent-settings"]
        XCTAssertTrue(settings.waitForExistence(timeout: timeout))
        settings.tap()
        XCTAssertTrue(app.textFields["field-display_name"].waitForExistence(timeout: timeout), "no form")
        shot("44-settings")
        let before = try XCTUnwrap(api.json("GET", "/api/agents/\(agent)")["permissions"] as? String)
        let other = before == "trusted" ? "ask" : "trusted"
        reveal(app, app.buttons["permissions:\(other)"]).tap()
        let save = app.buttons["settings-save"]
        XCTAssertTrue(save.waitForExistence(timeout: timeout))
        shot("45-settings-edited")
        save.tap()
        XCTAssertTrue(app.staticTexts["Saved."].waitForExistence(timeout: timeout), "no Saved.")
        shot("46-settings-saved")
        XCTAssertEqual(try api.json("GET", "/api/agents/\(agent)")["permissions"] as? String, other)
        app.buttons["permissions:\(before)"].tap()
        save.tap()
        XCTAssertTrue(api.waitFor { try api.json("GET", "/api/agents/\(agent)")["permissions"] as? String == before },
                      "the level didn't go back")

        // Settings: the browser profiles, one of them.
        app.tabBars.buttons["Settings"].tap()
        let browser = app.buttons["settings-browser"]
        XCTAssertTrue(browser.waitForExistence(timeout: timeout))
        shot("47-settings-tab")
        browser.tap()
        let row = app.buttons["profile-row"].firstMatch
        XCTAssertTrue(row.waitForExistence(timeout: timeout))
        shot("48-browser-profiles")
        row.tap()
        XCTAssertTrue(app.buttons["profile-live"].waitForExistence(timeout: timeout), "the profile didn't open")
        sleep(1)
        shot("49-browser-profile")
    }

    /// Always-on (M4) against the same scratch stack: Retry on a failed turn, a queued prompt removed, a schedule and
    /// a webhook added in the settings form, the webhook called, a wake-up the agent set and cancelled here, the
    /// runs and the main session.
    func testAlwaysOnFlow() throws {
        guard let server = env["VALET_E2E_SERVER"], let password = env["VALET_E2E_PASSWORD"] else {
            throw XCTSkip("Set TEST_RUNNER_VALET_E2E_SERVER and _PASSWORD to run it.")
        }
        let agent = env["VALET_E2E_PHASE2_AGENT"] ?? "scout"
        let api = try E2EAPI(server: server, password: password)
        // A clean start: no triggers, no pending wake-ups.
        var def = try XCTUnwrap(api.json("GET", "/api/agents/\(agent)")["definition"] as? [String: Any])
        def["triggers"] = [Any]()
        try api.json("PUT", "/api/agents/\(agent)/definition", ["definition": def])
        for w in try api.json("GET", "/api/agents/\(agent)/wakeups")["wakeups"] as? [[String: Any]] ?? [] {
            if let id = w["id"] as? Int { try? api.json("DELETE", "/api/agents/\(agent)/wakeups/\(id)") }
        }
        let sid = try XCTUnwrap(api.json("POST", "/api/agents/\(agent)/sessions")["id"] as? String)
        try api.prompt(agent, sid, "fail")
        XCTAssertTrue(api.waitFor { try api.turns(agent, sid) >= 1 })

        let app = XCUIApplication()
        app.launchEnvironment = ["VALET_DEBUG_RESET": "1", "VALET_DEBUG_NOLOCK": "1", "VALET_DEBUG_NOPUSH": "1",
                                 "VALET_DEBUG_SERVER": server, "VALET_DEBUG_PASSWORD": password,
                                 "VALET_DEBUG_SESSION": "\(agent)/\(sid)"]
        app.launch()

        // Retry: the failed turn goes again (and fails again); only the newest has Retry.
        let retry = app.buttons["retry-turn"]
        XCTAssertTrue(retry.waitForExistence(timeout: timeout), "no Retry on the failed turn")
        shot("50-retry")
        retry.tap()
        XCTAssertTrue(api.waitFor { try api.turns(agent, sid) >= 2 }, "the retry didn't run")
        XCTAssertTrue(app.staticTexts["Sent again"].waitForExistence(timeout: timeout), "no Sent again")
        XCTAssertTrue(wait(app.buttons.matching(identifier: "retry-turn"), "count == 1"), "the first Retry stayed")
        sleep(1)
        shot("51-retried")

        // The queue: a prompt waits behind a slow turn, and is taken out here.
        try api.prompt(agent, sid, "slow")
        XCTAssertTrue(api.waitFor { try api.json("GET", "/api/agents/\(agent)/sessions/\(sid)")["state"] as? String == "working" })
        try api.prompt(agent, sid, "this one waits")
        let remove = app.buttons["queue-remove"]
        XCTAssertTrue(remove.waitForExistence(timeout: timeout), "no queued prompt")
        shot("52-queued")
        remove.tap()
        XCTAssertTrue(wait(remove, "exists == false"), "the queued prompt stayed")
        XCTAssertTrue(api.waitFor { (try api.json("GET", "/api/agents/\(agent)/sessions/\(sid)")["queue"] as? [Any])?.isEmpty == true })
        try api.json("POST", "/api/agents/\(agent)/sessions/\(sid)/cancel")

        // Its settings: a schedule (with what it means) and a webhook, saved.
        app.navigationBars.buttons.element(boundBy: 0).tap()
        let settings = app.buttons["agent-settings"]
        XCTAssertTrue(settings.waitForExistence(timeout: timeout))
        settings.tap()
        XCTAssertTrue(app.textFields["field-display_name"].waitForExistence(timeout: timeout), "no form")
        reveal(app, app.buttons["trigger-add-schedule"]).tap()
        let prompt = reveal(app, app.descendants(matching: .any)["trigger-0-prompt"])
        prompt.tap()
        prompt.typeText("Check the test page")
        XCTAssertTrue(app.staticTexts.containing(NSPredicate(format: "label CONTAINS 'Every day'")).firstMatch
            .waitForExistence(timeout: timeout), "no preview of the schedule")
        shot("53-trigger-schedule")
        reveal(app, app.buttons["trigger-add-webhook"]).tap()
        app.buttons["settings-save"].tap()
        XCTAssertTrue(app.staticTexts["Saved."].waitForExistence(timeout: timeout), "no Saved.")
        let url = reveal(app, app.staticTexts["trigger-1-url"])
        XCTAssertTrue(url.waitForExistence(timeout: timeout), "no webhook address after the save")
        sleep(1)
        shot("54-trigger-webhook")
        let saved = try XCTUnwrap(api.json("GET", "/api/agents/\(agent)/triggers")["triggers"] as? [[String: Any]])
        XCTAssertEqual(saved.map { $0["trigger"] as? String }, ["schedule:0 8 * * *", "webhook:hook"])
        XCTAssertEqual(saved.first?["prompt"] as? String, "Check the test page")

        // The webhook is called (its run goes to the main session), and the agent sets itself a wake-up.
        let key = try XCTUnwrap(saved.last?["key"] as? String)
        try api.hook(agent, "hook", key: key, body: ["ref": "main"])
        try api.prompt(agent, sid, "mcp valet_owner__schedule_wakeup " + Self.args(["prompt": "Look at the page again",
                                                                                   "in_minutes": 90]))
        XCTAssertTrue(api.waitFor { try api.turns(agent, sid) >= 4 }, "no wake-up set")

        // The agent's page: the triggers, the wake-up (cancelled here), the runs and the main session.
        app.navigationBars.buttons.element(boundBy: 0).tap()
        XCTAssertTrue(app.buttons["agent-main-session"].waitForExistence(timeout: timeout), "no main session")
        let wake = app.buttons["wakeup-cancel"]
        XCTAssertTrue(reveal(app, wake).waitForExistence(timeout: timeout), "no wake-up on the agent's page")
        XCTAssertTrue(app.descendants(matching: .any)["trigger-row"].firstMatch.exists, "no triggers on the agent's page")
        shot("55-agent-triggers")
        wake.tap()
        XCTAssertTrue(wait(wake, "exists == false"), "the wake-up stayed")
        XCTAssertTrue(api.waitFor { (try api.json("GET", "/api/agents/\(agent)/wakeups")["wakeups"] as? [Any])?.isEmpty == true })
        let runs = reveal(app, app.buttons["trigger-runs"])
        XCTAssertTrue(runs.waitForExistence(timeout: timeout), "no recent runs")
        runs.tap()
        let run = app.staticTexts["Webhook hook"].firstMatch
        XCTAssertTrue(run.waitForExistence(timeout: timeout), "the webhook's run isn't listed")
        shot("56-runs")
        run.tap()
        XCTAssertTrue(app.descendants(matching: .any)["main-session"].waitForExistence(timeout: timeout),
                      "the main session doesn't say so")
        sleep(1)
        shot("57-main-session")
    }

    /// The agent's computer (m4-computer) against TEST_RUNNER_VALET_E2E_COMPUTER_AGENT (default helper), a host-level
    /// fake agent: a terminal (echo, a rotation that resizes the shell, a second shell, the shells closed on leaving);
    /// its files (a text file the shell wrote, a new folder, uploads from Files and Photos, Keep both on a clash).
    /// The Files upload picks report.txt from On My iPhone: put one there first (apps/README.md says how).
    func testComputerFlow() throws {
        guard let server = env["VALET_E2E_SERVER"], let password = env["VALET_E2E_PASSWORD"] else {
            throw XCTSkip("Set TEST_RUNNER_VALET_E2E_SERVER and TEST_RUNNER_VALET_E2E_PASSWORD to run it.")
        }
        let agent = env["VALET_E2E_COMPUTER_AGENT"] ?? "helper"
        let api = try E2EAPI(server: server, password: password)
        _ = try? api.json("POST", "/api/agents/\(agent)/start")
        XCUIDevice.shared.orientation = .portrait
        let app = XCUIApplication()
        app.launchEnvironment = ["VALET_DEBUG_RESET": "1", "VALET_DEBUG_NOLOCK": "1", "VALET_DEBUG_NOPUSH": "1",
                                 "VALET_DEBUG_SERVER": server, "VALET_DEBUG_PASSWORD": password,
                                 "VALET_DEBUG_AGENT": agent]
        app.launch()
        // TEST_RUNNER_VALET_E2E_COMPUTER_PARTS (terminal,files,attachments) runs only some (the files part needs
        // shell-notes.txt, which the terminal part writes).
        let parts = Set((env["VALET_E2E_COMPUTER_PARTS"] ?? "terminal,files,attachments").split(separator: ",").map(String.init))
        if parts.contains("terminal") { try computerTerminal(app, api, agent) }
        if parts.contains("files") { try computerFiles(app, api, agent) }
        if parts.contains("attachments") { try computerAttachments(app, api, agent) }
    }

    private func computerTerminal(_ app: XCUIApplication, _ api: E2EAPI, _ agent: String) throws {
        // The terminal, from the agent's page: a shell opens and takes the keyboard.
        let terminalRow = app.buttons["agent-terminal"]
        XCTAssertTrue(terminalRow.waitForExistence(timeout: timeout), "no Terminal on the agent's page")
        reveal(app, terminalRow).tap()
        let term = app.descendants(matching: .any)["terminal-1"]
        XCTAssertTrue(term.waitForExistence(timeout: timeout), "no shell")
        XCTAssertTrue(waitFor { !(term.value as? String ?? "").isEmpty }, "no prompt")
        sleep(1)
        app.typeText("echo hi\n")
        XCTAssertTrue(waitForLine(term, "hi"), "no output: \(term.value ?? "")")
        shot("60-terminal")
        // What the files part looks at: a text file from the shell, and nothing left from an earlier run.
        app.typeText("rm -rf archive report.txt report-2.txt; find . -maxdepth 1 -name 'Photo *' -delete; "
                     + "printf 'hello from the shell\\n' > shell-notes.txt\n")

        // Its size follows the view: turned sideways, the shell has more columns.
        app.typeText("clear; stty size\n")
        let portrait = try XCTUnwrap(sizeLine(term), "stty size printed nothing")
        XCUIDevice.shared.orientation = .landscapeLeft
        sleep(2)
        app.typeText("clear; stty size\n")
        XCTAssertTrue(waitFor { (self.sizeLine(term)?.cols ?? 0) > portrait.cols }, "the shell kept \(portrait)")
        shot("61-terminal-landscape")
        XCUIDevice.shared.orientation = .portrait
        sleep(1)

        // A second shell, as a tab; the first one is still there.
        app.buttons["terminal-new"].tap()
        let second = app.descendants(matching: .any)["terminal-2"]
        XCTAssertTrue(second.waitForExistence(timeout: timeout), "no second shell")
        XCTAssertTrue(app.buttons["terminal-tab-1"].exists)
        app.typeText("echo second\n")
        XCTAssertTrue(waitForLine(second, "second"), "the second shell didn't answer")
        shot("62-terminal-tabs")
        app.buttons["terminal-close-2"].tap()
        XCTAssertTrue(wait(second, "exists == false"), "the second tab stayed")

        // Leaving the screen ends its shells (the computer's audit says how each ended).
        app.navigationBars.buttons.element(boundBy: 0).tap()
        XCTAssertTrue(terminalRow.waitForExistence(timeout: timeout))
        XCTAssertTrue(waitFor {
            let entries = (try? api.json("GET", "/api/agents/\(agent)/computer/audit?limit=10")["entries"]) as? [[String: Any]] ?? []
            let closes = entries.prefix(4).filter { $0["action"] as? String == "terminal_close" }
            return closes.count >= 2 && closes.allSatisfy { ($0["note"] as? String ?? "").contains("the owner closed it") }
        }, "the shells weren't closed")
    }

    private func computerFiles(_ app: XCUIApplication, _ api: E2EAPI, _ agent: String) throws {
        // Its files: the folder, and the text file the shell wrote.
        let filesRow = app.buttons["agent-files"]
        XCTAssertTrue(filesRow.waitForExistence(timeout: timeout), "no Files on the agent's page")
        reveal(app, filesRow).tap()
        let notes = app.buttons["file-shell-notes.txt"]
        XCTAssertTrue(notes.waitForExistence(timeout: timeout), "the shell's file isn't listed")
        notes.tap()
        XCTAssertTrue(app.staticTexts.matching(NSPredicate(format: "label CONTAINS 'hello from the shell'")).firstMatch
            .waitForExistence(timeout: timeout), "no text preview")
        shot("63-files-text")
        app.navigationBars.buttons.element(boundBy: 0).tap()

        // A new folder.
        add(app, "files-new-folder")
        let name = app.alerts.textFields.firstMatch
        XCTAssertTrue(name.waitForExistence(timeout: timeout))
        name.typeText("archive")
        app.alerts.buttons["Create"].tap()
        XCTAssertTrue(app.buttons["file-archive"].waitForExistence(timeout: timeout), "no new folder")
        shot("64-files-folder")

        // Uploads: a file from Files, the same again (Keep both), a photo.
        add(app, "files-upload")
        XCTAssertTrue(pickFile(app, "report"), "couldn't pick report.txt from On My iPhone")
        XCTAssertTrue(app.buttons["file-report.txt"].waitForExistence(timeout: timeout), "the upload isn't listed")
        add(app, "files-upload")
        XCTAssertTrue(pickFile(app, "report"))
        let keepBoth = app.buttons["Keep both"].firstMatch
        XCTAssertTrue(keepBoth.waitForExistence(timeout: timeout), "a taken name didn't ask")
        sleep(1)
        shot("65-files-clash")
        keepBoth.tap()
        XCTAssertTrue(app.buttons["file-report-2.txt"].waitForExistence(timeout: timeout), "Keep both didn't keep both")
        add(app, "files-photos")
        XCTAssertTrue(pickPhoto(app), "couldn't pick a photo")
        let photo = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH 'file-Photo '")).firstMatch
        XCTAssertTrue(photo.waitForExistence(timeout: timeout), "the photo isn't listed")
        shot("66-files-uploads")
        let names = (try api.json("GET", "/api/agents/\(agent)/computer/files")["entries"] as? [[String: Any]] ?? [])
            .compactMap { $0["name"] as? String }
        XCTAssertTrue(["archive", "report.txt", "report-2.txt", "shell-notes.txt"].allSatisfy(names.contains), "\(names)")
    }

    private func computerAttachments(_ app: XCUIApplication, _ api: E2EAPI, _ agent: String) throws {
        // A new session, opened in the app.
        let sid = try XCTUnwrap(api.json("POST", "/api/agents/\(agent)/sessions")["id"] as? String)
        app.terminate()
        app.launchEnvironment["VALET_DEBUG_AGENT"] = nil
        app.launchEnvironment["VALET_DEBUG_SESSION"] = "\(agent)/\(sid)"
        app.launch()
        let composer = app.descendants(matching: .any)["composer"]
        XCTAssertTrue(composer.waitForExistence(timeout: timeout), "the session didn't open")

        // A photo and a file from the paperclip: chips, uploaded.
        attach(app, "attach-photos")
        XCTAssertTrue(pickPhoto(app), "couldn't pick a photo")
        attach(app, "attach-files")
        XCTAssertTrue(pickFile(app, "report"), "couldn't pick report.txt from On My iPhone")
        let chips = app.descendants(matching: .any).matching(identifier: "attachment-chip")
        XCTAssertTrue(waitFor { chips.count == 2 && (0..<2).allSatisfy { chips.element(boundBy: $0).value as? String == "ready" } },
                      "the attachments didn't upload")
        composer.tap()
        composer.typeText("Here are a photo and the report.")
        shot("67-attachments-composer")

        // Sent with the text: the picture and the file's link in the conversation; the file opens in Files.
        let send = app.buttons["send"]
        XCTAssertTrue(wait(send, "isEnabled == true"), "Send stays disabled")
        send.tap()
        XCTAssertTrue(app.descendants(matching: .any)["message-image"].firstMatch.waitForExistence(timeout: timeout),
                      "no picture in the conversation")
        let file = app.buttons["message-file"].firstMatch
        XCTAssertTrue(file.waitForExistence(timeout: timeout), "no file in the conversation")
        XCTAssertTrue(wait(chips.firstMatch, "exists == false"), "the chips stayed")
        XCTAssertTrue(api.waitFor { try api.turns(agent, sid) >= 1 }, "the turn didn't finish")
        sleep(1)
        shot("68-attachments-sent")
        let kinds = try api.eventContents(agent, sid).compactMap { $0["type"] as? String }
        XCTAssertTrue(kinds.contains("image") && kinds.contains("resource_link"), "\(kinds)")
        file.tap()
        XCTAssertTrue(app.staticTexts.matching(NSPredicate(format: "label CONTAINS 'A report the owner sends'")).firstMatch
            .waitForExistence(timeout: timeout), "the file didn't open")
        shot("69-attachments-file")
    }

    /// The composer's paperclip, then one of its items.
    private func attach(_ app: XCUIApplication, _ item: String) {
        let menu = app.buttons["attach"]
        XCTAssertTrue(menu.waitForExistence(timeout: timeout), "no paperclip")
        menu.tap()
        let button = app.buttons[item]
        XCTAssertTrue(button.waitForExistence(timeout: timeout), "no \(item) in the paperclip")
        button.tap()
    }

    /// The Files screen's Add menu, then one of its items.
    private func add(_ app: XCUIApplication, _ item: String) {
        let menu = app.buttons["files-add"]
        XCTAssertTrue(menu.waitForExistence(timeout: timeout), "no Add")
        XCTAssertTrue(wait(menu, "isEnabled == true"), "Add stays disabled")
        menu.tap()
        let button = app.buttons[item]
        XCTAssertTrue(button.waitForExistence(timeout: timeout), "no \(item) in Add")
        button.tap()
    }

    /// Picks a file by its name without extension in the document picker, from On My iPhone.
    private func pickFile(_ app: XCUIApplication, _ name: String) -> Bool {
        // The picker shows names without their extension ("report", or "report, 7:26 PM, 26 bytes"); the folder's
        // own rows behind it say "report.txt, 26 B".
        let file = app.descendants(matching: .any)
            .matching(NSPredicate(format: "label == %@ OR label BEGINSWITH %@", name, name + ",")).firstMatch
        if !file.waitForExistence(timeout: 6) {
            let browse = app.buttons["Browse"]
            if browse.waitForExistence(timeout: 3) { browse.tap() }
            let onPhone = app.descendants(matching: .any).matching(NSPredicate(format: "label == 'On My iPhone'")).firstMatch
            if onPhone.waitForExistence(timeout: 6) { onPhone.tap() }
        }
        guard file.waitForExistence(timeout: timeout) else {
            dump(app, "document-picker")
            return false
        }
        file.tap()
        let open = app.buttons["Open"]
        if open.waitForExistence(timeout: 3) && open.isEnabled { open.tap() }
        return true
    }

    /// Picks the first photo in the photo picker.
    private func pickPhoto(_ app: XCUIApplication) -> Bool {
        let photo = app.images.matching(NSPredicate(format: "label BEGINSWITH 'Photo'")).firstMatch
        guard photo.waitForExistence(timeout: timeout) else {
            dump(app, "photo-picker")
            return false
        }
        // The picker's grid says it isn't hittable while its sheet settles (and sometimes after): tap where it is.
        _ = wait(photo, "isHittable == true")
        photo.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5)).tap()
        // The picker's Add or Done (✓ on iOS 26; the screen's own Add menu is files-add).
        let add = app.buttons.matching(NSPredicate(format: "label IN {'Add', 'Done'} AND NOT identifier IN {'files-add', 'attach', 'shared-add'}"))
            .firstMatch
        guard add.waitForExistence(timeout: 5) else {
            dump(app, "photo-picker-selected")
            return false
        }
        add.tap()
        return true
    }

    /// The app's element tree, next to the screenshots (to see what a system picker shows).
    private func dump(_ app: XCUIApplication, _ name: String) {
        shot("dump-" + name)
        if let dir = shots {
            try? app.debugDescription.write(to: URL(fileURLWithPath: dir).appendingPathComponent("dump-\(name).txt"),
                                            atomically: true, encoding: .utf8)
        }
    }

    /// Waits for a line of the terminal's screen that is exactly `text`.
    private func waitForLine(_ term: XCUIElement, _ text: String) -> Bool {
        waitFor {
            let screen = term.value as? String ?? ""
            return screen.split(separator: "\n").contains { $0.trimmingCharacters(in: .whitespaces) == text }
        }
    }

    /// The rows and columns `stty size` printed on the terminal's screen.
    private func sizeLine(_ term: XCUIElement) -> (rows: Int, cols: Int)? {
        let screen = term.value as? String ?? ""
        for line in screen.split(separator: "\n").reversed() {
            let parts = line.split(separator: " ").compactMap { Int($0) }
            if parts.count == 2, line.split(separator: " ").count == 2 { return (parts[0], parts[1]) }
        }
        return nil
    }

    private func waitFor(_ seconds: TimeInterval = 20, _ check: () -> Bool) -> Bool {
        let end = Date().addingTimeInterval(seconds)
        while Date() < end {
            if check() { return true }
            Thread.sleep(forTimeInterval: 0.3)
        }
        return check()
    }

    /// A 96x64 PNG in two colours.
    static func swatch() -> Data {
        UIGraphicsImageRenderer(size: CGSize(width: 96, height: 64)).pngData { c in
            UIColor.systemTeal.setFill()
            c.fill(CGRect(x: 0, y: 0, width: 48, height: 64))
            UIColor.systemOrange.setFill()
            c.fill(CGRect(x: 48, y: 0, width: 48, height: 64))
        }
    }

    static func args(_ a: [String: Any]) -> String {
        String(decoding: try! JSONSerialization.data(withJSONObject: a, options: [.sortedKeys]), as: UTF8.self)
    }

    /// Scrolls up (a lazy form makes rows only near the screen) until the element can be tapped.
    @discardableResult
    private func reveal(_ app: XCUIApplication, _ e: XCUIElement) -> XCUIElement {
        for _ in 0..<12 where !(e.exists && e.isHittable) {
            app.swipeUp(velocity: .slow)
        }
        return e
    }

    private func wait(_ object: Any, _ predicate: String) -> Bool {
        let e = XCTNSPredicateExpectation(predicate: NSPredicate(format: predicate), object: object)
        return XCTWaiter().wait(for: [e], timeout: timeout) == .completed
    }

    private func send(_ app: XCUIApplication, _ text: String) {
        let composer = app.textViews["composer"].exists ? app.textViews["composer"] : app.textFields["composer"]
        XCTAssertTrue(composer.waitForExistence(timeout: timeout))
        composer.tap()
        composer.typeText(text)
        app.buttons["send"].tap()
    }

    private func shot(_ name: String) {
        let png = XCUIScreen.main.screenshot().pngRepresentation
        let a = XCTAttachment(data: png, uniformTypeIdentifier: "public.png")
        a.name = name
        a.lifetime = .keepAlways
        add(a)
        if let dir = shots {
            try? png.write(to: URL(fileURLWithPath: dir).appendingPathComponent(name + ".png"))
        }
    }
}

/// The control plane's API for the test itself: signs in as a native client and drives the agent.
struct E2EAPI {
    let server: URL
    let token: String

    struct Failure: Error, CustomStringConvertible {
        let description: String
    }

    init(server: String, password: String) throws {
        guard let url = URL(string: server) else { throw Failure(description: "bad server \(server)") }
        self.server = url
        let r = try Self.call(url, "POST", "/api/login", token: nil,
                              body: try JSONSerialization.data(withJSONObject: ["password": password, "client": "native"]))
        guard let t = r["token"] as? String else { throw Failure(description: "no token") }
        token = t
    }

    @discardableResult
    func json(_ method: String, _ path: String, _ body: [String: Any]? = nil) throws -> [String: Any] {
        try Self.call(server, method, path, token: token,
                      body: try body.map { try JSONSerialization.data(withJSONObject: $0) })
    }

    func prompt(_ agent: String, _ sid: String, _ text: String) throws {
        try json("POST", "/api/agents/\(agent)/sessions/\(sid)/prompt", ["text": text])
    }

    /// Calls a webhook with its key, as a sender would.
    func hook(_ agent: String, _ name: String, key: String, body: [String: Any]) throws {
        _ = try Self.call(server, "POST", "/hooks/\(agent)/\(name)", token: key,
                          body: try JSONSerialization.data(withJSONObject: body))
    }

    func upload(_ agent: String, name: String, data: Data) throws {
        let q = name.addingPercentEncoding(withAllowedCharacters: .alphanumerics) ?? name
        _ = try Self.call(server, "POST", "/api/agents/\(agent)/shared/files?name=\(q)", token: token, body: data,
                          type: "application/octet-stream", array: true)
    }

    private func events(_ agent: String, _ sid: String) throws -> [[String: Any]] {
        try json("GET", "/api/agents/\(agent)/sessions/\(sid)/events?limit=2000")["events"] as? [[String: Any]] ?? []
    }

    /// The owner's messages' content blocks (text, image, resource_link).
    func eventContents(_ agent: String, _ sid: String) throws -> [[String: Any]] {
        try events(agent, sid).filter { $0["kind"] as? String == "user_message" }
            .compactMap { ($0["data"] as? [String: Any])?["content"] as? [String: Any] }
    }

    /// Finished turns.
    func turns(_ agent: String, _ sid: String) throws -> Int {
        try events(agent, sid).filter { $0["kind"] as? String == "turn_end" }.count
    }

    /// The agent's last message, its chunks joined.
    func lastMessage(_ agent: String, _ sid: String) throws -> String {
        var text = ""
        for e in try events(agent, sid) {
            switch e["kind"] as? String {
            case "user_message": text = ""
            case "agent_message":
                let content = (e["data"] as? [String: Any])?["content"] as? [String: Any]
                text += content?["text"] as? String ?? ""
            default: break
            }
        }
        return text
    }

    func waitFor(_ seconds: TimeInterval = 20, _ check: () throws -> Bool) -> Bool {
        let end = Date().addingTimeInterval(seconds)
        while Date() < end {
            if (try? check()) == true { return true }
            Thread.sleep(forTimeInterval: 0.5)
        }
        return false
    }

    private static func call(_ server: URL, _ method: String, _ path: String, token: String?, body: Data?,
                             type: String = "application/json", array: Bool = false) throws -> [String: Any] {
        var req = URLRequest(url: URL(string: path, relativeTo: server)!)
        req.httpMethod = method
        req.timeoutInterval = 30
        if let token { req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
        if let body {
            req.httpBody = body
            req.setValue(type, forHTTPHeaderField: "Content-Type")
        }
        let done = DispatchSemaphore(value: 0)
        final class Box: @unchecked Sendable { var value: (Data?, URLResponse?, Error?) = (nil, nil, nil) }
        let box = Box()
        URLSession.shared.dataTask(with: req) { d, r, e in
            box.value = (d, r, e)
            done.signal()
        }.resume()
        done.wait()
        let result = box.value
        if let e = result.2 { throw e }
        let status = (result.1 as? HTTPURLResponse)?.statusCode ?? 0
        let data = result.0 ?? Data()
        guard (200..<300).contains(status) else {
            throw Failure(description: "\(method) \(path): \(status) \(String(decoding: data, as: UTF8.self))")
        }
        if array { return [:] }
        return (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
    }
}
