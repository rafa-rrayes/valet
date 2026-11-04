import SwiftUI

#if os(iOS)
import UIKit
typealias PlatformImage = UIImage
extension Image {
    init(platform: PlatformImage) { self.init(uiImage: platform) }
}
#else
import AppKit
typealias PlatformImage = NSImage
extension Image {
    init(platform: PlatformImage) { self.init(nsImage: platform) }
}
#endif

/// Screenshots behind the owner's auth (`GET /api/browser/shots/<name>`), fetched with the token and kept in memory
/// (the newest few): an ask's screenshot shows in the inbox, the conversation and the menu bar at once.
@MainActor
final class ShotCache {
    static let shared = ShotCache()
    private var images: [String: PlatformImage] = [:]
    private var order: [String] = []
    private var loading: [String: Task<PlatformImage?, Never>] = [:]
    static let keep = 40

    func cached(_ name: String) -> PlatformImage? { images[name] }

    func image(_ name: String, api: APIClient) async -> PlatformImage? {
        await image(key: name) { try await api.shot(name) }
    }

    /// Any picture behind the owner's auth, by a key of its own (an attachment's uri).
    func image(key: String, load: @escaping @Sendable () async throws -> Data) async -> PlatformImage? {
        if let i = images[key] { return i }
        if let t = loading[key] { return await t.value }
        let t = Task { () -> PlatformImage? in
            guard let data = try? await load() else { return nil }
            return PlatformImage(data: data)
        }
        loading[key] = t
        let img = await t.value
        loading[key] = nil
        if let img {
            images[key] = img
            order.append(key)
            if order.count > Self.keep { images[order.removeFirst()] = nil }
        }
        return img
    }

    func clear() {
        images = [:]
        order = []
    }
}

/// An ask's screenshot: loaded with the token, fitted, tap to see it bigger.
struct ShotView: View {
    let name: String
    var maxHeight: CGFloat = 240
    @Environment(AppModel.self) private var model
    @State private var image: PlatformImage?
    @State private var failed = false
    @State private var enlarged = false

    var body: some View {
        Group {
            if let image {
                Button { enlarged = true } label: {
                    Image(platform: image)
                        .resizable()
                        .aspectRatio(contentMode: .fit)
                        .frame(maxWidth: .infinity, maxHeight: maxHeight)
                        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
                        .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous).strokeBorder(.separator))
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Screenshot of the page")
                .accessibilityIdentifier("browser-shot")
            } else if failed {
                Label("The screenshot isn't available.", systemImage: "photo")
                    .font(.caption).foregroundStyle(.secondary)
            } else {
                RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .fill(.fill.quaternary)
                    .frame(height: min(120, maxHeight))
                    .overlay(ProgressView().controlSize(.small))
            }
        }
        .task(id: name) {
            image = ShotCache.shared.cached(name)
            if image == nil {
                image = await ShotCache.shared.image(name, api: model.api)
                failed = image == nil
            }
        }
        .sheet(isPresented: $enlarged) {
            if let image { EnlargedImage(image: image) }
        }
    }
}

/// A picture on its own, fitted to the screen (pinch to zoom on iOS).
struct EnlargedImage: View {
    let image: PlatformImage
    var title = "Screenshot"
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            ScrollView([.horizontal, .vertical]) {
                Image(platform: image).resizable().aspectRatio(contentMode: .fit)
                    .frame(minWidth: 300, maxWidth: .infinity)
            }
            .background(Color.black)
            .navigationTitle(title)
            #if os(iOS)
            .navigationBarTitleDisplayMode(.inline)
            #endif
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
        }
        #if os(macOS)
        .frame(minWidth: 720, minHeight: 520)
        #endif
    }
}

extension BrowserKind {
    var color: Color {
        switch self {
        case .read: return .teal
        case .interact: return .blue
        case .commit: return .orange
        case .login: return .purple
        case .human: return .pink
        default: return .gray
        }
    }

    var icon: String {
        switch self {
        case .read: return "eye"
        case .interact: return "hand.tap"
        case .commit: return "paperplane"
        case .login: return "key"
        case .human: return "person.fill.questionmark"
        default: return "globe"
        }
    }
}

/// A browser ask: who wants to do what on which site, the screenshot it took, the URL and the details; a take-over
/// says why and offers to open the tab. The answers come after it (the item's options).
struct BrowserAskSummary: View {
    let agent: String
    let ask: BrowserAsk?
    /// The item's title ("<host>: <summary>"), when the ask's own object isn't there.
    let fallbackTitle: String
    /// The inbox item it belongs to (a take-over's Done / I can't follow the owner into the live view).
    let itemID: String?
    var pending = true
    var compact = false
    @Environment(AppModel.self) private var model

    var body: some View {
        let who = agent.isEmpty ? "The agent" : model.title(of: agent)
        VStack(alignment: .leading, spacing: 8) {
            if let ask {
                if ask.isTakeOver {
                    Text("\(Text(verbatim: who)) needs you to take over")
                        .font(.headline).fixedSize(horizontal: false, vertical: true)
                    if let r = ask.reason {
                        Text(verbatim: r).font(.callout).fixedSize(horizontal: false, vertical: true)
                    }
                } else {
                    Text("\(Text(verbatim: who)) wants to \(Text(verbatim: ask.summary.isEmpty ? "act" : ask.summary).bold())")
                        .fixedSize(horizontal: false, vertical: true)
                }
                HStack(spacing: 6) {
                    Chip(text: ask.kind.label, color: ask.kind.color)
                    if !ask.host.isEmpty {
                        Text(verbatim: ask.host).font(.caption.weight(.semibold)).lineLimit(1)
                    }
                    if !ask.profile.isEmpty {
                        Label(ask.profile, systemImage: "person.crop.square").font(.caption).foregroundStyle(.secondary)
                            .labelStyle(InlineLabelStyle()).lineLimit(1)
                    }
                }
                if let shot = ask.shot { ShotView(name: shot, maxHeight: compact ? 130 : 260) }
                if !ask.url.isEmpty && !compact {
                    Text(verbatim: ask.url).font(.caption.monospaced()).foregroundStyle(.secondary)
                        .lineLimit(2).truncationMode(.middle).textSelection(.enabled)
                }
                let lines = ask.detailLines
                if !lines.isEmpty && !compact {
                    VStack(alignment: .leading, spacing: 3) {
                        ForEach(Array(lines.enumerated()), id: \.offset) { _, line in
                            HStack(alignment: .firstTextBaseline, spacing: 6) {
                                Text(line.label).font(.caption).foregroundStyle(.secondary)
                                Text(verbatim: line.text).font(.caption.monospaced()).lineLimit(3)
                            }
                        }
                    }
                }
                if pending && !ask.profile.isEmpty {
                    liveButton(ask)
                }
            } else {
                Text(fallbackTitle.isEmpty ? "A browser action" : fallbackTitle).font(.headline)
            }
        }
    }

    /// Take over (a request_human ask: the owner does it in the live view, then says Done there or here), or Look
    /// (any other ask: see the tab live before answering).
    @ViewBuilder private func liveButton(_ ask: BrowserAsk) -> some View {
        let target = LiveTarget(profile: ask.profile, tab: ask.tab, itemID: ask.isTakeOver ? itemID : nil)
        if ask.isTakeOver {
            Button { model.openLiveView(target) } label: {
                Label("Take over", systemImage: "hand.point.up.left")
            }
            .buttonStyle(.borderedProminent)
            .tint(.pink)
            .controlSize(compact ? .small : .regular)
            .accessibilityIdentifier("take-over")
        } else if !compact {
            Button { model.openLiveView(target) } label: {
                Label("Look at the tab", systemImage: "eye")
            }
            .buttonStyle(.bordered)
            .controlSize(.small)
            .accessibilityIdentifier("look-live")
        }
    }
}
