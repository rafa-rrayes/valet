import SwiftUI

// Pieces the settings forms share: a list of choices with a sentence each, a text field over a definition path,
// the site rules editor (an agent's browser.rules, a profile's sites), and a raw text file's editor.

/// One of several, each with its sentence (a level, a network, a harness): a radio list, as the web UI's cards.
struct ChoiceList: View {
    struct Item: Identifiable {
        let id: String
        let name: String
        var summary: String?
        var warning: String?
        /// A line in green ("Ready").
        var ok: String?
    }

    let items: [Item]
    let selection: String
    /// The accessibility identifiers' prefix ("level" -> "level:host").
    let field: String
    let pick: (String) -> Void

    var body: some View {
        ForEach(items) { it in
            let on = it.id == selection
            Button { pick(it.id) } label: {
                HStack(alignment: .firstTextBaseline, spacing: 10) {
                    Image(systemName: on ? "checkmark.circle.fill" : "circle")
                        .foregroundStyle(on ? AnyShapeStyle(.tint) : AnyShapeStyle(.secondary))
                        .imageScale(.large)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(it.name).foregroundStyle(.primary)
                        if let s = it.summary, !s.isEmpty {
                            Text(s).font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                        }
                        if let o = it.ok, !o.isEmpty {
                            Text(o).font(.caption).foregroundStyle(.green)
                        }
                        if let w = it.warning, !w.isEmpty {
                            Text(w).font(.caption).foregroundStyle(.orange).fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    Spacer(minLength: 0)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityIdentifier("\(field):\(it.id)")
            .accessibilityAddTraits(on ? .isSelected : [])
        }
    }
}

extension ChoiceList.Item {
    /// A harness as a choice, with whether a runner can run it (the web UI's `readiness`): Ready, No key on the
    /// runner (and the key it needs), or unknown while no runner is connected.
    init(harness h: Catalog.Harness) {
        let unknown = h.ready == nil ? "Unknown: no runner is connected" : nil
        self.init(id: h.id, name: h.name, summary: [h.by, unknown].compactMap { $0 }.joined(separator: " · "),
                  warning: h.ready == false ? "No key on the runner" + (h.key.map { " · Needs \($0)" } ?? "") : nil,
                  ok: h.ready == true ? "Ready" : nil)
    }
}

/// A text field over one of the definition's string fields: what's typed stays as typed, the draft gets it trimmed
/// (empty: null, the field's default).
struct DraftField: View {
    let title: String
    var prompt = ""
    let path: String
    var mono = false
    /// Its title beside it on iOS too (a grouped form there shows only the text).
    var labeled = false
    @Bindable var settings: AgentSettings
    @State private var text = ""

    var body: some View {
        #if os(iOS)
        if labeled {
            LabeledContent(title) { field.multilineTextAlignment(.trailing) }
        } else {
            field
        }
        #else
        field
        #endif
    }

    private var field: some View {
        TextField(title, text: $text, prompt: Text(prompt))
            .font(mono ? .body.monospaced() : .body)
            .autocorrectionDisabled()
            #if os(iOS)
            .textInputAutocapitalization(mono ? .never : .sentences)
            #endif
            .onAppear { text = settings.draft?.string(path) ?? "" }
            .onChange(of: text) { _, t in
                let v = t.trimmingCharacters(in: .whitespacesAndNewlines)
                if (settings.draft?.string(path) ?? "") != v {
                    settings.draft?.setString(path, t)
                    settings.changed(path)
                }
            }
            .onChange(of: settings.draft?.string(path) ?? "") { _, v in
                if text.trimmingCharacters(in: .whitespacesAndNewlines) != v { text = v }
            }
            .accessibilityIdentifier("field-\(path)")
    }
}

/// An error under its field, when there is one.
struct FieldError: View {
    let text: String?

    var body: some View {
        if let text, !text.isEmpty { ErrorText(text: text) }
    }
}

/// A pasted URL becomes its site: no scheme, path, user or port (app.js's siteOf).
func siteOf(_ text: String) -> String {
    var s = text.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
    if let r = s.range(of: #"^[a-z][a-z0-9+.-]*://"#, options: .regularExpression) { s.removeSubrange(r) }
    s = String(s.split(whereSeparator: { "/?#".contains($0) }).first ?? "")
    if let at = s.firstIndex(of: "@") { s = String(s[s.index(after: at)...]) }
    if let r = s.range(of: #":\d*$"#, options: .regularExpression) { s.removeSubrange(r) }
    if s.hasPrefix("*.") { s.removeFirst(2) }
    if s.hasSuffix(".") { s.removeLast() }
    return s
}

/// A rule pattern as typed: lowercase, no scheme, no trailing slash (the server stores it so).
func sitePattern(_ text: String) -> String {
    var s = text.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
    if let r = s.range(of: #"^[a-z][a-z0-9+.-]*://"#, options: .regularExpression) { s.removeSubrange(r) }
    while s.hasSuffix("/") { s.removeLast() }
    return s
}

/// Site rules: pattern -> kind (read, interact, commit, login) -> allow, ask or deny; a kind left out falls through
/// (to the profile's rules, then the agent's level).
struct SiteRulesEditor: View {
    @Binding var rules: SiteRules
    /// An error for the rules as a whole, and per pattern.
    var error: String?
    var errorFor: (String) -> String? = { _ in nil }
    var onChange: () -> Void = {}
    @State private var newPattern = ""
    @State private var newError: String?

    static let kinds: [BrowserKind] = [.read, .interact, .commit, .login]

    var body: some View {
        if rules.isEmpty {
            Text("No site rules: its level decides.").foregroundStyle(.secondary)
        }
        ForEach(rules.keys.sorted(), id: \.self) { site in
            VStack(alignment: .leading, spacing: 6) {
                HStack {
                    Text(site).font(.body.monospaced()).lineLimit(1).truncationMode(.middle)
                    Spacer()
                    Button(role: .destructive) {
                        rules[site] = nil
                        onChange()
                    } label: { Image(systemName: "minus.circle.fill") }
                        .buttonStyle(.borderless).tint(.red)
                        .foregroundStyle(.red)
                        .accessibilityLabel("Remove \(site)")
                }
                ForEach(Self.kinds, id: \.self) { k in
                    Picker(k.label.capitalizedFirst, selection: binding(site, k)) {
                        Text("Not set").tag("")
                        Text("Allow").tag("allow")
                        Text("Ask you").tag("ask")
                        Text("Deny").tag("deny")
                    }
                    .pickerStyle(.menu)
                    .accessibilityIdentifier("site-rule:\(site):\(k.rawValue)")
                }
                FieldError(text: errorFor(site))
            }
            .padding(.vertical, 2)
        }
        FieldError(text: error)
        HStack {
            TextField("Add a site", text: $newPattern, prompt: Text(verbatim: "github.com, *.google.com, a.com/admin"))
                .font(.body.monospaced())
                .autocorrectionDisabled()
                #if os(iOS)
                .textInputAutocapitalization(.never)
                .keyboardType(.URL)
                #endif
                .onSubmit(add)
                .accessibilityIdentifier("site-rule-new")
            Button("Add", action: add).disabled(newPattern.trimmingCharacters(in: .whitespaces).isEmpty)
        }
        FieldError(text: newError)
    }

    private func binding(_ site: String, _ k: BrowserKind) -> Binding<String> {
        Binding(get: { rules[site]?[k.rawValue] ?? "" }, set: { v in
            var r = rules[site] ?? [:]
            r[k.rawValue] = v.isEmpty ? nil : v
            rules[site] = r
            onChange()
        })
    }

    private func add() {
        let p = sitePattern(newPattern)
        newError = nil
        guard !p.isEmpty, !p.contains(" ") else { newError = "Type a site, like github.com or *.google.com."; return }
        guard rules[p] == nil else { newError = "There's already a rule for \(p)."; return }
        // A new site starts asking for what changes things; its picks follow.
        rules[p] = ["commit": "ask"]
        newPattern = ""
        onChange()
    }
}

/// A text file edited whole (agent.yaml, persona.md, browser.yaml): Save writes it as typed; a refusal says why.
struct RawFileEditor: View {
    let title: String
    var note: String?
    let load: () async throws -> String
    let save: (String) async throws -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var text = ""
    @State private var original: String?
    @State private var saving = false
    @State private var error: String?
    @State private var saved = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let note { Text(note).font(.caption).foregroundStyle(.secondary) }
            if let error { ErrorText(text: error) }
            if saved { Label("Saved.", systemImage: "checkmark.circle.fill").font(.caption).foregroundStyle(.green) }
            if original == nil && error == nil {
                ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                TextEditor(text: $text)
                    .font(.system(.callout, design: .monospaced))
                    .autocorrectionDisabled()
                    #if os(iOS)
                    .textInputAutocapitalization(.never)
                    #endif
                    .scrollContentBackground(.hidden)
                    .padding(6)
                    .background(Color.card, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                    .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(.separator))
                    .accessibilityIdentifier("raw-editor")
            }
        }
        .padding()
        .background(Color.screen)
        .navigationTitle(title)
        #if os(iOS)
        .navigationBarTitleDisplayMode(.inline)
        #endif
        .toolbar {
            ToolbarItem(placement: .confirmationAction) {
                Button { Task { await write() } } label: {
                    if saving { ProgressView().controlSize(.small) } else { Text("Save") }
                }
                .disabled(saving || original == nil || text == original)
                .accessibilityIdentifier("raw-save")
            }
        }
        .task {
            do {
                let t = try await load()
                text = t
                original = t
            } catch {
                self.error = error.localizedDescription
            }
        }
        .onChange(of: text) { saved = false }
    }

    private func write() async {
        saving = true
        error = nil
        defer { saving = false }
        do {
            try await save(text)
            original = text
            saved = true
        } catch {
            self.error = error.localizedDescription
        }
    }
}
