import SwiftUI

/// An agent's terminal: shells in its computer (its own container, as its user, in /work; or its working folder on a
/// host agent's runner), each on its own socket, as tabs. One opens when the screen does (once the agent runs); New
/// terminal opens another, up to 8. Closing a tab ends its shell, and leaving the screen ends them all (switching to
/// another of the app's tabs doesn't). The web UI's terminal.js.
public struct TerminalScreen: View {
    let agent: String
    @Environment(AppModel.self) private var model
    @Environment(\.isPresented) private var isPresented
    @State private var tabs: TerminalTabs?
    @State private var actions = AgentActions()

    public init(agent: String) { self.agent = agent }

    public var body: some View {
        VStack(spacing: 0) {
            if let a = model.agents[agent] {
                header(a)
            }
            if let tabs {
                if !tabs.shells.isEmpty { tabBar(tabs) }
                screens(tabs)
                status(tabs)
            } else {
                Spacer()
            }
        }
        .background(Color.screen)
        .navigationTitle("Terminal")
        #if os(iOS)
        .navigationBarTitleDisplayMode(.inline)
        #endif
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button {
                    tabs?.open()
                } label: {
                    Label("New terminal", systemImage: "plus")
                }
                .disabled(!up || !(tabs?.canOpen ?? false))
                .help("New terminal")
                .accessibilityIdentifier("terminal-new")
            }
        }
        .onAppear {
            if tabs == nil { tabs = TerminalTabs(agent: agent, api: model.api) }
            openFirst()
        }
        // Popped (or, on the Mac, another sidebar item picked): the shells end. Another app tab keeps them.
        .onChange(of: isPresented) { _, presented in if !presented { tabs?.closeAll() } }
        .onDisappear { if !isPresented { tabs?.closeAll() } }
        .onChange(of: up) { _, now in if now { openFirst() } }
        .agentActions(actions)
    }

    private var up: Bool { model.agents[agent]?.computerUp ?? false }

    /// A first shell, once the agent's computer is there and none is open.
    private func openFirst() {
        guard up, let tabs, tabs.shells.isEmpty else { return }
        tabs.open()
    }

    @ViewBuilder private func header(_ a: Agent) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(a.level == "container" ? "A shell in its container, as its user, in /work."
                 : "A shell on \(a.runner ?? "its runner")'s machine, as its user, in its working folder.")
                .font(.caption)
                .foregroundStyle(.secondary)
                .accessibilityIdentifier("terminal-where")
            ComputerNotice(agent: a, action: "open a terminal", actions: actions)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
    }

    private func tabBar(_ tabs: TerminalTabs) -> some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 6) {
                ForEach(tabs.shells) { s in
                    let on = s.id == tabs.current
                    HStack(spacing: 2) {
                        Button {
                            tabs.current = s.id
                        } label: {
                            HStack(spacing: 5) {
                                Circle().fill(dot(s)).frame(width: 6, height: 6)
                                Text("Shell \(s.id)").font(.callout.weight(on ? .semibold : .regular))
                            }
                            .padding(.leading, 10).padding(.vertical, 5)
                        }
                        .buttonStyle(.plain)
                        .accessibilityAddTraits(on ? .isSelected : [])
                        .accessibilityIdentifier("terminal-tab-\(s.id)")
                        Button {
                            tabs.close(s)
                        } label: {
                            Image(systemName: "xmark").font(.caption2.weight(.bold))
                                .frame(width: 22, height: 22)
                                .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .foregroundStyle(.secondary)
                        .accessibilityLabel("Close shell \(s.id)")
                        .accessibilityIdentifier("terminal-close-\(s.id)")
                        .padding(.trailing, 4)
                    }
                    .background(on ? Color.card : Color.clear, in: Capsule())
                    .overlay(Capsule().strokeBorder(on ? Color.secondary.opacity(0.3) : Color.clear))
                    .foregroundStyle(s.isEnded ? .secondary : .primary)
                }
            }
            .padding(.horizontal, 12)
            .padding(.bottom, 6)
        }
    }

    private func dot(_ s: TerminalSession) -> Color {
        switch s.state {
        case .open: return .green
        case .waiting, .connecting: return .orange
        case .ended: return .gray
        }
    }

    /// Every shell's view, the shown one on top: the others keep their screens (and their sizes) under it.
    @ViewBuilder private func screens(_ tabs: TerminalTabs) -> some View {
        ZStack {
            Rectangle().fill(Color.terminalBackground)
            ForEach(tabs.shells) { s in
                let on = s.id == tabs.current
                TerminalCanvas(session: s, active: on, ready: s.state == .open)
                    .opacity(on ? 1 : 0)
                    .allowsHitTesting(on)
                    .accessibilityHidden(!on)
            }
            if tabs.shells.isEmpty && up {
                VStack(spacing: 10) {
                    Text("No shell open.").foregroundStyle(.secondary)
                    Button("New terminal") { tabs.open() }.buttonStyle(.borderedProminent)
                        .accessibilityIdentifier("terminal-open")
                }
            }
        }
        .padding(.horizontal, 4)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    @ViewBuilder private func status(_ tabs: TerminalTabs) -> some View {
        if let s = tabs.shown, !s.statusText.isEmpty {
            HStack(spacing: 8) {
                if !s.isEnded { ProgressView().controlSize(.small) }
                Text(s.statusText).font(.callout).foregroundStyle(.secondary)
                    .accessibilityIdentifier("terminal-status")
                Spacer()
                if s.isEnded && up && tabs.canOpen {
                    Button("New terminal") { tabs.open() }.buttonStyle(.bordered).controlSize(.small)
                }
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
            .background(.bar)
        }
    }
}

extension Color {
    /// Behind the terminal: the web UI's terminal background, white or near black.
    static let terminalBackground: Color = {
        #if os(iOS)
        Color(UIColor { $0.userInterfaceStyle == .dark ? TerminalTheme.background(dark: true)
            : TerminalTheme.background(dark: false) })
        #else
        Color(NSColor(name: nil) { $0.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
            ? TerminalTheme.background(dark: true) : TerminalTheme.background(dark: false) })
        #endif
    }()
}
