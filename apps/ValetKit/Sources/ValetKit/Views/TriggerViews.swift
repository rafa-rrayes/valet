import SwiftUI

// Always-on (M4): what wakes an agent without its owner. On its page, its triggers, the wake-ups it set and the
// recent runs (triggers.js's agent section); in its settings, the triggers themselves (Settings › Triggers).

/// An agent's trigger runs, as a navigation value.
public struct TriggerRunsRef: Hashable, Sendable {
    public let agent: String
    public init(_ agent: String) { self.agent = agent }
}

// MARK: the agent's page

/// Its schedules and webhooks (when each runs next, where it goes, how the last went), the wake-ups it set (Cancel),
/// and a way to its recent runs. Hidden while it has none of them.
struct AgentTriggersSection: View {
    let agent: Agent
    let info: AgentTriggers?
    let runs: [TriggerRun]
    let reload: () async -> Void
    @Environment(AppModel.self) private var model
    @State private var cancelling: Int?
    @State private var error: String?

    var body: some View {
        if let info, !(info.triggers.isEmpty && info.wakeups.isEmpty && runs.isEmpty && info.error == nil) {
            let running = agent.desired == "running"
            Section {
                ForEach(info.triggers) { t in TriggerSummary(trigger: t, running: running) }
                ForEach(info.wakeups) { w in wakeUp(w) }
                if info.triggers.isEmpty && info.wakeups.isEmpty {
                    Text("No schedules, webhooks or wake-ups.").foregroundStyle(.secondary)
                }
                if let r = runs.first {
                    NavigationLink(value: TriggerRunsRef(agent.name)) {
                        VStack(alignment: .leading, spacing: 2) {
                            Text("Recent runs")
                            Text("Last: \(r.label.isEmpty ? r.trigger : r.label), \(r.outcomeWord) \(r.whenText)")
                                .font(.caption).foregroundStyle(.secondary).lineLimit(1)
                        }
                    }
                    .accessibilityIdentifier("trigger-runs")
                }
                if let e = info.error ?? error { ErrorText(text: e) }
            } header: {
                HStack {
                    Text("Triggers")
                    Spacer()
                    // Where they're changed: the settings form's Triggers.
                    NavigationLink(value: AgentSettingsRef(agent.name)) { Text("Edit") }
                        .textCase(nil)
                        .font(.callout)
                        .accessibilityIdentifier("triggers-edit")
                }
            } footer: {
                if running && info.triggers.contains(where: { !$0.isWebhook }) { Text(zoneNote(info.timezone)) }
            }
        }
    }

    private func wakeUp(_ w: WakeUp) -> some View {
        HStack(alignment: .top) {
            VStack(alignment: .leading, spacing: 3) {
                Label("Wake-up \(whenText(w.dueAt))", systemImage: "alarm").font(.body.weight(.medium))
                Text(w.prompt).font(.caption).foregroundStyle(.secondary).lineLimit(3)
            }
            Spacer()
            Button(role: .destructive) {
                Task { await cancel(w) }
            } label: {
                if cancelling == w.id { ProgressView().controlSize(.small) } else { Text("Cancel") }
            }
            .buttonStyle(.borderless).tint(.red)
            .disabled(cancelling != nil)
            .accessibilityIdentifier("wakeup-cancel")
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("wakeup-row")
    }

    private func cancel(_ w: WakeUp) async {
        cancelling = w.id
        error = nil
        defer { cancelling = nil }
        do {
            try await model.api.cancelWakeup(agent.name, id: w.id)
        } catch let e as APIError where e.status == 404 || e.status == 409 {
            // Fired, or cancelled elsewhere: the list says so once it's loaded again.
        } catch {
            self.error = error.localizedDescription
        }
        await reload()
    }
}

/// A trigger as the agent's page shows it.
private struct TriggerSummary: View {
    let trigger: AgentTrigger
    let running: Bool

    var body: some View {
        let t = trigger
        VStack(alignment: .leading, spacing: 3) {
            Label(t.isWebhook ? "Webhook \(t.webhook ?? "")" : t.description,
                  systemImage: t.isWebhook ? "arrow.down.forward.circle" : "clock")
                .font(.body.weight(.medium))
            Text(detail).font(.caption).foregroundStyle(.secondary)
            if let p = t.prompt, !p.isEmpty { Text(p).font(.caption).lineLimit(2) }
            if let r = t.lastRun {
                Text(r.lastLine).font(.caption).foregroundStyle(r.bad ? Color.orange : .secondary)
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier("trigger-row")
    }

    /// "next tomorrow 08:00 · in its main session", "skipped while it's stopped · a new session each time".
    private var detail: String {
        var parts: [String] = []
        if !running {
            parts.append("skipped while it's stopped")
        } else if !trigger.isWebhook, let next = trigger.next.first {
            parts.append("next " + whenText(next))
        }
        parts.append(trigger.session == "new" ? "a new session each time" : "in its main session")
        return parts.joined(separator: " · ")
    }
}

/// One run: which trigger, how it went, when; opens the session it went to.
struct TriggerRunRow: View {
    let agent: String
    let run: TriggerRun

    var body: some View {
        if let sid = run.sessionId {
            NavigationLink(value: SessionRef(agent: agent, id: sid)) { content }
        } else {
            content
        }
    }

    private var content: some View {
        let r = run
        let when = r.when.map { whenText($0) } ?? ""
        return VStack(alignment: .leading, spacing: 3) {
            HStack {
                Text(r.label.isEmpty ? r.trigger : r.label).lineLimit(1)
                Spacer()
                Chip(text: r.outcomeWord, color: Self.color(r.outcome))
            }
            Text(r.count > 1 ? "\(r.count) times, the last \(when)" : when).font(.caption).foregroundStyle(.secondary)
            if let e = r.error, !e.isEmpty { Text(e).font(.caption).foregroundStyle(.orange) }
        }
    }

    static func color(_ outcome: String) -> Color {
        switch outcome {
        case "started": return .green
        case "queued", "firing": return .blue
        case "skipped": return .gray
        default: return .orange
        }
    }
}

/// An agent's trigger runs, newest first, with Older.
struct TriggerRunsView: View {
    let agent: String
    @Environment(AppModel.self) private var model
    @State private var runs: [TriggerRun] = []
    @State private var more = false
    @State private var loaded = false
    @State private var loadingMore = false
    @State private var error: String?

    var body: some View {
        List {
            if loaded && runs.isEmpty { Text("No runs yet.").foregroundStyle(.secondary) }
            ForEach(runs) { r in TriggerRunRow(agent: agent, run: r) }
            if more {
                Button {
                    Task { await older() }
                } label: {
                    if loadingMore { ProgressView() } else { Text("Older") }
                }
                .disabled(loadingMore)
            }
            if let error { ErrorText(text: error) }
        }
        .overlay { if !loaded { ProgressView() } }
        .navigationTitle("Recent runs")
        .readableWidth()
        #if os(iOS)
        .navigationBarTitleDisplayMode(.inline)
        #endif
        .refreshable { await load() }
        .task(id: model.hellos) { await load() }
    }

    private func load() async {
        do {
            let page = try await model.api.triggerRuns(agent)
            runs = page.runs
            more = page.more
            error = nil
        } catch is CancellationError {
        } catch {
            self.error = error.localizedDescription
        }
        loaded = true
    }

    private func older() async {
        guard let last = runs.last else { return }
        loadingMore = true
        defer { loadingMore = false }
        do {
            let page = try await model.api.triggerRuns(agent, before: last.id)
            runs += page.runs.filter { r in !runs.contains { $0.id == r.id } }
            more = page.more
        } catch {
            self.error = error.localizedDescription
        }
    }
}

// MARK: the settings form

/// Settings › Triggers: each schedule (what it means, as it's typed; presets) or webhook (its address and key: Show,
/// Copy, New key), its prompt and where it goes; add and remove. Saved with the rest of the form.
struct TriggersFormSection: View {
    @Bindable var settings: AgentSettings
    @Environment(AppModel.self) private var model
    /// GET triggers for the saved definition: addresses, keys, last runs.
    @State private var info: AgentTriggers?
    @State private var infoError: String?
    @State private var previews: [String: SchedulePreviewState] = [:]
    @State private var shownKeys: Set<String> = []
    @State private var confirmKey: String?
    @State private var keyError: String?

    var body: some View {
        let s = settings
        let list = s.draft?.triggers ?? []
        Section {
            Text("What wakes it without you. Each prompt goes to its main session (one conversation it keeps) or a new "
                 + "session, and starts with a line saying where it came from. Triggers fire only while it's running.")
                .font(.callout).foregroundStyle(.secondary)
            if let zone = info?.timezone, !zone.isEmpty, list.contains(where: DefinitionDraft.isSchedule) {
                Text(zoneNote(zone)).font(.caption).foregroundStyle(.secondary)
            }
            if let infoError { ErrorText(text: "Couldn't load the webhooks' addresses: \(infoError)") }
            FieldError(text: s.errors["triggers"])
        } header: {
            Text("Triggers")
        }
        ForEach(Array(list.indices), id: \.self) { i in
            let t = list[i]
            Section {
                if DefinitionDraft.isSchedule(t) { schedule(s, i, t) } else { webhook(s, i, t) }
                TextField("Prompt", text: text(s, i, "prompt", nullIfEmpty: true),
                          prompt: Text(DefinitionDraft.isSchedule(t) ? "What it's asked each time"
                                       : "Optional: what to do with what the call sends"),
                          axis: .vertical)
                    .lineLimit(2...8)
                    .accessibilityIdentifier("trigger-\(i)-prompt")
                FieldError(text: s.errors["triggers.\(i).prompt"])
                Picker("Where it goes", selection: Binding(get: { t["session"]?.string ?? "main" }, set: { v in
                    s.draft?.setTrigger(i, "session", .string(v))
                    s.changed("triggers.\(i).session")
                })) {
                    Text("Its main session").tag("main")
                    Text("A new session each time").tag("new")
                }
                .accessibilityIdentifier("trigger-\(i)-session")
                if let r = saved(t)?.lastRun { Text(r.lastLine).font(.caption).foregroundStyle(.secondary) }
                FieldError(text: s.errors["triggers.\(i)"])
            } header: {
                HStack {
                    Text(DefinitionDraft.isSchedule(t) ? "Schedule" : "Webhook")
                    Spacer()
                    Button("Remove", role: .destructive) {
                        s.draft?.removeTrigger(i)
                        s.changed("triggers")
                    }
                    .font(.caption)
                    .textCase(nil)
                    .buttonStyle(.borderless).tint(.red)
                    .accessibilityIdentifier("trigger-\(i)-remove")
                }
            }
        }
        Section {
            Button {
                s.draft?.addSchedule()
                s.changed("triggers")
            } label: { Label("Add a schedule", systemImage: "clock") }
                .accessibilityIdentifier("trigger-add-schedule")
            Button {
                s.draft?.addWebhook()
                s.changed("triggers")
            } label: { Label("Add a webhook", systemImage: "arrow.down.forward.circle") }
                .accessibilityIdentifier("trigger-add-webhook")
        }
        .task(id: s.saved?["triggers"] ?? .null) { await loadInfo() }
        .confirmationDialog("Make a new key? The old one stops working at once, so update the sender.",
                            isPresented: Binding(get: { confirmKey != nil }, set: { if !$0 { confirmKey = nil } }),
                            titleVisibility: .visible) {
            Button("New key", role: .destructive) {
                if let hook = confirmKey { Task { await rotate(hook) } }
            }
        }
    }

    // MARK: a schedule

    @ViewBuilder private func schedule(_ s: AgentSettings, _ i: Int, _ t: JSONValue) -> some View {
        let expr = t["schedule"]?.string ?? ""
        TextField("Schedule", text: text(s, i, "schedule"), prompt: Text(verbatim: "0 8 * * *"))
            .font(.body.monospaced())
            .autocorrectionDisabled()
            #if os(iOS)
            .textInputAutocapitalization(.never)
            #endif
            .accessibilityIdentifier("trigger-\(i)-schedule")
        SchedulePreviewLine(expr: expr, previews: $previews)
        FieldError(text: s.errors["triggers.\(i).schedule"])
        Menu {
            ForEach(schedulePresets, id: \.schedule) { p in
                Button {
                    s.draft?.setTrigger(i, "schedule", .string(p.schedule))
                    s.changed("triggers.\(i).schedule")
                } label: {
                    if p.schedule == expr { Label(p.label, systemImage: "checkmark") } else { Text(p.label) }
                }
            }
        } label: {
            Label("Presets", systemImage: "list.bullet")
        }
        .accessibilityIdentifier("trigger-\(i)-presets")
    }

    // MARK: a webhook

    @ViewBuilder private func webhook(_ s: AgentSettings, _ i: Int, _ t: JSONValue) -> some View {
        TextField("Webhook name", text: Binding(get: { t["webhook"]?.string ?? "" }, set: { v in
            s.draft?.setTrigger(i, "webhook", .string(v.trimmingCharacters(in: .whitespaces)))
            s.changed("triggers.\(i).webhook")
        }), prompt: Text(verbatim: "github-push"))
            .font(.body.monospaced())
            .autocorrectionDisabled()
            #if os(iOS)
            .textInputAutocapitalization(.never)
            #endif
            .accessibilityIdentifier("trigger-\(i)-webhook")
        FieldError(text: s.errors["triggers.\(i).webhook"])
        if let hook = t["webhook"]?.string, let saved = saved(t) {
            if let key = saved.key, saved.keyError == nil {
                let shown = shownKeys.contains(hook)
                VStack(alignment: .leading, spacing: 4) {
                    Text("Address").font(.caption).foregroundStyle(.secondary)
                    HStack {
                        Text(verbatim: saved.url ?? "").font(.callout.monospaced()).textSelection(.enabled)
                            .accessibilityIdentifier("trigger-\(i)-url")
                        Spacer()
                        Button("Copy") { Clipboard.copy(saved.url ?? "") }.buttonStyle(.borderless)
                    }
                }
                VStack(alignment: .leading, spacing: 4) {
                    Text("Key").font(.caption).foregroundStyle(.secondary)
                    Text(verbatim: shown ? key : String(key.prefix(8)) + "••••••••")
                        .font(.callout.monospaced()).textSelection(.enabled)
                        .accessibilityIdentifier("trigger-\(i)-key")
                    HStack(spacing: 16) {
                        Button(shown ? "Hide" : "Show") {
                            if shown { shownKeys.remove(hook) } else { shownKeys.insert(hook) }
                        }
                        Button("Copy") { Clipboard.copy(key) }
                        Spacer()
                        Button("New key", role: .destructive) { confirmKey = hook }
                            .accessibilityIdentifier("trigger-\(i)-new-key")
                    }
                    .buttonStyle(.borderless).tint(.red)
                    if let keyError { ErrorText(text: keyError) }
                }
                Text("Send it as Authorization: Bearer <key>, as ?key=<key> in the address, or as the secret of a GitHub "
                     + "webhook (it checks X-Hub-Signature-256). What the call sends comes with the prompt, marked as "
                     + "data from outside.")
                    .font(.caption).foregroundStyle(.secondary)
            } else {
                ErrorText(text: saved.keyError ?? "No key.")
            }
        } else {
            Text("Save to get its address and key.").font(.caption).foregroundStyle(.secondary)
        }
    }

    // MARK: data

    /// The saved trigger this one is (by webhook name, or schedule expression), if it's saved.
    private func saved(_ t: JSONValue) -> AgentTrigger? {
        guard let info else { return nil }
        if let hook = t["webhook"]?.string { return info.triggers.first { $0.webhook == hook } }
        return info.triggers.first { $0.schedule == t["schedule"]?.string }
    }

    private func text(_ s: AgentSettings, _ i: Int, _ key: String, nullIfEmpty: Bool = false) -> Binding<String> {
        Binding(get: { (s.draft?.triggers ?? [])[safe: i]?[key]?.string ?? "" }, set: { v in
            s.draft?.setTrigger(i, key, nullIfEmpty && v.isEmpty ? nil : .string(v))
            s.changed("triggers.\(i).\(key)")
        })
    }

    private func loadInfo() async {
        do {
            let i = try await model.api.triggers(settings.name)
            info = i
            infoError = nil
            for t in i.triggers {
                if let e = t.schedule { previews[e] = .ready(SchedulePreview(description: t.description, next: t.next)) }
            }
        } catch is CancellationError {
        } catch {
            infoError = error.localizedDescription
        }
    }

    private func rotate(_ hook: String) async {
        keyError = nil
        do {
            let k = try await model.api.newWebhookKey(settings.name, hook: hook)
            if let i = info?.triggers.firstIndex(where: { $0.webhook == hook }) {
                info?.triggers[i].key = k.key
                info?.triggers[i].keyCreatedAt = k.createdAt
            }
            shownKeys.insert(hook)
        } catch {
            keyError = error.localizedDescription
        }
    }
}

enum SchedulePreviewState: Equatable {
    case ready(SchedulePreview)
    case invalid(String)
}

/// What a schedule means, a moment after it's typed (GET /api/triggers/preview): "Weekdays at 09:00 · next Mon
/// 09:00, Tue 09:00", or why it isn't one.
private struct SchedulePreviewLine: View {
    let expr: String
    @Binding var previews: [String: SchedulePreviewState]
    @Environment(AppModel.self) private var model

    var body: some View {
        Group {
            switch previews[expr] {
            case .ready(let p):
                let next = p.next.isEmpty ? "" : " · next " + p.next.map { whenText($0) }.joined(separator: ", ")
                Text("\(Text(p.description).bold())\(next)").font(.caption)
            case .invalid(let message):
                ErrorText(text: message)
            case nil:
                Text(expr.trimmingCharacters(in: .whitespaces).isEmpty ? "" : "…").font(.caption).foregroundStyle(.secondary)
            }
        }
        .accessibilityIdentifier("schedule-preview")
        .task(id: expr) {
            let e = expr
            guard !e.trimmingCharacters(in: .whitespaces).isEmpty, previews[e] == nil else { return }
            try? await Task.sleep(for: .milliseconds(300))
            guard !Task.isCancelled else { return }
            do {
                previews[e] = .ready(try await model.api.schedulePreview(e))
            } catch let err as APIError where err.status == 422 {
                previews[e] = .invalid(err.localizedDescription)
            } catch {}
        }
    }
}
