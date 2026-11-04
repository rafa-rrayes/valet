"""Review of the vault and the owner's sockets (m4): each test fails on the code as reviewed and shows one bug."""
import contextlib
import json
import os
import time
from urllib.parse import quote_plus

import pyotp
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from starlette.websockets import WebSocketDisconnect

from tests.browser_fake import FakeBackend, FakeElement, FakePage, FakeWeb
from tests.control.conftest import PASSWORD as OWNER_PASSWORD
from valet.control import auth, notify
from valet.control.app import open_database
from valet.control.browser.actions import Refused
from valet.control.secrets import SecretStore
from valet.control.vault import totp_code

AGENT = ("name: scout\nharness: claude\ncomputer:\n  level: host\npermissions: trusted\n"
         "browser:\n  profiles: [alice]\n")
LOGIN = "https://site.example/login"


def control(client):
    return client.app.state.control


def get_login_form_web(password: str) -> FakeWeb:
    """A login form without method=post (or one submitted natively before its JS hydrates): the browser submits it
    as a GET, so the URL after the login carries the password, form-encoded."""
    after = f"{LOGIN}?user=alice&password={quote_plus(password)}"
    return FakeWeb({LOGIN: FakePage(title="Sign in", user="u", password="p", after_login=after, elements={
        "u": FakeElement("textbox", "Username"), "p": FakeElement("textbox", "Password", type="password",
                                                                  password=True)})})


def setup_agent(client, password: str) -> FakeBackend:
    c = control(client)
    backend = FakeBackend(get_login_form_web(password))
    c.browser.backend = backend
    c.browser.actions.login_pause = 0
    assert client.put("/api/browser/yaml", json={"text": "profiles:\n  alice: {}\n"}).status_code == 200
    assert client.post("/api/agents", json={"name": "scout", "agent_yaml": AGENT}).status_code == 201
    r = client.post("/api/vault", json={"name": "site/alice", "profile": "alice", "site": "site.example",
                                        "username": "alice", "password": password})
    assert r.status_code == 201, r.text
    return backend


def test_a_password_in_the_url_after_login_reaches_the_audit_log_and_the_push(client, pool):
    """login() on a covered site whose form submits as GET puts the vault password in the tab's URL. The agent's
    results redact the URL, but the next action doesn't: its audit row (target = the raw URL, GET
    /api/browser/audit) and, when it asks, the browser item's url/summary/title, which is the APNs push body and the
    webhook's Slack/Discord line. API.md: "no route, frame, event or log carries a password"."""
    pw = "hunter2-correct-Horse"
    setup_agent(client, pw)
    svc = control(client).browser
    caller = svc.caller("scout", "alice")
    out = client.portal.call(svc.actions.open_tab, caller, LOGIN)
    client.portal.call(svc.actions.login, caller, out["tab"])
    client.portal.call(svc.actions.snapshot, caller, out["tab"])
    leaked = [json.dumps(client.get("/api/browser/audit").json())]
    # Reading the site asks now: the ask goes to the inbox, the phone and the webhook.
    yaml = "profiles:\n  alice:\n    sites:\n      site.example: {read: ask}\n"
    assert client.put("/api/browser/yaml", json={"text": yaml}).status_code == 200
    fut = pool.submit(client.portal.call, svc.actions.snapshot, svc.caller("scout", "alice"), out["tab"])
    for _ in range(200):
        if items := client.get("/api/inbox").json():
            break
        time.sleep(0.02)
    [item] = items
    leaked += [json.dumps(item), *notify.describe(item, "Scout"), notify.line(item, "Scout", "https://v/#/inbox")]
    client.post(f"/api/inbox/{item['id']}/answer", json={"option_id": "reject_once"})
    fut.exception(5)
    assert not [text for text in leaked if pw in text]


def test_a_form_encoded_password_in_the_url_is_shown_to_the_agent(client):
    """redact() masks a vault password only as written. In a URL the browser form-encodes it ("p@ss word&1!" ->
    "p%40ss+word%261%21"), so the agent's result after login() (and every later url: line, tabs()) carries the
    password, one unquote away."""
    pw = "p@ss word&1!"
    setup_agent(client, pw)
    svc = control(client).browser
    caller = svc.caller("scout", "alice")
    out = client.portal.call(svc.actions.open_tab, caller, LOGIN)
    result = client.portal.call(svc.actions.login, caller, out["tab"])
    assert quote_plus(pw) not in json.dumps(result, ensure_ascii=False)


def _rekey(client):
    """The master key changed under the vault (the key file was lost and load_key made a new one, or
    VALET_MASTER_KEY changed): what was sealed before no longer opens."""
    control(client).secrets._cipher = AESGCM(os.urandom(32))


def test_an_entry_that_doesnt_decrypt_cant_be_set_again(client):
    """VaultError says "doesn't decrypt with this master key: set it again", but PUT /api/vault/<name> with a new
    password and 2FA secret is a 500 (_entry_or_404 opens the old ciphertext first, and Vault.put opens it again):
    unlike a secret (PUT /api/secrets/X overwrites), an entry can only be deleted and re-created."""
    assert client.put("/api/browser/yaml", json={"text": "profiles:\n  alice: {}\n"}).status_code == 200
    r = client.post("/api/vault", json={"name": "github/alice", "profile": "alice", "site": "github.com",
                                        "password": "old-password-1"})
    assert r.status_code == 201
    _rekey(client)
    r = client.put("/api/vault/github/alice", json={"password": "new-password-2", "totp": ""})
    assert r.status_code == 200, r.text


def test_one_entry_that_doesnt_decrypt_hides_the_whole_vault(client):
    """GET /api/vault opens every entry for has_password/has_totp; one that doesn't decrypt makes the whole
    listing a 500, so the vault screen shows none of the entries (not even the ones that are fine)."""
    assert client.put("/api/browser/yaml", json={"text": "profiles:\n  alice: {}\n"}).status_code == 200
    assert client.post("/api/vault", json={"name": "old/alice", "profile": "alice", "site": "old.example",
                                           "password": "old-password-1"}).status_code == 201
    _rekey(client)
    assert client.post("/api/vault", json={"name": "new/alice", "profile": "alice", "site": "new.example",
                                           "password": "new-password-2"}).status_code == 201
    r = client.get("/api/vault")
    assert r.status_code == 200, r.text
    assert "new/alice" in [e["name"] for e in r.json()]


def test_a_login_url_pasted_with_www_doesnt_cover_itself(cfg):
    """clean_site drops "www." (so a bare domain covers its subdomains), but a site with a path is matched exactly
    as written (covers): pasting the login page, "https://www.instagram.com/accounts/login/" (clean_site's own
    example), stores instagram.com/accounts/login, which doesn't cover www.instagram.com, so login() there says
    there's no saved login."""
    vault = SecretStore(open_database(cfg), cfg).vault
    url = "https://www.instagram.com/accounts/login/"
    vault.put("instagram/alice", profile="alice", site=url, username="alice", password="hunter2-pw", create=True)
    assert vault.find("alice", url) is not None


def test_an_otpauth_link_keeps_its_digits_period_and_algorithm(cfg):
    """The 2FA secret may be "the otpauth:// URI a QR code holds", but clean_totp keeps only its secret: a QR code
    with digits=8 (or period=60, algorithm=SHA256) gives 6-digit SHA1/30s codes, which the site refuses."""
    vault = SecretStore(open_database(cfg), cfg).vault
    uri = "otpauth://totp/Acme:alice?secret=JBSWY3DPEHPK3PXP&issuer=Acme&digits=8"
    vault.put("acme/alice", profile="alice", site="acme.example", password="hunter2-pw", totp=uri, create=True)
    seed = vault.reveal(vault.row("acme/alice"))["totp"]
    assert len(totp_code(seed)) == len(pyotp.parse_uri(uri).now()) == 8


class _Field:
    """A visible, enabled input of a fake Playwright frame: what ChromeTab.login_step does to it, in order."""

    def __init__(self, kind: str, log: list) -> None:
        self.kind, self.log = kind, log

    async def is_visible(self):
        return True

    async def is_enabled(self):
        return True

    async def get_attribute(self, name):
        return self.kind if name == "type" else None

    async def input_value(self):
        return ""

    async def evaluate(self, js):
        self.log.append(("evaluate", self.kind, js))

    async def fill(self, value):
        self.log.append(("fill", self.kind))

    async def press(self, key):
        self.log.append(("press", self.kind, key))


class _Locator:
    def __init__(self, fields):
        self.fields = fields

    async def count(self):
        return len(self.fields)

    def nth(self, i):
        return self.fields[i]


async def test_the_password_login_fills_stays_blanked_in_screenshots_after_show_password():
    """A "show password" toggle makes the filled field type=text, which HIDE_PASSWORDS (input[type=password]) no
    longer blanked: a screenshot showed the vault password. login_step marks the field it fills, before filling
    it, and the screenshot's style blanks marked fields whatever their type."""
    from valet.control.browser import chrome
    from valet.control.browser.backend import LoginSecret

    log: list = []
    pw, user = _Field("password", log), _Field("email", log)

    class Frame:
        url = "https://site.example/login"

        def locator(self, sel):
            return _Locator([pw] if sel == "input[type=password]" else [user] if sel == chrome.USER_SEL else [])

    frame = Frame()
    page = type("Page", (), {"frames": [frame], "main_frame": frame})()
    tab = chrome.ChromeTab(None, page)
    steps = await tab.login_step(lambda u: True, LoginSecret("alice", "hunter2-correct-Horse"))
    assert steps == ["username", "password"]
    marks = [i for i, op in enumerate(log) if op[:2] == ("evaluate", "password") and chrome.FILLED in op[2]]
    assert marks and marks[0] < log.index(("fill", "password"))
    selectors = chrome.HIDE_PASSWORDS.split("{")[0].split(",")
    assert f"input[{chrome.FILLED}]" in selectors and "input[type=password]" in selectors


def test_type_text_tells_a_locked_agent_whether_a_guess_is_a_vault_password(client):
    """type_text checks the text against the vault in prepare(), before the rules: an agent that may not interact
    at all (locked) gets "Valet doesn't allow this" for a wrong guess but "That text contains a password saved in
    Valet's vault" for a right one. The text has no length limit, so one call tests thousands of candidates (a
    breach list for Alice's email) and a binary search finds the password in ~log2(N) more."""
    c = control(client)
    c.browser.backend = FakeBackend(FakeWeb({"https://search.example/": FakePage(
        elements={"q": FakeElement("textbox", "Search")})}))
    assert client.put("/api/browser/yaml", json={"text": "profiles:\n  alice: {}\n"}).status_code == 200
    yaml = AGENT.replace("permissions: trusted", "permissions: locked")
    assert client.post("/api/agents", json={"name": "scout", "agent_yaml": yaml}).status_code == 201
    assert client.post("/api/vault", json={"name": "github/alice", "profile": "alice", "site": "github.com",
                                           "password": "Summer2024!"}).status_code == 201
    svc = c.browser
    caller = svc.caller("scout", "alice")
    tab = client.portal.call(svc.actions.open_tab, caller, "https://search.example/")["tab"]
    said = []
    for guesses in (["password1", "letmein", "qwerty123"], ["Winter2023!", "Summer2024!"]):
        try:
            client.portal.call(svc.actions.type_text, caller, tab, "q", "\n".join(guesses))
        except Refused as e:
            said.append(str(e))
    assert said[0] == said[1]


def test_an_open_live_view_outlives_a_password_change(client):
    """set_password rotates session_secret, which "signs everyone out", but a live view opened before keeps
    driving Alice's Chrome (with every login in the profile) for as long as it stays open: the session is checked
    only at the handshake, never again (the same holds for /ws/client and its broadcasts; runner sockets, by
    contrast, are rechecked every TOKEN_CHECK seconds)."""
    backend = FakeBackend(FakeWeb())
    control(client).browser.backend = backend
    assert client.put("/api/browser/yaml", json={"text": "profiles:\n  alice: {}\n"}).status_code == 200
    with client.websocket_connect("/ws/browser/alice") as ws:
        ws.receive_text()                       # the first tabs message: it's open
        client.portal.call(auth.set_password, control(client).db, OWNER_PASSWORD + " rotated")
        time.sleep(1.2)                         # a couple of the view's polls
        with contextlib.suppress(WebSocketDisconnect, RuntimeError):
            ws.send_text(json.dumps({"type": "key", "key": "Enter"}))
        time.sleep(0.5)
    assert not [op for op in backend.log if op[0] == "key"]
