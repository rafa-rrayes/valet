#if DEBUG
import Foundation
#if os(macOS)
import AppKit
#endif
#if os(iOS)
import UIKit
#endif

/// Debug-only launch hooks for driving the apps from scripts (simulator runs, screenshots). Environment
/// variables (for the simulator, prefix them with SIMCTL_CHILD_):
/// VALET_DEBUG_SERVER (the server, e.g. http://127.0.0.1:8790) with VALET_DEBUG_PASSWORD signs in;
/// VALET_DEBUG_NOLOCK skips the lock; VALET_DEBUG_NOPUSH skips the notification prompt;
/// VALET_DEBUG_TAB (agents|inbox|settings) picks the first screen; VALET_DEBUG_AGENT=name opens an agent's page;
/// with it, VALET_DEBUG_OPEN=terminal opens its terminal, files (or files:/a/path) its files, settings its settings,
/// shared its Shared;
/// VALET_DEBUG_SESSION=agent/id opens a session;
/// VALET_DEBUG_ITEM=id opens an inbox item; VALET_DEBUG_LIVE=profile opens a browser profile's live view;
/// VALET_DEBUG_NEW_AGENT (1, or copy:name) the New agent form, with VALET_DEBUG_NEW_AGENT_NAME as its name (and
/// VALET_DEBUG_NEW_AGENT_CREATE: Create & start); VALET_DEBUG_APPEARANCE (light|dark) sets the Mac app's; VALET_DEBUG_LANDSCAPE turns the iPhone (the simulator)
/// to landscape; VALET_DEBUG_SCREEN (browser, vault, secrets, directives, skills,
/// profile:<name>, settings:<agent>) opens a screen over the first; VALET_DEBUG_RESET signs out first and forgets the server (a fresh
/// install, for the UI test). None of this exists in release builds.
@MainActor
enum DebugLaunch {
    static let env = ProcessInfo.processInfo.environment

    static var noLock: Bool { env["VALET_DEBUG_NOLOCK"] != nil }
    static var noPush: Bool { env["VALET_DEBUG_NOPUSH"] != nil }
    static var tab: String? { env["VALET_DEBUG_TAB"] }
    static var agent: String? { env["VALET_DEBUG_AGENT"] }
    static var open: String? { env["VALET_DEBUG_OPEN"] }
    /// VALET_DEBUG_NEW_AGENT: the New agent form (`copy:name`: a copy of that agent), with
    /// VALET_DEBUG_NEW_AGENT_NAME typed as its name.
    static var newAgent: NewAgentRequest? {
        guard let v = env["VALET_DEBUG_NEW_AGENT"] else { return nil }
        return NewAgentRequest(copyFrom: v.hasPrefix("copy:") ? String(v.dropFirst(5)) : nil)
    }
    /// For the first New agent form only (not a Copy… opened from the agent it made): VALET_DEBUG_NEW_AGENT_NAME,
    /// the name to type, and VALET_DEBUG_NEW_AGENT_CREATE, Create & start as if pressed.
    static func takeNewAgentScript() -> (name: String?, create: Bool) {
        guard !newAgentScriptTaken else { return (nil, false) }
        newAgentScriptTaken = true
        return (env["VALET_DEBUG_NEW_AGENT_NAME"], env["VALET_DEBUG_NEW_AGENT_CREATE"] != nil)
    }
    private static var newAgentScriptTaken = false
    static var screen: String? { env["VALET_DEBUG_SCREEN"] }

    static var reset: Bool { env["VALET_DEBUG_RESET"] != nil }

    static func run(_ model: AppModel) async {
        #if os(macOS)
        // The Mac app's own light or dark, whatever the system's (screenshots of both).
        switch env["VALET_DEBUG_APPEARANCE"] {
        case "light": NSApplication.shared.appearance = NSAppearance(named: .aqua)
        case "dark": NSApplication.shared.appearance = NSAppearance(named: .darkAqua)
        default: break
        }
        #endif
        if noLock && model.locked { model.unlockWithoutAsking() }
        if let pw = env["VALET_DEBUG_PASSWORD"], !model.signedIn {
            let server = env["VALET_DEBUG_SERVER"].flatMap(URL.init(string:)) ?? model.server
            do {
                try await model.signIn(server: server, password: pw)
                model.notice = nil          // an unsigned build can't keep the token: a script doesn't mind
            } catch {
                model.notice = error.localizedDescription
            }
        }
        if let s = env["VALET_DEBUG_SESSION"]?.split(separator: "/", maxSplits: 1), s.count == 2 {
            model.openSession = SessionRef(agent: String(s[0]), id: String(s[1]))
        }
        if let id = env["VALET_DEBUG_ITEM"] { model.openItem(id) }
        if let profile = env["VALET_DEBUG_LIVE"] {
            // Once the signed-in screens are in: a cover presented while they replace the sign-in screen is built
            // (its socket opens) but never shown. And not over an alert (an unsigned build's "Couldn't keep the
            // session in the Keychain"), which would keep the sheet from showing: once it's answered.
            try? await Task.sleep(for: .milliseconds(800))
            for _ in 0..<300 where model.notice != nil { try? await Task.sleep(for: .milliseconds(100)) }
            model.openLive = LiveTarget(profile: profile)
        }
        #if os(iOS)
        if env["VALET_DEBUG_LANDSCAPE"] != nil,
           let scene = UIApplication.shared.connectedScenes.first as? UIWindowScene {
            scene.requestGeometryUpdate(.iOS(interfaceOrientations: .landscapeRight))
        }
        #endif
    }
}
#endif
