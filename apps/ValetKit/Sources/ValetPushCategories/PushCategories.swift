import Foundation

/// The notification categories of Valet's inbox pushes, and which one fits a push. The apps register them
/// (ValetKit's `Push`); the iOS Notification Service Extension rewrites a push's category with `pick` before it
/// shows, for pushes sent with `mutable-content: 1`.
public enum PushCategories {
    /// The contract's category: Allow once, Allow for an hour, Reject. The gateway's and the
    /// browser's asks carry those options.
    public static let ask = "VALET_INBOX"
    /// A harness's permission: Allow once and Reject.
    public static let once = "VALET_INBOX_ONCE"
    /// An agent's question: a Reply field, and Dismiss.
    public static let question = "VALET_INBOX_QUESTION"
    /// An agent's message, or an item with nothing to allow or reject: Dismiss.
    public static let message = "VALET_INBOX_MESSAGE"

    public static let all = [ask, once, question, message]

    /// The category that fits a push: from the option ids it lists (a server may send them), else from its kind.
    public static func pick(kind: String?, options: [String]?) -> String {
        if kind == "question" { return question }
        if let options {
            let allows = options.contains { $0.hasPrefix("allow") }, rejects = options.contains { $0.hasPrefix("reject") }
            if options.contains("allow_hour") && rejects { return ask }
            return allows || rejects ? once : message
        }
        switch kind {
        case "permission": return once
        case "message": return message
        default: return ask        // tool, browser, and kinds to come: the full set, mapped when answered
        }
    }

    /// Rewrites a push's category in place when it's Valet's (it has an `item_id`, and no category or the
    /// generic one). Returns the category it ends with.
    @discardableResult
    public static func narrow(_ content: UNMutableNotificationContentLike) -> String {
        let info = content.userInfo
        guard info["item_id"] != nil, content.categoryIdentifier.isEmpty || content.categoryIdentifier == ask else {
            return content.categoryIdentifier
        }
        let options = (info["options"] as? [Any])?.compactMap { o -> String? in
            (o as? String) ?? ((o as? [String: Any])?["optionId"] as? String)
        }
        content.categoryIdentifier = pick(kind: info["kind"] as? String, options: options)
        return content.categoryIdentifier
    }
}

/// What `narrow` needs of a UNMutableNotificationContent (so it can be tested without one).
public protocol UNMutableNotificationContentLike: AnyObject {
    var userInfo: [AnyHashable: Any] { get }
    var categoryIdentifier: String { get set }
}
