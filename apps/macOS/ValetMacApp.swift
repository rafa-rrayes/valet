import AppKit
import SwiftUI
import UserNotifications
import ValetKit

@main
struct ValetMacApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    private let model = AppModel.shared

    var body: some Scene {
        Window("Valet", id: "main") {
            RootView()
                .environment(model)
                .frame(minWidth: 820, minHeight: 540)
        }
        .defaultSize(width: 1120, height: 760)
        .commands { AgentCommands(model: model) }

        MenuBarExtra {
            MenuBarView()
                .environment(model)
        } label: {
            MenuBarLabel(model: model)
        }
        .menuBarExtraStyle(.window)
    }
}

/// File > New Agent… (⌘N): the New agent form, in the main window (opened again if it was closed).
struct AgentCommands: Commands {
    let model: AppModel
    @Environment(\.openWindow) private var openWindow

    var body: some Commands {
        CommandGroup(replacing: .newItem) {
            Button("New Agent…") {
                openWindow(id: "main")
                model.newAgent = NewAgentRequest()
            }
            .keyboardShortcut("n")
            // While the form is open, ⌘N again would put a blank one in its place and lose what's typed.
            .disabled(!model.signedIn || model.locked || model.newAgent != nil)
        }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        let model = AppModel.shared
        Push.setUp(delegate: self)
        model.registerForRemoteNotifications = { NSApplication.shared.registerForRemoteNotifications() }
        Task {
            await model.connect()
            await model.enablePush()
        }
        // Lock when the screen locks or the Mac sleeps; redial the socket when it wakes.
        let ws = NSWorkspace.shared.notificationCenter
        ws.addObserver(forName: NSWorkspace.screensDidSleepNotification, object: nil, queue: .main) { _ in
            MainActor.assumeIsolated {
                AppModel.shared.didEnterBackground()
                AppModel.shared.lock()
            }
        }
        ws.addObserver(forName: NSWorkspace.didWakeNotification, object: nil, queue: .main) { _ in
            MainActor.assumeIsolated { AppModel.shared.willEnterForeground() }
        }
        DistributedNotificationCenter.default().addObserver(forName: .init("com.apple.screenIsLocked"), object: nil,
                                                            queue: .main) { _ in
            MainActor.assumeIsolated { AppModel.shared.lock() }
        }
    }

    /// The menu bar extra keeps the count live, so closing the window doesn't quit.
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func application(_ application: NSApplication, didRegisterForRemoteNotificationsWithDeviceToken token: Data) {
        Task { await AppModel.shared.didRegisterForPush(deviceToken: token) }
    }

    func application(_ application: NSApplication, didFailToRegisterForRemoteNotificationsWithError error: Error) {
        AppModel.shared.didFailToRegisterForPush(error)
    }

    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification)
        async -> UNNotificationPresentationOptions {
        [.banner, .sound, .list]
    }

    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter,
                                            didReceive response: UNNotificationResponse) async {
        let action = response.actionIdentifier
        let info = response.notification.request.content.userInfo
        let item = Push.itemID(info)
        let shared = Push.sharedTarget(info)
        let text = (response as? UNTextInputNotificationResponse)?.userText
        await Push.handle(action: action, itemID: item, shared: shared, text: text)
        if action == UNNotificationDefaultActionIdentifier {
            await MainActor.run { NSApplication.shared.activate(ignoringOtherApps: true) }
        }
    }
}
