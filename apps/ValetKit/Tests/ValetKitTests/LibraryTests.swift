import Foundation
import Testing
@testable import ValetKit

@MainActor
@Suite("What agents share: what they're told, skills, secrets, tool calls, the browser's log")
struct LibraryTests {
    func signedIn(_ stub: Stub) -> AppModel {
        let defaults = UserDefaults(suiteName: "valet-tests-\(UUID().uuidString)")!
        defaults.set("http://127.0.0.1:9", forKey: "server")
        return AppModel(store: MemoryTokenStore(["token": "vown_t"]), defaults: defaults, transport: FakeTransport(),
                        urlSession: stub.session)
    }

    @Test func thePresetAndTheLayersInTheirPlaces() throws {
        var d = DefinitionDraft(["name": "scout", "directives": ["owner", "persona.md", "environment"]])
        #expect(d.preset == nil)
        d.setPreset("standard")
        #expect(d.directives == ["owner", "authority/standard", "persona.md", "environment"])
        d.setPreset("github-only")                  // where the old one was
        #expect(d.directives == ["owner", "authority/github-only", "persona.md", "environment"])
        #expect(d.preset == "github-only")
        d.setPreset(nil)
        #expect(d.directives == ["owner", "persona.md", "environment"])
        d.setDirective("owner", on: false)
        d.setPreset("full")
        #expect(d.directives == ["authority/full", "persona.md", "environment"])
    }

    @Test func allSharedSkillsOnAndBackOff() {
        var d = DefinitionDraft(["name": "scout", "tools": ["mcp": [], "skills": ["review"]]])
        d.setAllSkills(true, saved: ["review"], library: ["review", "deploy"])
        #expect(d.skills == ["*"])
        d.setAllSkills(false, saved: ["review"], library: ["review", "deploy"])
        #expect(d.skills == ["review"])
        // Saved with all of them: off keeps each one the library has now.
        d.setAllSkills(false, saved: ["*"], library: ["review", "deploy"])
        #expect(d.skills == ["review", "deploy"])
        d.toggle("tools.skills", "deploy", on: false)
        #expect(d.skills == ["review"])
    }

    @Test func presetsReadAsTheirHeadings() throws {
        let p = try decode([AuthorityPreset].self, #"""
            [{"name": "standard", "text": "# Standing authority: standard\n\n- Fix what's broken", "used_by": ["scout"]},
             {"name": "github-only", "text": "Only GitHub.", "used_by": []}, {"name": "strict", "text": ""}]
            """#)
        #expect(p[0].label == "Standard" && p[0].usedBy == ["scout"])
        #expect(p[1].label == "Github only")
        #expect(AuthorityPreset.sorted(p.map(\.name)) == ["strict", "standard", "github-only"])
        #expect(gist(p[0].text) == "Fix what's broken")
        #expect(gist("# Title\n\nYou can:") == "You can…")
        #expect(slugify("GitHub only!") == "github-only")
    }

    @Test func directivesAndTheirLayers() throws {
        let d = try decode(Directives.self, #"""
            {"agent": "scout", "harness": "claude", "error": null, "text": "About Alice...",
             "layers": [{"name": "owner", "file": "owner.md", "state": "ok", "text": "About Alice..."},
                        {"name": "authority/github-only", "file": null, "state": "missing", "text": ""},
                        {"name": "memory", "state": "off", "text": ""}]}
            """#)
        #expect(d.layers.map(\.label) == ["About you", "Authority: Github only · missing", "Memory · off"])
        #expect(d.whereText.hasPrefix("Added to Claude Code's own system prompt"))
    }

    @Test func aSkillsEditSendsOnlyWhatChanged() throws {
        let s = try decode(Skill.self, #"""
            {"name": "review", "description": "Use when reviewing.", "files": ["SKILL.md", "ref.md", "old.md"], "size": 10,
             "error": null, "used_by": [], "agent": null, "instructions": "1. Read", "extra": {"license": "MIT"},
             "content": {"SKILL.md": "---\nname: review\n---\n1. Read", "ref.md": "a", "old.md": "b"}, "commit": "c"}
            """#)
        #expect(!s.raw && s.info.moreFiles == 2 && s.extra["license"] == "MIT")
        let saved = SkillDraft(s)
        #expect(saved.files.keys.sorted() == ["old.md", "ref.md"])         // SKILL.md is the fields above
        var draft = saved
        draft.instructions = "1. Read\n2. Comment"
        draft.files["ref.md"] = "a2"
        draft.files["old.md"] = nil
        draft.files["new.md"] = ""
        #expect(Skill.editBody(raw: false, saved: saved, draft: draft)
                == ["instructions": "1. Read\n2. Comment", "files": ["ref.md": "a2", "old.md": nil, "new.md": ""]])
        // A SKILL.md without a description is edited as a file, and the fields aren't sent.
        let raw = try decode(Skill.self, #"{"name": "x", "description": null, "content": {"SKILL.md": "oops"}}"#)
        #expect(raw.raw && SkillDraft(raw).files == ["SKILL.md": "oops"])
        #expect(Skill.isValidName("code-review") && !Skill.isValidName("Code_Review") && !Skill.isValidName("a--b"))
        #expect(Skill.name(from: " Code Review ") == "code-review")
        #expect(Skill.isValidPath("scripts/check.sh") && !Skill.isValidPath(".hidden") && !Skill.isValidPath("a/../b"))
    }

    @Test func theRoutes() async throws {
        let stub = Stub { r in
            switch (r.method, r.path) {
            case ("GET", "/api/skills"): return (200, body(#"[{"name": "review", "files": ["SKILL.md"], "used_by": ["scout"]}]"#))
            case ("GET", "/api/agents/scout/skills"):
                return (200, body(#"{"agent": "scout", "all": false, "own": [{"name": "deploy", "replaces_shared": true}], "missing": ["gone"], "problems": ["x: bad"]}"#))
            case ("PUT", "/api/agents/scout/skills/deploy"), ("POST", "/api/skills"):
                return (200, body(#"{"name": "deploy", "description": "d", "agent": "scout", "content": {}}"#))
            case ("DELETE", "/api/skills/review"), ("DELETE", "/api/authority/strict"), ("DELETE", "/api/secrets/OLD"):
                return (200, body(#"{"ok": true}"#))
            case ("PUT", "/api/owner"): return (200, body(#"{"text": "me", "commit": "c", "used_by": ["scout"]}"#))
            case ("POST", "/api/agents/scout/directives/preview"):
                return (200, body(#"{"agent": "scout", "harness": "claude", "layers": [], "text": ""}"#))
            case ("GET", "/api/gateway/calls"):
                return (200, body(#"{"calls": [{"id": 9, "agent": "scout", "server": "github", "tool": "create_issue", "arguments": {"title": "x"}, "decision": "ask", "outcome": "pending", "started_at": 1, "duration": 0.35}], "more": true}"#))
            case ("GET", "/api/gateway/servers"):
                return (200, body(#"{"servers": [{"name": "github", "kind": "http", "builtin": false, "url": "https://x/mcp", "headers": ["Authorization"], "secrets": ["GITHUB_TOKEN"], "agents": ["scout"], "connected": false}], "error": null}"#))
            case ("GET", "/api/browser/audit"):
                return (200, body(#"{"actions": [{"id": 3, "ts": 5, "agent": null, "profile": "web", "action": "live_view", "outcome": "ok"}], "more": false}"#))
            default: return (404, body(#"{"detail": "Not Found"}"#))
            }
        }
        let api = signedIn(stub).api
        #expect(try await api.skills().first?.usedBy == ["scout"])
        let mine = try await api.agentSkills("scout")
        #expect(mine.own.first?.replacesShared == true && mine.missing == ["gone"] && mine.problems == ["x: bad"])
        _ = try await api.updateSkill("deploy", agent: "scout", body: ["description": "d"])
        _ = try await api.createSkill("deploy", description: "d", instructions: "", agent: nil)
        #expect(stub.recorded[3].body == ["name": "deploy", "description": "d", "instructions": ""])
        try await api.deleteSkill("review", agent: nil)
        try await api.deletePreset("strict")
        try await api.deleteSecret("OLD")
        #expect(try await api.saveOwner("me").usedBy == ["scout"])
        _ = try await api.previewDirectives("scout", definition: DefinitionDraft(["name": "scout"]), persona: "p")
        #expect(stub.recorded[8].body == ["definition": ["name": "scout"], "persona": "p"])
        let calls = try await api.toolCalls(before: 12)
        #expect(stub.recorded[9].query == ["limit": "20", "before": "12"])
        #expect(calls.more && calls.calls[0].outcomeLabel.text == "waiting on you")
        #expect(durationText(calls.calls[0].duration) == "350 ms" && durationText(75) == "1 min")
        let servers = try await api.gatewayServerDetails()
        #expect(servers.servers[0].whereText == "https://x/mcp" && servers.servers[0].secrets == ["GITHUB_TOKEN"])
        let audit = try await api.browserAudit(agent: "scout", profile: "web")
        #expect(stub.recorded[11].query == ["limit": "30", "agent": "scout", "profile": "web"])
        #expect(audit.actions[0].agent == nil && audit.actions[0].actionText == "opened the live view"
               && audit.actions[0].outcomeLabel.text == "done")
    }
}
