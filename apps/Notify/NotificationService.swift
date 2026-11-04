import UserNotifications
import ValetPushCategories

/// Narrows a Valet push's category to what its item can be answered with, before iOS shows it: a harness's
/// permission gets Allow once and Reject, a question a Reply field, a message no buttons; the gateway's and the
/// browser's asks keep `VALET_INBOX` (Allow once, Allow for an hour, Reject). iOS runs it only for pushes sent
/// with `mutable-content: 1`.
final class NotificationService: UNNotificationServiceExtension {
    override func didReceive(_ request: UNNotificationRequest,
                             withContentHandler contentHandler: @escaping (UNNotificationContent) -> Void) {
        guard let content = request.content.mutableCopy() as? UNMutableNotificationContent else {
            contentHandler(request.content)
            return
        }
        PushCategories.narrow(content)
        contentHandler(content)
    }
}

extension UNMutableNotificationContent: @retroactive UNMutableNotificationContentLike {}
