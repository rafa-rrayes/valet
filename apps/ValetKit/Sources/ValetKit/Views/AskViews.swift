import SwiftUI

/// The answer buttons of a pending ask, in the order the item carries them. Shared by the conversation, the inbox,
/// a notification's item and the menu bar. Only the first allowing option is prominent, so the safest yes stands
/// out (app.js's optionClass); a rejecting one is red.
struct AskButtons: View {
    let itemID: String
    let options: [PermissionOption]
    var question: String?
    var compact = false
    @Environment(AppModel.self) private var model
    @State private var busy = false
    @State private var error: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let question { Text(question).font(.callout.weight(.semibold)) }
            ViewThatFits(in: .horizontal) {
                HStack(spacing: 8) { buttons }
                VStack(alignment: .leading, spacing: 8) { buttons }
            }
            .disabled(busy)
            if let error { ErrorText(text: error) }
        }
    }

    @ViewBuilder private var buttons: some View {
        let primary = PermissionOption.primaryID(in: options)
        ForEach(options) { o in
            Button(role: o.kind.rejects ? .destructive : nil) {
                Task { await answer(o.optionId) }
            } label: {
                Text(o.name).lineLimit(1).frame(minWidth: compact ? nil : 60)
            }
            .modifier(OptionStyle(emphasis: o.optionId == primary ? .primary : o.kind.rejects ? .reject : .normal))
            .controlSize(compact ? .small : .regular)
            .accessibilityIdentifier("option-\(o.optionId)")
        }
        if busy { ProgressView().controlSize(.small) }
    }

    private func answer(_ optionId: String) async {
        busy = true
        error = nil
        defer { busy = false }
        do { try await model.answer(itemID, optionId: optionId) } catch { self.error = error.localizedDescription }
    }
}

struct OptionStyle: ViewModifier {
    enum Emphasis { case primary, normal, reject }
    let emphasis: Emphasis

    func body(content: Content) -> some View {
        switch emphasis {
        case .primary: content.buttonStyle(.borderedProminent).tint(.valet)
        case .normal: content.buttonStyle(.bordered)
        case .reject: content.buttonStyle(.bordered).tint(.red)
        }
    }
}

/// valet.owner's message or question (triggers.js's ownerItemEl): "<Agent> asks" (or "says"), the text as
/// Markdown, then the answer while it's pending or how it was settled. The inbox, a notification's item, the menu
/// bar and the conversation all show it, so they can't drift apart.
struct OwnerAskView: View {
    let itemID: String
    let agent: String
    let question: Bool
    let text: String
    let options: [PermissionOption]
    let state: PermissionState
    var compact = false
    @Environment(AppModel.self) private var model

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("\(agent.isEmpty ? "The agent" : model.title(of: agent)) \(question ? "asks" : "says")")
                .font(.headline)
            if !text.isEmpty {
                MarkdownView(compact ? brief(text, 240) : text).font(.callout)
            }
            if state.isPending {
                OwnerAnswer(itemID: itemID, options: options, question: question, compact: compact)
            } else {
                ResultLine(state: state, options: options, owner: true)
            }
        }
    }
}

/// The answer to an owner's item: a question's own words and its choices in one answer (a choice carries the words
/// typed; Dismiss never does; Send needs words), a message's Dismiss. The text is cleared once it's answered.
struct OwnerAnswer: View {
    let itemID: String
    let options: [PermissionOption]
    let question: Bool
    var compact = false
    @Environment(AppModel.self) private var model
    @State private var text = ""
    @State private var busy = false
    @State private var error: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if question {
                HStack(alignment: .bottom, spacing: 8) {
                    AnswerField(placeholder: "Answer in your own words", text: $text) { Task { await send() } }
                    Button("Send") { Task { await send() } }
                        .buttonStyle(.borderedProminent)
                        .tint(.valet)
                        .controlSize(compact ? .small : .regular)
                        .accessibilityIdentifier("answer-send")
                }
            }
            if !options.isEmpty {
                ViewThatFits(in: .horizontal) {
                    HStack(spacing: 8) { buttons }
                    VStack(alignment: .leading, spacing: 8) { buttons }
                }
            }
            if busy { ProgressView().controlSize(.small) }
            if let error { ErrorText(text: error) }
        }
        .disabled(busy)
    }

    @ViewBuilder private var buttons: some View {
        ForEach(options) { o in
            Button { Task { await choose(o) } } label: {
                Text(o.name).lineLimit(1).frame(minWidth: compact ? nil : 60)
            }
            .modifier(OptionStyle(emphasis: o.optionId == Self.dismiss ? .normal : .primary))
            .controlSize(compact ? .small : .regular)
            .accessibilityIdentifier("option-\(o.optionId)")
        }
    }

    static let dismiss = "dismiss"

    /// What a choice sends: its id, and the words typed unless it's Dismiss.
    nonisolated static func body(option: String, typed: String) -> (optionId: String, text: String?) {
        let t = typed.trimmingCharacters(in: .whitespacesAndNewlines)
        return (option, t.isEmpty || option == dismiss ? nil : t)
    }

    private func choose(_ o: PermissionOption) async {
        let b = Self.body(option: o.optionId, typed: question ? text : "")
        await answer { try await model.answer(itemID, optionId: b.optionId, text: b.text) }
    }

    private func send() async {
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty else {
            error = "Write an answer, or pick one of the choices."
            return
        }
        await answer { try await model.answer(itemID, text: t) }
    }

    private func answer(_ call: () async throws -> Void) async {
        guard !busy else { return }
        busy = true
        error = nil
        defer { busy = false }
        do {
            try await call()
            text = ""
        } catch {
            self.error = error.localizedDescription
        }
    }
}

/// A multi-line text box drawn like the session's composer: a soft fill and a hairline, in light and dark (a
/// rounded-border field is a black hole inside a dark card).
struct AnswerField: View {
    let placeholder: String
    @Binding var text: String
    var submit: () -> Void = {}

    var body: some View {
        TextField(placeholder, text: $text, axis: .vertical)
            .lineLimit(1...5)
            .padding(.horizontal, 10).padding(.vertical, 7)
            .background(.fill.tertiary, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(.separator))
            .onSubmit(submit)
            .accessibilityLabel("Your answer")
            .accessibilityIdentifier("answer-text")
    }
}


/// A gateway ask: "Scout wants to use github › Create issue", what the tool says it does, its arguments (folded).
struct ToolAskSummary: View {
    let agent: String
    let tool: ToolAsk?
    /// The ask's title ("<server>: <tool>"), for an event from before asks carried their tool.
    let fallbackTitle: String
    let fallbackArgs: JSONValue?
    @Environment(AppModel.self) private var model
    @State private var showArgs = false

    var body: some View {
        let server = tool?.server ?? ""
        let title = tool?.label ?? fallbackTitle
        let args = tool?.arguments ?? fallbackArgs
        let who = agent.isEmpty ? "The agent" : model.title(of: agent)
        let what = Text(verbatim: (server.isEmpty ? "" : server + " › ") + title).bold()
        VStack(alignment: .leading, spacing: 6) {
            Text("\(Text(verbatim: who)) wants to use \(what)")
                .fixedSize(horizontal: false, vertical: true)
            if let d = tool?.description, !d.isEmpty {
                Text(brief(d, 300)).font(.callout).foregroundStyle(.secondary)
            }
            if let args, !args.isEmpty {
                DisclosureGroup("Arguments", isExpanded: $showArgs) {
                    CodeText(text: clip(args.display, 2000))
                }
                .font(.callout)
            }
        }
    }
}

/// Monospaced text in a soft box that scrolls sideways.
struct CodeText: View {
    let text: String
    var font: Font = .system(.footnote, design: .monospaced)
    /// Long lines wrap instead of scrolling sideways (a long text read whole).
    var wraps = false

    var body: some View {
        if wraps {
            Text(text)
                .font(font)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
                .padding(10)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(.fill.quaternary, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
        } else {
            scrolling
        }
    }

    private var scrolling: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            Text(text)
                .font(font)
                .textSelection(.enabled)
                .fixedSize(horizontal: true, vertical: false)
                .padding(10)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.fill.quaternary, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
    }
}
