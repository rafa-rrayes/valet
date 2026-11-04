import SwiftUI

/// Start, Stop and Restart for agent screens and rows. A start refused because secrets aren't set opens their
/// write-only fields, sets them and tries again; restarting an agent in the middle of a reply asks first.
@MainActor @Observable
final class AgentActions {
    enum Verb: String { case start = "Start", restart = "Restart", stop = "Stop" }

    struct SecretsPrompt: Identifiable {
        let id = UUID()
        let agent: String
        let names: [String]
        let lead: String
        let submit: String
        /// Start this once they're set (nil: just set them).
        let then: Verb?
    }

    struct DeleteRequest: Identifiable {
        let agent: String
        var id: String { agent }
    }

    var busy: Set<String> = []
    var error: String?
    var prompt: SecretsPrompt?
    var confirmRestart: String?
    /// Delete agent…: the confirmation shown for it.
    var deleting: DeleteRequest?

    func delete(_ name: String) { deleting = DeleteRequest(agent: name) }

    func run(_ verb: Verb, _ name: String, model: AppModel, confirmed: Bool = false) async {
        if verb == .restart, !confirmed, model.agents[name]?.status == .working {
            confirmRestart = name
            return
        }
        busy.insert(name)
        defer { busy.remove(name) }
        do {
            if verb == .stop { try await model.stop(name) } else { try await model.start(name) }
        } catch let e as APIError where !e.missingSecrets.isEmpty {
            let m = e.missingSecrets
            prompt = SecretsPrompt(agent: name, names: m,
                                   lead: m.count > 1 ? "It can't start until these secrets are set."
                                       : "It can't start until this secret is set.",
                                   submit: verb == .restart ? "Set & restart" : "Set & start", then: verb)
        } catch {
            self.error = error.localizedDescription
        }
    }

    /// "Set" on a "Needs X" line: just the fields.
    func offer(_ names: [String], for agent: String) {
        prompt = SecretsPrompt(agent: agent, names: names,
                               lead: "Stored encrypted. Nobody can read a value back, you included.",
                               submit: "Save", then: nil)
    }
}

extension View {
    /// The sheets and dialogs AgentActions opens.
    func agentActions(_ actions: AgentActions) -> some View {
        modifier(AgentActionsModifier(actions: actions))
    }
}

private struct AgentActionsModifier: ViewModifier {
    @Bindable var actions: AgentActions
    @Environment(AppModel.self) private var model

    func body(content: Content) -> some View {
        content
            .sheet(item: $actions.prompt) { p in
                SecretsSheet(prompt: p) { if let verb = p.then { await actions.run(verb, p.agent, model: model,
                                                                                    confirmed: true) } }
            }
            .confirmationDialog("It's in the middle of a reply: restarting stops it.",
                                isPresented: Binding(get: { actions.confirmRestart != nil },
                                                     set: { if !$0 { actions.confirmRestart = nil } }),
                                titleVisibility: .visible) {
                Button("Restart anyway", role: .destructive) {
                    if let n = actions.confirmRestart {
                        Task { await actions.run(.restart, n, model: model, confirmed: true) }
                    }
                }
                Button("Cancel", role: .cancel) {}
            }
            .sheet(item: $actions.deleting) { DeleteAgentSheet(name: $0.agent) }
            .errorAlert($actions.error)
    }
}

/// Delete agent… (its settings, its row's context menu, the Mac sidebar's): what goes, a container agent's volumes
/// if asked, and that a host agent's folder stays, as the web UI's confirmation says it. Once it's deleted its
/// screens close (AppModel.deletedAgents).
struct DeleteAgentSheet: View {
    let name: String
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var volumes = false
    @State private var busy = false
    @State private var error: String?

    var body: some View {
        let a = model.agents[name]
        let title = a?.title ?? name
        let on = a?.runner.map { " on \($0)" } ?? ""
        NavigationStack {
            Form {
                Section {
                    Text("It stops, and Valet forgets it: its settings, personality, skills and memory leave the "
                         + "definitions repo (its history keeps them), and its sessions, inbox items, shared notes "
                         + "and files, queued messages, triggers and wake-ups go for good.")
                    if a?.level == "host" {
                        Label("Its working folder\(on) stays: Valet doesn't delete your files.",
                              systemImage: "folder")
                            .foregroundStyle(.secondary)
                    }
                }
                // Never placed on a runner, it never ran: no volumes anywhere to offer (the web UI's rule).
                if let a, a.level != nil, a.level != "host", a.runner != nil {
                    Section {
                        Toggle("Also delete its volumes\(on)", isOn: $volumes)
                            .accessibilityIdentifier("delete-volumes")
                    } footer: {
                        Text("Its home and workspace, files and logins, for good. Without this they stay, and a new "
                             + "agent with this name gets them back.")
                    }
                }
                if let error {
                    Section { ErrorText(text: error) }
                }
                Section {
                    Button(role: .destructive) {
                        Task { await delete() }
                    } label: {
                        HStack {
                            Text(busy ? "Deleting…" : volumes ? "Delete with its volumes" : "Delete \(title)")
                            if busy { Spacer(); ProgressView().controlSize(.small) }
                        }
                    }
                    .disabled(busy)
                    #if os(macOS)
                    .buttonStyle(.borderedProminent)        // a grouped form's push button isn't red on its own
                    .tint(.red)
                    #endif
                    .accessibilityIdentifier("delete-agent-confirm")
                }
            }
            .formStyle(.grouped)
            .navigationTitle("Delete \(title)?")
            #if os(iOS)
            .navigationBarTitleDisplayMode(.inline)
            #endif
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() }.disabled(busy) }
            }
        }
        .interactiveDismissDisabled(busy)
        #if os(macOS)
        .frame(minWidth: 460, minHeight: 300)
        #endif
    }

    private func delete() async {
        busy = true
        error = nil
        defer { busy = false }
        do {
            try await model.deleteAgent(name, volumes: volumes)
            dismiss()
        } catch {
            self.error = error.localizedDescription
        }
    }
}

/// Delete agent… as a button (its settings, the context menus).
struct DeleteAgentButton: View {
    let name: String
    let actions: AgentActions

    var body: some View {
        Button(role: .destructive) { actions.delete(name) } label: {
            Label("Delete agent…", systemImage: "trash")
        }
        .accessibilityIdentifier("agent-delete")
    }
}

/// Closes an agent's screens once it's deleted (here or on another device): the stack they're in goes back to its
/// first screen. On the Mac, an agent selected in the sidebar falls back to All agents (RootView).
struct LeavesWhenDeleted: ViewModifier {
    let agent: String?
    @Environment(AppModel.self) private var model
    @Environment(\.popScreens) private var popScreens

    func body(content: Content) -> some View {
        content.onChange(of: agent.map { model.deletedAgents.contains($0) } ?? false, initial: true) { _, gone in
            if gone { popScreens?.pop(Int.max) }
        }
    }
}

extension View {
    func leavesWhenDeleted(_ agent: String?) -> some View { modifier(LeavesWhenDeleted(agent: agent)) }
}

/// Start / Restart / Stop buttons for an agent, as the agent page shows them.
struct AgentButtons: View {
    let agent: Agent
    let actions: AgentActions
    var size: ControlSize = .regular
    @Environment(AppModel.self) private var model

    var body: some View {
        let busy = actions.busy.contains(agent.name)
        HStack(spacing: 8) {
            if agent.canStart {
                Button {
                    Task { await actions.run(.start, agent.name, model: model) }
                } label: {
                    Label(agent.state == "failed" ? "Restart" : "Start", systemImage: "play.fill")
                }
                .buttonStyle(.borderedProminent)
            }
            if agent.canRestart {
                Button {
                    Task { await actions.run(.restart, agent.name, model: model) }
                } label: { Label("Restart", systemImage: "arrow.clockwise") }
                .buttonStyle(.bordered)
            }
            if agent.canStop {
                Button(role: .destructive) {
                    Task { await actions.run(.stop, agent.name, model: model) }
                } label: { Label("Stop", systemImage: "stop.circle") }
                .buttonStyle(.bordered)
                .tint(.red)
            }
            if busy { ProgressView().controlSize(.small) }
        }
        .disabled(busy)
        .controlSize(size)
    }
}

/// Write-only fields for secrets that aren't set. A value is never filled from anywhere and is dropped once
/// stored; a pasted single line loses the whitespace around it (a copied token often brings a newline along).
struct SecretsSheet: View {
    let prompt: AgentActions.SecretsPrompt
    let after: () async -> Void
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var values: [String: String] = [:]
    @State private var errors: [String: String] = [:]
    @State private var saved: Set<String> = []
    @State private var busy = false

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    ForEach(prompt.names, id: \.self) { name in
                        VStack(alignment: .leading, spacing: 6) {
                            Text(neededName(name)).font(.callout.monospaced().weight(.semibold))
                            if saved.contains(name) {
                                Label("Saved", systemImage: "checkmark.circle.fill").foregroundStyle(.green)
                            } else {
                                SecureField("Paste the value", text: binding(name))
                                    .autocorrectionDisabled()
                                    #if os(iOS)
                                    .textInputAutocapitalization(.never)
                                    #endif
                                    .accessibilityLabel("Value of \(neededName(name))")
                                    .accessibilityIdentifier("secret-\(name)")
                            }
                            if let e = errors[name] { ErrorText(text: e) }
                        }
                    }
                } header: {
                    Text(prompt.lead)
                } footer: {
                    Text("Stored encrypted on the control plane. Agents get them as environment variables.")
                }
            }
            .formStyle(.grouped)
            .navigationTitle(prompt.names.count > 1 ? "Secrets" : "Secret")
            #if os(iOS)
            .navigationBarTitleDisplayMode(.inline)
            #endif
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { values = [:]; dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button(prompt.submit) { Task { await submit() } }.disabled(busy)
                        .accessibilityIdentifier("secrets-submit")
                }
            }
        }
        #if os(macOS)
        .frame(minWidth: 420, minHeight: 300)
        #endif
    }

    private func binding(_ name: String) -> Binding<String> {
        Binding(get: { values[name] ?? "" }, set: { values[name] = $0 })
    }

    nonisolated static func cleaned(_ raw: String) -> String {
        let t = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        return t.contains("\n") ? raw : t
    }

    private func submit() async {
        errors = [:]
        let todo = prompt.names.filter { !saved.contains($0) }
        for n in todo where Self.cleaned(values[n] ?? "").isEmpty { errors[n] = "Paste its value." }
        guard errors.isEmpty else { return }
        busy = true
        defer { busy = false }
        let failed = await model.setSecrets(Dictionary(uniqueKeysWithValues: todo.map { ($0, Self.cleaned(values[$0] ?? "")) }))
        for n in todo where failed[n] == nil {
            saved.insert(n)
            values[n] = nil
        }
        errors = failed
        guard failed.isEmpty else { return }
        values = [:]
        dismiss()
        await after()
    }
}
