#if os(macOS)
import Testing
@testable import ValetKit

/// The Mac's sidebar selection as it's saved and restored, and what it falls back to.
@Test func theSidebarSelectionIsSavedAndRestored() {
    for item: SidebarItem in [.agents, .inbox, .settings, .agent("scout"), .agent("a:b")] {
        #expect(SidebarItem(key: item.key) == item)
    }
    #expect(SidebarItem(key: "agent:") == nil)
    #expect(SidebarItem(key: "later") == nil)
    #expect(SidebarItem(key: "shared") == nil)          // Shared is on each agent's page now: All agents
    #expect(SidebarItem(key: "") == nil)
    // An agent that isn't there once the agents are known: All agents. Before they're known, or one that is: kept.
    #expect(SidebarItem.agent("gone").fallback(agents: ["alpha"], loaded: true) == .agents)
    #expect(SidebarItem.agent("gone").fallback(agents: [], loaded: false) == nil)
    #expect(SidebarItem.agent("alpha").fallback(agents: ["alpha"], loaded: true) == nil)
    #expect(SidebarItem.inbox.fallback(agents: [], loaded: true) == nil)
}
#endif
