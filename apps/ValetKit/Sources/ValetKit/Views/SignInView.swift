import SwiftUI

struct SignInView: View {
    @Environment(AppModel.self) private var model
    @State private var server = ""
    @State private var password = ""
    @State private var busy = false
    @State private var error: String?
    @FocusState private var focus: Field?

    enum Field { case server, password }
    static let errorID = "sign-in-error"

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(spacing: 28) {
                    VStack(spacing: 10) {
                        ValetMark(size: 84)
                        Text("Valet").font(.largeTitle.bold()).padding(.top, 6)
                        Text("Your always-on agents, from anywhere.")
                            .font(.callout).foregroundStyle(.secondary).multilineTextAlignment(.center)
                    }
                    .padding(.top, 60)

                    VStack(alignment: .leading, spacing: 12) {
                        Text("Server").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                        TextField("valet.example.com", text: $server)
                            .textContentType(.URL)
                            #if os(iOS)
                            .keyboardType(.URL)
                            .textInputAutocapitalization(.never)
                            #endif
                            .autocorrectionDisabled()
                            .textFieldStyle(.roundedBorder)
                            .focused($focus, equals: .server)
                            .submitLabel(.next)
                            .onSubmit { focus = .password }
                            .accessibilityIdentifier("server")
                        Text("Where your Valet control plane answers: mac.local:8790 on your network, or "
                             + "valet.example.com. Local addresses use http, anything else https; type the scheme to choose.")
                            .font(.caption).foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                        Text("Password").font(.caption.weight(.semibold)).foregroundStyle(.secondary).padding(.top, 4)
                        SecureField("The owner password", text: $password)
                            .textContentType(.password)
                            .textFieldStyle(.roundedBorder)
                            .focused($focus, equals: .password)
                            .submitLabel(.go)
                            .onSubmit(signIn)
                            .accessibilityIdentifier("password")
                        Button(action: signIn) {
                            Group {
                                if busy { ProgressView() } else { Text("Sign in").fontWeight(.semibold) }
                            }
                            .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(.borderedProminent)
                        .controlSize(.large)
                        .disabled(password.isEmpty || server.trimmingCharacters(in: .whitespaces).isEmpty || busy)
                        .padding(.top, 8)
                        .accessibilityIdentifier("sign-in")
                        if let error = error ?? model.notice {
                            ErrorText(text: error).padding(.top, 4)
                                .padding(.bottom, 56)     // clear of the keyboard's bar when scrolled to
                                .id(Self.errorID)
                        }
                    }
                    .frame(maxWidth: 380)
                }
                .padding(24)
                .frame(maxWidth: .infinity)
            }
            .scrollDismissesKeyboard(.interactively)
            // An error under the button is under the keyboard too: bring it up.
            .onChange(of: error ?? model.notice) { _, e in
                guard e != nil else { return }
                withAnimation { proxy.scrollTo(Self.errorID, anchor: .bottom) }
            }
            .onAppear {
                if server.isEmpty { server = model.rememberedServer }
                focus = server.isEmpty ? .server : .password
            }
        }
    }

    /// What the owner typed -> the server's address: https unless it's this machine or the LAN.
    static func url(_ typed: String) -> URL? {
        var s = typed.trimmingCharacters(in: .whitespacesAndNewlines)
        while s.hasSuffix("/") { s.removeLast() }
        guard !s.isEmpty else { return nil }
        if !s.contains("://") {
            let host = s.split(separator: "/").first.map(String.init)?.split(separator: ":").first.map(String.init) ?? s
            let local = host == "localhost" || host.hasSuffix(".local") || host.allSatisfy { $0.isNumber || $0 == "." }
            s = (local ? "http://" : "https://") + s
        }
        guard let url = URL(string: s), let scheme = url.scheme, ["http", "https"].contains(scheme), url.host != nil
        else { return nil }
        return url
    }

    private func signIn() {
        guard !password.isEmpty, !busy else { return }
        guard let url = Self.url(server) else {
            error = "That server address doesn't look right."
            return
        }
        busy = true
        error = nil
        model.notice = nil
        Task {
            defer { busy = false }
            do {
                try await model.signIn(server: url, password: password)
                password = ""
            } catch let e as APIError {
                switch e {
                case .http(401, _, _): error = "Wrong password."
                case .http(429, _, _): error = "Too many attempts. Try again in 15 minutes."
                case .http(409, _, _): error = AppModel.SignInError.noPassword.localizedDescription
                case .offline: error = "Can't reach \(url.host ?? url.absoluteString). Check the address and the connection."
                case .invalidResponse, .http(404, _, _), .http(405, _, _):
                    error = "That doesn't look like a Valet server. Check the address (and its path, behind a proxy)."
                default: error = e.localizedDescription
                }
            } catch {
                self.error = error.localizedDescription
            }
        }
    }
}

struct LockView: View {
    @Environment(AppModel.self) private var model
    @State private var confirmSignOut = false

    var body: some View {
        VStack(spacing: 20) {
            ValetMark(size: 72)
            Text("Valet is locked").font(.title2.bold())
            Button {
                Task { await model.unlock() }
            } label: {
                Label("Unlock with \(Biometry.name)", systemImage: Biometry.symbol).frame(minWidth: 200)
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)
            .accessibilityIdentifier("unlock")
            if let e = model.unlockError {
                Text(e).font(.callout).foregroundStyle(.red).multilineTextAlignment(.center)
                    .frame(maxWidth: 360)
            }
            // A way out when the check can't pass any more (the device's Face ID changed): sign in again.
            Button("Sign out") { confirmSignOut = true }
                .buttonStyle(.borderless)
                .foregroundStyle(.secondary)
                .accessibilityIdentifier("lock-sign-out")
        }
        .padding()
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .task { await model.unlock() }
        .confirmationDialog("Sign out of Valet on this device? You'll need the server's address and the password "
                            + "to sign in again.", isPresented: $confirmSignOut, titleVisibility: .visible) {
            Button("Sign out", role: .destructive) { model.signOut() }
        }
    }
}

/// Valet's mark: the icon's violet V on its dark tile, for the sign-in screen and the lock.
struct ValetMark: View {
    var size: CGFloat = 80

    var body: some View {
        RoundedRectangle(cornerRadius: size * 0.225, style: .continuous)
            .fill(LinearGradient(colors: [Color(red: 0.10, green: 0.11, blue: 0.15), Color(red: 0.05, green: 0.06, blue: 0.08)],
                                 startPoint: .top, endPoint: .bottom))
            .frame(width: size, height: size)
            .overlay {
                Path { p in
                    p.move(to: CGPoint(x: size * 0.28, y: size * 0.31))
                    p.addLine(to: CGPoint(x: size * 0.5, y: size * 0.75))
                    p.addLine(to: CGPoint(x: size * 0.72, y: size * 0.31))
                }
                .stroke(Color(red: 0.655, green: 0.545, blue: 0.980),
                        style: StrokeStyle(lineWidth: size * 0.095, lineCap: .round, lineJoin: .round))
            }
            .overlay {
                RoundedRectangle(cornerRadius: size * 0.225, style: .continuous).strokeBorder(.white.opacity(0.08))
            }
            .accessibilityHidden(true)
    }
}

/// What the app switcher sees while Valet isn't in front and the lock is on.
struct PrivacyCover: View {
    var body: some View {
        ZStack {
            Rectangle().fill(.background)
            ValetMark(size: 84)
        }
        .ignoresSafeArea()
    }
}
