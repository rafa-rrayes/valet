"""A scripted browser behind control/browser/backend.py's interface, so the actions, the rules, the inbox flow and
the live view are tested without Chrome (tests/control/test_browser_*.py, tests/e2e/test_e2e_browser.py). Real
Chrome is tested in tests/control/test_browser_chrome.py.

A FakeWeb maps URLs to pages: a title, some text, elements by ref (with what a click does), and login fields.
Every operation lands in FakeBackend.log, so a test can check what the browser was asked to do (a password filled,
an iframe left alone) as well as what the agent was told."""
import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import Any

from valet.control.browser.backend import (
    Backend, BrowserFailure, Element, FrameSink, LoginSecret, Screencast, Session, Tab,
)
from valet.shared.browser import host_of

JPEG = b"\xff\xd8\xff\xe0fake-jpeg\xff\xd9"


@dataclass
class FakeElement:
    tag: str = "button"
    name: str = ""
    type: str = ""
    submit: bool = False
    password: bool = False
    href: str = ""                  # a click goes here
    options: list[str] = field(default_factory=list)


@dataclass
class FakePage:
    title: str = "Page"
    text: str = ""
    elements: dict[str, FakeElement] = field(default_factory=dict)
    # Login: the refs of the fields login_step fills, and where each step leads.
    user: str | None = None
    password: str | None = None
    otp: str | None = None
    after_login: str | None = None  # URL once the password (or the username alone, without a password) is in
    after_otp: str | None = None
    iframe: str | None = None       # the URL of an iframe with a password field (never filled unless covered)
    in_form: bool = False           # the focused element is in a form (Enter commits)


class FakeWeb:
    def __init__(self, pages: dict[str, FakePage] | None = None) -> None:
        self.pages = dict(pages or {})

    def get(self, url: str) -> FakePage:
        return self.pages.get(url) or FakePage(title=host_of(url) or url, text=f"the page at {url}")


class FakeTab(Tab):
    def __init__(self, session: "FakeSession", n: int) -> None:
        self.session = session
        self.id = f"t{n:05d}"
        self._url = "about:blank"
        self.history: list[str] = []
        self.values: dict[str, str] = {}
        self._closed = False
        self.size = (1280, 900)

    @property
    def log(self) -> list:
        return self.session.backend.log

    @property
    def page(self) -> FakePage:
        return self.session.backend.web.get(self._url)

    @property
    def url(self) -> str:
        return self._url

    @property
    def closed(self) -> bool:
        return self._closed

    async def title(self) -> str:
        return "" if self._url == "about:blank" else self.page.title

    async def goto(self, url: str) -> None:
        self.log.append(("goto", self.id, url))
        if "unreachable" in url:
            raise BrowserFailure(f"couldn't open {url}: net::ERR_NAME_NOT_RESOLVED")
        if self._url != "about:blank":
            self.history.append(self._url)
        self._url = url
        self.values = {}

    async def settle(self) -> None:
        await asyncio.sleep(0)

    async def snapshot(self) -> str:
        p = self.page
        lines = [f'- heading "{p.title}"']
        if p.text:
            lines.append(f"- text: {p.text}")
        for ref, el in p.elements.items():
            value = self.values.get(ref)
            lines.append(f'- {el.tag} "{el.name}" [ref={ref}]' + (f": {value}" if value else ""))
        return "\n".join(lines)

    async def screenshot(self, quality: int = 70) -> bytes:
        self.log.append(("screenshot", self.id))
        return JPEG

    async def describe(self, ref: str) -> Element:
        el = self.page.elements.get(ref)
        if el is None:
            raise KeyError(f"no element {ref!r} on the page; take a new snapshot (refs change when the page changes)")
        return Element(tag=el.tag, type=el.type, name=el.name, submit=el.submit, password=el.password, href=el.href)

    async def click(self, ref: str) -> None:
        self.log.append(("click", self.id, ref))
        el = self.page.elements[ref]
        if el.href:
            await self.goto(el.href)

    async def fill(self, ref: str, text: str, submit: bool = False) -> None:
        self.log.append(("fill", self.id, ref, text, submit))
        self.values[ref] = text

    async def select(self, ref: str, values: list[str]) -> list[str]:
        self.log.append(("select", self.id, ref, values))
        return [v for v in values if v in self.page.elements[ref].options]

    async def focus_in_form(self) -> bool:
        return self.page.in_form

    async def press(self, key: str) -> None:
        self.log.append(("press", self.id, key))

    async def scroll(self, dy: int) -> None:
        self.log.append(("scroll", self.id, dy))

    async def back(self) -> None:
        self.log.append(("back", self.id))
        if self.history:
            self._url = self.history.pop()

    async def reload(self) -> None:
        self.log.append(("reload", self.id))

    async def close(self) -> None:
        self._closed = True

    async def password_values(self) -> list[str]:
        p = self.page
        return [v for ref, v in self.values.items() if p.elements.get(ref, FakeElement()).password and len(v) >= 3]

    async def login_step(self, covers, secret: LoginSecret) -> list[str]:
        p = self.page
        if p.iframe and covers(p.iframe):
            self.log.append(("fill-iframe", self.id, p.iframe, secret.password))
        steps = []
        if p.user and secret.username and not self.values.get(p.user):
            self.values[p.user] = secret.username
            self.log.append(("fill-username", self.id, secret.username))
            steps.append("username")
        if p.password and secret.password:
            self.values[p.password] = secret.password
            self.log.append(("fill-password", self.id, secret.password))
            steps.append("password")
            if p.after_login:
                await self.goto(p.after_login)
        elif p.otp and secret.code:
            code = secret.code()
            self.log.append(("fill-otp", self.id, code))
            steps.append("2FA code")
            if p.after_otp:
                await self.goto(p.after_otp)
        elif steps and p.after_login:
            await self.goto(p.after_login)
        return steps

    async def wants_code(self) -> bool:
        return bool(self.page.otp)

    async def screencast(self, sink: FrameSink) -> Screencast:
        return _Cast(self, sink)

    async def mouse(self, action: str, x: float, y: float, clicks: int = 1) -> None:
        self.log.append(("mouse", self.id, action, round(x, 3), round(y, 3)))

    async def wheel(self, x: float, y: float, dy: float) -> None:
        self.log.append(("wheel", self.id, dy))

    async def insert_text(self, text: str) -> None:
        self.log.append(("text", self.id, text))

    async def key(self, key: str) -> None:
        self.log.append(("key", self.id, key))


class _Cast(Screencast):
    def __init__(self, tab: FakeTab, sink: FrameSink) -> None:
        self.tab = tab

        async def pump():
            n = 0
            while True:
                n += 1
                with contextlib.suppress(Exception):
                    await sink(JPEG[:4] + f"{tab.id}:{n}".encode() + JPEG[4:])
                await asyncio.sleep(0.05)

        self.task = asyncio.create_task(pump())

    async def resize(self, width: int, height: int, scale: float | None = None) -> None:
        self.tab.log.append(("viewport", self.tab.id, width, height) + ((scale,) if scale else ()))

    async def stop(self) -> None:
        self.task.cancel()
        with contextlib.suppress(BaseException):
            await self.task


class FakeSession(Session):
    def __init__(self, backend: "FakeBackend", profile: str) -> None:
        super().__init__(profile)
        self.backend = backend
        self._tabs: list[FakeTab] = []

    def tabs(self) -> list[Tab]:
        return [t for t in self._tabs if not t.closed]

    async def new_tab(self) -> Tab:
        self.backend.opened += 1
        tab = FakeTab(self, self.backend.opened)
        self._tabs.append(tab)
        return tab


class FakeBackend(Backend):
    def __init__(self, web: FakeWeb | None = None) -> None:
        self.web = web or FakeWeb()
        self.sessions: dict[str, FakeSession] = {}
        self.log: list[tuple[Any, ...]] = []
        self.opened = 0
        self.started: list[str] = []
        self.wiped: list[str] = []
        self.fail: str | None = None        # set: starting a browser fails with this

    async def session(self, profile: str) -> Session:
        if self.fail:
            raise BrowserFailure(self.fail)
        s = self.sessions.get(profile)
        if s is None:
            s = self.sessions[profile] = FakeSession(self, profile)
            self.started.append(profile)
        s.touch()
        return s

    def running(self) -> dict[str, Session]:
        return dict(self.sessions)

    async def stop(self, profile: str) -> bool:
        return self.sessions.pop(profile, None) is not None

    async def wipe(self, profile: str) -> None:
        await self.stop(profile)
        self.wiped.append(profile)

    async def close(self) -> None:
        self.sessions.clear()
