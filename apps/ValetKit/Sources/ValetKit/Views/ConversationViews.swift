import SwiftUI

/// One item of a conversation (EventFolder's), as docs/API.md "Rendering events" describes.
struct ConversationRow: View {
    let item: ConversationItem

    var body: some View {
        switch item {
        case .message(let m): MessageRow(message: m)
        case .tool(let t): ToolCard(entry: t)
        case .permission(let p): PermissionCard(entry: p)
        case .plan(let p): PlanCard(plan: p)
        case .handoff(let h): HandoffRow(entry: h)
        case .turnEnd(let e):
            Text(e.text)
                .font(.footnote.weight(.medium))
                .foregroundStyle(e.bad ? Color.red : .secondary)
                .frame(maxWidth: .infinity)
                .padding(.vertical, 2)
        }
    }
}

struct MessageRow: View {
    let message: MessageEntry
    @State private var showThought = false

    var body: some View {
        switch message.role {
        case .user:
            VStack(alignment: .trailing, spacing: 3) {
                if let s = message.source {
                    Label(s, systemImage: "arrow.turn.down.right").font(.caption).foregroundStyle(.secondary)
                }
                if !message.text.isEmpty || message.attachments.isEmpty {
                    Text(message.text)          // the owner's text, as typed
                        .textSelection(.enabled)
                        .foregroundStyle(.white)
                        .padding(.horizontal, 12).padding(.vertical, 8)
                        .background(Color.ownerBubble, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
                }
                if !message.attachments.isEmpty { MessageAttachments(items: message.attachments, trailing: true) }
            }
            .frame(maxWidth: .infinity, alignment: .trailing)
            .padding(.leading, 40)
        case .agent:
            if message.attachments.isEmpty {
                MarkdownView(message.text)
            } else {
                VStack(alignment: .leading, spacing: 8) {
                    if !message.text.isEmpty { MarkdownView(message.text) }
                    MessageAttachments(items: message.attachments)
                }
            }
        case .thought:
            DisclosureGroup(isExpanded: $showThought) {
                MarkdownView(message.text)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .padding(.top, 4)
            } label: {
                Label("Thinking", systemImage: "brain").font(.callout).foregroundStyle(.secondary)
            }
        }
    }
}

/// A hand-off's earlier conversation, folded under where it came from (it's often a screen or more).
struct HandoffRow: View {
    let entry: HandoffEntry
    @State private var open = false

    var body: some View {
        DisclosureGroup(isExpanded: $open) {
            Text(clip(entry.text, 60_000))
                .font(.system(.footnote, design: .monospaced))
                .foregroundStyle(.secondary)
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.top, 4)
        } label: {
            Label(entry.label, systemImage: "arrow.triangle.branch").font(.callout).foregroundStyle(.secondary)
                .lineLimit(2)
        }
        .accessibilityIdentifier("handoff")
    }
}

// MARK: attachments

/// A message's pictures and files, after its text: a picture fitted (tap to see it bigger), a file as a row that
/// opens it in the agent's files.
struct MessageAttachments: View {
    let items: [MessageAttachment]
    var trailing = false

    var body: some View {
        VStack(alignment: trailing ? .trailing : .leading, spacing: 6) {
            ForEach(Array(items.enumerated()), id: \.offset) { _, a in
                switch a.kind {
                case .image(let uri, let data): AttachmentImage(uri: uri, data: data, name: a.name)
                case .file(let path, let uri): AttachmentFile(attachment: a, path: path, uri: uri)
                }
            }
        }
    }
}

/// An image in a message: the owner's, fetched with the token from its uri (cached); an agent's, from its bytes.
struct AttachmentImage: View {
    let uri: String?
    let data: String?
    let name: String
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
                        .frame(maxWidth: 240, maxHeight: 240)
                        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
                        .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(.separator))
                }
                .buttonStyle(.plain)
                .accessibilityLabel(name)
                .accessibilityIdentifier("message-image")
            } else if failed {
                Label(name, systemImage: "photo").font(.caption).foregroundStyle(.secondary)
                    .padding(10)
                    .background(Color.card, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                    .help("The picture isn't available.")
            } else {
                RoundedRectangle(cornerRadius: 12, style: .continuous).fill(.fill.quaternary)
                    .frame(width: 160, height: 120)
                    .overlay(ProgressView().controlSize(.small))
            }
        }
        .task(id: uri ?? data?.prefix(64).description) { await load() }
        .sheet(isPresented: $enlarged) {
            if let image { EnlargedImage(image: image, title: name) }
        }
    }

    private func load() async {
        if let data {
            image = Data(base64Encoded: data).flatMap(PlatformImage.init(data:))
        } else if let uri, APIClient.isServerPath(uri) {
            let api = model.api
            image = await ShotCache.shared.image(key: uri) { try await api.attachmentData(uri) }
        }
        failed = image == nil
    }
}

/// A file in a message: where it is in the agent's computer (it opens there), or the link it is.
struct AttachmentFile: View {
    let attachment: MessageAttachment
    let path: String?
    let uri: String

    var body: some View {
        if let path {
            NavigationLink(value: FilesRef(attachment.agent, path: path)) { row(chevron: true) }
                .buttonStyle(.plain)
                .accessibilityIdentifier("message-file")
        } else if let url = URL(string: uri), ["http", "https"].contains(url.scheme?.lowercased() ?? "") {
            Link(destination: url) { row(chevron: false) }.buttonStyle(.plain)
        } else {
            row(chevron: false)
        }
    }

    private func row(chevron: Bool) -> some View {
        HStack(spacing: 10) {
            Image(systemName: "doc").font(.title3).foregroundStyle(.secondary)
            VStack(alignment: .leading, spacing: 2) {
                Text(attachment.name).font(.callout.weight(.medium)).lineLimit(1).truncationMode(.middle)
                Text([path ?? uri, attachment.size.map(byteSize)].compactMap { $0 }.joined(separator: " · "))
                    .font(.caption).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
            }
            if chevron { Image(systemName: "chevron.right").font(.caption.weight(.semibold)).foregroundStyle(.tertiary) }
        }
        .padding(.horizontal, 12).padding(.vertical, 9)
        .frame(maxWidth: 320, alignment: .leading)
        .background(Color.card, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(.separator))
        .contentShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
    }
}

// MARK: tool calls

extension ToolEntry {
    var kindIcon: String {
        switch kind {
        case "read": return "doc.text"
        case "edit": return "pencil"
        case "delete": return "trash"
        case "move": return "arrow.right.doc.on.clipboard"
        case "search": return "magnifyingglass"
        case "execute": return "terminal"
        case "think": return "brain"
        case "fetch": return "globe"
        case "switch_mode": return "arrow.triangle.swap"
        default: return "wrench.and.screwdriver"
        }
    }

    var statusText: String {
        switch status {
        case "pending": return "pending"
        case "in_progress": return "running"
        case "completed": return "done"
        case "failed": return "failed"
        default: return status
        }
    }

    var statusColor: Color {
        switch status {
        case "completed": return .green
        case "failed": return .red
        case "in_progress": return .blue
        default: return .gray
        }
    }
}

extension PermissionState.Tone {
    var color: Color {
        switch self {
        case .allowed: return .green
        case .rejected: return .red
        case .expired: return .gray
        case .answered: return .blue
        }
    }
}

/// A tool call: kind icon, title, status; inside, its command, diff and output. Open while it runs or asks,
/// closed once it's done; only a change of that moves it, so a card the owner opened or closed stays so.
struct ToolCard: View {
    let entry: ToolEntry
    @Environment(AppModel.self) private var model
    @State private var expanded = false
    @State private var auto: Bool?

    var body: some View {
        let perm = entry.permission
        let item = perm.flatMap { model.inbox[$0.requestId] }
        let state = perm?.state(item: item)
        let asking = state?.isPending ?? false
        let result = perm.flatMap { p in state?.result(options: p.options) }
        let open = entry.isOpen(asking: asking)
        DisclosureGroup(isExpanded: $expanded) {
            VStack(alignment: .leading, spacing: 8) {
                ToolDetails(data: entry.data)
                if asking, let perm {
                    AskButtons(itemID: perm.requestId, options: perm.shownOptions(item: item), question: "Allow this?")
                }
            }
            .padding(.top, 6)
        } label: {
            HStack(spacing: 8) {
                Image(systemName: entry.kindIcon).foregroundStyle(.secondary).frame(width: 18)
                Text(entry.displayName).font(.callout.weight(.medium)).lineLimit(2)
                    .frame(maxWidth: .infinity, alignment: .leading)
                if asking {
                    Chip(text: "needs you", color: .orange)
                } else if let result {
                    Chip(text: result.text, color: result.tone.color)
                }
                Chip(text: entry.statusText, color: entry.statusColor)
            }
        }
        .cardStyle(highlight: asking ? .orange : nil)
        .onAppear { if auto == nil { auto = open; expanded = open } }
        .onChange(of: open) { _, now in auto = now; withAnimation { expanded = now } }
    }
}

/// What a tool does, from its ACP ToolCall fields: the command, its content (diffs, output), else its raw input
/// and output (app.js's toolDetails). Shared by tool cards and the inbox.
struct ToolDetails: View {
    let data: [String: JSONValue]

    /// Whether there's anything to show (a permission's tool call may carry no more than its title).
    static func has(_ data: [String: JSONValue]) -> Bool {
        let e = ToolEntry(id: "", data: data)
        return e.command != nil || !e.content.isEmpty || e.rawInput != nil || e.rawOutput != nil || !e.locations.isEmpty
    }

    var body: some View {
        let entry = ToolEntry(id: "", data: data)
        let content = entry.content
        let hasDiff = content.contains { if case .diff = $0 { return true } else { return false } }
        VStack(alignment: .leading, spacing: 8) {
            if let cmd = entry.command { CodeText(text: "$ " + clip(cmd)) }
            if !hasDiff {
                ForEach(entry.locations, id: \.self) { l in
                    Text(l).font(.caption.monospaced()).foregroundStyle(.secondary).lineLimit(2).truncationMode(.middle)
                }
            }
            ForEach(Array(content.enumerated()), id: \.offset) { _, c in
                switch c {
                case .diff(let path, let old, let new): DiffView(path: path, oldText: old, newText: new)
                case .block(let b):
                    if b["type"]?.string == "text" {
                        MarkdownView(clip(b["text"]?.string ?? "", 20000)).font(.callout)
                    } else {
                        CodeText(text: clip(blockText(b)))
                    }
                case .terminal: Text("Terminal output isn't shown here.").font(.caption).foregroundStyle(.secondary)
                }
            }
            if content.isEmpty {
                if entry.command == nil, let input = entry.rawInput { CodeText(text: clip(input.display)) }
                if let out = entry.rawOutput { CodeText(text: clip(out.display)) }
            }
            if entry.command == nil && content.isEmpty && entry.rawInput == nil && entry.rawOutput == nil
                && entry.locations.isEmpty {
                Text("No details.").font(.caption).foregroundStyle(.secondary)
            }
        }
    }
}

/// A line diff that's cheap and good enough for a tool's edit: the common head and tail are context, the middle
/// is the change (app.js's diffLines).
struct DiffView: View {
    let path: String
    let oldText: String?
    let newText: String

    static let maxLines = 400

    enum Mark: Character { case add = "+", del = "-", ctx = " ", gap = "…" }

    nonisolated static func lines(_ oldText: String?, _ newText: String) -> [(Mark, String)] {
        let a = oldText.map { $0.components(separatedBy: "\n") } ?? []
        let b = newText.components(separatedBy: "\n")
        var p = 0, s = 0
        while p < a.count && p < b.count && a[p] == b[p] { p += 1 }
        while s < a.count - p && s < b.count - p && a[a.count - 1 - s] == b[b.count - 1 - s] { s += 1 }
        let ctx = 3
        var out: [(Mark, String)] = []
        if p > ctx { out.append((.gap, "")) }
        for k in max(0, p - ctx)..<p { out.append((.ctx, a[k])) }
        for k in p..<(a.count - s) { out.append((.del, a[k])) }
        for k in p..<(b.count - s) { out.append((.add, b[k])) }
        for k in (b.count - s)..<min(b.count, b.count - s + ctx) { out.append((.ctx, b[k])) }
        if s > ctx { out.append((.gap, "")) }
        return out
    }

    var body: some View {
        let all = Self.lines(oldText, newText)
        VStack(alignment: .leading, spacing: 4) {
            if !path.isEmpty { Text(path).font(.caption.monospaced()).foregroundStyle(.secondary).lineLimit(2) }
            ScrollView(.horizontal, showsIndicators: false) {
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(all.prefix(Self.maxLines).enumerated()), id: \.offset) { _, line in
                        Text(verbatim: line.0 == .gap ? "…" : "\(line.0.rawValue) \(line.1)")
                            .font(.system(.caption, design: .monospaced))
                            .fixedSize(horizontal: true, vertical: false)
                            .padding(.horizontal, 8)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .background(line.0 == .add ? Color.green.opacity(0.16)
                                        : line.0 == .del ? Color.red.opacity(0.16) : Color.clear)
                            .foregroundStyle(line.0 == .ctx || line.0 == .gap ? Color.secondary : Color.primary)
                    }
                    if all.count > Self.maxLines {
                        Text("… \(all.count - Self.maxLines) more lines").font(.caption).foregroundStyle(.secondary)
                            .padding(.horizontal, 8)
                    }
                }
                .padding(.vertical, 6)
            }
            .background(.fill.quaternary, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
            .textSelection(.enabled)
        }
    }
}

// MARK: permissions and plans

/// A permission request with no tool card to sit on (a gateway ask, or one the harness sent before its call).
struct PermissionCard: View {
    let entry: PermissionEntry
    @Environment(AppModel.self) private var model

    var body: some View {
        let item = model.inbox[entry.requestId]
        let state = entry.state(item: item)
        VStack(alignment: .leading, spacing: 8) {
            if entry.isOwnerAsk {
                OwnerAskView(itemID: entry.requestId, agent: entry.agent, question: entry.kind == "question",
                             text: entry.text ?? item?.ownerText ?? entry.title,
                             options: item?.options.isEmpty == false ? item?.options ?? [] : entry.options,
                             state: state)
            } else if entry.isBrowserAsk {
                BrowserAskSummary(agent: entry.agent, ask: entry.browser ?? item?.browser, fallbackTitle: entry.title,
                                  itemID: entry.requestId, pending: state.isPending)
            } else if entry.isToolAsk {
                ToolAskSummary(agent: entry.agent, tool: entry.tool ?? item?.tool, fallbackTitle: entry.title,
                               fallbackArgs: entry.toolCall["rawInput"])
            } else {
                Text(entry.title).font(.callout.weight(.semibold))
                if ToolDetails.has(entry.toolCall) { ToolDetails(data: entry.toolCall) }
            }
            if entry.isOwnerAsk {
                // OwnerAskView has its answer and its result.
            } else if state.isPending {
                AskButtons(itemID: entry.requestId, options: entry.shownOptions(item: item))
            } else {
                ResultLine(state: state, options: entry.options)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cardStyle(highlight: state.isPending ? .orange : nil)
    }
}

/// How a settled ask ended.
struct ResultLine: View {
    let state: PermissionState
    let options: [PermissionOption]
    /// valet.owner's message or question: "Dismissed", "You answered: …".
    var owner = false

    var body: some View {
        if let r = state.result(options: options, owner: owner) {
            Text(!owner && r.tone == .expired && state.status == .expired ? "Expired: the agent stopped waiting" : r.text)
                .font(.callout.weight(.medium)).foregroundStyle(r.tone.color)
        } else {
            Text("No longer waiting").font(.callout).foregroundStyle(.secondary)
        }
    }
}

struct PlanCard: View {
    let plan: PlanEntry

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Label("Plan", systemImage: "checklist").font(.callout.weight(.semibold))
            ForEach(Array(plan.steps.enumerated()), id: \.offset) { _, step in
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Image(systemName: step.status == "completed" ? "checkmark.circle.fill"
                          : step.status == "in_progress" ? "circle.dotted" : "circle")
                        .foregroundStyle(step.status == "completed" ? Color.green
                                         : step.status == "in_progress" ? .blue : .secondary)
                    Text(step.content)
                        .strikethrough(step.status == "completed", color: .secondary)
                        .foregroundStyle(step.status == "completed" ? .secondary : .primary)
                        .font(.callout)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cardStyle()
    }
}
