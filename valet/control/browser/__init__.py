"""The browser (DESIGN.md, "valet.browser"): real Chrome per profile over CDP, Valet's site rules and the vault's
`login()`, served to agents as the gateway's `valet.browser` server, and to the owner as the API and the live view.

- policy.py: the decision (level, rules, grants -> allow / ask / deny), and what makes a click a commit;
- backend.py: what a browser must do (the interface), chrome.py: real Chrome through Playwright;
- service.py: rules from the definitions repo, grants, asks through the inbox, the audit log, screenshots;
- actions.py: the actions; server.py: them as MCP tools;
- live.py: the owner's live view (a WebSocket of JPEG frames and input)."""
from .service import BrowserService

__all__ = ["BrowserService"]
