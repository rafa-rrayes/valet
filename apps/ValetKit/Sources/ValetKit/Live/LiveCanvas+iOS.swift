#if os(iOS)
import SwiftUI
import UIKit

/// The live view's picture and touch input on iOS: a tap is a click, a one-finger drag scrolls the page (with
/// momentum), a long press then a drag is a mouse drag (sliders, selecting text), pinch and two fingers zoom and pan
/// the picture here without touching the page. (The old app's canvas, with the drag added.)
struct LiveCanvas: UIViewRepresentable {
    let session: LiveSession

    func makeUIView(context: Context) -> LiveCanvasView {
        let v = LiveCanvasView()
        v.session = session
        session.onFrame = { [weak v] image in v?.show(image) }
        if let f = session.latestFrame { v.show(f) }
        return v
    }

    func updateUIView(_ uiView: LiveCanvasView, context: Context) {}

    static func dismantleUIView(_ uiView: LiveCanvasView, coordinator: ()) {
        uiView.session?.onFrame = nil
        uiView.stopMomentum()
    }
}

final class LiveCanvasView: UIView, UIScrollViewDelegate, UIGestureRecognizerDelegate {
    weak var session: LiveSession?
    private let scroll = UIScrollView()
    private let content = UIView()
    private var imageSize: CGSize = .zero

    private var link: CADisplayLink?
    private var velocity: CGFloat = 0          // view points per second, in the finger's direction
    private var lastPan: CGPoint = .zero
    private var momentumPoint: CGPoint = .zero

    override init(frame: CGRect) {
        super.init(frame: frame)
        backgroundColor = .black
        isAccessibilityElement = true
        accessibilityLabel = "The browser's page"
        accessibilityIdentifier = "live-canvas"
        accessibilityTraits = [.allowsDirectInteraction]
        scroll.delegate = self
        scroll.minimumZoomScale = 1
        scroll.maximumZoomScale = 5
        scroll.bouncesZoom = true
        scroll.showsHorizontalScrollIndicator = false
        scroll.showsVerticalScrollIndicator = false
        scroll.contentInsetAdjustmentBehavior = .never
        scroll.panGestureRecognizer.minimumNumberOfTouches = 2   // one finger belongs to the page
        scroll.delaysContentTouches = false
        addSubview(scroll)

        content.layer.contentsGravity = .resizeAspect
        content.layer.magnificationFilter = .linear
        content.layer.minificationFilter = .trilinear
        scroll.addSubview(content)

        let press = UILongPressGestureRecognizer(target: self, action: #selector(onPress(_:)))
        press.minimumPressDuration = 0.35
        press.delegate = self
        content.addGestureRecognizer(press)

        let tap = UITapGestureRecognizer(target: self, action: #selector(onTap(_:)))
        content.addGestureRecognizer(tap)

        let pan = UIPanGestureRecognizer(target: self, action: #selector(onPan(_:)))
        pan.maximumNumberOfTouches = 1
        pan.delegate = self
        content.addGestureRecognizer(pan)
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    override func layoutSubviews() {
        super.layoutSubviews()
        if scroll.frame != bounds {
            scroll.frame = bounds
            scroll.zoomScale = 1
            content.frame = bounds
            scroll.contentSize = bounds.size
            // The page is laid out at this size, at the screen's scale: the picture fills the view, sharp.
            session?.viewport(bounds.size, scale: traitCollection.displayScale)
        }
    }

    func show(_ image: CGImage) {
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        content.layer.contents = image
        CATransaction.commit()
        imageSize = CGSize(width: image.width, height: image.height)
        if accessibilityValue != "page shown" { accessibilityValue = "page shown" }
    }

    func viewForZooming(in scrollView: UIScrollView) -> UIView? { content }

    private func fraction(_ p: CGPoint) -> CGPoint? {
        LiveGeometry.normalized(p, image: imageSize, bounds: content.bounds)
    }

    @objc private func onTap(_ g: UITapGestureRecognizer) {
        stopMomentum()
        guard let f = fraction(g.location(in: content)) else { return }
        session?.tap(x: f.x, y: f.y)
        UIImpactFeedbackGenerator(style: .light).impactOccurred(intensity: 0.5)
    }

    /// Long press, then drag: the mouse button held from where the finger rested to where it lifts.
    @objc private func onPress(_ g: UILongPressGestureRecognizer) {
        let p = LiveGeometry.clamped(g.location(in: content), image: imageSize, bounds: content.bounds)
        switch g.state {
        case .began:
            stopMomentum()
            guard fraction(g.location(in: content)) != nil else { g.state = .cancelled; return }
            UIImpactFeedbackGenerator(style: .medium).impactOccurred()
            session?.mouse("down", x: p.x, y: p.y, clicks: 1)
        case .changed:
            session?.mouse("move", x: p.x, y: p.y)
        case .ended, .cancelled, .failed:
            session?.mouse("up", x: p.x, y: p.y, clicks: 1)
        default:
            break
        }
    }

    @objc private func onPan(_ g: UIPanGestureRecognizer) {
        // Translation in `content` coordinates already accounts for the zoom.
        let p = g.location(in: content)
        switch g.state {
        case .began:
            stopMomentum()
            lastPan = g.translation(in: content)
        case .changed:
            let t = g.translation(in: content)
            sendScroll(fingerDelta: t.y - lastPan.y, at: p)
            lastPan = t
        case .ended:
            velocity = g.velocity(in: content).y
            if abs(velocity) > 80 { startMomentum(at: p) }
        default:
            break
        }
    }

    private func sendScroll(fingerDelta: CGFloat, at p: CGPoint) {
        guard let session else { return }
        let f = fraction(p) ?? CGPoint(x: 0.5, y: 0.5)
        let scale = LiveGeometry.pixelsPerPoint(image: imageSize, bounds: content.bounds)
        session.scroll(x: f.x, y: f.y, dy: -Double(fingerDelta) * scale)
    }

    private func startMomentum(at p: CGPoint) {
        momentumPoint = p
        link?.invalidate()
        let l = CADisplayLink(target: self, selector: #selector(stepMomentum(_:)))
        l.add(to: .main, forMode: .common)
        link = l
    }

    @objc private func stepMomentum(_ l: CADisplayLink) {
        let dt = CGFloat(l.targetTimestamp - l.timestamp)
        velocity *= pow(0.12, dt)                 // about UIScrollView's normal deceleration
        if abs(velocity) < 25 { stopMomentum(); return }
        sendScroll(fingerDelta: velocity * dt, at: momentumPoint)
    }

    func stopMomentum() {
        link?.invalidate()
        link = nil
        velocity = 0
    }

    func gestureRecognizer(_ g: UIGestureRecognizer, shouldRecognizeSimultaneouslyWith other: UIGestureRecognizer) -> Bool {
        false
    }

    /// The scrolling pan waits until the long press has failed (the finger moved before 0.35 s).
    func gestureRecognizer(_ g: UIGestureRecognizer, shouldRequireFailureOf other: UIGestureRecognizer) -> Bool {
        g is UIPanGestureRecognizer && g.view === content && other is UILongPressGestureRecognizer
    }
}
#endif
