import SwiftUI

// The browser's log, its hour grants and the whole vault: what Settings › Browser and a profile show besides the
// profiles themselves (browser.js's auditBox, grantsBox and #/vault).

/// The activity log, newest first: who did what where, how it ended, the page then. `profile` fixes it to one
/// profile (a profile's screen); without it, Everyone/an agent and every profile/one are menus. Older pages back;
/// `reload` bumping (Refresh, a browser ask settled, a reconnect) loads it again.
struct BrowserActivitySection: View {
    var profile: String?
    var profiles: [BrowserProfile] = []
    let reload: Int
    @Environment(AppModel.self) private var model
    @State private var agent: String?
    @State private var picked: String?
    @State private var rows: [BrowserAction] = []
    @State private var more = false
    @State private var loaded = false
    @State private var loadingOlder = false
    @State private var error: String?
    @State private var refreshes = 0

    private var key: String { "\(reload)/\(refreshes)/\(agent ?? "")/\(picked ?? "")" }

    var body: some View {
        Section {
            if profile == nil { filters }
            if let error { ErrorText(text: error) }
            if !loaded && error == nil { ProgressView() }
            if loaded && rows.isEmpty && error == nil { Text("Nothing yet.").foregroundStyle(.secondary) }
            ForEach(rows, id: \.rowKey) { a in BrowserActionRow(action: a, showsProfile: profile == nil) }
            if more {
                Button {
                    Task { await load(before: rows.last?.id) }
                } label: {
                    if loadingOlder { ProgressView().controlSize(.small) } else { Text("Older") }
                }
                .disabled(loadingOlder)
                .accessibilityIdentifier("audit-older")
            }
        } header: {
            HStack {
                Text("Activity")
                Spacer()
                Button("Refresh") { refreshes += 1 }
                    .font(.caption)
                    .buttonStyle(.borderless)
                    .accessibilityIdentifier("audit-refresh")
            }
        } footer: {
            Text("Every action in the browser, the agents' and yours in the live view, with the page as it was.")
        }
        .task(id: key) { await load(before: nil) }
    }

    @ViewBuilder private var filters: some View {
        HStack(spacing: 12) {
            Menu {
                Picker("Whose actions", selection: $agent) {
                    Text("Everyone").tag(String?.none)
                    ForEach(model.agentList) { a in Text(a.title).tag(String?.some(a.name)) }
                }
            } label: {
                Label(agent.map(model.title(of:)) ?? "Everyone", systemImage: "person.2")
            }
            .accessibilityIdentifier("audit-agent")
            Menu {
                Picker("Which profile", selection: $picked) {
                    Text("Every profile").tag(String?.none)
                    ForEach(profiles) { p in Text(p.displayTitle).tag(String?.some(p.name)) }
                }
            } label: {
                Label(picked.flatMap { n in profiles.first { $0.name == n }?.displayTitle ?? n } ?? "Every profile",
                      systemImage: "safari")
            }
            .accessibilityIdentifier("audit-profile")
            Spacer(minLength: 0)
        }
        .font(.callout)
        #if os(macOS)
        .menuStyle(.borderlessButton)
        .fixedSize()
        #endif
    }

    private func load(before: Int?) async {
        if before != nil { loadingOlder = true }
        defer { loadingOlder = false }
        // An older page asked for under another filter (or before a refresh) isn't this list's: drop it, as
        // browser.js's seq does.
        let asked = key
        do {
            let page = try await model.api.browserAudit(agent: profile == nil ? agent : nil, profile: profile ?? picked,
                                                        before: before)
            guard asked == key else { return }
            rows = before == nil ? page.actions : rows + page.actions.filter { a in !rows.contains { $0.id == a.id } }
            more = page.more
            error = nil
        } catch is CancellationError {
            return
        } catch let e as APIError where e.status == 404 {
            rows = []
            more = false
        } catch {
            self.error = error.localizedDescription
        }
        loaded = true
    }
}

/// One action: "Scout clicked #submit", how it ended, when, where, and the page then (tap to see it bigger).
struct BrowserActionRow: View {
    let action: BrowserAction
    var showsProfile = true
    @Environment(AppModel.self) private var model

    var body: some View {
        let a = action
        let o = a.outcomeLabel
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                let who = Text(verbatim: a.agent.map(model.title(of:)) ?? "You").bold()
                let target = a.target.map { Text(verbatim: " " + $0).font(.callout.monospaced()) } ?? Text("")
                Text("\(who) \(a.actionText)\(target)")
                    .lineLimit(3)
                    .frame(maxWidth: .infinity, alignment: .leading)
                Chip(text: o.text, color: o.tone.color).fixedSize()
            }
            Text([Date(timeIntervalSince1970: a.ts).ago, a.host, showsProfile ? a.profile : nil, a.kindText,
                  a.decision == "ask" ? "asked you" : nil, a.note]
                .compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " · "))
                .font(.caption).foregroundStyle(.secondary)
                .lineLimit(3)
            if let shot = a.shot, !shot.isEmpty { ShotView(name: shot, maxHeight: 140) }
        }
        .padding(.vertical, 2)
        .accessibilityIdentifier("audit-row")
    }
}

/// The "Allow for an hour" answers still running, every agent's, each with Revoke. The screen loads them (a section
/// with nothing in it never runs a task): nothing at all is drawn when there are none, as browser.js hides its card.
struct BrowserGrantsSection: View {
    let grants: [BrowserGrant]
    let revoked: () async -> Void
    @Environment(AppModel.self) private var model
    @State private var revoking: Int?
    @State private var error: String?

    var body: some View {
        if !grants.isEmpty {
            Section {
                ForEach(grants, id: \.rowKey) { g in row(g) }
                if let error { ErrorText(text: error) }
            } header: {
                Text("Allowed for an hour")
            } footer: {
                Text("What you allowed for an hour on a browser ask. Revoke ends it now: the agent asks again.")
            }
        }
    }

    private func row(_ g: BrowserGrant) -> some View {
        HStack(spacing: 8) {
            VStack(alignment: .leading, spacing: 2) {
                Text("\(Text(verbatim: model.title(of: g.agent)).bold()) can \(BrowserKind(rawValue: g.kind).label) on \(Text(verbatim: g.host).font(.callout.monospaced()))")
                    .lineLimit(2)
                Text("\(g.profile) · until \(Date(timeIntervalSince1970: g.expiresAt).formatted(date: .omitted, time: .shortened))")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            Button("Revoke") { Task { await revoke(g) } }
                .buttonStyle(.bordered).controlSize(.small).tint(.red)
                .disabled(revoking == g.id)
                .accessibilityIdentifier("grant-revoke")
        }
    }

    private func revoke(_ g: BrowserGrant) async {
        revoking = g.id
        error = nil
        defer { revoking = nil }
        do {
            try await model.api.deleteBrowserGrant(g.id)
        } catch let e as APIError where e.status == 404 {
        } catch {
            self.error = error.localizedDescription
            return
        }
        await revoked()
    }
}

// MARK: the vault, every profile's

/// Settings › Browser › Vault, as a navigation value.
public struct VaultRef: Hashable, Sendable {
    public init() {}
}

/// Every saved login, under the profile it's for. Tap one to edit it; a new one picks its profile.
public struct VaultListView: View {
    @Environment(AppModel.self) private var model
    @State private var entries: [VaultEntry]?
    @State private var profiles: [BrowserProfile] = []
    @State private var error: String?
    @State private var editing: VaultEntry?
    @State private var adding: String?

    public init() {}

    public var body: some View {
        List {
            if let error { ErrorText(text: error) }
            if let entries {
                if entries.isEmpty {
                    ContentUnavailableView("No logins saved", systemImage: "key",
                                           description: Text("Add one to a profile: the browser fills it in for your "
                                                             + "agents, without them seeing it."))
                }
                ForEach(groups(entries), id: \.profile) { g in
                    Section {
                        ForEach(g.entries) { e in
                            Button { editing = e } label: { VaultRow(entry: e) }
                                .buttonStyle(.plain)
                                .accessibilityIdentifier("vault-row")
                        }
                    } header: {
                        Text(profiles.first { $0.name == g.profile }?.displayTitle ?? g.profile)
                    }
                }
            } else if error == nil {
                ProgressView()
            }
        }
        .navigationTitle("Vault")
        .readableWidth()
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Menu {
                    ForEach(profiles) { p in Button(p.displayTitle) { adding = p.name } }
                } label: {
                    Label("New login", systemImage: "plus")
                }
                .disabled(profiles.isEmpty)
                .accessibilityIdentifier("vault-new")
            }
        }
        .task(id: model.hellos) { await load() }
        .refreshable { await load() }
        .sheet(item: $editing) { e in
            VaultEntryEditor(profile: e.profile, entry: e) { Task { await load() } }
        }
        .sheet(item: Binding(get: { adding.map(VaultRef.Pick.init) }, set: { adding = $0?.id })) { p in
            VaultEntryEditor(profile: p.id, entry: nil) { Task { await load() } }
        }
    }

    private func groups(_ entries: [VaultEntry]) -> [(profile: String, entries: [VaultEntry])] {
        Dictionary(grouping: entries, by: \.profile)
            .map { (profile: $0.key, entries: $0.value.sorted { $0.name < $1.name }) }
            .sorted { $0.profile < $1.profile }
    }

    private func load() async {
        do {
            async let e = model.api.vault()
            async let p = model.api.browserProfiles()
            entries = try await e
            profiles = (try? await p)?.profiles ?? profiles
            error = nil
        } catch is CancellationError {
        } catch {
            self.error = error.localizedDescription
        }
    }
}

extension VaultRef {
    /// A profile a new login is for, as a sheet's item.
    struct Pick: Identifiable { let id: String }
}

/// A saved login as a row: what it is, a "no password" chip when there's none yet, 2FA when there's a secret.
struct VaultRow: View {
    let entry: VaultEntry

    var body: some View {
        HStack(spacing: 8) {
            VStack(alignment: .leading, spacing: 2) {
                Text(entry.label?.isEmpty == false ? entry.label! : entry.name).lineLimit(1)
                Text([entry.label?.isEmpty == false ? entry.name : nil, entry.username, entry.site]
                    .compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " · "))
                    .font(.caption).foregroundStyle(.secondary).lineLimit(1)
            }
            Spacer()
            if !entry.hasPassword { Chip(text: "no password", color: .orange).fixedSize() }
            if entry.hasTotp { Chip(text: "2FA", color: .gray).fixedSize() }
            Image(systemName: "chevron.right").font(.caption).foregroundStyle(.tertiary)
        }
        .contentShape(Rectangle())
    }
}

// A Mac list reuses a row by its id across sections: ids that are only numbers (an action, a grant, a tool call)
// collide there (a grant drawn as the action with its number), so their rows are keyed by kind too.
extension BrowserAction { var rowKey: String { "audit:\(id)" } }
extension BrowserGrant { var rowKey: String { "grant:\(id)" } }
extension ToolCallRecord { var rowKey: String { "call:\(id)" } }
