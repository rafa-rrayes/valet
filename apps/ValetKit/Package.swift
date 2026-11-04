// swift-tools-version: 6.0
import PackageDescription

// Everything the iOS and macOS apps share: the API client and its models, the live socket, the conversation
// folder, the app's state and every screen. The app targets (../iOS, ../macOS) are entry points around it.
// ValetPushCategories is the one piece the iOS Notification Service Extension (../Notify) links: which
// notification category fits a push, with Foundation only (an extension has little memory).
//
// SwiftTerm draws the agent's terminal (MIT; its LICENSE also carries the xterm.js authors' notice). Pinned exactly:
// from 1.19.0 its library runs a build-tool plugin, which Xcode won't build until someone trusts it by hand.
let package = Package(
    name: "ValetKit",
    defaultLocalization: "en",
    platforms: [.iOS(.v17), .macOS(.v14)],
    products: [
        .library(name: "ValetKit", targets: ["ValetKit"]),
        .library(name: "ValetPushCategories", targets: ["ValetPushCategories"]),
    ],
    dependencies: [
        .package(url: "https://github.com/migueldeicaza/SwiftTerm", exact: "1.18.0"),
    ],
    targets: [
        .target(name: "ValetPushCategories"),
        .target(name: "ValetKit", dependencies: ["ValetPushCategories",
                                                 .product(name: "SwiftTerm", package: "SwiftTerm")]),
        // Fixtures/: answers and socket frames recorded from a real control plane with the fake agent.
        .testTarget(name: "ValetKitTests", dependencies: ["ValetKit", "ValetPushCategories",
                                                         .product(name: "SwiftTerm", package: "SwiftTerm")],
                    resources: [.copy("Fixtures")]),
    ]
)
