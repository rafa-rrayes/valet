import SwiftUI
import UIKit
import UserNotifications
import ValetKit

@main
struct ValetApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @Environment(\.scenePhase) private var phase
    private let model = AppModel.shared

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(model)
        }
        .onChange(of: phase) { _, phase in
            switch phase {
            case .background: model.didEnterBackground()
            case .active: model.willEnterForeground()
            default: break
            }
        }
    }
}

final class AppDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        Push.setUp(delegate: self)
        let model = AppModel.shared
        model.registerForRemoteNotifications = { application.registerForRemoteNotifications() }
        Task {
            await model.connect()
            await model.enablePush()
        }
        return true
    }

    func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken token: Data) {
        Task { await AppModel.shared.didRegisterForPush(deviceToken: token) }
    }

    func application(_ application: UIApplication, didFailToRegisterForRemoteNotificationsWithError error: Error) {
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
    }
}
