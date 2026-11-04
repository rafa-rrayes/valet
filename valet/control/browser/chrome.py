"""The real browser: one Chrome per profile, started by Valet without automation flags (sites such as Google refuse
sign-in in an obviously automated browser) and driven over CDP through Playwright's `connect_over_cdp`. Its
user-data-dir is `<data>/browser/profiles/<name>`, so logins persist. A browser nobody used for a while is stopped,
and started again by the next action.

Chrome is `VALET_CHROME`, else Playwright's own Chromium. Headless when `VALET_HEADLESS=1`, or when it's unset and
there's no `DISPLAY` (a server; on Linux, Xvfb provides one); `VALET_HEADLESS=0` always shows the window
(ControlConfig.headless).
`--no-sandbox` only when the control plane runs as root (in a container). The DevTools port is picked by Chrome
(`--remote-debugging-port=0`, read back from `DevToolsActivePort`), on loopback."""
import asyncio
import base64
import contextlib
import logging
import os
import secrets
import shutil
import string
import time
from collections.abc import Callable
from pathlib import Path

from .backend import Backend, BrowserFailure, Element, FrameSink, LoginSecret, Screencast, Session, Tab

log = logging.getLogger(__name__)

# --disable-search-engine-choice-screen: no chrome://settings/search tab of Chrome's own on a new profile.
FLAGS = ["--no-first-run", "--no-default-browser-check", "--disable-search-engine-choice-screen",
         "--disable-background-networking", "--disable-features=Translate,MediaRouter", "--window-size=1280,900",
         "--lang=en-US", "--password-store=basic", "--disable-dev-shm-usage"]
START_TIMEOUT = 20.0
REAP_EVERY = 60.0
# Blanked in screenshots: password fields, and every field login() filled, which stays hidden after a "show
# password" toggle makes it type=text (the snapshot masks its text; a JPEG can't be masked).
FILLED = "data-valet-filled"
HIDE_PASSWORDS = (f"input[type=password],input[{FILLED}]"
                  "{-webkit-text-security:disc!important;color:transparent!important}")

DESCRIBE = """el => {
    const t = (el.getAttribute('type') || '').toLowerCase();
    const tag = el.tagName.toLowerCase();
    const inForm = !!el.closest('form');
    const submit = t === 'submit' || (tag === 'button' && !el.getAttribute('type') && inForm);
    const raw = el.getAttribute('aria-label') || (tag !== 'input' && tag !== 'textarea' ? el.innerText : '') ||
                el.getAttribute('title') || el.getAttribute('placeholder') || el.getAttribute('name') ||
                (t === 'submit' || t === 'button' ? el.value : '') || '';
    const name = raw.trim().replace(/\\s+/g, ' ').slice(0, 120);
    return {tag, type: t, name, submit, password: tag === 'input' && t === 'password',
            href: el.getAttribute('href') || ''};
}"""

USER_SEL = ("input[autocomplete~=username], input[autocomplete~=email], input[type=email], "
            "input[name*=user i], input[name*=email i], input[name*=login i], input[id*=user i], input[id*=email i], "
            "input[name=identifier], input[id=identifierId]")
OTP_SEL = ("input[autocomplete=one-time-code], input[name*=otp i], input[name*=totp i], input[id*=otp i], "
           "input[name*=code i][inputmode=numeric], input[id*=code i][inputmode=numeric], input[name=totpPin], "
           "input[name=verificationCode], input[aria-label*=code i]")


def _tab_id() -> str:
    return "t" + "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(5))


def _failure(what: str, e: Exception) -> BrowserFailure:
    text = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
    return BrowserFailure(f"{what}: {text[:300]}")


class ChromeTab(Tab):
    def __init__(self, session: "ChromeSession", page) -> None:
        self.session = session
        self.page = page
        self.id = _tab_id()
        self._cdp = None                    # a CDP session for the live view's input
        self.size = (1280, 900)             # the viewport in CSS pixels, from the last screencast frame

    @property
    def url(self) -> str:
        return self.page.url

    @property
    def closed(self) -> bool:
        return self.page.is_closed()

    async def title(self) -> str:
        try:
            return await self.page.title()
        except Exception:
            return ""

    async def goto(self, url: str) -> None:
        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            raise _failure(f"couldn't open {url}", e) from None

    async def settle(self) -> None:
        with contextlib.suppress(Exception):
            await self.page.wait_for_load_state("domcontentloaded", timeout=8000)
        await asyncio.sleep(0.4)

    async def snapshot(self) -> str:
        try:
            return await self.page.aria_snapshot(mode="ai", timeout=15000)
        except Exception as e:
            return f"(snapshot failed: {type(e).__name__})"

    async def screenshot(self, quality: int = 70) -> bytes:
        css = None
        try:
            with contextlib.suppress(Exception):
                css = await self.page.add_style_tag(content=HIDE_PASSWORDS)
            return await self.page.screenshot(type="jpeg", quality=quality, timeout=15000)
        except Exception as e:
            raise _failure("couldn't take a screenshot", e) from None
        finally:
            if css is not None:
                with contextlib.suppress(Exception):
                    await css.evaluate("e => e.remove()")

    def _ref(self, ref: str):
        return self.page.locator(f"aria-ref={ref}")

    async def describe(self, ref: str) -> Element:
        loc = self._ref(ref)
        try:
            if await loc.count() == 0:
                raise KeyError(ref)
            info = await loc.first.evaluate(DESCRIBE)
        except (KeyError, Exception):
            raise KeyError(f"no element {ref!r} on the page; take a new snapshot (refs change when the page "
                           "changes)") from None
        return Element(tag=info["tag"], type=info["type"], name=info["name"], submit=bool(info["submit"]),
                       password=bool(info["password"]), href=info["href"])

    async def _act(self, what: str, coro) -> None:
        try:
            await coro
        except Exception as e:
            raise _failure(what, e) from None

    async def click(self, ref: str) -> None:
        await self._act("the click failed", self._ref(ref).first.click(timeout=15000))

    async def fill(self, ref: str, text: str, submit: bool = False) -> None:
        loc = self._ref(ref).first
        await self._act("typing failed", loc.fill(text, timeout=15000))
        if submit:
            await self._act("pressing Enter failed", loc.press("Enter"))

    async def select(self, ref: str, values: list[str]) -> list[str]:
        try:
            return await self._ref(ref).first.select_option(values, timeout=15000)
        except Exception as e:
            raise _failure("choosing failed", e) from None

    async def focus_in_form(self) -> bool:
        try:
            return bool(await self.page.evaluate(
                "() => !!(document.activeElement && document.activeElement.closest('form'))"))
        except Exception:
            return False

    async def press(self, key: str) -> None:
        await self._act(f"pressing {key} failed", self.page.keyboard.press(" " if key == "Space" else key))

    async def scroll(self, dy: int) -> None:
        await self._act("scrolling failed", self.page.mouse.wheel(0, dy))

    # Back and reload return once the navigation commits (settle() waits for the page): a page restored from the
    # back/forward cache may never fire the load events a full wait would need.
    async def back(self) -> None:
        await self._act("going back failed", self.page.go_back(wait_until="commit", timeout=20000))

    async def reload(self) -> None:
        await self._act("reloading failed", self.page.reload(wait_until="commit", timeout=30000))

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self.page.close()

    async def password_values(self) -> list[str]:
        out: list[str] = []
        for frame in self.page.frames:
            with contextlib.suppress(Exception):
                out += await frame.evaluate(
                    "() => [...document.querySelectorAll('input[type=password]')].map(e => e.value)"
                    ".filter(v => v && v.length >= 3)")
        return out

    @staticmethod
    async def _visible(frame, sel: str):
        loc = frame.locator(sel)
        try:
            n = await loc.count()
        except Exception:
            return None
        for i in range(min(n, 8)):
            el = loc.nth(i)
            with contextlib.suppress(Exception):
                if (await el.is_visible() and await el.is_enabled()
                        and (await el.get_attribute("type") or "") != "hidden"):
                    return el
        return None

    async def login_step(self, covers: Callable[[str], bool], secret: LoginSecret) -> list[str]:
        """The login, one step (actions.py repeats it while the page keeps asking)."""
        for frame in self.page.frames:
            if frame != self.page.main_frame and not covers(frame.url or ""):
                continue                                    # never fill a third-party iframe
            try:
                pw = await self._visible(frame, "input[type=password]")
                user = await self._visible(frame, USER_SEL)
                otp = await self._visible(frame, OTP_SEL)
                steps = []
                if user and secret.username and not await user.input_value():
                    await user.fill(secret.username)
                    steps.append("username")
                    if not pw:
                        await user.press("Enter")
                if pw and secret.password:
                    await pw.evaluate(f"e => e.setAttribute('{FILLED}', '')")
                    await pw.fill(secret.password)
                    await pw.press("Enter")
                    steps.append("password")
                elif otp and secret.code and not pw:
                    await otp.fill(secret.code())
                    await otp.press("Enter")
                    steps.append("2FA code")
            except Exception as e:
                raise _failure("filling the login form failed", e) from None
            if steps:
                return steps
        return []

    async def wants_code(self) -> bool:
        for frame in self.page.frames:
            if await self._visible(frame, OTP_SEL):
                return True
        return False

    # ---- the live view ----
    async def screencast(self, sink: FrameSink) -> Screencast:
        cdp = await self.session.context.new_cdp_session(self.page)
        cast = _Screencast(cdp, self, sink)
        await cast.start()
        return cast

    async def _input(self):
        if self._cdp is None:
            self._cdp = await self.session.context.new_cdp_session(self.page)
        return self._cdp

    async def mouse(self, action: str, x: float, y: float, clicks: int = 1) -> None:
        kind = {"down": "mousePressed", "up": "mouseReleased", "move": "mouseMoved"}[action]
        cdp = await self._input()
        await cdp.send("Input.dispatchMouseEvent", {
            "type": kind, "x": x * self.size[0], "y": y * self.size[1], "button": "left" if action != "move" else "none",
            "clickCount": clicks if action != "move" else 0, "buttons": 1 if action == "down" else 0})

    async def wheel(self, x: float, y: float, dy: float) -> None:
        cdp = await self._input()
        await cdp.send("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": x * self.size[0],
                                                    "y": y * self.size[1], "deltaX": 0, "deltaY": dy})

    # Keys and text through Playwright's keyboard, which sends each platform's own key codes (raw CDP key events
    # with Windows codes are other keys on a Mac, and can trigger browser shortcuts).
    async def insert_text(self, text: str) -> None:
        await self._act("typing failed", self.page.keyboard.insert_text(text))

    async def key(self, key: str) -> None:
        await self._act(f"pressing {key} failed", self.page.keyboard.press(key))


class _Screencast(Screencast):
    """CDP Page.startScreencast on one tab: each frame goes to the sink, and is acknowledged once the sink has
    taken it (Chrome sends the next one only then), so a slow viewer gets fewer frames rather than a backlog."""

    def __init__(self, cdp, tab: ChromeTab, sink: FrameSink) -> None:
        self.cdp = cdp
        self.tab = tab
        self.sink = sink
        self._latest: dict | None = None
        self._ready = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._sized = False

    async def start(self) -> None:
        self.cdp.on("Page.screencastFrame", self._on_frame)
        self._task = asyncio.create_task(self._pump())
        await self.cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 60, "maxWidth": 1600,
                                                     "maxHeight": 1600})

    def _on_frame(self, params: dict) -> None:
        self._latest = params
        self._ready.set()

    async def _pump(self) -> None:
        while True:
            await self._ready.wait()
            self._ready.clear()
            p, self._latest = self._latest, None
            if p is None:
                continue
            meta = p.get("metadata") or {}
            if meta.get("deviceWidth") and meta.get("deviceHeight"):
                self.tab.size = (meta["deviceWidth"], meta["deviceHeight"])
            with contextlib.suppress(Exception):
                await self.sink(base64.b64decode(p["data"]))
            with contextlib.suppress(Exception):
                await self.cdp.send("Page.screencastFrameAck", {"sessionId": p["sessionId"]})

    async def resize(self, width: int, height: int, scale: float | None = None) -> None:
        # On this CDP session, so it ends with it: the page gets Chrome's own size back once nobody watches. A scale
        # factor of 0 is Chrome's own (1 when headless); frames stay within startScreencast's maxWidth/maxHeight.
        await self.cdp.send("Emulation.setDeviceMetricsOverride", {"width": width, "height": height,
                                                                   "deviceScaleFactor": scale or 0, "mobile": False})
        self._sized = True

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task
        if self._sized:
            with contextlib.suppress(Exception):
                await self.cdp.send("Emulation.clearDeviceMetricsOverride")
        with contextlib.suppress(Exception):
            await self.cdp.send("Page.stopScreencast")
        with contextlib.suppress(Exception):
            await self.cdp.detach()


class ChromeSession(Session):
    def __init__(self, profile: str) -> None:
        super().__init__(profile)
        self.proc: asyncio.subprocess.Process | None = None
        self.browser = None
        self.context = None
        self._tabs: dict[object, ChromeTab] = {}        # page -> tab, in the order they opened
        self.startup_blank = None                       # the about:blank Chrome opens with, reused once

    @property
    def alive(self) -> bool:
        return bool(self.browser and self.browser.is_connected() and self.proc and self.proc.returncode is None)

    def _register(self, page) -> ChromeTab:
        tab = self._tabs.get(page)
        if tab is None:
            tab = self._tabs[page] = ChromeTab(self, page)
            page.on("close", lambda p: self._tabs.pop(p, None))
        return tab

    def tabs(self) -> list[Tab]:
        return [t for p, t in self._tabs.items() if not p.is_closed() and p is not self.startup_blank]

    async def new_tab(self) -> Tab:
        blank = self.startup_blank
        if blank is not None and not blank.is_closed() and blank.url == "about:blank":
            self.startup_blank = None
            page = blank
        else:
            self.startup_blank = None
            try:
                page = await self.context.new_page()
            except Exception as e:
                raise _failure("couldn't open a tab", e) from None
        tab = self._register(page)
        # Last in the order: the most recent tab is the one actions use without a tab id.
        self._tabs.pop(page)
        self._tabs[page] = tab
        return tab


class ChromeBackend(Backend):
    def __init__(self, data: Path, chrome: str | None = None, headless_mode: bool = True,
                 idle_seconds: float = 1800.0, no_sandbox: bool | None = None) -> None:
        """data: <VALET_DATA>/browser (profiles/<name> are the user-data-dirs)."""
        self.dir = data / "profiles"
        self.chrome = chrome
        self.headless = headless_mode
        self.idle_seconds = idle_seconds
        self.no_sandbox = (os.geteuid() == 0) if no_sandbox is None else no_sandbox
        self.sessions: dict[str, ChromeSession] = {}
        self._pw = None
        self._reaper: asyncio.Task | None = None
        self._starting: dict[str, asyncio.Lock] = {}

    async def _playwright(self):
        if self._pw is None:
            from playwright.async_api import async_playwright
            self._pw = await async_playwright().start()
        return self._pw

    def profile_dir(self, profile: str) -> Path:
        return self.dir / profile

    async def session(self, profile: str) -> Session:
        lock = self._starting.setdefault(profile, asyncio.Lock())
        async with lock:
            s = self.sessions.get(profile)
            if s and s.alive:
                s.touch()
                return s
            if s:
                log.warning("browser: Chrome for %s is gone; starting it again", profile)
                await self._stop(s)
            s = await self._start(profile)
            self.sessions[profile] = s
            if self._reaper is None or self._reaper.done():
                self._reaper = asyncio.create_task(self._reap())
            return s

    def running(self) -> dict[str, Session]:
        return {n: s for n, s in self.sessions.items() if s.alive}

    async def _start(self, profile: str) -> ChromeSession:
        pw = await self._playwright()
        exe = self.chrome or pw.chromium.executable_path
        udir = self.profile_dir(profile)
        udir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            self.dir.chmod(0o700)
        for f in ("SingletonLock", "SingletonSocket", "SingletonCookie", "DevToolsActivePort"):  # left by a crash
            (udir / f).unlink(missing_ok=True)
        args = [exe, f"--user-data-dir={udir}", "--remote-debugging-port=0", *FLAGS,
                *(["--no-sandbox"] if self.no_sandbox else []), *(["--headless=new"] if self.headless else []),
                "about:blank"]
        s = ChromeSession(profile)
        try:
            s.proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL,
                                                          stderr=asyncio.subprocess.DEVNULL)
        except OSError as e:
            raise BrowserFailure(f"Chrome ({exe}) didn't start: {e.strerror or e}") from None
        port_file, last = udir / "DevToolsActivePort", None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + START_TIMEOUT
        while loop.time() < deadline and s.proc.returncode is None:
            await asyncio.sleep(0.1)
            try:
                port = int(port_file.read_text().split()[0])
            except (OSError, ValueError, IndexError):
                continue
            try:
                s.browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=10000)
                break
            except Exception as e:
                last = e
        if s.browser is None:
            if s.proc.returncode is None:
                s.proc.kill()
                await s.proc.wait()
            raise BrowserFailure(f"Chrome didn't start for profile {profile} (exit {s.proc.returncode}): "
                                 f"{last or 'no DevTools port'}")
        s.context = s.browser.contexts[0] if s.browser.contexts else await s.browser.new_context()
        for p in s.context.pages:
            s._register(p)
            if p.url == "about:blank" and s.startup_blank is None:
                s.startup_blank = p
        s.context.on("page", s._register)
        log.info("browser: Chrome up for %s (pid %s%s)", profile, s.proc.pid, ", headless" if self.headless else "")
        return s

    async def _stop(self, s: ChromeSession) -> None:
        # Closed as its window would be: Chrome writes the cookies it still holds in memory (up to 30 seconds of
        # them, so a login made a moment before) and removes its locks. A signal alone would lose them.
        if s.browser and s.proc and s.proc.returncode is None:
            with contextlib.suppress(Exception):
                cdp = await s.browser.new_browser_cdp_session()
                await asyncio.wait_for(cdp.send("Browser.close"), 5)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(s.proc.wait(), 10)
        with contextlib.suppress(Exception):
            if s.browser:
                await s.browser.close()
        if s.proc and s.proc.returncode is None:
            s.proc.terminate()
            try:
                await asyncio.wait_for(s.proc.wait(), 10)
            except TimeoutError:
                s.proc.kill()
                await s.proc.wait()

    async def stop(self, profile: str) -> bool:
        s = self.sessions.pop(profile, None)
        if s is None:
            return False
        await self._stop(s)
        log.info("browser: Chrome for %s stopped", profile)
        return True

    async def wipe(self, profile: str) -> None:
        await self.stop(profile)
        shutil.rmtree(self.profile_dir(profile), ignore_errors=True)

    async def _reap(self) -> None:
        while self.sessions:
            await asyncio.sleep(REAP_EVERY)
            now = time.monotonic()
            for name, s in list(self.sessions.items()):
                if now - s.last_used > self.idle_seconds and not s.lock.locked() and not s.viewers:
                    log.info("browser: stopping %s's idle Chrome", name)
                    await self.stop(name)

    async def close(self) -> None:
        if self._reaper:
            self._reaper.cancel()
        for name in list(self.sessions):
            await self.stop(name)
        if self._pw:
            with contextlib.suppress(Exception):
                await self._pw.stop()
            self._pw = None
