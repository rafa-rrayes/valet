import SwiftUI

/// An agent's terminal (navigation value).
public struct TerminalRef: Hashable, Sendable {
    public let agent: String
    public init(_ agent: String) { self.agent = agent }
}

/// A screen of an agent's files (navigation value): a folder or a file; nil, its first screen.
public struct FilesRef: Hashable, Sendable {
    public let agent: String
    public let path: String?
    /// The paths of the Files screens right under this one in the stack, in order: a crumb that's one of them goes
    /// back to it rather than pushing it again.
    var trail: [String] = []

    public init(_ agent: String, path: String? = nil) {
        self.agent = agent
        self.path = path
    }

    init(_ agent: String, path: String?, trail: [String]) {
        self.agent = agent
        self.path = path
        self.trail = trail
    }

    /// The trail's mark for a first screen that lists the places (no crumb is it).
    static let places = ""
}

/// Goes back `n` screens in the navigation stack the view is in: set on the stacks whose path the app keeps.
struct PopScreens {
    let pop: @MainActor (Int) -> Void
}

extension EnvironmentValues {
    @Entry var popScreens: PopScreens? = nil
}

extension Agent {
    /// It has a computer of its own: its container, or its working folder on its runner's machine (not level none).
    var hasComputer: Bool { level == "container" || level == "host" }

    /// Its computer can be reached now: it runs on a connected runner.
    var computerUp: Bool { hasComputer && state == "running" && connected }
}

/// Why the agent's computer can't be reached now, with Start (or Stop) when that's the way: what the terminal and
/// the files say while it's down.
struct ComputerNotice: View {
    let agent: Agent
    /// "open a terminal", "look".
    let action: String
    let actions: AgentActions
    @Environment(AppModel.self) private var model

    var body: some View {
        if !agent.hasComputer {
            NoteBox(text: "It has no computer (level none).", systemImage: "nosign", color: .secondary)
        } else if !agent.computerUp {
            let text = agent.desired == "running" && !agent.connected ? "Its runner is offline."
                : agent.state == "starting" ? "It's starting…"
                : agent.state == "failed" ? "It failed: start it again to \(action)."
                : "It isn't running: start it to \(action)."
            NoteBox(text: text, systemImage: "power", color: .orange) {
                if agent.state != "starting" && agent.canStart {
                    Button(agent.state == "failed" ? "Restart" : "Start") {
                        Task { await actions.run(.start, agent.name, model: model) }
                    }
                    .buttonStyle(.borderedProminent).controlSize(.small)
                    .disabled(actions.busy.contains(agent.name))
                    .accessibilityIdentifier("computer-start")
                } else if agent.state == "starting" || actions.busy.contains(agent.name) {
                    ProgressView().controlSize(.small)
                }
            }
        }
    }
}
