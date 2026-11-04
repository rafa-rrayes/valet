"""What the browser's actions and the live view need from a browser, as an interface: a Backend runs one Session per
profile (a browser with that profile's logins), a Session has Tabs. chrome.py implements it with real Chrome over
CDP; the tests implement it with scripted pages (tests/browser_fake.py), so the gateway, the rules and the inbox
flow are tested without Chrome.

Nothing here decides anything: actions.py checks the rules first, and only then calls these. There is deliberately
no "run JavaScript" and no cookie access: those would bypass the rules."""
import asyncio
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass


class BrowserFailure(Exception):
    """A browser that can't be started or a page operation that failed; str() is shown to the agent."""


@dataclass(frozen=True)
class Element:
    """What an element is, for classifying a click and for the audit log. Never a field's value."""
    tag: str
    type: str = ""              # the type attribute, lowercase
    name: str = ""              # its accessible label, at most 120 characters
    submit: bool = False        # submits a form (type=submit, or a button without a type inside a form)
    password: bool = False      # an <input type=password>
    href: str = ""


# A typed login secret's parts, revealed just in time: username, password, a function giving the current TOTP code
# (None without a TOTP secret).
@dataclass(frozen=True)
class LoginSecret:
    username: str
    password: str
    code: Callable[[], str] | None = None


# Frames for the live view: the JPEG bytes of one frame.
FrameSink = Callable[[bytes], Awaitable[None]]


class Screencast(ABC):
    @abstractmethod
    async def stop(self) -> None: ...

    async def resize(self, width: int, height: int, scale: float | None = None) -> None:
        """Lay the tab out at this size (CSS pixels) while it's streamed: the viewer's own shape, so its picture
        fills the view; `scale`, device pixels per CSS pixel (None: the browser's own), so the picture is as sharp
        as the viewer's screen. Undone when the screencast stops. A backend that can't: nothing (the picture is
        fitted)."""


class Tab(ABC):
    """One open page. `id` is stable while the tab lives (t + 5 characters)."""
    id: str

    @property
    @abstractmethod
    def url(self) -> str: ...

    @property
    @abstractmethod
    def closed(self) -> bool: ...

    @abstractmethod
    async def title(self) -> str: ...

    @abstractmethod
    async def goto(self, url: str) -> None:
        """Load `url` (domcontentloaded, 45 s). BrowserFailure if it can't."""

    @abstractmethod
    async def settle(self) -> None:
        """Wait a little for the page to settle after an action (domcontentloaded, then a short pause)."""

    @abstractmethod
    async def snapshot(self) -> str:
        """The page as an accessibility snapshot (Playwright's aria snapshot, ai mode: refs like [ref=e12])."""

    @abstractmethod
    async def screenshot(self, quality: int = 70) -> bytes:
        """A JPEG of the viewport, password fields blanked."""

    @abstractmethod
    async def describe(self, ref: str) -> Element:
        """KeyError if no element on the page has this ref."""

    @abstractmethod
    async def click(self, ref: str) -> None: ...

    @abstractmethod
    async def fill(self, ref: str, text: str, submit: bool = False) -> None:
        """Replace the field's content with `text`; submit: press Enter in it after."""

    @abstractmethod
    async def select(self, ref: str, values: list[str]) -> list[str]:
        """Choose option(s) of a select element; the values chosen."""

    @abstractmethod
    async def focus_in_form(self) -> bool:
        """Whether the focused element is inside a form (Enter there submits it)."""

    @abstractmethod
    async def press(self, key: str) -> None:
        """One of actions.KEYS, on the focused element."""

    @abstractmethod
    async def scroll(self, dy: int) -> None: ...

    @abstractmethod
    async def back(self) -> None: ...

    @abstractmethod
    async def reload(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def password_values(self) -> list[str]:
        """The values in the page's password fields (every frame), for masking page text."""

    @abstractmethod
    async def login_step(self, covers: Callable[[str], bool], secret: LoginSecret) -> list[str]:
        """Fill one step of a login form: the username (Enter if there's no password field yet), the password
        (then Enter), or a TOTP code when the page asks for one and has no password field. Only in the main frame
        and in frames whose URL `covers` accepts: never in a third-party iframe. The steps done ("username",
        "password", "2FA code"); [] if there was nothing to fill."""

    @abstractmethod
    async def wants_code(self) -> bool:
        """Whether the page shows a field for a one-time code."""

    # ---- the live view ----
    @abstractmethod
    async def screencast(self, sink: FrameSink) -> Screencast:
        """Stream JPEG frames of the tab to `sink` (the next one after the sink returns) until stopped."""

    @abstractmethod
    async def mouse(self, action: str, x: float, y: float, clicks: int = 1) -> None:
        """action: down | up | move; x, y: fractions 0..1 of the viewport."""

    @abstractmethod
    async def wheel(self, x: float, y: float, dy: float) -> None: ...

    @abstractmethod
    async def insert_text(self, text: str) -> None: ...

    @abstractmethod
    async def key(self, key: str) -> None:
        """A special key from the live view (live.KEYS)."""


class Session(ABC):
    """One running browser for a profile. `lock` is its action lock: an agent's action, or one input event from the
    live view, holds it while it touches the browser."""

    def __init__(self, profile: str) -> None:
        self.profile = profile
        self.lock = asyncio.Lock()
        self.started_at = time.time()
        self.last_used = time.monotonic()
        self.viewers = 0                # live views open on it: an idle browser isn't stopped while watched

    def touch(self) -> None:
        self.last_used = time.monotonic()

    @abstractmethod
    def tabs(self) -> list[Tab]:
        """Open tabs, oldest first."""

    def tab(self, tab_id: str | None) -> Tab:
        """The tab with this id, or the most recent one. KeyError saying what to do if there's none."""
        tabs = self.tabs()
        if tab_id:
            for t in tabs:
                if t.id == tab_id:
                    return t
            raise KeyError(f"no tab {tab_id!r}; call tabs() to list the open tabs")
        if not tabs:
            raise KeyError("no open tab; call open_tab(url) first")
        return tabs[-1]

    @abstractmethod
    async def new_tab(self) -> Tab: ...


class Backend(ABC):
    @abstractmethod
    async def session(self, profile: str) -> Session:
        """The profile's running browser, started if it isn't. BrowserFailure if it can't start."""

    @abstractmethod
    def running(self) -> dict[str, Session]:
        """Profiles with a running browser."""

    @abstractmethod
    async def stop(self, profile: str) -> bool:
        """Stop the profile's browser; False if it wasn't running."""

    async def wipe(self, profile: str) -> None:
        """Stop it and delete its data (logins, cookies)."""
        await self.stop(profile)

    @abstractmethod
    async def close(self) -> None:
        """Stop every browser (the control plane's shutdown)."""
