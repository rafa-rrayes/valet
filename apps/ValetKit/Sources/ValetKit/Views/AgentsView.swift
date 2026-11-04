import SwiftUI

/// Every agent with its status, what it's doing, and what it needs; Start / Stop / Restart from a swipe or the
/// context menu.
public struct AgentsView: View {
    @Environment(AppModel.self) private var model
    @State private var actions = AgentActions()
    /// The Mac's sidebar: an agent opens as its own item there.
    @Environment(\.showAgent) private var showAgent

    public init() {}

    public var body: some View {
        List {
            if model.agents.isEmpty && model.loadedOnce {
                ContentUnavailableView {
                    Label("No agents yet", systemImage: "person.2")
                } description: {
                    Text("Create one to start.")
                } actions: {
                    Button("New agent") { model.newAgent = NewAgentRequest() }
                        .buttonStyle(.borderedProminent)
                }
                .listRowBackground(Color.clear)
            }
            ForEach(model.agentList) { a in
                let row = AgentRow(agent: a, busy: actions.busy.contains(a.name),
                                   sharedNew: model.sharedCounts.agents[a.name] ?? 0)
                Group {
                    if let showAgent {
                        Button { showAgent.show(a.name) } label: { row.contentShape(Rectangle()) }
                            .buttonStyle(.plain)
                    } else {
                        NavigationLink(value: AgentRef(a.name)) { row }
                    }
                }
                .accessibilityIdentifier("agent-row:\(a.name)")
                .contextMenu { menu(a) }
                #if os(iOS)
                .swipeActions(edge: .leading) {
                    if a.canStart {
                        Button { run(.start, a) } label: { Label(a.state == "failed" ? "Restart" : "Start",
                                                                 systemImage: "play.fill") }
                            .tint(.green)
                    } else if a.canRestart {
                        Button { run(.restart, a) } label: { Label("Restart", systemImage: "arrow.clockwise") }
                            .tint(.blue)
                    }
                }
                .swipeActions(edge: .trailing) {
                    if a.canStop {
                        Button(role: .destructive) { run(.stop, a) } label: { Label("Stop", systemImage: "stop.fill") }
                    }
                }
                #endif
            }
            SharedUnassignedSection()
        }
        .animation(.default, value: model.agentList.map(\.name))
        .navigationTitle("Agents")
        .readableWidth()
        .toolbar {
            ToolbarItem(placement: .automatic) { ConnectionDot() }
            ToolbarItem(placement: .primaryAction) {
                Button { model.newAgent = NewAgentRequest() } label: { Label("New agent", systemImage: "plus") }
                    .help("New agent")
                    .accessibilityIdentifier("new-agent")
            }
        }
        .refreshable { await model.refresh() }
        .agentActions(actions)
    }

    @ViewBuilder private func menu(_ a: Agent) -> some View {
        if a.canStart {
            Button { run(.start, a) } label: { Label(a.state == "failed" ? "Restart" : "Start", systemImage: "play.fill") }
        }
        if a.canRestart {
            Button { run(.restart, a) } label: { Label("Restart", systemImage: "arrow.clockwise") }
        }
        if a.canStop {
            Button(role: .destructive) { run(.stop, a) } label: { Label("Stop", systemImage: "stop.fill") }
        }
        if !a.missingSecrets.isEmpty {
            Button { actions.offer(a.missingSecrets, for: a.name) } label: {
                Label("Set secrets", systemImage: "key")
            }
        }
        Divider()
        CopyAgentButton(name: a.name)
        DeleteAgentButton(name: a.name, actions: actions)
    }

    private func run(_ verb: AgentActions.Verb, _ a: Agent) {
        Task { await actions.run(verb, a.name, model: model) }
    }
}

/// Copy… : the New agent form, starting from a copy of this agent (its ID can't change, so this is how to get
/// another one like it).
struct CopyAgentButton: View {
    let name: String
    @Environment(AppModel.self) private var model

    var body: some View {
        Button { model.newAgent = NewAgentRequest(copyFrom: name) } label: {
            Label("Copy to a new agent…", systemImage: "plus.square.on.square")
        }
        .accessibilityIdentifier("agent-copy")
    }
}

/// An agent in the list: emoji and name (two lines at most), its status, what it's doing, then a meta line with its
/// id when the name differs ("writer-with-a-long-name · claude · host"). At accessibility sizes the status goes
/// under the name, so neither is cut to a letter.
struct AgentRow: View {
    let agent: Agent
    var busy = false
    /// What it added or changed in its Shared since you last looked.
    var sharedNew = 0
    @Environment(\.dynamicTypeSize) private var typeSize

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            if typeSize.isAccessibilitySize {
                title
                HStack(spacing: 6) {
                    StatusBadge(agent: agent).fixedSize()
                    if busy { ProgressView().controlSize(.mini) }
                }
            } else {
                HStack(alignment: .firstTextBaseline) {
                    title.layoutPriority(1)
                    Spacer(minLength: 8)
                    if busy { ProgressView().controlSize(.mini) }
                    StatusBadge(agent: agent).fixedSize()
                }
            }
            if let act = agent.activityText {
                Text(act).font(.subheadline).foregroundStyle(.secondary).lineLimit(2)
            }
            Text(metaLine).font(.caption).foregroundStyle(.secondary).lineLimit(typeSize.isAccessibilitySize ? 3 : 1)
            AgentExtras(agent: agent)
            if sharedNew > 0 {
                Label("\(sharedNew) new in Shared", systemImage: "rectangle.stack")
                    .font(.caption.weight(.semibold)).foregroundStyle(.tint)
            }
            if agent.restartNeeded {
                Label("Restart to apply saved changes", systemImage: "arrow.clockwise.circle")
                    .font(.caption).foregroundStyle(.orange)
            }
            if let needs = agent.needsLine {
                Label(needs, systemImage: "key.fill")
                    .font(.caption).foregroundStyle(.orange)
            }
            if let e = agent.definitionError ?? agent.error {
                Text(e).font(.caption).foregroundStyle(.red).lineLimit(3)
            }
        }
        .labelStyle(InlineLabelStyle())
        .padding(.vertical, 3)
    }

    private var title: some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            if let e = agent.emoji, !e.isEmpty { Text(e) }
            Text(agent.title).font(.headline).lineLimit(2)
        }
    }

    /// The id (when the title isn't it), then the harness, level, model and runner.
    private var metaLine: String {
        let meta = agent.meta.isEmpty ? "definition invalid" : agent.meta
        return agent.title == agent.name ? meta : agent.name + " · " + meta
    }
}
