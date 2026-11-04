import Foundation
import LocalAuthentication

/// Face ID / Touch ID / the device passcode, for the app's lock.
public enum Biometry {
    /// What the unlock button says: "Face ID", "Touch ID", "Optic ID", or "Passcode".
    public static var name: String {
        let ctx = LAContext()
        _ = ctx.canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, error: nil)
        switch ctx.biometryType {
        case .faceID: return "Face ID"
        case .touchID: return "Touch ID"
        case .opticID: return "Optic ID"
        default: return "Passcode"
        }
    }

    public static var symbol: String {
        switch name {
        case "Face ID": return "faceid"
        case "Touch ID": return "touchid"
        case "Optic ID": return "opticid"
        default: return "lock.open"
        }
    }

    /// Whether the device can check its owner at all (no passcode set: nothing to protect with, so the app
    /// doesn't lock rather than locking the owner out).
    public static var available: Bool { LAContext().canEvaluatePolicy(.deviceOwnerAuthentication, error: nil) }

    /// Asks for Face ID / Touch ID, falling back to the passcode. nil once the owner passed; else why not, as a sentence
    /// ("" when they cancelled it themselves: nothing to say).
    public static func authenticate(reason: String) async -> String? {
        let ctx = LAContext()
        guard ctx.canEvaluatePolicy(.deviceOwnerAuthentication, error: nil) else { return nil }
        do {
            return try await ctx.evaluatePolicy(.deviceOwnerAuthentication, localizedReason: reason) ? nil : ""
        } catch let e as LAError {
            return why(e.code) ?? e.localizedDescription
        } catch {
            return error.localizedDescription
        }
    }

    /// What a failed check means, in Valet's words; "" for a cancel (the owner's own, or the app's).
    static func why(_ code: LAError.Code) -> String? {
        switch code {
        case .userCancel, .appCancel, .systemCancel: return ""
        case .authenticationFailed: return "\(name) didn't recognise you."
        case .biometryLockout: return "\(name) is locked after too many tries. Unlock the device with its passcode first."
        case .biometryNotEnrolled, .biometryNotAvailable: return "\(name) isn't set up on this device."
        case .passcodeNotSet: return "This device has no passcode."
        default: return nil
        }
    }
}
