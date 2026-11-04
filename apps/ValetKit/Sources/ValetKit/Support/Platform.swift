import SwiftUI

#if os(iOS)
import UIKit
#else
import AppKit
#endif

public enum Clipboard {
    @MainActor public static func copy(_ s: String) {
        #if os(iOS)
        UIPasteboard.general.string = s
        #else
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(s, forType: .string)
        #endif
    }
}

@MainActor
public enum DeviceInfo {
    /// The device's name as the owner knows it ("My iPhone"), for the login and the push registration.
    public static var name: String {
        #if os(iOS)
        UIDevice.current.name
        #else
        Host.current().localizedName ?? "Mac"
        #endif
    }

    public static var platform: String {
        #if os(iOS)
        "ios"
        #else
        "macos"
        #endif
    }
}

extension Color {
    /// Valet's violet (the icon's V; the AccentColor asset has the same two): #7C5CE0 in light mode (4.7:1
    /// with white either way), #9B7BF7 in dark (5.3:1 on a dark card as text, 3.2:1 under a button's white bold
    /// label; the icon's #A78BFA would be 2.7:1 there).
    public static let valet: Color = {
        #if os(iOS)
        Color(UIColor { $0.userInterfaceStyle == .dark
            ? UIColor(red: 0.608, green: 0.482, blue: 0.969, alpha: 1)
            : UIColor(red: 0.486, green: 0.361, blue: 0.878, alpha: 1) })
        #else
        Color(NSColor(name: nil) { $0.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
            ? NSColor(red: 0.608, green: 0.482, blue: 0.969, alpha: 1)
            : NSColor(red: 0.486, green: 0.361, blue: 0.878, alpha: 1) })
        #endif
    }()

    /// Behind the owner's bubbles: a deeper violet (#6D4ED6) in both modes, so white text stays readable (5.7:1).
    static let ownerBubble = Color(red: 0.427, green: 0.306, blue: 0.839)

    /// A grouped card's background, a step above the screen's.
    static var card: Color {
        #if os(iOS)
        Color(UIColor.secondarySystemGroupedBackground)
        #else
        Color(NSColor.controlBackgroundColor)
        #endif
    }

    static var screen: Color {
        #if os(iOS)
        Color(UIColor.systemGroupedBackground)
        #else
        Color(NSColor.windowBackgroundColor)
        #endif
    }
}

extension Date {
    /// "2m ago", "yesterday"...
    var ago: String {
        let s = Date().timeIntervalSince(self)
        if s < 60 { return "just now" }
        return formatted(.relative(presentation: .named, unitsStyle: .abbreviated))
    }
}
