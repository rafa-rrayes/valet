import CoreGraphics
import Foundation
import ImageIO
import Observation

/// Keys the live view takes (`{"type": "key"}`).
public enum LiveKey: String, CaseIterable, Sendable {
    case enter = "Enter", backspace = "Backspace", tab = "Tab", escape = "Escape"
    case left = "ArrowLeft", up = "ArrowUp", right = "ArrowRight", down = "ArrowDown"
    case delete = "Delete", home = "Home", end = "End", pageUp = "PageUp", pageDown = "PageDown"

    public var symbol: String {
        switch self {
        case .enter: return "return"
        case .backspace: return "delete.left"
        case .tab: return "arrow.right.to.line"
        case .escape: return "escape"
        case .left: return "arrow.left"
        case .right: return "arrow.right"
        case .up: return "arrow.up"
        case .down: return "arrow.down"
        case .delete: return "delete.right"
        case .home: return "arrow.up.to.line"
        case .end: return "arrow.down.to.line"
        case .pageUp: return "chevron.up.2"
        case .pageDown: return "chevron.down.2"
        }
    }

    /// What VoiceOver says for its button.
    public var label: String {
        switch self {
        case .enter: return "Return"
        case .left: return "Left arrow"
        case .up: return "Up arrow"
        case .right: return "Right arrow"
        case .down: return "Down arrow"
        case .pageUp: return "Page up"
        case .pageDown: return "Page down"
        default: return rawValue
        }
    }
}

/// What the app sends on `/ws/browser/{profile}` (docs/API.md, "The live view"). x and y are fractions of the frame
/// (0..1 from its top left).
public enum LiveInput: Equatable, Sendable {
    case tap(x: Double, y: Double)
    case mouse(action: String, x: Double, y: Double, clicks: Int?)
    case wheel(x: Double, y: Double, dy: Double)
    case text(String)
    case key(LiveKey)
    case nav(String)
    case back, reload
    case tab(String)
    case open(String?)
    /// The view's size in points (CSS pixels): the page is laid out at it while it's watched, at `scale` device
    /// pixels per point (the screen's, so the picture is sharp).
    case viewport(width: Int, height: Int, scale: Double)

    static func clamp(_ v: Double) -> Double { v.isFinite ? min(1, max(0, v)) : 0.5 }

    /// The JSON message, keys sorted.
    public var message: String {
        let v: JSONValue
        switch self {
        case .tap(let x, let y): v = ["type": "tap", "x": .number(Self.clamp(x)), "y": .number(Self.clamp(y))]
        case .mouse(let action, let x, let y, let clicks):
            var o: [String: JSONValue] = ["type": "mouse", "action": .string(action), "x": .number(Self.clamp(x)),
                                          "y": .number(Self.clamp(y))]
            if let clicks { o["clicks"] = .number(Double(clicks)) }
            v = .object(o)
        case .wheel(let x, let y, let dy):
            v = ["type": "wheel", "x": .number(Self.clamp(x)), "y": .number(Self.clamp(y)),
                 "dy": .number(min(5000, max(-5000, dy.rounded())))]
        case .text(let t): v = ["type": "text", "text": .string(t)]
        case .key(let k): v = ["type": "key", "key": .string(k.rawValue)]
        case .nav(let url): v = ["type": "nav", "url": .string(url)]
        case .back: v = ["type": "back"]
        case .reload: v = ["type": "reload"]
        case .tab(let id): v = ["type": "tab", "tab": .string(id)]
        case .open(let url): v = url.map { ["type": "open", "url": .string($0)] } ?? ["type": "open"]
        case .viewport(let w, let h, let scale):
            v = ["type": "viewport", "width": .number(Double(w)), "height": .number(Double(h)), "scale": .number(scale)]
        }
        let enc = JSONEncoder()
        enc.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        return (try? enc.encode(v)).flatMap { String(data: $0, encoding: .utf8) } ?? "{}"
    }

    /// Typed text in pieces the server takes (2000 characters at most each).
    public static func texts(_ text: String, limit: Int = 2000) -> [LiveInput] {
        guard !text.isEmpty else { return [] }
        var out: [LiveInput] = []
        var rest = Substring(text)
        while !rest.isEmpty {
            out.append(.text(String(rest.prefix(limit))))
            rest = rest.dropFirst(limit)
        }
        return out
    }
}

/// What the server says on the live view's socket (text messages).
public enum LiveMessage: Equatable, Sendable {
    case tabs([BrowserTab], current: String?)
    case page(tab: String, url: String, title: String)
    case busy(Bool, dropped: String?)
    case error(String, fatal: Bool)
    case unknown

    public static func decode(_ text: String) -> LiveMessage {
        guard let v = try? JSONDecoder().decode(JSONValue.self, from: Data(text.utf8)) else { return .unknown }
        switch v["type"]?.string {
        case "tabs":
            let tabs = (v["tabs"]?.array ?? []).compactMap { $0.decode(BrowserTab.self) }
            return .tabs(tabs, current: v["tab"]?.string)
        case "page":
            return .page(tab: v["tab"]?.string ?? "", url: v["url"]?.string ?? "", title: v["title"]?.string ?? "")
        case "busy":
            return .busy(v["busy"]?.bool ?? false, dropped: v["dropped"]?.string)
        case "error":
            return .error(v["error"]?.string ?? "Something went wrong.", fatal: v["fatal"]?.bool ?? true)
        default:
            return .unknown
        }
    }
}

/// One live view: JPEG frames in, the owner's input out. Frames bypass SwiftUI: the canvas registers `onFrame` and
/// puts each decoded image on a layer. Decoding runs off the main thread and only the newest frame is kept, so a
/// slow device drops frames rather than lagging. A dropped connection redials (0.5 s, doubling to 8 s, six times)
/// on the tab it was showing.
@MainActor @Observable
public final class LiveSession {
    public enum State: Equatable, Sendable {
        case connecting, live, reconnecting
        /// Over: why, if the server said.
        case closed(String?)
    }

    public let profile: String
    public private(set) var state: State = .connecting
    public private(set) var tabs: [BrowserTab] = []
    /// The tab it streams.
    public private(set) var tab: String?
    public private(set) var url = ""
    public private(set) var title = ""
    /// An agent's action holds the browser: input waits (5 s at most, then it's dropped).
    public private(set) var busy = false
    /// A passing note: an input that was dropped, a non-fatal error.
    public private(set) var notice: String?
    /// Pixel size of the latest frame.
    public private(set) var frameSize: CGSize = .zero
    public private(set) var frames = 0

    @ObservationIgnored public var onFrame: ((CGImage) -> Void)?
    @ObservationIgnored public private(set) var latestFrame: CGImage?
    @ObservationIgnored private let api: APIClient
    @ObservationIgnored private var task: URLSessionWebSocketTask?
    @ObservationIgnored private var receiveLoop: Task<Void, Never>?
    @ObservationIgnored private var stopped = false
    @ObservationIgnored private var attempts = 0
    @ObservationIgnored private var fatalError: String?
    @ObservationIgnored private let decoder = FrameDecoder()
    @ObservationIgnored private var noticeTimer: Task<Void, Never>?
    @ObservationIgnored private var pendingWheel: (x: Double, y: Double, dy: Double)?
    @ObservationIgnored private var wheelFlush: Task<Void, Never>?
    @ObservationIgnored private var pendingMove: (x: Double, y: Double)?
    @ObservationIgnored private var moveFlush: Task<Void, Never>?
    @ObservationIgnored private var wantedViewport: LiveViewport?
    @ObservationIgnored private var sentViewport: LiveViewport?
    @ObservationIgnored private var viewportFlush: Task<Void, Never>?
    /// What was sent, for tests (the newest last; capped).
    @ObservationIgnored private(set) var sent: [String] = []

    public init(profile: String, tab: String?, api: APIClient) {
        self.profile = profile
        self.tab = tab
        self.api = api
    }

    // MARK: connection

    public func start() {
        stopped = false
        fatalError = nil
        connect()
    }

    public func stop() {
        stopped = true
        receiveLoop?.cancel()
        task?.cancel(with: .normalClosure, reason: nil)
        task = nil
        if case .closed = state {} else { state = .closed(nil) }
    }

    public func reconnect() {
        attempts = 0
        stopped = false
        fatalError = nil
        task?.cancel(with: .normalClosure, reason: nil)
        state = .connecting
        connect()
    }

    private func connect() {
        let t = URLSession.shared.webSocketTask(with: api.liveRequest(profile: profile, tab: tab))
        t.maximumMessageSize = 16 << 20
        task = t
        t.resume()
        sentViewport = nil              // a new socket: the server knows no size yet
        sendViewport()
        receiveLoop?.cancel()
        receiveLoop = Task { [weak self] in await self?.receive(on: t) }
    }

    private func receive(on t: URLSessionWebSocketTask) async {
        while !Task.isCancelled {
            let message: URLSessionWebSocketTask.Message
            do {
                message = try await t.receive()
            } catch {
                dropped(t)
                return
            }
            guard task === t else { return }
            attempts = 0
            if state != .live { state = .live }
            switch message {
            case .data(let jpeg):
                decoder.decode(jpeg) { [weak self] frame in
                    Task { @MainActor in self?.show(frame.image) }
                }
            case .string(let text):
                handle(LiveMessage.decode(text))
            @unknown default:
                break
            }
        }
    }

    func handle(_ m: LiveMessage) {
        switch m {
        case .tabs(let list, let current):
            tabs = list
            if let current { tab = current }
            if let t = list.first(where: { $0.tab == tab }) {
                url = t.url
                title = t.title
            }
        case .page(let t, let u, let ti):
            if tab == nil || t == tab {
                url = u
                title = ti
            }
            if let i = tabs.firstIndex(where: { $0.tab == t }) {
                tabs[i].url = u
                tabs[i].title = ti
            }
        case .busy(let b, let dropped):
            busy = b
            if let dropped { say("An agent was using the browser: your \(Self.inputName(dropped)) was dropped.") }
        case .error(let e, let fatal):
            if fatal { fatalError = e } else { say(e) }
        case .unknown:
            break
        }
    }

    static func inputName(_ type: String) -> String {
        switch type {
        case "tap", "mouse": return "click"
        case "wheel": return "scroll"
        case "text": return "typing"
        case "key": return "key"
        case "nav", "open": return "navigation"
        default: return type
        }
    }

    private func say(_ text: String) {
        notice = text
        noticeTimer?.cancel()
        noticeTimer = Task { [weak self] in
            try? await Task.sleep(for: .seconds(4))
            guard !Task.isCancelled else { return }
            self?.notice = nil
        }
    }

    private func dropped(_ t: URLSessionWebSocketTask) {
        guard task === t, !stopped else { return }
        let code = t.closeCode.rawValue
        let reason = fatalError
        switch code {
        case 4401: state = .closed("You're not signed in."); return
        case 4403: state = .closed("Valet refused the live view from here."); return
        case 4404: state = .closed(reason ?? "There's no browser profile named \(profile)."); return
        case 1011: state = .closed(reason ?? "The browser didn't start."); return
        default: break
        }
        if let reason {
            state = .closed(reason)
            return
        }
        attempts += 1
        if attempts > 6 {
            state = .closed("Lost the connection to the browser.")
            return
        }
        state = .reconnecting
        let delay = min(8.0, 0.5 * pow(2, Double(attempts - 1)))
        Task { [weak self] in
            try? await Task.sleep(for: .seconds(delay))
            guard let self, !self.stopped, self.task === t else { return }
            self.connect()
        }
    }

    private func show(_ image: CGImage) {
        guard !stopped else { return }
        latestFrame = image
        frames += 1
        let size = CGSize(width: image.width, height: image.height)
        if size != frameSize { frameSize = size }
        onFrame?(image)
    }

    // MARK: input

    func send(_ input: LiveInput) {
        let text = input.message
        sent.append(text)
        if sent.count > 50 { sent.removeFirst(sent.count - 50) }
        task?.send(.string(text)) { _ in }
    }

    public func tap(x: Double, y: Double) {
        flush()
        send(.tap(x: x, y: y))
    }

    /// A button press, release or (pressed) move: drags and double clicks. Moves are sent at most 30 times a second.
    public func mouse(_ action: String, x: Double, y: Double, clicks: Int? = nil) {
        if action == "move" {
            pendingMove = (x, y)
            guard moveFlush == nil else { return }
            moveFlush = Task { [weak self] in
                try? await Task.sleep(for: .milliseconds(33))
                self?.flushMove()
            }
            return
        }
        flush()
        send(.mouse(action: action, x: x, y: y, clicks: clicks))
    }

    /// Wheel deltas (CSS pixels, positive scrolls the page down), added up and sent at most 60 times a second.
    public func scroll(x: Double, y: Double, dy: Double) {
        pendingWheel = (x, y, (pendingWheel?.dy ?? 0) + dy)
        guard wheelFlush == nil else { return }
        wheelFlush = Task { [weak self] in
            try? await Task.sleep(for: .milliseconds(16))
            self?.flushWheel()
        }
    }

    func flush() {
        flushMove()
        flushWheel()
    }

    private func flushMove() {
        moveFlush?.cancel()
        moveFlush = nil
        guard let p = pendingMove else { return }
        pendingMove = nil
        send(.mouse(action: "move", x: p.x, y: p.y, clicks: nil))
    }

    private func flushWheel() {
        wheelFlush?.cancel()
        wheelFlush = nil
        guard let p = pendingWheel else { return }
        pendingWheel = nil
        if p.dy.rounded() != 0 { send(.wheel(x: p.x, y: p.y, dy: p.dy)) }
    }

    public func type(_ text: String) {
        flush()
        for m in LiveInput.texts(text) { send(m) }
    }

    public func key(_ key: LiveKey) {
        flush()
        send(.key(key))
    }

    public func navigate(_ address: String) {
        let u = address.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !u.isEmpty else { return }
        send(.nav(u))
    }

    /// The canvas's size in points, and its screen's scale. The page is laid out at that size (CSS pixels) and scale,
    /// so frames come in the canvas's shape, fill it and are as sharp as the screen: the first size goes at once,
    /// later ones once they settle (a rotation, a window being resized), and the latest again on every new
    /// connection. A server that doesn't know the message letterboxes as before.
    public func viewport(_ size: CGSize, scale: CGFloat = 1) {
        guard let v = LiveViewport(size, scale: scale) else { return }
        wantedViewport = v
        viewportFlush?.cancel()
        viewportFlush = nil
        if sentViewport == nil {
            sendViewport()
            return
        }
        viewportFlush = Task { [weak self] in
            try? await Task.sleep(for: .milliseconds(250))
            guard !Task.isCancelled else { return }
            self?.sendViewport()
        }
    }

    private func sendViewport() {
        viewportFlush = nil
        guard let v = wantedViewport, v != sentViewport else { return }
        sentViewport = v
        send(.viewport(width: v.width, height: v.height, scale: v.scale))
    }

    public func back() { send(.back) }
    public func reload() { send(.reload) }

    public func select(tab id: String) {
        guard id != tab else { return }
        tab = id
        if let t = tabs.first(where: { $0.tab == id }) {
            url = t.url
            title = t.title
        }
        send(.tab(id))
    }

    public func openTab(_ address: String? = nil) {
        let u = address?.trimmingCharacters(in: .whitespacesAndNewlines)
        send(.open(u?.isEmpty == false ? u : nil))
    }
}

/// A view's size as the live view sends it: whole points, at least one each way; and its screen's scale, 1 to 2
/// (a 3x phone gets 2x: sharp enough, at about the pixels a 1280-wide page had, so frames stay as quick).
struct LiveViewport: Equatable, Sendable {
    let width: Int
    let height: Int
    let scale: Double

    init?(_ size: CGSize, scale: CGFloat = 1) {
        guard size.width.isFinite, size.height.isFinite, size.width >= 1, size.height >= 1 else { return nil }
        width = Int(size.width.rounded())
        height = Int(size.height.rounded())
        self.scale = scale.isFinite ? min(2, max(1, Double(scale))) : 1
    }
}

/// A decoded frame, handed from the decoding queue to the main actor.
struct DecodedFrame: @unchecked Sendable {
    let image: CGImage
}

/// Decodes JPEGs on a background queue, always skipping to the newest frame.
final class FrameDecoder: @unchecked Sendable {
    private let queue = DispatchQueue(label: "valet.live.decode", qos: .userInteractive)
    private let lock = NSLock()
    private var busy = false
    private var next: (Data, @Sendable (DecodedFrame) -> Void)?

    func decode(_ data: Data, done: @escaping @Sendable (DecodedFrame) -> Void) {
        lock.lock()
        next = (data, done)
        let start = !busy
        busy = true
        lock.unlock()
        if start { queue.async { self.drain() } }
    }

    private func drain() {
        while true {
            lock.lock()
            guard let (data, done) = next else {
                busy = false
                lock.unlock()
                return
            }
            next = nil
            lock.unlock()
            if let image = Self.image(from: data) { done(DecodedFrame(image: image)) }
        }
    }

    static func image(from data: Data) -> CGImage? {
        guard let src = CGImageSourceCreateWithData(data as CFData, nil) else { return nil }
        // Decode here rather than at the first draw on the main thread.
        return CGImageSourceCreateImageAtIndex(src, 0, [kCGImageSourceShouldCacheImmediately: true] as CFDictionary)
    }
}

/// Points in a view <-> fractions of the frame, drawn aspect-fit in the view. Once the page is laid out at the view's
/// size (LiveSession.viewport) the frame fills the view and there's nothing around it; until then (an older server,
/// the moment after a resize) the frame is centred with bars around it, where a click does nothing.
public enum LiveGeometry {
    public static func fitted(_ image: CGSize, in bounds: CGRect) -> CGRect {
        guard image.width > 0, image.height > 0, bounds.width > 0, bounds.height > 0 else { return bounds }
        let scale = min(bounds.width / image.width, bounds.height / image.height)
        let size = CGSize(width: image.width * scale, height: image.height * scale)
        return CGRect(x: bounds.midX - size.width / 2, y: bounds.midY - size.height / 2,
                      width: size.width, height: size.height)
    }

    /// nil when the point is in the letterbox around the frame.
    public static func normalized(_ p: CGPoint, image: CGSize, bounds: CGRect) -> CGPoint? {
        let r = fitted(image, in: bounds)
        guard r.width > 0, r.height > 0, (r.minX...r.maxX).contains(p.x), (r.minY...r.maxY).contains(p.y) else {
            return nil
        }
        return CGPoint(x: (p.x - r.minX) / r.width, y: (p.y - r.minY) / r.height)
    }

    /// Like `normalized`, but a point outside the frame is pulled to its edge (a drag that leaves it).
    public static func clamped(_ p: CGPoint, image: CGSize, bounds: CGRect) -> CGPoint {
        let r = fitted(image, in: bounds)
        guard r.width > 0, r.height > 0 else { return CGPoint(x: 0.5, y: 0.5) }
        return CGPoint(x: min(1, max(0, (p.x - r.minX) / r.width)), y: min(1, max(0, (p.y - r.minY) / r.height)))
    }

    /// Frame pixels per view point (about CSS pixels), to turn a finger's drag into a wheel delta.
    public static func pixelsPerPoint(image: CGSize, bounds: CGRect) -> Double {
        let r = fitted(image, in: bounds)
        return r.height > 0 ? image.height / r.height : 1
    }
}
