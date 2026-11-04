import SwiftUI

/// An agent to show (navigation value).
public struct AgentRef: Hashable, Sendable {
    public var name: String
    public init(_ name: String) { self.name = name }
}

// MARK: agent status

extension Agent {
    /// "idle", "working", "waiting on you", "runner offline"...
    var statusLabel: String {
        if runnerOffline { return "runner offline" }
        return status == .waiting ? "waiting on you" : status.rawValue.replacingOccurrences(of: "_", with: " ")
    }

    var statusColor: Color {
        if runnerOffline { return .gray }
        switch status {
        case .idle: return .green
        case .working: return .blue
        case .waiting: return .orange
        case .starting: return .teal
        case .failed: return .red
        case .stopped: return .gray
        default: return .purple
        }
    }

    /// Start when it's down (Restart when it failed); Restart and Stop when it's up; Stop while it should run.
    var canStart: Bool { !(desired == "running" && state != "failed") }
    var canStop: Bool { desired == "running" }
    var canRestart: Bool { isUp }
}

struct StatusBadge: View {
    let agent: Agent

    var body: some View {
        Chip(text: agent.statusLabel, color: agent.statusColor, dot: true)
            .accessibilityLabel("Status: \(agent.statusLabel)")
    }
}

struct Chip: View {
    let text: String
    let color: Color
    var dot = false

    var body: some View {
        HStack(spacing: 4) {
            if dot { Circle().fill(color).frame(width: 7, height: 7) }
            Text(text).lineLimit(1)
        }
        .font(.caption.weight(.semibold))
        .padding(.horizontal, 8).padding(.vertical, 3)
        .background(color.opacity(0.14), in: Capsule())
        .foregroundStyle(color)
    }
}

/// Emoji, display name, and the id (small) when it differs.
struct AgentLabel: View {
    let agent: Agent
    var font: Font = .headline

    var body: some View {
        HStack(spacing: 6) {
            if let e = agent.emoji, !e.isEmpty { Text(e) }
            Text(agent.title).font(font).lineLimit(1)
            if agent.title != agent.name {
                Text(agent.name).font(.caption.monospaced()).foregroundStyle(.secondary).lineLimit(1)
            }
        }
    }
}

/// "Next: Every day at 08:00, tomorrow 08:00" (while it should run) and "2 prompts waiting": M4's fields, as the
/// web UI's agent line says them.
struct AgentExtras: View {
    let agent: Agent

    var body: some View {
        let next = agent.desired == "running" ? agent.nextTriggerLine : nil
        let queued = agent.queuedCount
        if next != nil || queued > 0 {
            VStack(alignment: .leading, spacing: 2) {
                if let next { Label(next, systemImage: "alarm") }
                if queued > 0 {
                    Label(queued == 1 ? "1 prompt waiting" : "\(queued) prompts waiting", systemImage: "tray.full")
                }
            }
            .labelStyle(InlineLabelStyle())
            .font(.caption)
            .foregroundStyle(.secondary)
        }
    }
}

extension Agent {
    /// "Next: Every day at 08:00, tomorrow 08:00", "Next: a wake-up it set, today 15:30" (triggers.js's nextLine).
    var nextTriggerLine: String? {
        guard let t = nextTriggerInfo else { return nil }
        let what = nextTrigger?["kind"]?.string == "wakeup" ? "a wake-up it set" : t.name ?? "a schedule"
        guard let at = t.at else { return "Next: \(what)" }
        return "Next: \(what), \(whenText(at.timeIntervalSince1970))"
    }
}

/// A label as a small icon before its text (a List row otherwise lines its icons up in a wide column).
struct InlineLabelStyle: LabelStyle {
    func makeBody(configuration: Configuration) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 4) {
            configuration.icon
            configuration.title
        }
    }
}

// MARK: errors and notes

struct ErrorText: View {
    let text: String

    var body: some View {
        Label { Text(text).fixedSize(horizontal: false, vertical: true) } icon: {
            Image(systemName: "exclamationmark.triangle.fill")
        }
        .font(.callout)
        .foregroundStyle(.red)
        .textSelection(.enabled)
    }
}

/// A tinted box for a note with an optional action ("Saved changes apply after a restart." Restart).
struct NoteBox<Trailing: View>: View {
    let text: String
    var systemImage = "info.circle"
    var color: Color = .orange
    @ViewBuilder var trailing: Trailing

    var body: some View {
        HStack(alignment: .center, spacing: 10) {
            Image(systemName: systemImage).foregroundStyle(color)
            Text(text).font(.callout).frame(maxWidth: .infinity, alignment: .leading)
            trailing
        }
        .padding(10)
        .background(color.opacity(0.1), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
    }
}

extension NoteBox where Trailing == EmptyView {
    init(text: String, systemImage: String = "info.circle", color: Color = .orange) {
        self.init(text: text, systemImage: systemImage, color: color) { EmptyView() }
    }
}

/// The live socket's state, small: a dot and a word.
struct ConnectionDot: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        let (color, text): (Color, String) = switch model.connection {
        case .live: (.green, "Live")
        case .connecting: (.orange, "Connecting")
        case .offline: (.red, "Offline, reconnecting")
        case .idle: (.gray, "Not connected")
        }
        HStack(spacing: 5) {
            Circle().fill(color).frame(width: 8, height: 8)
            if model.connection != .live { Text(text).font(.caption).foregroundStyle(.secondary) }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(text)
        .help(text)
    }
}

/// A short text: the first paragraph on one line, cut at n characters (app.js's brief).
func brief(_ text: String, _ n: Int = 200) -> String {
    let first = text.trimmingCharacters(in: .whitespacesAndNewlines).components(separatedBy: "\n\n").first ?? ""
    let s = first.split(whereSeparator: \.isWhitespace).joined(separator: " ")
    return s.count > n ? String(s.prefix(n - 1)) + "…" : s
}

func clip(_ text: String, _ n: Int = 4000) -> String { text.count > n ? String(text.prefix(n)) + "\n…" : text }

extension View {
    /// An alert for an error message held in a binding (nil hides it).
    func errorAlert(_ message: Binding<String?>) -> some View {
        alert("Something went wrong", isPresented: Binding(get: { message.wrappedValue != nil },
                                                           set: { if !$0 { message.wrappedValue = nil } })) {
            Button("OK", role: .cancel) { message.wrappedValue = nil }
        } message: {
            Text(message.wrappedValue ?? "")
        }
    }

    @ViewBuilder func cardStyle(highlight: Color? = nil) -> some View {
        padding(12)
            .background(Color.card, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
            .overlay {
                RoundedRectangle(cornerRadius: 14, style: .continuous)
                    .strokeBorder(highlight ?? Color.secondary.opacity(0.18), lineWidth: highlight == nil ? 0.5 : 1.5)
            }
    }
}

/// Puts the keyboard away (iOS): after a save, so what's below it shows again.
@MainActor func endEditing() {
    #if os(iOS)
    UIApplication.shared.sendAction(#selector(UIResponder.resignFirstResponder), to: nil, from: nil, for: nil)
    #endif
}
