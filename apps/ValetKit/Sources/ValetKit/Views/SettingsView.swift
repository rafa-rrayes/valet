import SwiftUI

/// The server, the lock, the browser and its vault, notifications (push, the webhook, the devices), runners, and
/// signing out.
public struct SettingsView: View {
    @Environment(AppModel.self) private var model
    @State private var devices: [Device] = []
    @State private var devicesState: DevicesState = .loading
    @State private var error: String?
    @State private var confirmSignOut = false

    enum DevicesState: Equatable { case loading, loaded, unsupported, failed(String) }

    public init() {}

    public var body: some View {
        @Bindable var model = model
        Form {
            Section("Server") {
                LabeledContent("Address") {
                    Text(model.server.absoluteString).textSelection(.enabled).lineLimit(1).truncationMode(.middle)
                }
                if let v = model.me?.version { LabeledContent("Valet", value: v) }
                LabeledContent("Live updates") { ConnectionDot() }
            }

            Section {
                Toggle("Require \(Biometry.name)", isOn: $model.requireLock)
                    .disabled(!Biometry.available)
            } header: {
                Text("Lock")
            } footer: {
                #if os(iOS)
                Text(Biometry.available
                     ? "Valet asks for \(Biometry.name) when it opens, and after a minute in the background."
                     : "Set a passcode on this device to lock Valet.")
                #else
                Text(Biometry.available ? "Valet asks for \(Biometry.name) when it opens, and when the Mac locks or sleeps."
                     : "Set a login password to lock Valet.")
                #endif
            }

            Section {
                NavigationLink(value: DirectivesRef()) { Label("About you & authority", systemImage: "person.text.rectangle") }
                    .accessibilityIdentifier("settings-directives")
                NavigationLink(value: SkillsRef()) { Label("Skills", systemImage: "book.closed") }
                    .accessibilityIdentifier("settings-skills")
                NavigationLink(value: SecretsToolsRef()) { Label("Secrets & tools", systemImage: "key") }
                    .accessibilityIdentifier("settings-secrets")
            } header: {
                Text("What agents share")
            } footer: {
                Text("What every agent can be told about you, the skills they can load, and the secrets and tool "
                     + "servers they use.")
            }

            Section {
                NavigationLink(value: BrowserSettingsRef()) {
                    Label("Browser profiles and logins", systemImage: "safari")
                }
                .accessibilityIdentifier("settings-browser")
            } header: {
                Text("Browser")
            } footer: {
                Text("Chrome profiles your agents can use, their site rules, and the logins they sign in with.")
            }

            NotificationsSection()

            Section {
                pushLine
                switch devicesState {
                case .loading: ProgressView()
                case .unsupported: Text("This Valet doesn't send notifications yet.").foregroundStyle(.secondary)
                case .failed(let e): ErrorText(text: e)
                case .loaded:
                    if devices.isEmpty { Text("No devices yet.").foregroundStyle(.secondary) }
                    ForEach(devices) { d in deviceRow(d) }
                        .onDelete { idx in idx.map { devices[$0] }.forEach(remove) }
                }
            } header: {
                Text("Devices")
            } footer: {
                Text("New requests from your agents are pushed to these devices, with Allow and Reject on the notification.")
            }

            Section("Runners") {
                if model.runnerList.isEmpty { Text("No runners yet.").foregroundStyle(.secondary) }
                ForEach(model.runnerList) { r in
                    VStack(alignment: .leading, spacing: 3) {
                        HStack {
                            Text(r.name).font(.body.weight(.medium))
                            Spacer()
                            Chip(text: r.connected ? "connected"
                                 : r.lastSeen.map { "seen " + Date(timeIntervalSince1970: $0).ago } ?? "never seen",
                                 color: r.connected ? .green : .gray, dot: true)
                        }
                        let info = [r.version.map { "v" + $0 }, r.harnesses.isEmpty ? nil : r.harnesses.joined(separator: ", ")]
                            .compactMap { $0 }.joined(separator: " · ")
                        if !info.isEmpty { Text(info).font(.caption).foregroundStyle(.secondary) }
                    }
                }
            }

            Section {
                Button("Sign out", role: .destructive) { confirmSignOut = true }
            }
        }
        .formStyle(.grouped)
        .navigationTitle("Settings")
        .readableWidth()
        .task { await loadDevices() }
        .refreshable { await loadDevices() }
        .confirmationDialog("Sign out of Valet on this device?", isPresented: $confirmSignOut, titleVisibility: .visible) {
            Button("Sign out", role: .destructive) { model.signOut() }
        }
        .errorAlert($error)
    }

    @ViewBuilder private var pushLine: some View {
        switch model.push {
        case .registered: Label("This device gets notifications", systemImage: "bell.badge").foregroundStyle(.green)
        case .denied: Label("Notifications are off for Valet in Settings", systemImage: "bell.slash").foregroundStyle(.orange)
        case .unsupported: EmptyView()
        case .failed(let e): Label("Couldn't register: \(e)", systemImage: "exclamationmark.triangle").foregroundStyle(.orange)
        case .off: EmptyView()
        }
    }

    private func deviceRow(_ d: Device) -> some View {
        HStack {
            Image(systemName: d.platform == "macos" ? "laptopcomputer" : "iphone")
            VStack(alignment: .leading, spacing: 2) {
                Text(d.name ?? d.platform).lineLimit(1)
                let mine = d.id.value == model.deviceID || (d.token != nil && d.token == model.pushToken)
                Text([mine ? "This device" : nil, d.createdAt.map { "added " + Date(timeIntervalSince1970: $0).ago },
                      d.lastSeen.map { "last push " + Date(timeIntervalSince1970: $0).ago }]
                    .compactMap { $0 }.joined(separator: " · "))
                    .font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            #if os(macOS)
            Button(role: .destructive) { remove(d) } label: { Image(systemName: "trash") }
                .buttonStyle(.borderless)
                .help("Remove")
            #endif
        }
    }

    private func loadDevices() async {
        do {
            devices = try await model.api.devices()
            devicesState = .loaded
        } catch let e as APIError where e.status == 404 || e.status == 405 {
            devicesState = .unsupported
        } catch is CancellationError {
        } catch {
            devicesState = .failed(error.localizedDescription)
        }
    }

    private func remove(_ d: Device) {
        devices.removeAll { $0.id == d.id }
        Task {
            do { try await model.api.deleteDevice(d.id.value) } catch { self.error = error.localizedDescription }
            await loadDevices()
        }
    }
}

/// Push as the server has it set up, the webhook (a URL that gets a JSON POST for each new request), and a test
/// that sends to both.
private struct NotificationsSection: View {
    @Environment(AppModel.self) private var model
    @State private var settings: NotificationSettings?
    @State private var unsupported = false
    @State private var webhook = ""
    @State private var saving = false
    @State private var testing = false
    @State private var result: [(tone: NotifyTestResult.Tone, text: String)]?
    @State private var error: String?

    var body: some View {
        Section {
            if unsupported {
                Text("This Valet doesn't have notification settings yet.").foregroundStyle(.secondary)
            } else if let s = settings {
                LabeledContent("Push") {
                    Text(s.push.error != nil ? "Can't send" : s.push.configured
                         ? "On\(s.push.sandbox ? " (sandbox)" : ""), \(s.push.devices) device\(s.push.devices == 1 ? "" : "s")"
                         : "Not set up on the server")
                        .foregroundStyle(s.push.error != nil ? .orange : .secondary)
                }
                if let e = s.push.error { ErrorText(text: e) }
                VStack(alignment: .leading, spacing: 6) {
                    TextField("Webhook", text: $webhook, prompt: Text(verbatim: "https://hooks.example.com/…"))
                        .autocorrectionDisabled()
                        #if os(iOS)
                        .textInputAutocapitalization(.never)
                        .keyboardType(.URL)
                        #endif
                        .accessibilityIdentifier("field-webhook")
                    HStack {
                        let current = s.webhookURL ?? ""
                        let typed = webhook.trimmingCharacters(in: .whitespacesAndNewlines)
                        if typed != current {
                            Button("Save") { Task { await save(typed) } }
                                .buttonStyle(.borderedProminent)
                                .disabled(saving)
                                .accessibilityIdentifier("webhook-save")
                        }
                        if !current.isEmpty {
                            Button("Remove", role: .destructive) { Task { await save(nil) } }
                                .buttonStyle(.bordered).tint(.red)
                                .disabled(saving)
                        }
                        Spacer()
                        Button {
                            Task { await test(typed: typed, saved: current) }
                        } label: {
                            if testing { ProgressView().controlSize(.small) } else { Text("Send a test") }
                        }
                        .buttonStyle(.bordered)
                        .disabled(testing || saving || (typed.isEmpty && current.isEmpty && !s.push.configured))
                        .accessibilityIdentifier("notify-test")
                    }
                    .controlSize(.small)
                }
                if let result {
                    ForEach(Array(result.enumerated()), id: \.offset) { _, line in
                        Label(line.text, systemImage: line.tone == .ok ? "checkmark.circle.fill"
                              : line.tone == .bad ? "xmark.octagon.fill" : "minus.circle")
                            .font(.callout)
                            .foregroundStyle(line.tone == .ok ? Color.green : line.tone == .bad ? .red : .secondary)
                    }
                }
                if let error { ErrorText(text: error) }
            } else if let error {
                ErrorText(text: error)
            } else {
                ProgressView()
            }
        } header: {
            Text("Notifications")
        } footer: {
            VStack(alignment: .leading, spacing: 6) {
                if let s = settings, !s.push.configured, s.push.error == nil {
                    // One literal, so its `code` is drawn as code.
                    Text("Push isn't set up: the control plane needs your APNs key. Set `VALET_APNS_KEY_FILE`, `VALET_APNS_KEY_ID` and `VALET_APNS_TEAM_ID` where it runs, and restart it.")
                }
                Text("The webhook gets a JSON POST for each new request (a Slack or ntfy URL works); the URL carries its "
                 + "credential, so the server never logs it.")
            }
        }
        .task(id: model.hellos) { await load() }
    }

    private func load() async {
        do {
            let s = try await model.api.notifications()
            settings = s
            webhook = s.webhookURL ?? ""
            error = nil
        } catch let e as APIError where e.status == 404 || e.status == 405 {
            unsupported = true
        } catch is CancellationError {
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func save(_ url: String?) async {
        saving = true
        error = nil
        result = nil
        defer { saving = false }
        do {
            let s = try await model.api.setWebhook(url)
            settings = s
            webhook = s.webhookURL ?? ""
        } catch {
            self.error = error.localizedDescription
        }
    }

    /// A URL typed but not saved is saved first: the test goes where notifications will (notify.js).
    private func test(typed: String, saved: String) async {
        if typed != saved {
            await save(typed)
            guard error == nil else { return }
        }
        testing = true
        error = nil
        result = nil
        defer { testing = false }
        do { result = try await model.api.testNotifications().lines } catch { self.error = error.localizedDescription }
    }
}
