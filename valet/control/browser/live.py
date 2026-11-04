"""The owner's live view of a profile's browser: `/ws/browser/<profile>` (docs/API.md, "The live view"). JPEG frames
of one tab (CDP Page.startScreencast) go out as binary messages; the owner's input comes in as JSON and is
dispatched to the tab: taps and clicks, scrolling, typed text, special keys, navigating, back, reload, and
choosing or opening a tab. The viewer may say its size: the tab it streams is laid out at that size while it does
(so the picture fills the viewer's screen), at the viewer's scale (so it's sharp on a Retina screen), and gets its own back when the view moves on or closes.

The owner isn't held to the rules (they're the agents'), but input takes the profile's action lock, one event at a
time, so it never lands in the middle of an agent's action; an agent waiting for an answer doesn't hold the lock.
Frames flow regardless. A browser that's being watched isn't stopped for being idle."""
import asyncio
import contextlib
import json
import logging
import math
import re
import time
from typing import Any

from fastapi import WebSocket

from valet.shared.browser import host_of

from .. import auth
from .backend import BrowserFailure, Screencast, Session, Tab

log = logging.getLogger(__name__)

KEYS = ("Enter", "Backspace", "Tab", "Escape", "ArrowLeft", "ArrowUp", "ArrowRight", "ArrowDown", "Delete", "Home",
        "End", "PageUp", "PageDown")
POLL = 0.5              # seconds between looks at the tabs, the current page and the lock
LOCK_WAIT = 5.0         # seconds an input event waits for an agent's action to finish before it's dropped
TEXT_LIMIT = 2000
VIEWPORT = (100, 4000)  # the sizes a viewer may ask for, CSS pixels, each side
SCALE = (1.0, 3.0)      # device pixels per CSS pixel a viewer may ask for (its screen's, for a sharp picture)
CLOSE_NOT_FOUND = 4404
CLOSE_UNAUTHORIZED = 4401


def _frac(v: Any) -> float:
    try:
        return min(1.0, max(0.0, float(v)))
    except (TypeError, ValueError):
        return 0.5


def _viewport(m: dict[str, Any]) -> tuple[int, int, float | None] | None:
    """A viewport message's size, each side clamped to VIEWPORT, and its scale clamped to SCALE (None when it gives
    none, or not a number: Chrome's own); None if the size isn't two numbers."""
    try:
        w, h = round(float(m.get("width"))), round(float(m.get("height")))
    except (TypeError, ValueError, OverflowError):
        return None
    try:
        scale = float(m["scale"])
        scale = min(SCALE[1], max(SCALE[0], scale)) if math.isfinite(scale) else None
    except (KeyError, TypeError, ValueError):
        scale = None
    lo, hi = VIEWPORT
    return min(hi, max(lo, w)), min(hi, max(lo, h)), scale


def _url(url: str) -> str:
    url = url.strip()
    return url if re.match(r"^(https?://|about:blank)", url, re.I) else "https://" + url


class LiveView:
    def __init__(self, service: Any, ws: WebSocket, session: Session, token: str) -> None:
        self.svc = service
        self.ws = ws
        self.s = session
        self.token = token              # the owner's session it was opened with, checked again at every poll
        self.tab: Tab | None = None
        self.cast: Screencast | None = None
        self._send_lock = asyncio.Lock()
        self._seen: Any = None          # what the last tabs / page / busy messages said
        self._page: Any = None
        self._busy: bool | None = None
        self.viewport: tuple[int, int, float | None] | None = None   # the viewer's size (CSS pixels) and scale

    async def send(self, message: dict[str, Any]) -> None:
        async with self._send_lock:
            await self.ws.send_text(json.dumps(message))

    async def frame(self, jpeg: bytes) -> None:
        async with self._send_lock:
            await self.ws.send_bytes(jpeg)

    async def watch(self, tab: Tab | None) -> None:
        """Stream `tab` (None: nothing) instead of the current one."""
        if self.cast is not None:
            await self.cast.stop()
            self.cast = None
        self.tab = tab
        if tab is not None and not tab.closed:
            try:
                self.cast = await tab.screencast(self.frame)
            except Exception as e:
                log.warning("live view: no screencast for %s: %s", tab.id, e)
            await self.resize()
        await self.report(force=True)

    async def resize(self) -> None:
        """Lay the streamed tab out at the viewer's size, if it gave one."""
        if self.cast is None or self.viewport is None:
            return
        try:
            await self.cast.resize(*self.viewport)
        except Exception as e:
            log.warning("live view: couldn't resize %s: %s", self.tab.id if self.tab else "-", e)

    async def report(self, force: bool = False) -> None:
        """Tell the client what changed: the tabs, the current page, whether an agent is acting."""
        tabs = [{"tab": t.id, "url": t.url, "title": await t.title()} for t in reversed(self.s.tabs())]
        current = self.tab.id if self.tab and not self.tab.closed else None
        if force or (tabs, current) != self._seen:
            self._seen = (tabs, current)
            await self.send({"type": "tabs", "tabs": tabs, "tab": current})
        page = None
        if current:
            page = {"type": "page", "tab": current, "url": self.tab.url, "title": await self.tab.title()}
        if page and (force or page != self._page):
            self._page = page
            await self.send(page)
        busy = self.s.lock.locked()
        if force or busy != self._busy:
            self._busy = busy
            await self.send({"type": "busy", "busy": busy})

    async def poll(self) -> None:
        while True:
            await asyncio.sleep(POLL)
            if not auth.is_owner_token(self.svc.control.db, self.token):    # a new password, or the session's age
                await self.send({"type": "error", "error": "signed out"})
                with contextlib.suppress(Exception):
                    await self.ws.close(CLOSE_UNAUTHORIZED)
                return
            if self.svc.backend.running().get(self.s.profile) is not self.s:
                await self.send({"type": "error", "error": "the browser stopped"})
                with contextlib.suppress(Exception):
                    await self.ws.close()
                return
            self.s.touch()
            if self.tab is not None and self.tab.closed:
                tabs = self.s.tabs()
                await self.watch(tabs[-1] if tabs else None)
            await self.report()

    async def handle(self, m: dict[str, Any]) -> None:
        kind = m.get("type")
        if kind == "viewport":         # how the owner watches, like choosing a tab: no lock
            size = _viewport(m)
            if size is not None and size != self.viewport:
                self.viewport = size
                await self.resize()
            return
        if kind == "tab":
            try:
                await self.watch(self.s.tab(str(m.get("tab") or "")))
            except KeyError as e:
                await self.send({"type": "error", "error": e.args[0], "fatal": False})
            return
        try:
            async with asyncio.timeout(LOCK_WAIT):
                await self.s.lock.acquire()
        except TimeoutError:
            await self.send({"type": "busy", "busy": True, "dropped": kind})
            return
        try:
            self.s.touch()
            if kind == "open":
                tab = await self.s.new_tab()
                url = _url(str(m.get("url") or "about:blank"))
                if url != "about:blank":
                    with contextlib.suppress(BrowserFailure):
                        await tab.goto(url)
                self.svc.audit(None, self.s.profile, "live_open", host=host_of(url), target=url)
                await self.watch(tab)
                return
            tab = self.tab
            if tab is None or tab.closed:
                return
            if kind == "tap":
                x, y = _frac(m.get("x")), _frac(m.get("y"))
                await tab.mouse("down", x, y, 1)
                await tab.mouse("up", x, y, 1)
            elif kind == "mouse" and m.get("action") in ("down", "up", "move"):
                await tab.mouse(m["action"], _frac(m.get("x")), _frac(m.get("y")), int(m.get("clicks") or 1))
            elif kind == "wheel":
                dy = float(m.get("dy") or 0)
                await tab.wheel(_frac(m.get("x")), _frac(m.get("y")), max(-5000.0, min(5000.0, dy)))
            elif kind == "text" and m.get("text"):
                await tab.insert_text(str(m["text"])[:TEXT_LIMIT])
            elif kind == "key" and m.get("key") in KEYS:
                await tab.key(m["key"])
            elif kind == "nav" and m.get("url"):
                url = _url(str(m["url"]))
                self.svc.audit(None, self.s.profile, "live_navigate", host=host_of(url), target=url)
                await tab.goto(url)
            elif kind == "back":
                await tab.back()
            elif kind == "reload":
                await tab.reload()
        except BrowserFailure as e:
            await self.send({"type": "error", "error": str(e), "fatal": False})
        finally:
            self.s.lock.release()
        await self.report()


async def serve(service: Any, ws: WebSocket, profile: str, tab_id: str | None, token: str) -> None:
    """Own one accepted, authenticated live-view socket (`token`: the owner's session it came with) until it closes."""
    if service.profile(profile) is None:
        await ws.send_text(json.dumps({"type": "error", "error": f"no browser profile named {profile}"}))
        await ws.close(CLOSE_NOT_FOUND)
        return
    try:
        s = await service.backend.session(profile)
    except BrowserFailure as e:
        await ws.send_text(json.dumps({"type": "error", "error": str(e)}))
        await ws.close(1011)
        return
    view = LiveView(service, ws, s, token)
    opened = time.monotonic()
    s.viewers += 1
    poller = None
    try:
        tabs = s.tabs()
        if tab_id:
            tab = next((t for t in tabs if t.id == tab_id), tabs[-1] if tabs else None)
        else:
            tab = tabs[-1] if tabs else None
        if tab is None:
            async with s.lock:
                tab = await s.new_tab()
        service.audit(None, profile, "live_view", host=host_of(tab.url) or None, target=tab.url)
        await view.watch(tab)
        poller = asyncio.create_task(view.poll())
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            try:
                data = json.loads(msg.get("text") or "")
            except ValueError:
                continue
            if isinstance(data, dict):
                if not auth.is_owner_token(service.control.db, token):
                    break               # signed out since it opened (poll() closes the socket)
                await view.handle(data)
    except Exception as e:      # the socket went away mid-send, or the browser did
        if not isinstance(e, (RuntimeError, ConnectionError)):
            log.debug("live view of %s ended: %s", profile, e)
    finally:
        s.viewers -= 1
        s.touch()
        if poller:
            poller.cancel()
        with contextlib.suppress(Exception):
            if view.cast:
                await view.cast.stop()
        log.info("live view of %s closed after %.0fs", profile, time.monotonic() - opened)
