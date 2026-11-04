import Foundation
import UserNotifications
import ValetPushCategories

/// A shared item to open (a push's tap): it's in `agent`'s space (nil: not known).
public struct SharedPushTarget: Equatable, Sendable {
    public let agent: String?
    public let id: Int
    public init(agent: String?, id: Int) {
        self.agent = agent
        self.id = id
    }
}

/// Push notifications for the inbox (`aps.alert {title, body}`, category
/// `VALET_INBOX`, plus `item_id`, `agent`, `kind`). Actions answer from the notification, in the background, after
/// the device is unlocked (`.authenticationRequired`); a tap opens the item.
///
/// `VALET_INBOX` offers Allow once, Allow for an hour and Reject: what the gateway's and the browser's asks carry;
/// questions get Reply and Dismiss, messages Dismiss.
/// An action is mapped onto what the item really offers when it's answered (`option(for:in:)`): an hour that
/// isn't offered becomes once, never more. The iOS Notification Service Extension (apps/Notify) narrows the
/// category to the kind (`PushCategories.pick`), for pushes sent with `mutable-content: 1`.
public enum Push {

    public enum Action: String, Sendable, CaseIterable {
        case allowOnce = "ALLOW_ONCE", allowHour = "ALLOW_HOUR", reject = "REJECT", reply = "REPLY", dismiss = "DISMISS"
    }

    /// Notifications need an app bundle (a unit-test process has none, and UNUserNotificationCenter traps there).
    public static var available: Bool { Bundle.main.bundleURL.pathExtension == "app" }

    /// Registers the categories (call at launch; it doesn't prompt).
    public static func setUp(delegate: UNUserNotificationCenterDelegate) {
        let center = UNUserNotificationCenter.current()
        center.delegate = delegate
        center.setNotificationCategories(categories)
    }

    public static var categories: Set<UNNotificationCategory> {
        let once = UNNotificationAction(identifier: Action.allowOnce.rawValue, title: "Allow once",
                                        options: [.authenticationRequired])
        let hour = UNNotificationAction(identifier: Action.allowHour.rawValue, title: "Allow for an hour",
                                        options: [.authenticationRequired])
        let reject = UNNotificationAction(identifier: Action.reject.rawValue, title: "Reject",
                                          options: [.authenticationRequired, .destructive])
        let reply = UNTextInputNotificationAction(identifier: Action.reply.rawValue, title: "Reply",
                                                  options: [.authenticationRequired], textInputButtonTitle: "Send",
                                                  textInputPlaceholder: "Your answer")
        let dismiss = UNNotificationAction(identifier: Action.dismiss.rawValue, title: "Dismiss",
                                           options: [.authenticationRequired])
        return [
            UNNotificationCategory(identifier: PushCategories.ask, actions: [once, hour, reject], intentIdentifiers: []),
            UNNotificationCategory(identifier: PushCategories.once, actions: [once, reject], intentIdentifiers: []),
            UNNotificationCategory(identifier: PushCategories.question, actions: [reply, dismiss], intentIdentifiers: []),
            UNNotificationCategory(identifier: PushCategories.message, actions: [dismiss], intentIdentifiers: []),
        ]
    }

    /// The option an action picks among the item's, or nil if it offers nothing like it.
    public static func option(for action: Action, in item: InboxItem) -> String? {
        let opts = item.options
        let once = opts.first { $0.optionId == "allow_once" }
            ?? opts.first { $0.kind == .allowOnce && $0.optionId != PermissionOption.allowHourID }
        switch action {
        case .allowOnce: return once?.optionId
        case .allowHour: return opts.first { $0.optionId == PermissionOption.allowHourID }?.optionId ?? once?.optionId
        case .reject:
            return (opts.first { $0.optionId == "reject_once" } ?? opts.first { $0.kind == .rejectOnce }
                ?? opts.first { $0.kind.rejects })?.optionId
        case .dismiss:      // valet.owner's messages and questions offer it
            return (opts.first { $0.optionId == "dismiss" } ?? opts.first { $0.name == "Dismiss" })?.optionId
        case .reply: return nil
        }
    }

    /// The inbox item a push is about (`item_id`).
    public static func itemID(_ userInfo: [AnyHashable: Any]) -> String? {
        let v = userInfo["item_id"]
        return (v as? String) ?? (v as? NSNumber)?.stringValue
    }

    /// The shared item a push is about (`kind: shared`, `shared_id`, and `agent`, the agent that added it, whose
    /// space it's in), or nil.
    public static func sharedTarget(_ userInfo: [AnyHashable: Any]) -> SharedPushTarget? {
        guard userInfo["kind"] as? String == "shared" else { return nil }
        let v = userInfo["shared_id"]
        guard let id = (v as? Int) ?? (v as? NSNumber)?.intValue ?? (v as? String).flatMap(Int.init) else { return nil }
        let agent = (userInfo["agent"] as? String).flatMap { $0.isEmpty || $0 == "owner" ? nil : $0 }
        return SharedPushTarget(agent: agent, id: id)
    }

    /// Asks for permission to notify (once signed in, so the prompt doesn't greet a signed-out user).
    static func requestAuthorization() async -> Bool {
        guard available else { return false }
        return (try? await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge])) ?? false
    }

    /// A tap or an action button. From `userNotificationCenter(_:didReceive:)`: the action's identifier,
    /// `itemID(userInfo)` or `sharedTarget(userInfo)`, and the typed text of a Reply. A shared item's push has no
    /// buttons: a tap opens the item.
    @MainActor
    public static func handle(action: String, itemID: String?, shared: SharedPushTarget? = nil, text: String?,
                              model: AppModel = .shared) async {
        if let shared, itemID == nil {
            if action == UNNotificationDefaultActionIdentifier { model.openShared = shared }
            return
        }
        guard let itemID else { return }
        guard let a = Action(rawValue: action) else {
            if action != UNNotificationDismissActionIdentifier { model.openItem(itemID) }
            return
        }
        do {
            if a == .reply {
                let t = (text ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
                guard !t.isEmpty else { model.openItem(itemID); return }
                try await model.answer(itemID, text: t)
                return
            }
            let item = try await model.api.inboxItem(itemID)
            guard item.isPending else { model.put(item); return }
            guard let option = option(for: a, in: item) else { model.openItem(itemID); return }
            try await model.answer(itemID, optionId: option)
        } catch {
            model.notice = "Couldn't answer from the notification: \(error.localizedDescription)"
            model.openItem(itemID)
        }
    }
}
