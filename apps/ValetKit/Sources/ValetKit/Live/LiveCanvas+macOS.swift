#if os(macOS)
import AppKit
import SwiftUI

/// The live view's picture and input on the Mac: clicks, double clicks and drags, the trackpad or wheel, and the
/// keyboard while the picture has focus (typed characters go to the page, arrows, return and delete as keys, ⌘V
/// pastes as text). (The old app's canvas, on mouse down / move / up.)
struct LiveCanvas: NSViewRepresentable {
    let session: LiveSession

    func makeNSView(context: Context) -> LiveCanvasView {
        let v = LiveCanvasView()
        v.session = session
        session.onFrame = { [weak v] image in v?.show(image) }
        if let f = session.latestFrame { v.show(f) }
        DispatchQueue.main.async { v.window?.makeFirstResponder(v) }
        return v
    }

    func updateNSView(_ nsView: LiveCanvasView, context: Context) {}

    static func dismantleNSView(_ nsView: LiveCanvasView, coordinator: ()) {
        nsView.session?.onFrame = nil
    }
}

final class LiveCanvasView: NSView {
    weak var session: LiveSession?
    private var imageSize: CGSize = .zero

    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
        layer?.backgroundColor = NSColor.black.cgColor
        layer?.contentsGravity = .resizeAspect
        layer?.magnificationFilter = .linear
        layer?.minificationFilter = .trilinear
        setAccessibilityLabel("The browser's page")
        setAccessibilityIdentifier("live-canvas")
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    override var isFlipped: Bool { true }

    /// The page is laid out at the view's size and its screen's scale, so the picture fills it, sharp, as the window
    /// is resized or moves to another screen.
    override func setFrameSize(_ newSize: NSSize) {
        super.setFrameSize(newSize)
        sayViewport()
    }

    override func viewDidChangeBackingProperties() {
        super.viewDidChangeBackingProperties()
        sayViewport()
    }

    private func sayViewport() {
        session?.viewport(bounds.size, scale: window?.backingScaleFactor ?? NSScreen.main?.backingScaleFactor ?? 1)
    }
    override var acceptsFirstResponder: Bool { true }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    func show(_ image: CGImage) {
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        layer?.contents = image
        CATransaction.commit()
        imageSize = CGSize(width: image.width, height: image.height)
        if (accessibilityValue() as? String) != "page shown" { setAccessibilityValue("page shown") }
    }

    private func point(_ e: NSEvent) -> CGPoint { convert(e.locationInWindow, from: nil) }

    override func mouseDown(with e: NSEvent) {
        window?.makeFirstResponder(self)
        guard let f = LiveGeometry.normalized(point(e), image: imageSize, bounds: bounds) else { return }
        session?.mouse("down", x: f.x, y: f.y, clicks: e.clickCount)
    }

    override func mouseDragged(with e: NSEvent) {
        let f = LiveGeometry.clamped(point(e), image: imageSize, bounds: bounds)
        session?.mouse("move", x: f.x, y: f.y)
    }

    override func mouseUp(with e: NSEvent) {
        let f = LiveGeometry.clamped(point(e), image: imageSize, bounds: bounds)
        session?.mouse("up", x: f.x, y: f.y, clicks: e.clickCount)
    }

    override func scrollWheel(with e: NSEvent) {
        let f = LiveGeometry.normalized(point(e), image: imageSize, bounds: bounds) ?? CGPoint(x: 0.5, y: 0.5)
        let scale = LiveGeometry.pixelsPerPoint(image: imageSize, bounds: bounds)
        let dy = e.hasPreciseScrollingDeltas ? -e.scrollingDeltaY * scale : -e.scrollingDeltaY * 40
        if dy != 0 { session?.scroll(x: f.x, y: f.y, dy: dy) }
    }

    override func performKeyEquivalent(with e: NSEvent) -> Bool {
        guard window?.firstResponder === self else { return super.performKeyEquivalent(with: e) }
        if e.modifierFlags.contains(.command), e.charactersIgnoringModifiers == "v" {
            if let s = NSPasteboard.general.string(forType: .string) { session?.type(s) }
            return true
        }
        return super.performKeyEquivalent(with: e)
    }

    override func keyDown(with e: NSEvent) {
        if let key = Self.special[e.keyCode] {
            session?.key(key)
            return
        }
        if e.modifierFlags.intersection([.command, .control]).isEmpty, let chars = e.characters, !chars.isEmpty,
           chars.unicodeScalars.allSatisfy({ !CharacterSet.controlCharacters.contains($0) }) {
            session?.type(chars)
            return
        }
        super.keyDown(with: e)
    }

    private static let special: [UInt16: LiveKey] = [
        36: .enter, 76: .enter, 51: .backspace, 48: .tab, 53: .escape, 123: .left, 124: .right, 125: .down,
        126: .up, 117: .delete, 115: .home, 119: .end, 116: .pageUp, 121: .pageDown,
    ]
}
#endif
