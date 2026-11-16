import SwiftUI

// Agent text is Markdown and untrusted (docs/API.md, "Rendering events"): it's parsed into blocks and inline runs,
// never HTML, and only http(s) links stay links.

/// The block structure of an agent's Markdown: enough for headings, lists, code, quotes and tables. Inline styling
/// (bold, italics, code spans, links) is left to `AttributedString(markdown:)`.
public enum MarkdownBlock: Hashable, Sendable {
    public struct ListItem: Hashable, Sendable {
        public let indent: Int
        /// "•", or the number as written ("3.").
        public let marker: String
        public let text: String
    }

    case heading(level: Int, text: String)
    case paragraph(String)
    case code(language: String?, code: String)
    case list([ListItem])
    case quote(String)
    case table(header: [String], rows: [[String]])
    case rule

    public static func parse(_ source: String) -> [MarkdownBlock] {
        var blocks: [MarkdownBlock] = []
        var paragraph: [String] = []
        let lines = source.replacingOccurrences(of: "\r\n", with: "\n").components(separatedBy: "\n")
        var i = 0

        func flush() {
            if !paragraph.isEmpty { blocks.append(.paragraph(paragraph.joined(separator: "\n"))) }
            paragraph = []
        }

        while i < lines.count {
            let line = lines[i]
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.hasPrefix("```") || trimmed.hasPrefix("~~~") {
                flush()
                let fence = String(trimmed.prefix(3))
                let language = trimmed.dropFirst(3).trimmingCharacters(in: .whitespaces)
                var code: [String] = []
                i += 1
                while i < lines.count, !lines[i].trimmingCharacters(in: .whitespaces).hasPrefix(fence) {
                    code.append(lines[i])
                    i += 1
                }
                blocks.append(.code(language: language.isEmpty ? nil : language, code: code.joined(separator: "\n")))
                i += 1                                               // the closing fence (or past the end)
                continue
            }
            if trimmed.isEmpty {
                flush()
                i += 1
                continue
            }
            if let level = headingLevel(trimmed) {
                flush()
                blocks.append(.heading(level: level, text: String(trimmed.dropFirst(level)).trimmingCharacters(in: .whitespaces)))
                i += 1
                continue
            }
            if isRule(trimmed) {
                flush()
                blocks.append(.rule)
                i += 1
                continue
            }
            if trimmed.hasPrefix("|"), i + 1 < lines.count, isTableSeparator(lines[i + 1]) {
                flush()
                let header = cells(trimmed)
                var rows: [[String]] = []
                i += 2
                while i < lines.count, lines[i].trimmingCharacters(in: .whitespaces).hasPrefix("|") {
                    var row = cells(lines[i].trimmingCharacters(in: .whitespaces))
                    row += Array(repeating: "", count: max(0, header.count - row.count))
                    rows.append(Array(row.prefix(header.count)))
                    i += 1
                }
                blocks.append(.table(header: header, rows: rows))
                continue
            }
            if trimmed.hasPrefix(">") {
                flush()
                var quoted: [String] = []
                while i < lines.count, lines[i].trimmingCharacters(in: .whitespaces).hasPrefix(">") {
                    quoted.append(String(lines[i].trimmingCharacters(in: .whitespaces).dropFirst())
                        .trimmingCharacters(in: .whitespaces))
                    i += 1
                }
                blocks.append(.quote(quoted.joined(separator: "\n")))
                continue
            }
            if listItem(line) != nil {
                flush()
                var items: [ListItem] = []
                while i < lines.count {
                    if let item = listItem(lines[i]) {
                        items.append(item)
                    } else if let last = items.last, !lines[i].trimmingCharacters(in: .whitespaces).isEmpty,
                              lines[i].hasPrefix("  ") {                // a wrapped line of the item above
                        items[items.count - 1] = ListItem(indent: last.indent, marker: last.marker,
                                                          text: last.text + "\n" + lines[i].trimmingCharacters(in: .whitespaces))
                    } else {
                        break
                    }
                    i += 1
                }
                blocks.append(.list(items))
                continue
            }
            paragraph.append(line)
            i += 1
        }
        flush()
        return blocks
    }

    /// Only http(s) links open (an agent's text could hold any scheme).
    public static func safeLink(_ url: URL) -> Bool {
        let scheme = url.scheme?.lowercased()
        return scheme == "http" || scheme == "https"
    }

    private static func headingLevel(_ s: String) -> Int? {
        let hashes = s.prefix { $0 == "#" }.count
        guard (1...6).contains(hashes), s.dropFirst(hashes).first == " " else { return nil }
        return hashes
    }

    private static func isRule(_ s: String) -> Bool {
        let chars = s.filter { $0 != " " }
        guard chars.count >= 3, let c = chars.first, "-*_".contains(c) else { return false }
        return chars.allSatisfy { $0 == c }
    }

    private static func isTableSeparator(_ line: String) -> Bool {
        let s = line.trimmingCharacters(in: .whitespaces)
        return s.contains("-") && s.contains("|") && s.allSatisfy { "|-: ".contains($0) }
    }

    private static func cells(_ row: String) -> [String] {
        var s = Substring(row)
        if s.hasPrefix("|") { s = s.dropFirst() }
        if s.hasSuffix("|") { s = s.dropLast() }
        return s.components(separatedBy: "|").map { $0.trimmingCharacters(in: .whitespaces) }
    }

    private static func listItem(_ line: String) -> ListItem? {
        let spaces = line.prefix { $0 == " " }.count
        let rest = line.dropFirst(spaces)
        if let first = rest.first, "-*+".contains(first), rest.dropFirst().first == " " {
            return ListItem(indent: min(spaces / 2, 4), marker: "•", text: String(rest.dropFirst(2)))
        }
        let digits = rest.prefix { $0.isNumber }
        guard !digits.isEmpty, digits.count <= 4 else { return nil }
        let after = rest.dropFirst(digits.count)
        guard let punct = after.first, ".)".contains(punct), after.dropFirst().first == " " else { return nil }
        return ListItem(indent: min(spaces / 2, 4), marker: digits + ".", text: String(after.dropFirst(2)))
    }
}

/// Parsed blocks by text, so a long conversation isn't parsed again on every redraw.
final class MarkdownCache: @unchecked Sendable {
    static let shared = MarkdownCache()
    private final class Box { let blocks: [MarkdownBlock]; init(_ b: [MarkdownBlock]) { blocks = b } }
    private let cache: NSCache<NSString, Box> = {
        let c = NSCache<NSString, Box>()
        c.countLimit = 600
        return c
    }()

    func blocks(_ text: String) -> [MarkdownBlock] {
        if let hit = cache.object(forKey: text as NSString) { return hit.blocks }
        let blocks = MarkdownBlock.parse(text)
        cache.setObject(Box(blocks), forKey: text as NSString)
        return blocks
    }
}

/// An agent's Markdown, rendered: selectable text, code blocks that scroll sideways, real tables. Body text takes
/// the environment's font.
struct MarkdownView: View {
    let blocks: [MarkdownBlock]

    init(_ text: String) { blocks = MarkdownCache.shared.blocks(text) }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(Array(blocks.enumerated()), id: \.offset) { _, block in
                view(for: block)
            }
        }
        .textSelection(.enabled)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    @ViewBuilder private func view(for block: MarkdownBlock) -> some View {
        switch block {
        case .heading(let level, let text):
            Text(Self.inline(text))
                .font(level == 1 ? .title2.bold() : level == 2 ? .title3.bold() : .headline)
                .padding(.top, 4)
        case .paragraph(let text):
            Text(Self.inline(text))
        case .code(let language, let code):
            CodeBlock(language: language, code: code)
        case .list(let items):
            VStack(alignment: .leading, spacing: 5) {
                ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Text(item.marker)
                            .monospacedDigit()
                            .foregroundStyle(.secondary)
                            .frame(minWidth: 14, alignment: .trailing)
                        Text(Self.inline(item.text))
                    }
                    .padding(.leading, CGFloat(item.indent) * 16)
                }
            }
        case .quote(let text):
            HStack(spacing: 10) {
                RoundedRectangle(cornerRadius: 1.5).fill(.tint.opacity(0.6)).frame(width: 3)
                Text(Self.inline(text)).foregroundStyle(.secondary)
            }
            .fixedSize(horizontal: false, vertical: true)
        case .table(let header, let rows):
            ScrollView(.horizontal, showsIndicators: false) {
                Grid(alignment: .leading, horizontalSpacing: 14, verticalSpacing: 7) {
                    GridRow {
                        ForEach(Array(header.enumerated()), id: \.offset) { _, cell in
                            Text(Self.inline(cell)).font(.callout.weight(.semibold))
                        }
                    }
                    Divider().gridCellUnsizedAxes(.horizontal)
                    ForEach(Array(rows.enumerated()), id: \.offset) { _, row in
                        GridRow {
                            ForEach(Array(row.enumerated()), id: \.offset) { _, cell in
                                Text(Self.inline(cell)).font(.callout)
                            }
                        }
                    }
                }
                .padding(12)
            }
            .background(.fill.quaternary, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
        case .rule:
            Divider()
        }
    }

    /// Inline markdown with line breaks kept; code spans get a monospaced font and a soft background; a link
    /// that isn't http(s) is plain text.
    static func inline(_ text: String) -> AttributedString {
        guard var s = try? AttributedString(markdown: text, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace))
        else { return AttributedString(text) }
        for run in s.runs {
            if run.inlinePresentationIntent?.contains(.code) == true {
                s[run.range].font = .system(.subheadline, design: .monospaced)
                s[run.range].backgroundColor = Color.secondary.opacity(0.14)
            }
            if let url = run.link, !MarkdownBlock.safeLink(url) { s[run.range].link = nil }
        }
        return s
    }
}

/// A fenced code block: monospaced, scrolls sideways, with its language and a Copy button.
struct CodeBlock: View {
    let language: String?
    let code: String
    @State private var copied = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text(language ?? "code").font(.caption.weight(.medium)).foregroundStyle(.secondary)
                Spacer()
                Button {
                    Clipboard.copy(code)
                    copied = true
                    Task { try? await Task.sleep(for: .seconds(1.5)); copied = false }
                } label: {
                    Label(copied ? "Copied" : "Copy", systemImage: copied ? "checkmark" : "doc.on.doc")
                        .font(.caption)
                }
                .buttonStyle(.borderless)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 6)
            Divider()
            ScrollView(.horizontal, showsIndicators: false) {
                Text(code)
                    .font(.system(.footnote, design: .monospaced))
                    .fixedSize(horizontal: true, vertical: false)
                    .padding(12)
            }
        }
        .background(.fill.quaternary, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(.separator.opacity(0.6)))
    }
}
