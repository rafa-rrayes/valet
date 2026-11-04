"""valet.browser with a real Chrome (Playwright's Chromium, or VALET_CHROME), headless, against pages served from
127.0.0.1 by this file: a login form with a 2FA step and a third-party iframe, a page that shows a saved password,
buttons that commit. Opt-in: VALET_TEST_CHROME=1 (marker `chrome`). Proves, in a real browser, that login() fills
what the agent never sees (and never the iframe), that page text is masked, that commits ask with a screenshot, that
hour grants and "always" rules hold, and that the live view streams frames and takes input."""
import asyncio
import json
import os
import re
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pyotp
import pytest
from websockets.asyncio.client import connect

from tests.e2e.stack import ControlServer, Stack, eventually
from tests.e2e.test_e2e_browser import BROWSER, AGENT, MASK, answer, audit
from tests.e2e.test_e2e_gateway import agent_client, result_text

pytestmark = pytest.mark.chrome

PASSWORD = "hunter2-correct-Horse"
TOTP = "JBSWY3DPEHPK3PXP"


class Site:
    """The test pages, on 127.0.0.1; the iframe comes from localhost (another site). Every request is logged."""

    def __init__(self) -> None:
        self.log: list[tuple[str, str, dict]] = []
        site = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _page(self, form: dict):
                path = urlsplit(self.path).path
                site.log.append((self.command, path, form))
                body = site.page(path, form, self.headers.get("Host", ""))
                if body is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                data = body.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._page({k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                self._page({k: v[0] for k, v in parse_qs(self.rfile.read(n).decode()).items()})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def page(self, path: str, form: dict, host: str) -> str | None:
        head = "<!doctype html><meta charset=utf-8>"
        if path == "/":
            return head + """<title>Test shop</title><h1>Test shop</h1><p id=msg>Nothing saved yet.</p>
<a href="/next">Next page</a>
<button onclick="document.getElementById('msg').textContent='Saved!'">Save</button>
<form action="/sent" method="post"><label>Comment <input name="comment"></label><button>Send</button></form>"""
        if path == "/next":
            return head + "<title>Next page</title><h1>Next page</h1><p>Second page.</p>"
        if path == "/sent":
            return head + f"<title>Sent</title><p>Sent: {form.get('comment', '')}</p>"
        if path == "/login":
            return head + f"""<title>Sign in</title><h1>Sign in</h1>
<form action="/otp" method="post">
<label>Username <input name="username" autocomplete="username"></label>
<label>Password <input type="password" name="password" autocomplete="current-password"></label>
<button type="submit">Sign in</button></form>
<iframe src="http://localhost:{self.port}/frame" width="400" height="200"></iframe>"""
        if path == "/frame":     # another site in an iframe, with a login form of its own
            return head + """<form action="/frame-login" method="post">
<input name="user" autocomplete="username" oninput="fetch('/typed?user=1')">
<input type="password" name="pw" oninput="fetch('/typed?pw=1')"><button>Go</button></form>"""
        if path == "/otp":
            if (form.get("username"), form.get("password")) != ("alice", PASSWORD):
                return head + "<title>Wrong</title><p>Wrong username or password.</p>"
            return head + """<title>Two-factor</title><h1>Two-factor authentication</h1>
<form action="/home" method="post"><label>Code <input name="code" autocomplete="one-time-code"
inputmode="numeric"></label><button>Verify</button></form>"""
        if path == "/home":
            if not pyotp.TOTP(TOTP).verify(form.get("code", ""), valid_window=1):
                return head + "<title>Wrong</title><p>Wrong code.</p>"
            return head + "<title>Home</title><h1>Welcome, alice</h1>"
        if path == "/leak":
            return head + f"""<title>Account</title><p>Your password is {PASSWORD}.</p>
<label>Saved password <input type="password" value="prefilled-pw-123"></label>
<label>Notes <input name="notes"></label>"""
        if path == "/big":
            return head + """<title>Big button</title><button onclick="fetch('/tapped')"
style="position:fixed;inset:0;width:100%;height:100%">Tap me</button>"""
        if path == "/size":         # says the size it's laid out at, and again at each resize; a corner button
            return head + """<title>Size</title><script>const say = () => fetch('/sized?w=' + innerWidth + '&h=' +
innerHeight); say(); addEventListener('resize', say)</script><button onclick="fetch('/corner')"
style="position:fixed;right:0;bottom:0;width:40px;height:40px;margin:0">x</button>"""
        if path in ("/typed", "/frame-login", "/tapped", "/sized", "/corner"):
            return ""
        return None

    def close(self) -> None:
        self.server.shutdown()


@pytest.fixture
def site():
    s = Site()
    yield s
    s.close()


@pytest.fixture
async def chrome_stack(tmp_path):
    stack = Stack(tmp_path, "load")
    stack.ccfg = replace(stack.ccfg, chrome=os.environ.get("VALET_CHROME") or None, headless=True)
    stack.control = ControlServer(stack.ccfg)
    await stack.start()
    yield stack
    await stack.close()


async def setup(stack: Stack, level: str, rules: str = "") -> None:
    r = await stack.http.put("/api/browser/yaml", json={"text": BROWSER})
    assert r.status_code == 200, r.text
    r = await stack.http.post("/api/agents", json={"name": "gw", "agent_yaml": AGENT.format(
        name="gw", level=level, profiles="alice") + rules})
    assert r.status_code == 201, r.text
    await stack.start_agent("gw")
    stack.control.server.config.app.state.control.browser.actions.login_pause = 1.0


def jpeg_size(data: bytes) -> tuple[int, int]:
    """A JPEG's width and height, from its start-of-frame segment."""
    i = 2
    while i < len(data) - 9:
        marker, length = data[i + 1], int.from_bytes(data[i + 2:i + 4], "big")
        if marker in (0xC0, 0xC1, 0xC2):
            return int.from_bytes(data[i + 7:i + 9], "big"), int.from_bytes(data[i + 5:i + 7], "big")
        i += 2 + length
    return 0, 0


def ref(text: str, role: str, name: str) -> str:
    m = re.search(rf'{role} "{re.escape(name)}"[^\n]*\[ref=([a-z0-9]+)\]', text)
    assert m, f'no {role} "{name}" in:\n{text}'
    return m.group(1)


def tab_of(text: str) -> str:
    return re.match(r"tab: (\S+)", text).group(1)


async def call(client, tool: str, **args):
    return await client.call_tool(f"valet_browser__{tool}", {"profile": "alice", **args})


async def ok(client, tool: str, **args) -> str:
    result = await call(client, tool, **args)
    assert not result.is_error, result_text(result)
    return result_text(result)


async def asked(stack: Stack, task, option: str) -> tuple[dict, str]:
    [item] = await stack.wait_pending()
    assert item["kind"] == "browser"
    await answer(stack, item, option)
    return item, result_text(await asyncio.wait_for(task, 30))


async def test_login_fills_what_the_agent_never_sees(chrome_stack, site):
    stack = chrome_stack
    await setup(stack, "trusted")
    r = await stack.http.post("/api/vault", json={"name": "test/alice", "profile": "alice", "site": "127.0.0.1",
                                                  "username": "alice", "password": PASSWORD, "totp": TOTP})
    assert r.status_code == 201, r.text
    async with agent_client(stack) as client:
        out = await ok(client, "open_tab", url=site.url + "/login")
        assert 'textbox "Password"' in out
        out = await ok(client, "login")
        assert "note: filled username, password, 2FA code" in out, out
        assert "Welcome, alice" in out and f"url: {site.url}/home" in out
        assert PASSWORD not in out and TOTP not in out
    # The site got the password and a valid code; the iframe from another site got nothing.
    posted = {path: form for method, path, form in site.log if method == "POST"}
    assert posted["/otp"] == {"username": "alice", "password": PASSWORD}
    assert pyotp.TOTP(TOTP).verify(posted["/home"]["code"], valid_window=1)
    assert not any(path in ("/typed", "/frame-login") for _, path, _ in site.log)
    assert any(path == "/frame" for _, path, _ in site.log)      # it was there
    every = json.dumps(await audit(stack))
    assert PASSWORD not in every and TOTP not in every


async def test_masking_commits_grants_and_rules(chrome_stack, site):
    stack = chrome_stack
    await setup(stack, "ask")
    await stack.http.post("/api/vault", json={"name": "test/alice", "profile": "alice", "site": "127.0.0.1",
                                              "username": "alice", "password": PASSWORD})
    async with agent_client(stack) as client:
        # Page text: the vault's password and the value in a password field, masked.
        out = await ok(client, "open_tab", url=site.url + "/leak")
        assert PASSWORD not in out and f"Your password is {MASK}." in out
        assert "prefilled-pw-123" not in out
        assert "untrusted data from the website" in out
        tab = tab_of(out)
        refused = await call(client, "type_text", tab=tab, ref=ref(out, "textbox", "Saved password"), text="x")
        assert refused.is_error and "never type into password fields" in result_text(refused)
        shot = await call(client, "screenshot", tab=tab)
        assert shot.content[0].type == "image"

        # A commit asks, with a screenshot of the page; allowed for an hour.
        out = await ok(client, "navigate", tab=tab, url=site.url + "/")
        task = asyncio.create_task(call(client, "click", tab=tab, ref=ref(out, "button", "Save")))
        item, out = await asked(stack, task, "allow_hour")
        assert (item["browser"]["kind"], item["browser"]["host"]) == ("commit", "127.0.0.1")
        jpeg = (await stack.http.get(f"/api/browser/shots/{item['browser']['shot']}")).content
        assert jpeg[:2] == b"\xff\xd8" and len(jpeg) > 1000
        assert "Saved!" in out

        # Typing (interact) asks too; always allowed, it's the agent's rule from then on.
        task = asyncio.create_task(call(client, "type_text", tab=tab, ref=ref(out, "textbox", "Comment"),
                                        text="hello"))
        item, _ = await asked(stack, task, "allow_always")
        assert item["browser"]["kind"] == "interact"
        agent = await stack.agent("gw")
        assert agent["definition"]["browser"]["rules"] == {"127.0.0.1": {"interact": "allow"}}
        out = await ok(client, "type_text", tab=tab, ref=ref(out, "textbox", "Comment"), text="hello again")
        assert await stack.pending() == []

        # Submitting the form is a commit: the hour's grant covers it.
        out = await ok(client, "click", tab=tab, ref=ref(out, "button", "Send"))
        assert "Sent: hello again" in out and await stack.pending() == []
    rows = await audit(stack, agent="gw")
    send = next(a for a in rows if a["action"] == "click" and a["target"] == "Send")
    assert (send["kind"], send["decision"], send["outcome"]) == ("commit", "allow", "ok")
    assert send["note"].startswith("Alice allowed it for an hour") and send["shot"]       # the page after the commit


async def test_live_view(chrome_stack, site):
    stack = chrome_stack
    await setup(stack, "ask")
    url = f"ws://127.0.0.1:{stack.ccfg.port}/ws/browser/alice?token={stack.owner_token}"
    frames, messages = [], []

    def message(type_, after: int = 0, **match):
        return next((m for m in messages[after:] if m["type"] == type_
                     and all(m.get(k) == v for k, v in match.items())), None)

    def requested(path: str):
        return [form for _, p, form in site.log if p == path] or None

    async with connect(url, max_size=None) as ws:
        async def read():
            async for raw in ws:
                if isinstance(raw, bytes):
                    frames.append(raw)
                else:
                    messages.append(json.loads(raw))
        reader = asyncio.create_task(read())

        async def send(**m):
            await ws.send(json.dumps(m))

        await eventually(lambda: frames, timeout=20, what="a frame")
        assert frames[0][:2] == b"\xff\xd8"
        await eventually(lambda: message("tabs") and message("busy", busy=False), what="the first messages")

        # A tap lands where it's aimed: the middle of a page that is one big button.
        await send(type="nav", url=site.url + "/big")
        await eventually(lambda: message("page", title="Big button"), timeout=20, what="the page")
        await send(type="tap", x=0.5, y=0.5)
        await eventually(lambda: requested("/tapped"), timeout=20, what="the tap")

        # Keys and text: Tab to the comment field, type, Enter submits the form.
        await send(type="nav", url=site.url + "/")
        await eventually(lambda: message("page", title="Test shop"), timeout=20, what="the page")
        for _ in range(3):              # the link, the Save button, the comment field
            await send(type="key", key="Tab")
        await send(type="text", text="from the live view")
        await send(type="key", key="Enter")
        await eventually(lambda: requested("/sent"), timeout=20, what="the form")
        assert requested("/sent") == [{"comment": "from the live view"}]
        await eventually(lambda: message("page", title="Sent"), timeout=20, what="the result")

        n = len(messages)
        await send(type="back")
        await eventually(lambda: message("page", after=n, title="Test shop"), timeout=20, what="back")

        # A new tab: listed, and streamed instead.
        n, before = len(messages), len(frames)
        await send(type="open", url=site.url + "/next")
        tabs = await eventually(lambda: message("tabs", after=n), timeout=20, what="two tabs")
        assert len(tabs["tabs"]) == 2 and tabs["tab"] == tabs["tabs"][0]["tab"]
        await eventually(lambda: message("page", after=n, title="Next page"), timeout=20, what="the new tab")
        await eventually(lambda: len(frames) > before, timeout=20, what="frames of the new tab")

        # The viewer's size: the page is laid out at it, frames come in its shape, and a tap still lands where
        # it's aimed (a 40 px button in the bottom right corner).
        await send(type="viewport", width=400, height=700)
        await send(type="nav", url=site.url + "/size")
        await eventually(lambda: {"w": "400", "h": "700"} in (requested("/sized") or []), timeout=20,
                         what="the page at the viewer's size")
        await eventually(lambda: jpeg_size(frames[-1]) == (400, 700), timeout=20, what="frames of that size")
        await send(type="tap", x=380 / 400, y=680 / 700)
        await eventually(lambda: requested("/corner"), timeout=20, what="the corner tap")
        # At a Retina viewer's scale: the same page size, a tap still where it's aimed. (Frames are twice as sharp
        # in a headed Chrome, as in production; a headless one sends them at CSS pixels whatever the scale.)
        corner, n = len(requested("/corner")), len(frames)
        await send(type="viewport", width=400, height=700, scale=2)
        await send(type="reload")
        await eventually(lambda: len(frames) > n and jpeg_size(frames[-1]) in ((400, 700), (800, 1400)), timeout=20,
                         what="frames at scale 2")
        assert requested("/sized")[-1] == {"w": "400", "h": "700"}
        await send(type="tap", x=380 / 400, y=680 / 700)
        await eventually(lambda: len(requested("/corner")) > corner, timeout=20, what="the corner tap at scale 2")
        reader.cancel()
    # Nobody watching: the page has Chrome's own size again.
    await eventually(lambda: requested("/sized")[-1]["w"] == "1280", timeout=20, what="the size given back")
    rows = [a["action"] for a in await audit(stack)]
    assert rows[:5] == ["live_navigate", "live_open", "live_navigate", "live_navigate", "live_view"]
