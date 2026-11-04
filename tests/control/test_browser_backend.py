"""The Chrome backend's parts that don't need Chrome (control/browser/chrome.py): how it's launched (no automation
flags, its own user-data-dir, headless or not, the sandbox), the idle reaper, and the browser settings. Chrome
itself is tested in tests/e2e/test_e2e_browser_chrome.py (VALET_TEST_CHROME=1)."""
import asyncio

import pytest

from valet.control.browser import chrome
from valet.control.browser.backend import BrowserFailure
from valet.control.browser.chrome import ChromeBackend, ChromeSession
from valet.shared.config import ControlConfig


class _NoPlaywright:
    class chromium:
        executable_path = "/playwright/chromium"


@pytest.fixture
def launched(monkeypatch):
    """Every Chrome command line the backend tries to run (each launch fails, as if Chrome weren't installed)."""
    commands = []

    async def exec_(*args, **kw):
        commands.append(list(args))
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_)
    return commands


async def _pw(self):
    return _NoPlaywright()


async def test_launch_arguments(tmp_path, launched, monkeypatch):
    monkeypatch.setattr(ChromeBackend, "_playwright", _pw)
    b = ChromeBackend(tmp_path / "browser", chrome=None, headless_mode=True, no_sandbox=False)
    (tmp_path / "browser" / "profiles" / "alice").mkdir(parents=True)
    (tmp_path / "browser" / "profiles" / "alice" / "SingletonLock").write_text("left by a crash")
    with pytest.raises(BrowserFailure, match=r"Chrome \(/playwright/chromium\) didn't start: No such file"):
        await b.session("alice")
    [args] = launched
    assert args[0] == "/playwright/chromium"
    assert args[1] == f"--user-data-dir={tmp_path / 'browser' / 'profiles' / 'alice'}"
    assert "--remote-debugging-port=0" in args and "--headless=new" in args and "--no-sandbox" not in args
    assert args[-1] == "about:blank"
    # Real Chrome, as a person runs it: no automation flags (Google refuses sign-in in an automated browser).
    assert not any("automation" in a.lower() or a.startswith("--enable-automation") for a in args)
    assert not (tmp_path / "browser" / "profiles" / "alice" / "SingletonLock").exists()
    assert (tmp_path / "browser" / "profiles").stat().st_mode & 0o777 == 0o700
    assert b.running() == {}

    launched.clear()
    b = ChromeBackend(tmp_path / "browser", chrome="/opt/google/chrome", headless_mode=False, no_sandbox=True)
    with pytest.raises(BrowserFailure):
        await b.session("work")
    [args] = launched
    assert args[0] == "/opt/google/chrome" and "--no-sandbox" in args and "--headless=new" not in args


class _Lock:
    def __init__(self, held: bool) -> None:
        self.held = held

    def locked(self) -> bool:
        return self.held


async def test_idle_browsers_are_stopped(tmp_path, monkeypatch):
    monkeypatch.setattr(chrome, "REAP_EVERY", 0.01)
    b = ChromeBackend(tmp_path, idle_seconds=60)
    sessions = {}
    for name in ("idle", "busy", "watched", "recent"):
        s = sessions[name] = ChromeSession(name)
        s.last_used -= 0 if name == "recent" else 120
        b.sessions[name] = s
    sessions["busy"].lock = _Lock(True)         # an agent's action in progress
    sessions["watched"].viewers = 1             # the owner's live view
    task = asyncio.create_task(b._reap())
    for _ in range(100):
        await asyncio.sleep(0.01)
        if "idle" not in b.sessions:
            break
    assert sorted(b.sessions) == ["busy", "recent", "watched"]
    task.cancel()


def test_browser_settings(tmp_path):
    base = {"VALET_DATA": str(tmp_path)}
    c = ControlConfig.from_env(base)
    assert (c.chrome, c.headless, c.browser_idle) == (None, True, 1800.0)
    c = ControlConfig.from_env({**base, "DISPLAY": ":99", "VALET_CHROME": "/x/chrome", "VALET_BROWSER_IDLE": "60"})
    assert (c.chrome, c.headless, c.browser_idle) == ("/x/chrome", False, 60.0)       # a display: a window
    assert ControlConfig.from_env({**base, "DISPLAY": ":99", "VALET_HEADLESS": "1"}).headless is True
    assert ControlConfig.from_env({**base, "VALET_HEADLESS": "0"}).headless is False


@pytest.mark.chrome
async def test_a_stopped_chrome_keeps_a_login_made_a_moment_before(tmp_path):
    """Stopping Chrome (idle, wiped, the control plane going down) shuts it down as closing its window would: the
    cookies Chrome still held in memory (up to 30 seconds of them) reach the profile, and its locks go. Real Chrome."""
    import sqlite3
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Page(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Set-Cookie", "session=signed-in; Max-Age=86400; Path=/")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Page)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    b = ChromeBackend(tmp_path / "browser", headless_mode=True)
    try:
        s = await b.session("alice")
        await (await s.new_tab()).goto(f"http://127.0.0.1:{srv.server_port}/")
        assert await b.stop("alice")
    finally:
        await b.close()
        srv.shutdown()
    udir = tmp_path / "browser" / "profiles" / "alice"
    cookies = sqlite3.connect(udir / "Default" / "Cookies").execute("SELECT name FROM cookies").fetchall()
    assert cookies == [("session",)]
    assert not [p.name for p in udir.glob("Singleton*")]
