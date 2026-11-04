import Foundation
import Security

/// Where the owner token lives. The Keychain in the apps; memory in the tests.
public protocol TokenStore: Sendable {
    func get(_ account: String) -> String?
    /// Stores (or with nil, deletes) a value; false when it couldn't be stored.
    @discardableResult func set(_ value: String?, for account: String) -> Bool
}

/// The `vown_` owner token. Readable after the first unlock, so a notification action can answer while the device
/// is locked in a pocket (it still asks for Face ID first), and never synced or moved to another device.
public struct Keychain: TokenStore {
    public static let standard = Keychain()

    let service: String

    public init(service: String = "br.com.rrayes.valet") { self.service = service }

    private func base(_ account: String) -> [String: Any] {
        var q: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
                                kSecAttrService as String: service,
                                kSecAttrAccount as String: account]
        #if os(macOS)
        // The iOS-style keychain on the Mac, never the login keychain: its items are tied to the app's signature and
        // ask for the password when a rebuilt app reads them. A build without entitlements can't use it, and says so.
        q[kSecUseDataProtectionKeychain as String] = true
        #endif
        return q
    }

    @discardableResult
    public func set(_ value: String?, for account: String) -> Bool {
        SecItemDelete(base(account) as CFDictionary)
        guard let value else { return true }
        var q = base(account)
        q[kSecValueData as String] = Data(value.utf8)
        q[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        return SecItemAdd(q as CFDictionary, nil) == errSecSuccess     // errSecMissingEntitlement: an unsigned build
    }

    public func get(_ account: String) -> String? {
        var q = base(account)
        q[kSecReturnData as String] = true
        q[kSecMatchLimit as String] = kSecMatchLimitOne
        var out: CFTypeRef?
        guard SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess, let d = out as? Data else { return nil }
        return String(data: d, encoding: .utf8)
    }
}

/// A TokenStore in memory (tests, previews).
public final class MemoryTokenStore: TokenStore, @unchecked Sendable {
    private let lock = NSLock()
    private var values: [String: String] = [:]

    public init(_ values: [String: String] = [:]) { self.values = values }

    public func get(_ account: String) -> String? { lock.withLock { values[account] } }
    @discardableResult
    public func set(_ value: String?, for account: String) -> Bool {
        lock.withLock { values[account] = value }
        return true
    }
}
