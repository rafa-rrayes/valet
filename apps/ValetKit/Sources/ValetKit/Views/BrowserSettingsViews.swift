import SwiftUI

public struct BrowserProfileRef: Hashable, Sendable {
    public let name: String
    public init(_ name: String) { self.name = name }
}

/// Settings › Browser, as a navigation value like the profiles it links to.
public struct BrowserSettingsRef: Hashable, Sendable {
    public init() {}
}

/// Settings › Browser: the profiles (real Chrome, each with its logins and site rules), the vault, the hour grants,
/// the activity log, and browser.yaml as text.
public struct BrowserSettingsView: View {
    @Environment(AppModel.self) private var model
    @State private var profiles: [BrowserProfile]?
    @State private var running: [String: RunningBrowser] = [:]
    @State private var grants: [BrowserGrant] = []
    @State private var yamlError: String?
    @State private var error: String?
    @State private var unsupported = false
    @State private var adding = false
    /// Bumped when a browser ask is settled (a grant, an action): the grants and the log load again.
    @State private var settled = 0
    @State private var subscription: UUID?

    public init() {}

    public var body: some View {
        List {
            Section {
                if let yamlError {
                    NoteBox(text: "browser.yaml has an error, so no profile can be used until it's fixed: \(yamlError)",
                            systemImage: "exclamationmark.triangle")
                }
                if let error { ErrorText(text: error) }
                if unsupported { Text("This Valet has no browser yet.").foregroundStyle(.secondary) }
                if let profiles {
                    if profiles.isEmpty && yamlError == nil {
                        Text("No profiles yet. A profile is a Chrome of its own, with its logins: add one, then give it "
                             + "to an agent in its settings.").foregroundStyle(.secondary)
                    }
                    ForEach(profiles) { p in
                        NavigationLink(value: BrowserProfileRef(p.name)) { ProfileRow(profile: p, running: running[p.name]) }
                            .accessibilityIdentifier("profile-row")
                    }
                } else if error == nil && !unsupported {
                    ProgressView()
                }
            } header: {
                Text("Profiles")
            } footer: {
                Text("Agents use the browser through the valet.browser tool server, on the profiles their settings list.")
            }
            if !unsupported {
                Section {
                    NavigationLink(value: VaultRef()) {
                        Label {
                            VStack(alignment: .leading, spacing: 2) {
                                Text("Vault")
                                Text("Saved logins the browser fills in for your agents, without them seeing")
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                        } icon: {
                            Image(systemName: "key")
                        }
                    }
                    .accessibilityIdentifier("browser-vault")
                }
                BrowserGrantsSection(grants: grants) { await loadGrants() }
                BrowserActivitySection(profiles: profiles ?? [], reload: model.hellos + settled)
            }
            Section {
                NavigationLink {
                    RawFileEditor(title: "browser.yaml",
                                  note: "The profiles and their site rules. Saved as written, comments kept.",
                                  load: { try await model.api.browserYAML().text },
                                  save: { text in
                                      try await model.api.saveBrowserYAML(text)
                                      await load()
                                  })
                } label: { Label("browser.yaml", systemImage: "curlybraces") }
            } header: {
                Text("Advanced")
            }
        }
        .navigationTitle("Browser")
        .readableWidth()
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button { adding = true } label: { Label("New profile", systemImage: "plus") }
                    .disabled(unsupported)
                    .accessibilityIdentifier("profile-new")
            }
        }
        // No frame says a browser started or stopped, or an agent opened a tab: look again now and then.
        .task(id: model.hellos) {
            await load()
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(15))
                if Task.isCancelled { break }
                await load()
            }
        }
        .refreshable { await load() }
        .onAppear {
            subscription = model.subscribe { frame in
                if case .inbox(let item) = frame, item.kind == .browser, item.status != .pending { settled += 1 }
            }
        }
        .task(id: settled) { await loadGrants() }
        .onDisappear { if let s = subscription { model.unsubscribe(s) } }
        .sheet(isPresented: $adding) {
            ProfileEditor(profile: nil) { _ in Task { await load() } }
        }
    }

    private func loadGrants() async {
        if let g = try? await model.api.browserGrants() { grants = g }
    }

    private func load() async {
        do {
            async let r = model.api.browserProfiles()
            async let up = model.api.runningBrowsers()
            let got = try await r
            profiles = got.profiles
            yamlError = got.error
            error = nil
            if let up = try? await up { running = Dictionary(up.map { ($0.profile, $0) }, uniquingKeysWith: { a, _ in a }) }
            await loadGrants()
        } catch let e as APIError where e.status == 404 {
            unsupported = true
        } catch is CancellationError {
        } catch {
            self.error = error.localizedDescription
        }
    }
}

private struct ProfileRow: View {
    let profile: BrowserProfile
    let running: RunningBrowser?
    @Environment(AppModel.self) private var model

    var body: some View {
        let up = running != nil || profile.running
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 6) {
                Text(profile.displayTitle).lineLimit(1)
                if up { Chip(text: "running", color: .green, dot: true) }
            }
            let parts = [profile.displayTitle != profile.name ? profile.name : nil,
                         running.map { "\($0.tabs) tab\($0.tabs == 1 ? "" : "s")" },
                         running?.busy == true ? "an agent is acting" : nil,
                         profile.vault > 0 ? "\(profile.vault) login\(profile.vault == 1 ? "" : "s")" : nil,
                         profile.usedBy.isEmpty ? "no agent uses it"
                            : "used by " + andList(profile.usedBy.map(model.title(of:)))]
            Text(parts.compactMap { $0 }.joined(separator: " · ")).font(.caption).foregroundStyle(.secondary).lineLimit(2)
        }
    }
}

// MARK: a profile

/// One profile: its Chrome (running or not, its tabs, the live view, Stop), the agents that use it, its site rules,
/// its saved logins (the vault), Edit and Delete.
public struct BrowserProfileView: View {
    let name: String
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var profile: BrowserProfile?
    @State private var tabs: [BrowserTab] = []
    @State private var entries: [VaultEntry]?
    @State private var error: String?
    @State private var gone = false
    @State private var editing = false
    @State private var editingEntry: VaultEntry?
    @State private var addingEntry = false
    @State private var confirmDelete = false
    @State private var busy = false
    @State private var address = ""
    @State private var tabError: String?
    @State private var settled = 0
    @State private var subscription: UUID?

    public init(name: String) { self.name = name }

    public var body: some View {
        List {
            if gone {
                ContentUnavailableView("No profile named \(name)", systemImage: "questionmark.circle")
            } else if let p = profile {
                header(p)
                chrome(p)
                rules(p)
                vault(p)
                BrowserActivitySection(profile: name, reload: model.hellos + settled)
                Section {
                    Button("Delete the profile", role: .destructive) { confirmDelete = true }
                        .disabled(!p.usedBy.isEmpty || p.vault > 0)
                } footer: {
                    if !p.usedBy.isEmpty || p.vault > 0 {
                        Text("A profile can't be deleted while an agent lists it or the vault has logins for it.")
                    }
                }
            } else if let error {
                ErrorText(text: error)
            } else {
                ProgressView()
            }
        }
        .navigationTitle(profile?.displayTitle ?? name)
        .readableWidth()
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button("Edit") { editing = true }.disabled(profile == nil)
                    .accessibilityIdentifier("profile-edit")
            }
        }
        .task(id: model.hellos) { await load() }
        .refreshable { await load() }
        .onAppear {
            subscription = model.subscribe { frame in
                if case .inbox(let item) = frame, item.kind == .browser, item.status != .pending,
                   item.browser?.profile == name { settled += 1 }
            }
        }
        .onDisappear { if let s = subscription { model.unsubscribe(s) } }
        .sheet(isPresented: $editing) {
            if let p = profile { ProfileEditor(profile: p) { profile = $0 } }
        }
        .sheet(item: $editingEntry) { e in
            VaultEntryEditor(profile: name, entry: e) { Task { await loadVault() } }
        }
        .sheet(isPresented: $addingEntry) {
            VaultEntryEditor(profile: name, entry: nil) { Task { await loadVault() } }
        }
        .confirmationDialog("Delete \(name)?", isPresented: $confirmDelete, titleVisibility: .visible) {
            Button("Delete, keep its sign-ins") { Task { await delete(wipe: false) } }
            Button("Delete and sign out of every site", role: .destructive) { Task { await delete(wipe: true) } }
        } message: {
            Text("Its Chrome stops. Keeping its data means a profile of the same name gets its sign-ins back.")
        }
        .errorAlert($error)
    }

    @ViewBuilder private func header(_ p: BrowserProfile) -> some View {
        Section {
            if p.displayTitle != p.name {
                LabeledContent("Name") { Text(p.name).font(.body.monospaced()) }
            }
            if let note = p.note, !note.isEmpty { Text(note).font(.callout) }
            LabeledContent("Used by") {
                Text(p.usedBy.isEmpty ? "No agent" : p.usedBy.map(model.title(of:)).joined(separator: ", "))
                    .multilineTextAlignment(.trailing)
            }
        }
    }

    @ViewBuilder private func chrome(_ p: BrowserProfile) -> some View {
        Section {
            HStack {
                Label(p.running ? "Running" : "Not running", systemImage: p.running ? "circle.fill" : "circle")
                    .foregroundStyle(p.running ? .green : .secondary)
                Spacer()
                if p.running {
                    Button("Stop") { Task { await stop() } }.buttonStyle(.bordered).controlSize(.small).disabled(busy)
                }
            }
            Button {
                model.openLiveView(LiveTarget(profile: p.name))
            } label: { Label("Open the live view", systemImage: "rectangle.on.rectangle") }
                .accessibilityIdentifier("profile-live")
            ForEach(tabs) { t in
                Button {
                    model.openLiveView(LiveTarget(profile: p.name, tab: t.tab))
                } label: {
                    VStack(alignment: .leading, spacing: 1) {
                        Text(t.label).lineLimit(1)
                        if !t.url.isEmpty && t.url != t.label {
                            Text(t.url).font(.caption.monospaced()).foregroundStyle(.secondary).lineLimit(1)
                        }
                    }
                }
                .buttonStyle(.plain)
            }
            HStack(spacing: 8) {
                TextField("Open a tab", text: $address, prompt: Text("Open a tab at an address"))
                    .autocorrectionDisabled()
                    #if os(iOS)
                    .textInputAutocapitalization(.never)
                    .keyboardType(.URL)
                    #endif
                    .onSubmit { Task { await openTab() } }
                    .accessibilityLabel("Open a tab")
                    .accessibilityIdentifier("profile-open-tab")
                Button("Open") { Task { await openTab() } }
                    .buttonStyle(.bordered).controlSize(.small)
                    .disabled(busy)
            }
            if let tabError { ErrorText(text: tabError) }
        } header: {
            Text("Chrome")
        } footer: {
            Text("The live view starts it when it isn't running. Stopping it closes its tabs; its sign-ins stay.")
        }
    }

    @ViewBuilder private func rules(_ p: BrowserProfile) -> some View {
        Section {
            if p.sites.isEmpty { Text("None: each agent's level and own rules decide.").foregroundStyle(.secondary) }
            ForEach(p.sites.keys.sorted(), id: \.self) { site in
                VStack(alignment: .leading, spacing: 2) {
                    Text(site).font(.body.monospaced())
                    Text(SiteRulesEditor.kinds.compactMap { k in
                        p.sites[site]?[k.rawValue].map { "\(k.label): \(Self.word($0))" }
                    }.joined(separator: " · "))
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
        } header: {
            Text("Site rules")
        } footer: {
            Text("For every agent that uses it. An agent's own rules beat these.")
        }
    }

    static func word(_ mode: String) -> String {
        switch mode {
        case "allow": return "allow"
        case "ask": return "ask you"
        case "deny": return "deny"
        default: return mode
        }
    }

    @ViewBuilder private func vault(_ p: BrowserProfile) -> some View {
        Section {
            if let entries {
                if entries.isEmpty { Text("No logins saved.").foregroundStyle(.secondary) }
                ForEach(entries) { e in
                    Button { editingEntry = e } label: { VaultRow(entry: e) }
                        .buttonStyle(.plain)
                        .accessibilityIdentifier("vault-row")
                }
            } else {
                ProgressView()
            }
            Button { addingEntry = true } label: { Label("Add a login", systemImage: "plus") }
                .accessibilityIdentifier("vault-new")
        } header: {
            Text("Logins")
        } footer: {
            Text("The agent's login() fills them into the page; it never sees a password or a 2FA code. Values go in "
                 + "and never come back out.")
        }
    }

    private func load() async {
        do {
            let r = try await model.api.browserProfiles()
            guard let p = r.profiles.first(where: { $0.name == name }) else { gone = true; return }
            profile = p
            tabs = p.running ? ((try? await model.api.browserTabs(name)) ?? []) : []
            await loadVault()
        } catch is CancellationError {
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func loadVault() async {
        do { entries = try await model.api.vault(profile: name) } catch { self.error = error.localizedDescription }
        if let p = profile, let n = entries?.count, p.vault != n { profile?.vault = n }
    }

    /// A new tab at the address typed (a blank one without), then the live view on it (browser.js's openTab).
    private func openTab() async {
        busy = true
        tabError = nil
        defer { busy = false }
        do {
            let t = try await model.api.openTab(name, url: address.trimmingCharacters(in: .whitespacesAndNewlines))
            address = ""
            model.openLiveView(LiveTarget(profile: name, tab: t.tab))
            await load()
        } catch {
            tabError = error.localizedDescription
        }
    }

    private func stop() async {
        busy = true
        defer { busy = false }
        do {
            try await model.api.stopBrowser(name)
            await load()
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func delete(wipe: Bool) async {
        do {
            try await model.api.deleteProfile(name, wipe: wipe)
            dismiss()
        } catch {
            self.error = error.localizedDescription
        }
    }
}

// MARK: editors

/// A new profile, or a profile's title, note and site rules.
struct ProfileEditor: View {
    let profile: BrowserProfile?
    let done: (BrowserProfile) -> Void
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var title = ""
    @State private var note = ""
    @State private var sites: SiteRules = [:]
    @State private var errors = FieldErrors()
    @State private var saving = false

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    if profile == nil {
                        TextField("Name", text: $name, prompt: Text("work"))
                            .font(.body.monospaced())
                            .autocorrectionDisabled()
                            #if os(iOS)
                            .textInputAutocapitalization(.never)
                            #endif
                            .accessibilityIdentifier("profile-name")
                        FieldError(text: errors["name"])
                    }
                    TextField("Title", text: $title, prompt: Text("Personal logins"))
                    FieldError(text: errors["title"])
                    TextField("Note", text: $note, prompt: Text("Shown to you and to the agents that use it"),
                              axis: .vertical)
                        .lineLimit(2...6)
                    FieldError(text: errors["note"])
                } footer: {
                    if profile == nil { Text("Lowercase letters, digits and dashes. It can't change later.") }
                }
                Section {
                    SiteRulesEditor(rules: $sites, error: errors["sites"], errorFor: { errors["sites.\($0)"] })
                } header: {
                    Text("Site rules")
                } footer: {
                    Text("For every agent that uses it: allow, ask you or deny each kind of action on a site.")
                }
                if !errors.general.isEmpty { Section { ErrorText(text: errors.general.joined(separator: "\n")) } }
            }
            .formStyle(.grouped)
            .navigationTitle(profile == nil ? "New profile" : "Edit \(profile!.name)")
            #if os(iOS)
            .navigationBarTitleDisplayMode(.inline)
            #endif
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button { Task { await save() } } label: {
                        if saving { ProgressView().controlSize(.small) } else { Text(profile == nil ? "Add" : "Save") }
                    }
                    .disabled(saving || (profile == nil && name.trimmingCharacters(in: .whitespaces).isEmpty))
                    .accessibilityIdentifier("profile-save")
                }
            }
        }
        .onAppear {
            if let p = profile {
                title = p.title ?? ""
                note = p.note ?? ""
                sites = p.sites
            }
        }
        #if os(macOS)
        .frame(minWidth: 520, minHeight: 520)
        #endif
    }

    private func save() async {
        saving = true
        errors = FieldErrors()
        defer { saving = false }
        do {
            let p: BrowserProfile
            if let profile {
                p = try await model.api.updateProfile(profile.name, title: title, note: note, sites: sites)
            } else {
                let n = name.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
                guard n.range(of: #"^[a-z0-9][a-z0-9-]{0,30}$"#, options: .regularExpression) != nil else {
                    errors = FieldErrors(body: ["errors": [["field": "name",
                        "msg": "Lowercase letters, digits and dashes, at most 31, starting with a letter or digit."]]],
                                         keys: ["name"])
                    return
                }
                p = try await model.api.createProfile(name: n, title: title, note: note, sites: sites)
            }
            done(p)
            dismiss()
        } catch let e as APIError where e.body?["errors"]?.array != nil {
            errors = FieldErrors(body: e.body, keys: ["name", "title", "note", "sites"] + sites.keys.map { "sites.\($0)" })
        } catch {
            errors = FieldErrors(body: ["errors": [["field": "", "msg": .string(error.localizedDescription)]]], keys: [])
        }
    }
}

/// A saved login: its name (new ones), site, label, username, and the write-only password and 2FA secret: an
/// existing one says whether it's set and can be replaced or cleared, never shown.
struct VaultEntryEditor: View {
    let profile: String
    let entry: VaultEntry?
    let done: () -> Void
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var site = ""
    @State private var label = ""
    @State private var username = ""
    @State private var password = ""
    @State private var totp = ""
    @State private var clearPassword = false
    @State private var clearTotp = false
    @State private var saving = false
    @State private var error: String?
    @State private var confirmDelete = false

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    if entry == nil {
                        TextField("Name", text: $name, prompt: Text("github/alice"))
                            .font(.body.monospaced())
                            .autocorrectionDisabled()
                            #if os(iOS)
                            .textInputAutocapitalization(.never)
                            #endif
                            .accessibilityIdentifier("vault-name")
                    } else {
                        LabeledContent("Name") { Text(entry!.name).font(.body.monospaced()) }
                    }
                    TextField("Site", text: $site, prompt: Text(verbatim: "github.com"))
                        .autocorrectionDisabled()
                        #if os(iOS)
                        .textInputAutocapitalization(.never)
                        .keyboardType(.URL)
                        #endif
                        .accessibilityIdentifier("vault-site")
                    TextField("Label", text: $label, prompt: Text("optional"))
                    TextField("Username", text: $username)
                        .autocorrectionDisabled()
                        #if os(iOS)
                        .textInputAutocapitalization(.never)
                        .textContentType(.username)
                        #endif
                        .accessibilityIdentifier("vault-username")
                } footer: {
                    if entry == nil {
                        Text("Lowercase, up to three parts separated by /. A bare domain covers its subdomains.")
                    }
                }
                Section {
                    SecureField("Password", text: $password,
                                prompt: Text(entry?.hasPassword == true ? "Saved: type to replace it" : "Password"))
                        .disabled(clearPassword)
                        #if os(iOS)
                        .textContentType(.password)
                        #endif
                        .accessibilityIdentifier("vault-password")
                    if entry?.hasPassword == true { Toggle("Clear the saved password", isOn: $clearPassword) }
                    SecureField("2FA secret", text: $totp,
                                prompt: Text(entry?.hasTotp == true ? "Saved: type to replace it" : "Base32 key or otpauth:// link"))
                        .disabled(clearTotp)
                        .accessibilityIdentifier("vault-totp")
                    if entry?.hasTotp == true { Toggle("Clear the saved 2FA secret", isOn: $clearTotp) }
                } header: {
                    Text("Secrets")
                } footer: {
                    Text("Stored encrypted. Nobody can read them back, you included.")
                }
                if let error { Section { ErrorText(text: error) } }
                if entry != nil {
                    Section { Button("Delete this login", role: .destructive) { confirmDelete = true } }
                }
            }
            .formStyle(.grouped)
            .navigationTitle(entry == nil ? "New login" : "Edit login")
            #if os(iOS)
            .navigationBarTitleDisplayMode(.inline)
            #endif
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") {
                        password = ""
                        totp = ""
                        dismiss()
                    }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button { Task { await save() } } label: {
                        if saving { ProgressView().controlSize(.small) } else { Text("Save") }
                    }
                    .disabled(saving || (entry == nil && (name.isEmpty || site.isEmpty)))
                    .accessibilityIdentifier("vault-save")
                }
            }
            .confirmationDialog("Delete this login?", isPresented: $confirmDelete, titleVisibility: .visible) {
                Button("Delete", role: .destructive) { Task { await delete() } }
            } message: {
                Text("Agents that list it can't start until it's back.")
            }
        }
        .onAppear {
            if let e = entry {
                site = e.site
                label = e.label ?? ""
                username = e.username ?? ""
            }
        }
        .onDisappear {
            password = ""
            totp = ""
        }
        #if os(macOS)
        .frame(minWidth: 460, minHeight: 480)
        #endif
    }

    /// What to send: a new entry's fields that aren't empty; an edit's changes only, the secrets when typed or
    /// cleared ("" clears).
    var write: VaultWrite {
        let t = { (s: String) in s.trimmingCharacters(in: .whitespacesAndNewlines) }
        guard let e = entry else {
            return VaultWrite(profile: profile, site: t(site), label: t(label).isEmpty ? nil : t(label),
                              username: t(username).isEmpty ? nil : t(username),
                              password: password.isEmpty ? nil : password, totp: t(totp).isEmpty ? nil : t(totp))
        }
        return VaultWrite(site: t(site) == e.site ? nil : t(site),
                          label: t(label) == (e.label ?? "") ? nil : t(label),
                          username: t(username) == (e.username ?? "") ? nil : t(username),
                          password: clearPassword ? "" : password.isEmpty ? nil : password,
                          totp: clearTotp ? "" : t(totp).isEmpty ? nil : t(totp))
    }

    private func save() async {
        saving = true
        error = nil
        defer { saving = false }
        do {
            if let e = entry {
                let w = write
                if w != VaultWrite() { _ = try await model.api.updateVaultEntry(e.name, w) }
            } else {
                let n = name.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
                _ = try await model.api.createVaultEntry(n, write)
            }
            password = ""
            totp = ""
            done()
            dismiss()
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func delete() async {
        guard let e = entry else { return }
        do {
            try await model.api.deleteVaultEntry(e.name)
            done()
            dismiss()
        } catch {
            self.error = error.localizedDescription
        }
    }
}
