#if os(macOS)
import AppKit
import SwiftTerm
import Testing
@testable import ValetKit

/// The shell's view (SwiftTerm's, on the Mac where the tests run): it keeps the web UI's 5000 lines above the screen
/// (SwiftTerm's own default keeps 500).
@MainActor @Test func theTerminalKeeps5000LinesOfScrollback() {
    let v = ShellView(frame: CGRect(x: 0, y: 0, width: 600, height: 300),
                      font: NSFont.monospacedSystemFont(ofSize: 13, weight: .regular), options: TerminalSetup.options)
    let t = v.getTerminal()
    #expect(t.options.scrollback == 5000)
    let text = (1...6000).map { "line \($0)\r\n" }.joined()
    v.feed(byteArray: Array(text.utf8)[...])
    #expect(t.buffer.yDisp == 5000)         // the lines scrolled off the screen and kept
}
#endif
