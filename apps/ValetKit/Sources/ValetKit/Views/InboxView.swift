import SwiftUI

/// Everything waiting on the owner, newest first, whatever its kind, answered with every option it carries.
public struct InboxView: View {
    @Environment(AppModel.self) private var model
    @State private var error: String?

    public init() {}

    public var body: some View {
        List {
            if model.pending.isEmpty {
                ContentUnavailableView("Nothing is waiting on you", systemImage: "tray",
                                       description: Text("When an agent asks for something, it shows up here."))
                    .listRowBackground(Color.clear)
                    .listRowSeparator(.hidden)
            }
            ForEach(model.pending) { item in
                InboxCard(item: item)
                    .listRowSeparator(.hidden)
                    .listRowBackground(Color.clear)
                    .listRowInsets(EdgeInsets(top: 6, leading: 12, bottom: 6, trailing: 12))
            }
        }
        #if os(iOS)
        .listStyle(.plain)
        #endif
        .animation(.default, value: model.pending.map(\.id))
        .navigationTitle("Inbox")
        .readableWidth()
        .toolbar { ToolbarItem(placement: .automatic) { ConnectionDot() } }
        .refreshable { await reload() }
        .task { await reload() }
        .errorAlert($error)
    }

    private func reload() async {
        do {
            for item in try await model.api.inbox() { model.put(item) }
        } catch is CancellationError {
        } catch APIError.unauthorized {
        } catch {
            // The socket's hello covers it; say so only if there's nothing to show.
            if model.pending.isEmpty { self.error = error.localizedDescription }
        }
    }
}

/// One inbox item: who asks and when, what (by kind), and its answers. The inbox, a notification's item and the
/// menu bar show it.
struct InboxCard: View {
    let item: InboxItem
    var compact = false
    @Environment(AppModel.self) private var model

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 6) {
                KindChip(kind: item.kind)
                Text(model.title(of: item.agent)).font(.subheadline.weight(.semibold)).lineLimit(1)
                Text("· \(item.created.ago)").font(.caption).foregroundStyle(.secondary).lineLimit(1)
                Spacer(minLength: 4)
                if let sid = item.sessionId, !compact {
                    Button {
                        model.openSession = SessionRef(agent: item.agent, id: sid)
                    } label: { Label("Open session", systemImage: "bubble.left.and.text.bubble.right") }
                        .labelStyle(.iconOnly)
                        .buttonStyle(.borderless)
                        .help("Open session")
                        .accessibilityLabel("Open session")
                }
            }
            if item.isOwnerAsk {
                // Its text once (the title is only its first line), and its answer or how it was settled.
                OwnerAskView(itemID: item.id, agent: item.agent, question: item.kind == .question, text: item.ownerText,
                             options: item.options, state: item.state, compact: compact)
            } else {
                what
                if item.isPending {
                    if !item.shownOptions.isEmpty {
                        AskButtons(itemID: item.id, options: item.shownOptions, compact: compact)
                    }
                } else {
                    ResultLine(state: item.state, options: item.options)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cardStyle(highlight: item.isPending && !compact ? .orange.opacity(0.6) : nil)
    }

    @ViewBuilder private var what: some View {
        if item.kind == .browser || item.browser != nil {
            BrowserAskSummary(agent: item.agent, ask: item.browser, fallbackTitle: item.title, itemID: item.id,
                              pending: item.isPending, compact: compact)
        } else if item.isToolAsk {
            ToolAskSummary(agent: item.agent, tool: item.tool, fallbackTitle: item.title,
                           fallbackArgs: item.toolCall["rawInput"])
        } else {
            switch item.kind {
            case .permission:
                Text(item.title.isEmpty ? "Permission request" : item.title).font(.headline)
                // What it would do (the command, the diff) is what the answer depends on: shown, not folded.
                if ToolDetails.has(item.toolCall) {
                    if compact {
                        if let cmd = item.toolCall["rawInput"]?["command"]?.string {
                            Text(verbatim: "$ " + brief(cmd, 160)).font(.caption.monospaced()).lineLimit(3)
                        }
                    } else {
                        ToolDetails(data: item.toolCall)
                    }
                }
            default:
                // browser (M3) and kinds to come: the title, the text if any, and what else it says, plainly.
                Text(item.title.isEmpty ? "Request" : item.title).font(.headline)
                if let body = item.bodyText { MarkdownView(compact ? brief(body, 240) : body).font(.callout) }
                if !compact { ExtraFields(extra: item.extra) }
            }
        }
    }
}

/// The scalar fields an unfamiliar kind carries (a browser ask's site and action...), as label: value lines.
struct ExtraFields: View {
    let extra: [String: JSONValue]

    static let skip: Set<String> = ["text", "body", "message", "question", "detail", "description", "screenshot",
                                    "shot", "image"]

    var body: some View {
        let rows = flatten(extra).filter { !Self.skip.contains($0.0) }.prefix(8)
        if !rows.isEmpty {
            VStack(alignment: .leading, spacing: 3) {
                ForEach(Array(rows), id: \.0) { k, v in
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Text(k.replacingOccurrences(of: "_", with: " ")).font(.caption).foregroundStyle(.secondary)
                        Text(v).font(.caption.monospaced()).lineLimit(2).truncationMode(.middle)
                    }
                }
            }
        }
    }

    /// Top-level scalars, and one level into objects ("browser.site").
    private func flatten(_ o: [String: JSONValue]) -> [(String, String)] {
        var out: [(String, String)] = []
        for (k, v) in o.sorted(by: { $0.key < $1.key }) {
            if let t = v.text, !t.isEmpty { out.append((k, t)) }
            else if let inner = v.object {
                for (k2, v2) in inner.sorted(by: { $0.key < $1.key }) {
                    if let t = v2.text, !t.isEmpty, !Self.skip.contains(k2) { out.append(("\(k).\(k2)", t)) }
                }
            }
        }
        return out
    }
}

struct KindChip: View {
    let kind: InboxKind

    var body: some View {
        let (text, color, icon): (String, Color, String) = switch kind {
        case .permission: ("permission", .orange, "hand.raised")
        case .tool: ("tool", .purple, "wrench.and.screwdriver")
        case .browser: ("browser", .blue, "globe")
        case .question: ("question", .teal, "questionmark.bubble")
        case .message: ("message", .green, "envelope")
        default: (kind.rawValue.isEmpty ? "request" : kind.rawValue, .gray, "tray")
        }
        Label(text, systemImage: icon)
            .font(.caption.weight(.semibold))
            .padding(.horizontal, 8).padding(.vertical, 3)
            .background(color.opacity(0.14), in: Capsule())
            .foregroundStyle(color)
    }
}

/// One item, by id (where a notification tap lands): from the live inbox, else fetched.
struct InboxItemSheet: View {
    let id: String
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var error: String?

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    if let item = model.inbox[id] {
                        InboxCard(item: item)
                        if !item.isPending {
                            Label("This request is \(item.status.rawValue).", systemImage: "info.circle")
                                .foregroundStyle(.secondary)
                        }
                    } else if let error {
                        ErrorText(text: error)
                    } else {
                        ProgressView().frame(maxWidth: .infinity)
                    }
                }
                .padding()
            }
            .background(Color.screen)
            .navigationTitle("Request")
            #if os(iOS)
            .navigationBarTitleDisplayMode(.inline)
            #endif
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } }
            }
            .task {
                do { model.put(try await model.api.inboxItem(id)) } catch { if model.inbox[id] == nil {
                    self.error = error.localizedDescription
                } }
            }
        }
        #if os(macOS)
        .frame(minWidth: 480, minHeight: 420)
        #endif
    }
}
