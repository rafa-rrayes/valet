import Foundation
import Testing
@testable import ValetKit

@MainActor
@Suite("An agent's settings, browser profiles, the vault, notifications")
struct SettingsTests {
    /// GET /api/agents/scout as the server sends it (abridged), with fields this app doesn't know.
    nonisolated static let detail = #"""
    {"name": "scout", "display_name": "Scout", "emoji": "🔭", "state": "running", "desired": "running",
     "harness": "claude", "level": "container", "restart_needed": false, "missing_secrets": ["GITHUB_TOKEN"],
     "definition": {"name": "scout", "display_name": "Scout", "emoji": "🔭", "harness": "claude", "model": null,
                    "permissions": "ask", "directives": ["owner", "persona.md", "environment"],
                    "computer": {"level": "container", "network": "none", "allowlist": [], "mounts": [
                        {"host": "/Users/r/notes", "path": "/work/notes", "readonly": true}], "image": null},
                    "tools": {"mcp": ["github"], "skills": ["*"], "rules": {"github": {"*": "ask", "create_issue": "allow"}}},
                    "secrets": ["GITHUB_TOKEN", {"vault": "github/alice", "env": "GH_PASSWORD"}],
                    "browser": {"profiles": ["web"], "rules": {"github.com": {"commit": "allow"}}},
                    "memory": false, "triggers": [{"cron": "0 9 * * *", "prompt": "morning"}], "provider": {"x": 1}},
     "files": {"agent.yaml": "name: scout\n", "persona.md": "You are Scout.\n"}, "commit": "abc123"}
    """#

    func signedIn(_ stub: Stub) -> AppModel {
        let defaults = UserDefaults(suiteName: "valet-tests-\(UUID().uuidString)")!
        defaults.set("http://127.0.0.1:9", forKey: "server")
        return AppModel(store: MemoryTokenStore(["token": "vown_t"]), defaults: defaults, transport: FakeTransport(),
                        urlSession: stub.session)
    }

    @Test func theDraftReadsAndWritesTheFormsFields() throws {
        let d = try decode(AgentDetail.self, Self.detail)
        #expect(d.agent.name == "scout" && d.commit == "abc123" && d.persona == "You are Scout.\n")
        var draft = try #require(d.definition.map(DefinitionDraft.init))
        let saved = draft
        #expect(draft.displayName == "Scout" && draft.harness == "claude" && draft.model == nil)
        #expect(draft.network == "gateway")            // none shows as gateway
        #expect(draft.mounts == [DefinitionDraft.Mount(host: "/Users/r/notes", path: "/work/notes", readonly: true)])
        #expect(draft.toolRule("github", "*") == "ask")
        #expect(draft.secretNames == ["GITHUB_TOKEN"])
        #expect(draft.siteRules == ["github.com": ["commit": "allow"]])

        draft.setString("display_name", "  New name  ")
        #expect(draft.displayName == "New name")
        draft.setString("emoji", "   ")
        #expect(draft["emoji"] == .null)
        draft.setString("computer.workdir", "/x")
        #expect(draft.workdir == "/x")
        draft.setToolRule("github", "create_issue", nil)
        draft.setToolRule("github", "*", nil)
        #expect(draft.toolRules["github"] == nil)      // a server left without rules goes
        draft.setToolRule("linear", "search", "deny")
        draft.toggle("tools.mcp", "github", on: false)
        draft.toggle("tools.mcp", "valet.browser", on: true)
        draft.toggle("tools.mcp", "github", on: true, savedOrder: saved.mcp)
        #expect(draft.mcp == ["github", "valet.browser"])     // back in its saved place
        draft.setMemory(true)
        #expect(draft.memory && draft.directives == ["owner", "persona.md", "environment", "memory"])
        draft.setDirective("authority/standard", on: true)
        #expect(draft.directives == ["owner", "authority/standard", "persona.md", "environment", "memory"])
        draft.setMemory(false)
        #expect(!draft.directives.contains("memory"))
        // What the form doesn't know rides along untouched.
        #expect(draft["triggers"] == saved["triggers"])
        #expect(draft["provider.x"] == 1)
        #expect(draft["tools.skills"] == ["*"])
        #expect(!draft.same(as: saved))
        // A field cleared to null and one never set are the same.
        var a = saved
        a.set("computer.workdir", nil)
        #expect(a.same(as: saved))
    }

    @Test func savingSendsTheWholeDefinitionSorted() async throws {
        let stub = Stub { r in
            r.method == "PUT" ? (200, body(Self.detail)) : (404, body("{}"))
        }
        let api = APIClient(baseURL: URL(string: "http://h")!, urlSession: stub.session)
        var draft = try #require(try decode(AgentDetail.self, Self.detail).definition.map(DefinitionDraft.init))
        draft.siteRules = ["b.com": ["read": "allow"], "a.com": ["commit": "deny", "interact": "ask"]]
        _ = try await api.saveDefinition("scout", draft, persona: nil)
        let r = stub.recorded[0]
        #expect(r.path == "/api/agents/scout/definition")
        #expect(r.body?["persona"] == nil)
        #expect(r.body?["definition"]?["triggers"]?.array?.first?["cron"] == "0 9 * * *")
        let raw = String(decoding: r.raw ?? Data(), as: UTF8.self)
        #expect(raw.range(of: #""a.com":{"commit":"deny","interact":"ask"},"b.com""#) != nil)
        _ = try await api.saveDefinition("scout", draft, persona: "")
        #expect(stub.recorded[1].body?["persona"] == "")
    }

    @Test func errorsGoUnderTheirFields() {
        let body = json(#"""
        {"detail": "invalid", "errors": [
          {"field": "computer.mounts.0.host", "msg": "must be absolute"},
          {"field": "computer.mounts.0", "msg": "twice"},
          {"field": "browser.profiles", "msg": "nope: there's no such browser profile"},
          {"field": "triggers.0.cron", "msg": "bad cron"},
          {"field": "", "msg": "something else"}]}
        """#)
        let keys = ["computer.mounts", "computer.mounts.0", "computer.mounts.0.host", "browser.profiles"]
        let e = FieldErrors(body: body, keys: keys)
        #expect(e["computer.mounts.0.host"] == "must be absolute")
        #expect(e["computer.mounts.0"] == "twice")
        #expect(e["browser.profiles"]?.contains("no such") == true)
        #expect(e.general == ["triggers.0.cron: bad cron", "something else"])
        var cleared = e
        cleared.clear("browser.profiles")
        #expect(cleared["browser.profiles"] == nil)
    }

    @Test func aRefusedSaveShowsItsErrorsAndKeepsTheDraft() async throws {
        let stub = Stub { r in
            switch (r.method, r.path) {
            case ("GET", "/api/agents/scout"): return (200, body(Self.detail))
            case ("PUT", "/api/agents/scout/definition"):
                return (422, body(#"{"detail": "invalid", "errors": [{"field": "computer.allowlist.0", "msg": "not a site"}]}"#))
            case ("GET", "/api/catalog"): return (200, body(#"{"harnesses": [], "permissions": [], "levels": [], "networks": []}"#))
            case ("GET", "/api/secrets"): return (200, body(#"[{"name": "GITHUB_TOKEN", "set": false}]"#))
            case ("GET", "/api/gateway/servers"): return (200, body(#"{"servers": [], "error": null}"#))
            case ("GET", "/api/browser/profiles"): return (200, body(#"{"profiles": [{"name": "web"}], "error": null}"#))
            default: return (200, body("[]"))
            }
        }
        let m = signedIn(stub)
        let s = AgentSettings(name: "scout", model: m)
        await s.load()
        #expect(s.loaded && s.saved != nil && !s.isDirty)
        #expect(s.secretStatus("GITHUB_TOKEN") == false)
        #expect(s.profiles?.map(\.name) == ["web"])
        s.draft?.setStrings("computer.allowlist", ["not a site"])
        s.persona = "Changed."
        #expect(s.isDirty)
        let ok = await s.save()
        #expect(!ok)
        #expect(s.errors["computer.allowlist.0"] == "not a site")
        #expect(s.barError == "Some settings need fixing: see the notes in red.")
        #expect(s.isDirty)
        let put = try #require(stub.recorded.first { $0.method == "PUT" })
        #expect(put.body?["persona"] == "Changed.")
        s.discard()
        #expect(!s.isDirty && s.errors.isEmpty)
    }

    @Test func whatACallToAToolDoes() throws {
        let t = try decode(AgentTools.Tool.self, #"{"name": "github__create_issue", "tool": "create_issue", "decision": "ask", "source": "level", "level_decision": "ask"}"#)
        #expect(ToolFate.decide(t, rules: [:], grant: false) == ("ask", "level"))
        #expect(ToolFate.decide(t, rules: [:], grant: true) == ("allow", "grant"))
        #expect(ToolFate.decide(t, rules: ["*": "deny"], grant: true) == ("deny", "rule"))
        #expect(ToolFate.decide(t, rules: ["*": "deny", "create_issue": "allow"], grant: false) == ("allow", "rule"))
        #expect(ToolFate.decide(t, rules: ["create_issue": "bogus"], grant: false) == ("ask", "level"))
    }

    @Test func sitesAsTyped() {
        #expect(siteOf("https://user@API.GitHub.com:443/x?y#z") == "api.github.com")
        #expect(siteOf("*.google.com.") == "google.com")
        #expect(sitePattern("HTTPS://admin.shopify.com/store/") == "admin.shopify.com/store")
        #expect(sitePattern("*.google.com") == "*.google.com")
    }

    // MARK: browser profiles and the vault

    @Test func profilesAndVaultEntriesDecode() throws {
        let p = try decode(BrowserProfile.self, #"""
        {"name": "web", "title": "Alice's logins", "note": "Personal.", "sites": {"github.com": {"commit": "allow", "read": 3}},
         "used_by": ["scout"], "vault": 2, "running": true}
        """#)
        #expect(p.displayTitle == "Alice's logins" && p.usedBy == ["scout"] && p.vault == 2 && p.running)
        #expect(p.sites == ["github.com": ["commit": "allow"]])
        let v = try decode(VaultEntry.self, #"""
        {"name": "github/alice", "profile": "web", "site": "github.com", "label": null, "username": "alice",
         "has_password": true, "has_totp": false, "created_at": 1, "updated_at": 2, "used_at": null, "used_by": ["scout"],
         "password": "never-here"}
        """#)
        #expect(v.hasPassword && !v.hasTotp && v.username == "alice")
    }

    @Test func vaultWritesSendOnlyWhatChanged() async throws {
        let entry = #"{"name": "github/alice", "profile": "web", "site": "github.com", "has_password": true}"#
        let stub = Stub { r in
            switch (r.method, r.path) {
            case ("POST", "/api/vault"): return (201, body(entry))
            case ("PUT", "/api/vault/github/alice"): return (200, body(entry))
            case ("PUT", "/api/browser/profiles/web"): return (200, body(#"{"name": "web"}"#))
            default: return (404, body(#"{"detail": "no"}"#))
            }
        }
        let api = APIClient(baseURL: URL(string: "http://h")!, urlSession: stub.session)
        _ = try await api.createVaultEntry("github/alice", VaultWrite(profile: "web", site: "github.com", username: "alice",
                                                                      password: "s3cret"))
        #expect(stub.recorded[0].body == ["name": "github/alice", "profile": "web", "site": "github.com",
                                          "username": "alice", "password": "s3cret"])
        // An edit that leaves the password alone doesn't send it; clearing the 2FA sends "".
        _ = try await api.updateVaultEntry("github/alice", VaultWrite(label: "Mine", totp: ""))
        #expect(stub.recorded[1].body == ["label": "Mine", "totp": ""])
        _ = try await api.updateProfile("web", title: "Web", note: nil, sites: ["a.com": ["read": "allow"]])
        #expect(stub.recorded[2].body == ["title": "Web", "sites": ["a.com": ["read": "allow"]]])
    }

    // MARK: hand-off

    @Test func handingOff() async throws {
        let stub = Stub { r in
            switch (r.method, r.path) {
            case ("POST", "/api/handoff/preview"):
                return (200, body(#"{"text": "…", "bytes": 2048, "entries": 12, "omitted": 3, "title": "Fix the build", "source": "a Claude Code session"}"#))
            case ("POST", "/api/agents/scout/handoff"):
                return (201, body(#"{"session": {"agent": "scout", "id": "s9", "state": "working"}, "turn_id": "t1"}"#))
            default: return (404, body("{}"))
            }
        }
        let api = APIClient(baseURL: URL(string: "http://h")!, urlSession: stub.session)
        let p = try await api.handoffPreview(.claude(folder: "-Users-r-x", sessionID: "abc", runner: nil))
        #expect(p.entries == 12 && p.omitted == 3 && p.title == "Fix the build")
        #expect(stub.recorded[0].body == ["source": ["kind": "claude", "folder": "-Users-r-x", "session_id": "abc"]])
        let s = try await api.handoff(agent: "scout", source: .session(agent: "kit", sessionID: "s1"), note: "  ")
        #expect(s.id == "s9")
        #expect(stub.recorded[1].body == ["source": ["kind": "session", "agent": "kit", "session_id": "s1"]])
    }

    // MARK: notifications

    @Test func notificationsAndTheirTest() throws {
        let n = try decode(NotificationSettings.self, #"""
        {"webhook_url": "https://hooks.example.com/x", "push": {"configured": true, "error": null, "topic": "br.com.rrayes.valet",
         "sandbox": true, "devices": 2}}
        """#)
        #expect(n.webhookURL == "https://hooks.example.com/x" && n.push.configured && n.push.devices == 2)
        let r = try decode(NotifyTestResult.self, #"""
        {"webhook": {"ok": false, "error": "502 from the hook"}, "push": {"sent": 1, "failed": 1, "removed": 0, "error": null}}
        """#)
        #expect(r.lines.map(\.text) == ["Webhook: 502 from the hook", "Push: sent to 1 device, 1 failed."])
        #expect(r.lines[0].tone == .bad && r.lines[1].tone == .ok)
        // What isn't set up says so (the web's lines), and no device to send to isn't a success.
        let none = try decode(NotifyTestResult.self, #"{"webhook": null, "push": null}"#)
        #expect(none.lines.map(\.text) == ["Webhook: none set.", "Push: not set up."])
        let noDevices = try decode(NotifyTestResult.self, #"{"webhook": null, "push": {"sent": 0, "failed": 0, "removed": 0}}"#)
        #expect(noDevices.lines[1].text == "Push: no devices yet." && noDevices.lines[1].tone == .neutral)
    }
}
