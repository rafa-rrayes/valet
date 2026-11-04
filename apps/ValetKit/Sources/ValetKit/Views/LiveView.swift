import SwiftUI

/// A browser profile's live view: watch a tab, take it over (tap, drag, scroll, type, keys, navigate, back, reload,
/// switch or open tabs). Full screen on iOS, a sheet the size of a window on the Mac. Opened from a take-over ask, its
/// Done and I can't sit under the page, and answering one closes the view.
public struct LiveView: View {
    let target: LiveTarget
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var session: LiveSession?
    @State private var address = ""
    @State private var typed = ""
    @FocusState private var addressFocused: Bool
    @FocusState private var typingFocused: Bool
    #if os(iOS)
    @Environment(\.verticalSizeClass) private var verticalSizeClass
    #endif

    public init(target: LiveTarget) { self.target = target }

    public var body: some View {
        VStack(spacing: 0) {
            if let session {
                header(session)
                Divider()
                ZStack {
                    Color.black
                    LiveCanvas(session: session)
                    overlays(session)
                }
                .clipped()
                handBack
                Divider()
                inputBar(session)
            } else {
                ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .background(Color.screen)
        .onAppear {
            if session == nil { session = LiveSession(profile: target.profile, tab: target.tab, api: model.api) }
            session?.start()
        }
        .onDisappear { session?.stop() }
        // The page's address, unless you're typing one. An empty field always takes it: on the Mac the sheet's field
        // counts as focused as it opens, and the first address was never shown.
        .onChange(of: session?.url ?? "") { _, url in if !addressFocused || address.isEmpty { address = url } }
        .onChange(of: answered) { _, done in
            // The take-over was answered (here, or elsewhere): the view's job is done.
            if done { DispatchQueue.main.asyncAfter(deadline: .now() + 0.8) { close() } }
        }
        #if os(macOS)
        // Fits in the smallest main window (820 x 540) with room around it; the page is laid out at whatever size
        // the picture gets.
        .frame(minWidth: 700, idealWidth: 1000, minHeight: 480, idealHeight: 680)
        #endif
    }

    private func close() {
        session?.stop()
        dismiss()
    }

    // MARK: the bars

    /// A phone on its side: the bars take one row each, so the page keeps as much height as it can.
    private var compact: Bool {
        #if os(iOS)
        verticalSizeClass == .compact
        #else
        false
        #endif
    }

    @ViewBuilder private func header(_ s: LiveSession) -> some View {
        Group {
            if compact {
                HStack(spacing: 10) {
                    closeButton
                    navigation(s)
                    stateDot(s)
                    tabsMenu(s)
                }
            } else {
                VStack(spacing: 8) {
                    HStack(spacing: 10) {
                        closeButton
                        VStack(alignment: .leading, spacing: 1) {
                            Text(pageTitle(s)).font(.headline).lineLimit(1)
                            Text(target.profile).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                        }
                        Spacer(minLength: 4)
                        stateDot(s)
                        tabsMenu(s)
                    }
                    navigation(s)
                }
            }
        }
        .buttonStyle(.borderless)
        .padding(.horizontal, 12)
        .padding(.vertical, compact ? 5 : 8)
    }

    private var closeButton: some View {
        Button(action: close) { Image(systemName: "xmark.circle.fill").font(.title2) }
            .foregroundStyle(.secondary)
            .keyboardShortcut("w", modifiers: .command)     // Escape belongs to the page
            .accessibilityLabel("Close")
            .accessibilityIdentifier("live-close")
    }

    /// Back, Reload and the address.
    private func navigation(_ s: LiveSession) -> some View {
        HStack(spacing: 12) {
            Button { s.back() } label: { Image(systemName: "chevron.backward") }
                .accessibilityLabel("Back")
            Button { s.reload() } label: { Image(systemName: "arrow.clockwise") }
                .accessibilityLabel("Reload")
            TextField("Address", text: $address)
                .textFieldStyle(.roundedBorder)
                .autocorrectionDisabled()
                #if os(iOS)
                .keyboardType(.URL)
                .textInputAutocapitalization(.never)
                .submitLabel(.go)
                #endif
                .focused($addressFocused)
                .onSubmit {
                    s.navigate(address)
                    addressFocused = false
                }
                .accessibilityIdentifier("live-address")
        }
    }

    private func pageTitle(_ s: LiveSession) -> String {
        if !s.title.trimmingCharacters(in: .whitespaces).isEmpty { return s.title }
        if let host = URL(string: s.url)?.host, !host.isEmpty { return host }
        return "Live view"
    }

    private func stateDot(_ s: LiveSession) -> some View {
        let (color, label): (Color, String) = switch s.state {
        case .live: s.busy ? (.orange, "An agent is using it") : (.green, "Live")
        case .connecting, .reconnecting: (.orange, "Connecting")
        case .closed: (.red, "Disconnected")
        }
        return Circle().fill(color).frame(width: 9, height: 9)
            .accessibilityElement()
            .accessibilityLabel(label)
            .accessibilityIdentifier("live-state")
            .help(label)
    }

    private func tabsMenu(_ s: LiveSession) -> some View {
        Menu {
            ForEach(s.tabs) { t in
                Button {
                    s.select(tab: t.tab)
                } label: {
                    if t.tab == s.tab { Label(t.label, systemImage: "checkmark") } else { Text(t.label) }
                }
            }
            Divider()
            Button { s.openTab() } label: { Label("New tab", systemImage: "plus") }
        } label: {
            HStack(spacing: 3) {
                Image(systemName: "square.on.square")
                if s.tabs.count > 1 { Text("\(s.tabs.count)").font(.caption.monospacedDigit()) }
            }
        }
        .accessibilityLabel("Tabs")
        .accessibilityIdentifier("live-tabs")
        #if os(macOS)
        .menuStyle(.borderlessButton)
        .fixedSize()
        #endif
    }

    @ViewBuilder private func overlays(_ s: LiveSession) -> some View {
        VStack {
            if s.busy, s.state == .live {
                Label("An agent is using the browser: your input waits for it.", systemImage: "hourglass")
                    .font(.callout)
                    .padding(.horizontal, 12).padding(.vertical, 7)
                    .background(.ultraThinMaterial, in: Capsule())
                    .padding(.top, 10)
                    .accessibilityIdentifier("live-busy")
            }
            Spacer()
            if let n = s.notice {
                Text(n).font(.callout)
                    .padding(.horizontal, 12).padding(.vertical, 7)
                    .background(.ultraThinMaterial, in: Capsule())
                    .padding(.bottom, 10)
                    .transition(.opacity)
            }
        }
        .allowsHitTesting(false)
        .animation(.default, value: s.busy)
        .animation(.default, value: s.notice)
        switch s.state {
        case .connecting, .reconnecting:
            if s.frames == 0 || s.state == .reconnecting {
                Label(s.state == .connecting ? "Starting the browser…" : "Reconnecting…",
                      systemImage: "antenna.radiowaves.left.and.right")
                    .padding(10)
                    .background(.ultraThinMaterial, in: Capsule())
            }
        case .closed(let msg):
            VStack(spacing: 12) {
                Text(msg ?? "Disconnected.").multilineTextAlignment(.center)
                Button("Reconnect") { s.reconnect() }.buttonStyle(.borderedProminent)
            }
            .padding()
            .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 14))
            .padding()
        case .live:
            if s.frames == 0 {
                ProgressView().tint(.white)
            }
        }
    }

    // MARK: a take-over's answer

    private var item: InboxItem? { target.itemID.flatMap { model.inbox[$0] } }

    private var answered: Bool { item.map { !$0.isPending } ?? false }

    @ViewBuilder private var handBack: some View {
        if let item {
            VStack(alignment: .leading, spacing: 8) {
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: "person.fill.questionmark").foregroundStyle(.pink)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(item.isPending ? "\(model.title(of: item.agent)) is waiting for you"
                             : "Answered: \(model.title(of: item.agent)) goes on")
                            .font(.subheadline.weight(.semibold))
                        if let r = item.browser?.reason {
                            Text(verbatim: r).font(.caption).foregroundStyle(.secondary).lineLimit(3)
                        }
                    }
                    Spacer(minLength: 0)
                }
                if item.isPending {
                    AskButtons(itemID: item.id, options: item.shownOptions, compact: true)
                }
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.pink.opacity(0.08))
        }
    }

    // MARK: typing

    @ViewBuilder private func inputBar(_ s: LiveSession) -> some View {
        Group {
            if compact {
                HStack(spacing: 8) {
                    typing(s).frame(maxWidth: 300)
                    keys(s)
                }
            } else {
                VStack(spacing: 8) {
                    typing(s)
                    keys(s)
                }
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, compact ? 5 : 8)
        .buttonStyle(.borderless)
    }

    private func typing(_ s: LiveSession) -> some View {
        HStack(spacing: 8) {
            TextField("Type into the page", text: $typed)
                .textFieldStyle(.roundedBorder)
                .autocorrectionDisabled()
                #if os(iOS)
                .textInputAutocapitalization(.never)
                .submitLabel(.send)
                #endif
                .focused($typingFocused)
                .onSubmit { sendTyped(s) }
                .accessibilityIdentifier("live-type")
            Button { sendTyped(s) } label: { Image(systemName: "paperplane.fill") }
                .disabled(typed.isEmpty)
                .accessibilityLabel("Type it")
                .accessibilityIdentifier("live-send")
        }
    }

    private func keys(_ s: LiveSession) -> some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 6) {
                ForEach([LiveKey.enter, .tab, .backspace, .escape, .left, .right, .up, .down, .pageUp, .pageDown],
                        id: \.self) { k in
                    Button { s.key(k) } label: {
                        Image(systemName: k.symbol).frame(minWidth: 28, minHeight: 22)
                    }
                    .buttonStyle(.bordered)
                    .accessibilityLabel(k.label)
                }
                #if os(iOS)
                Button {
                    if let text = UIPasteboard.general.string { s.type(text) }
                } label: { Label("Paste", systemImage: "doc.on.clipboard") }
                    .buttonStyle(.bordered)
                #endif
            }
        }
    }

    private func sendTyped(_ s: LiveSession) {
        guard !typed.isEmpty else { return }
        s.type(typed)
        typed = ""
        #if os(iOS)
        typingFocused = true
        #endif
    }
}
