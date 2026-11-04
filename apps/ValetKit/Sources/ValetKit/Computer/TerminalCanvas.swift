import SwiftTerm
import SwiftUI

#if os(iOS)
import UIKit
typealias PlatformColor = UIColor
#else
import AppKit
typealias PlatformColor = NSColor
#endif

// SwiftTerm's view (UIKit on iOS, AppKit on macOS) for one TerminalSession: the session's output is fed to it, its
// keystrokes and its size in cells go to the session. The size is reported after every layout, so turning the phone,
// the keyboard coming up, a split view or a window resize all resize the shell. SwiftTerm is Swift 5 code that calls
// its delegate on the main thread: the conformance is @preconcurrency.

/// What a shell's view starts with: the web UI's scrollback (terminal.js keeps 5000 lines above the screen; SwiftTerm's
/// own default is 500).
enum TerminalSetup {
    static let scrollback = 5000
    static var options: TerminalOptions { TerminalOptions(scrollback: scrollback) }
}

/// The web UI's terminal colours (terminal.js's TERM_THEMES).
enum TerminalTheme {
    static func background(dark: Bool) -> PlatformColor { dark ? rgb(0x0B0D11) : rgb(0xFFFFFF) }
    static func foreground(dark: Bool) -> PlatformColor { dark ? rgb(0xE8EAEE) : rgb(0x14161A) }
    static func caret(dark: Bool) -> PlatformColor { dark ? rgb(0x5CC994) : rgb(0x1F8A5B) }
    static func selection(dark: Bool) -> PlatformColor { dark ? rgb(0x2A4A3A) : rgb(0xCFE9DC) }

    static func rgb(_ v: Int) -> PlatformColor {
        PlatformColor(red: CGFloat((v >> 16) & 0xFF) / 255, green: CGFloat((v >> 8) & 0xFF) / 255,
                      blue: CGFloat(v & 0xFF) / 255, alpha: 1)
    }
}

/// Where SwiftTerm's delegate calls go: keystrokes and sizes to the session, http(s) links to the system.
@MainActor
final class TerminalCoordinator: NSObject, @preconcurrency TerminalViewDelegate {
    let session: TerminalSession
    var openURL: OpenURLAction?
    var dark: Bool?
    /// Focus is taken when the tab becomes the one shown with its shell running, not at every update (the owner
    /// may have put the keyboard away).
    private var wasFocusable = false

    init(session: TerminalSession) { self.session = session }

    func send(source: SwiftTerm.TerminalView, data: ArraySlice<UInt8>) { session.send(Data(data)) }

    func sizeChanged(source: SwiftTerm.TerminalView, newCols: Int, newRows: Int) {
        session.resize(cols: newCols, rows: newRows)
    }

    func setTerminalTitle(source: SwiftTerm.TerminalView, title: String) {}
    func hostCurrentDirectoryUpdate(source: SwiftTerm.TerminalView, directory: String?) {}
    func scrolled(source: SwiftTerm.TerminalView, position: Double) {}
    func rangeChanged(source: SwiftTerm.TerminalView, startY: Int, endY: Int) {}

    /// Only http(s): what the shell prints is untrusted, like an agent's Markdown.
    func requestOpenLink(source: SwiftTerm.TerminalView, link: String, params: [String: String]) {
        guard let url = URL(string: link), ["http", "https"].contains(url.scheme?.lowercased() ?? "") else { return }
        openURL?(url)
    }

    /// Hooks a view to the session: its output in, its size out (now, and after every layout).
    func attach(_ view: ShellView) {
        view.terminalDelegate = self
        // The Tango colours, xterm.js's (and so the web UI's) default ANSI palette: readable on white and on black.
        view.installColors(SwiftTerm.Color.paleColors)
        view.onLayout = { [weak self] cols, rows in self?.session.resize(cols: cols, rows: rows) }
        session.onOutput = { [weak view] data in
            guard let view else { return }
            let bytes = [UInt8](data)
            view.feed(byteArray: bytes[...])
        }
    }

    /// True when the view should take the keyboard now: it just became the shown tab with a running shell.
    func shouldFocus(active: Bool, ready: Bool) -> Bool {
        let now = active && ready
        defer { wasFocusable = now }
        return now && !wasFocusable
    }

    func theme(_ view: ShellView, dark: Bool) {
        guard self.dark != dark else { return }
        self.dark = dark
        view.nativeForegroundColor = TerminalTheme.foreground(dark: dark)
        view.nativeBackgroundColor = TerminalTheme.background(dark: dark)
        view.caretColor = TerminalTheme.caret(dark: dark)
        view.selectedTextBackgroundColor = TerminalTheme.selection(dark: dark)
        #if os(iOS)
        view.backgroundColor = TerminalTheme.background(dark: dark)
        view.layer.backgroundColor = TerminalTheme.background(dark: dark).cgColor
        view.indicatorStyle = dark ? .white : .black
        #else
        view.layer?.backgroundColor = TerminalTheme.background(dark: dark).cgColor
        #endif
    }
}

#if os(iOS)
/// SwiftTerm's view, saying its size after each layout, with its screen's text as its accessibility value. Its input
/// accessory (SwiftTerm's own) is the row of keys a phone keyboard lacks: Esc, Ctrl, Tab, the arrows (they repeat
/// while held), ~ | / -, and the keyboard and touch switches.
final class ShellView: SwiftTerm.TerminalView {
    var onLayout: ((Int, Int) -> Void)?

    override func layoutSubviews() {
        super.layoutSubviews()
        guard bounds.width > 0, bounds.height > 0 else { return }
        let t = getTerminal()
        onLayout?(t.cols, t.rows)
    }

    override var accessibilityValue: String? {
        get { screenText }
        set {}
    }

    /// What the screen shows, line by line (trailing blanks dropped).
    var screenText: String {
        let t = getTerminal()
        var lines = (0..<t.rows).map { t.getLine(row: $0)?.translateToString(trimRight: true) ?? "" }
        while lines.last?.isEmpty == true { lines.removeLast() }
        return lines.joined(separator: "\n")
    }
}

struct TerminalCanvas: UIViewRepresentable {
    let session: TerminalSession
    /// The tab shown: it takes the keyboard once its shell runs (`ready`).
    let active: Bool
    let ready: Bool
    @Environment(\.colorScheme) private var scheme
    @Environment(\.openURL) private var openURL

    func makeCoordinator() -> TerminalCoordinator { TerminalCoordinator(session: session) }

    func makeUIView(context: Context) -> ShellView {
        let size: CGFloat = UIDevice.current.userInterfaceIdiom == .phone ? 12 : 13
        let v = ShellView(frame: .zero, font: UIFont.monospacedSystemFont(ofSize: size, weight: .regular),
                          options: TerminalSetup.options)
        v.optionAsMetaKey = true
        v.autocorrectionType = .no
        v.autocapitalizationType = .none
        v.smartQuotesType = .no
        v.smartDashesType = .no
        v.keyboardAppearance = scheme == .dark ? .dark : .light
        v.accessibilityIdentifier = "terminal-\(session.id)"
        v.accessibilityLabel = "Shell \(session.id)"
        context.coordinator.attach(v)
        context.coordinator.theme(v, dark: scheme == .dark)
        return v
    }

    func updateUIView(_ v: ShellView, context: Context) {
        context.coordinator.openURL = openURL
        context.coordinator.theme(v, dark: scheme == .dark)
        v.keyboardAppearance = scheme == .dark ? .dark : .light
        v.isUserInteractionEnabled = active
        v.accessibilityElementsHidden = !active
        if context.coordinator.shouldFocus(active: active, ready: ready) {
            DispatchQueue.main.async { _ = v.becomeFirstResponder() }
        } else if !active, v.isFirstResponder {
            _ = v.resignFirstResponder()
        }
    }

    static func dismantleUIView(_ v: ShellView, coordinator: TerminalCoordinator) {
        v.onLayout = nil
        coordinator.session.onOutput = nil
    }
}
#else
/// SwiftTerm's view, saying its size whenever its frame changes.
final class ShellView: SwiftTerm.TerminalView {
    var onLayout: ((Int, Int) -> Void)?

    override func setFrameSize(_ newSize: NSSize) {
        super.setFrameSize(newSize)
        guard newSize.width > 0, newSize.height > 0 else { return }
        let t = getTerminal()
        onLayout?(t.cols, t.rows)
    }
}

struct TerminalCanvas: NSViewRepresentable {
    let session: TerminalSession
    /// The tab shown: it takes the keyboard once its shell runs (`ready`).
    let active: Bool
    let ready: Bool
    @Environment(\.colorScheme) private var scheme
    @Environment(\.openURL) private var openURL

    func makeCoordinator() -> TerminalCoordinator { TerminalCoordinator(session: session) }

    func makeNSView(context: Context) -> ShellView {
        let v = ShellView(frame: .zero, font: NSFont.monospacedSystemFont(ofSize: 13, weight: .regular),
                          options: TerminalSetup.options)
        v.optionAsMetaKey = true
        v.setAccessibilityIdentifier("terminal-\(session.id)")
        context.coordinator.attach(v)
        context.coordinator.theme(v, dark: scheme == .dark)
        return v
    }

    func updateNSView(_ v: ShellView, context: Context) {
        context.coordinator.openURL = openURL
        context.coordinator.theme(v, dark: scheme == .dark)
        v.isHidden = !active
        if context.coordinator.shouldFocus(active: active, ready: ready) {
            DispatchQueue.main.async { v.window?.makeFirstResponder(v) }
        }
    }

    static func dismantleNSView(_ v: ShellView, coordinator: TerminalCoordinator) {
        v.onLayout = nil
        coordinator.session.onOutput = nil
    }
}
#endif
