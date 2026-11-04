"""Telling the owner that something waits in the inbox: a push to the native apps (APNs) and a POST to the owner's
webhook, for every new pending inbox item, whatever asked (Hub.item_created). Best effort: nothing here raises into
its caller, and the hub runs it in the background, so a slow Apple or webhook never holds up an agent.

APNs: token auth with the owner's Apple key (VALET_APNS_KEY_FILE, a .p8;
VALET_APNS_KEY_ID; VALET_APNS_TEAM_ID), an ES256 JWT made here with `cryptography` and renewed every 40 minutes, over
HTTP/2 (`httpx[http2]`, one connection kept open, as Apple asks). Every device registered through /api/devices
(table devices) gets the alert, category `VALET_INBOX`, time-sensitive, with `item_id`, `agent` and `kind` beside
`aps`. A token Apple calls dead (410, or 400 BadDeviceToken) is removed. Off until the three settings are there.

The webhook (ported from the old notify.py): one http(s) URL the owner sets (settings table, `webhook_url`). The JSON
body has `event`, a ready-made line in both `text` and `content` (so Slack and Discord incoming webhooks take it as
is), the item's fields and a link to the inbox, `<VALET_PUBLIC_URL>/#/inbox`. A tool call's arguments aren't sent.
The URL is never logged: incoming-webhook URLs carry their own credential."""
import asyncio
import base64
import json
import logging
import re
import time
import unicodedata
from typing import Any
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from valet.shared.definition import DefinitionError

log = logging.getLogger(__name__)

APNS_PRODUCTION = "https://api.push.apple.com"
APNS_SANDBOX = "https://api.sandbox.push.apple.com"
CATEGORY = "VALET_INBOX"
JWT_TTL = 40 * 60           # Apple refuses a provider token older than an hour, and one renewed more than every 20 min
PUSH_TIMEOUT = 15.0
WEBHOOK_TIMEOUT = 10.0
WEBHOOK_SETTING = "webhook_url"
URL_LIMIT = 2000
TOKEN_RE = re.compile(r"^[0-9a-f]{32,200}$")    # an APNs device token, as hex
PLATFORMS = {"ios": "iPhone", "macos": "Mac"}   # platform -> the name a device gets when it gives none
NAME_LIMIT = 80
ASKS = {"permission", "tool", "browser"}        # kinds that wait for an answer: "needs your OK"


# ---------- what an item says ----------

def _clip(text: str, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 1] + "…"


def describe(item: dict[str, Any], who: str) -> tuple[str, str]:
    """(title, body) of the push for an InboxItem, `who` being its agent's display name. A kind this doesn't know
    (one a later milestone adds) says its title."""
    kind, title = item.get("kind"), item.get("title") or ""
    if kind == "tool" and item.get("tool"):
        t = item["tool"]
        body = f"{t.get('server') or ''} › {t.get('title') or t.get('name') or ''}"
    else:
        body = title or "Something is waiting for you"
    head = f"{who} needs your OK" if kind in ASKS else f"{who} asks" if kind == "question" else who
    return _clip(head, 100), _clip(body, 240)


def line(item: dict[str, Any], who: str, link: str) -> str:
    """The webhook's one line, for Slack and Discord."""
    kind = item.get("kind")
    if kind == "tool" and item.get("tool"):
        t = item["tool"]
        what = f"wants to use {t.get('server')} › {t.get('title') or t.get('name')}"
    elif kind in ASKS or kind == "question":
        what = f"asks: {_clip(item.get('title') or '', 200)}"
    elif kind == "message":
        what = f"says: {_clip(item.get('title') or '', 200)}"
    else:
        what = _clip(item.get("title") or "", 200)
    mark = "🔐" if kind in ASKS else "🔔"
    stop = "" if what.endswith((".", "?", "!", "…")) else "."
    return f"{mark} Valet: {who} {what}{stop} {'See it' if kind == 'message' else 'Answer'} in the inbox: {link}"


def shared_line(item: dict[str, Any], who: str, link: str) -> str:
    """The webhook's line for something an agent added to the shared space."""
    what = "a note" if item.get("kind") == "note" else "a file"
    return f"📎 Valet: {who} added {what} to Shared: {_clip(item.get('title') or '', 200)}. Open it: {link}"


def item_fields(item: dict[str, Any]) -> dict[str, Any]:
    """What the webhook carries of an InboxItem: everything but the tool call's input and arguments."""
    out = {"item_id": item["id"], "agent": item["agent"], "kind": item["kind"], "title": item.get("title"),
           "session_id": item.get("session_id"), "created_at": item.get("created_at"),
           "options": [{"optionId": o.get("optionId"), "name": o.get("name")} for o in item.get("options") or []]}
    if item.get("tool"):
        t = item["tool"]
        out["tool"] = {"server": t.get("server"), "name": t.get("name"), "title": t.get("title")}
    return out


# ---------- the webhook URL ----------

def webhook_url_error(url: Any) -> str | None:
    """Why `url` can't be the owner's webhook, or None if it can: an http(s) URL with a host, one line, at most
    2000 characters."""
    if not isinstance(url, str) or not url:
        return "a URL like https://hooks.slack.com/services/..."
    if len(url) > URL_LIMIT or any(c.isspace() or unicodedata.category(c) == "Cc" for c in url):
        return f"one URL, no spaces, at most {URL_LIMIT} characters"
    try:
        parts = urlsplit(url)
        parts.port      # noqa: B018 (raises on a bad port)
    except ValueError:
        return "that isn't a URL"
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "an http:// or https:// URL"
    return None


def _host(url: str) -> str:
    """Where a webhook goes, for logs and messages: its host, never the rest (the path holds its credential)."""
    try:
        return urlsplit(url).hostname or "the webhook"
    except ValueError:
        return "the webhook"


# ---------- APNs ----------

def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def es256_jwt(key: ec.EllipticCurvePrivateKey, key_id: str, team_id: str, issued: int) -> str:
    """APNs' provider token: header {alg: ES256, kid}, claims {iss: team, iat}, signed with the P-256 key; the
    signature is r || s, 32 bytes each (JWS), not DER."""
    head = _b64(json.dumps({"alg": "ES256", "kid": key_id}, separators=(",", ":")).encode())
    claims = _b64(json.dumps({"iss": team_id, "iat": issued}, separators=(",", ":")).encode())
    signing = f"{head}.{claims}".encode()
    r, s = decode_dss_signature(key.sign(signing, ec.ECDSA(hashes.SHA256())))
    return f"{head}.{claims}.{_b64(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


def push_payload(item: dict[str, Any], title: str, body: str) -> dict[str, Any]:
    return {"aps": {"alert": {"title": title, "body": body}, "sound": "default", "category": CATEGORY,
                    "interruption-level": "time-sensitive", "thread-id": item["agent"],
                    "mutable-content": 1},     # the apps' service extension picks the buttons by kind
            "item_id": item["id"], "agent": item["agent"], "kind": item["kind"]}


class Apns:
    """One HTTP/2 client to APNs, made on first use. `transport`: an httpx transport for tests."""

    def __init__(self, cfg, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.cfg = cfg
        self.transport = transport
        self._client: httpx.AsyncClient | None = None
        self._jwt: tuple[float, str] | None = None
        self._key: ec.EllipticCurvePrivateKey | None = None

    @property
    def configured(self) -> bool:
        return bool(self.cfg.apns_key_file and self.cfg.apns_key_id and self.cfg.apns_team_id)

    @property
    def base(self) -> str:
        return self.cfg.apns_url or (APNS_SANDBOX if self.cfg.apns_sandbox else APNS_PRODUCTION)

    def key_error(self) -> str | None:
        """Why push can't be sent with the configured key, or None (not configured is no error)."""
        if not self.configured:
            return None
        try:
            self._load_key()
        except (OSError, ValueError, TypeError) as e:
            return f"VALET_APNS_KEY_FILE: {e.strerror if isinstance(e, OSError) else 'not a P-256 .p8 key'}"
        return None

    def _load_key(self) -> ec.EllipticCurvePrivateKey:
        if self._key is None:
            key = serialization.load_pem_private_key(self.cfg.apns_key_file.read_bytes(), password=None)
            if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
                raise ValueError("not a P-256 key")
            self._key = key
        return self._key

    def token(self) -> str:
        now = time.time()
        if self._jwt is None or now - self._jwt[0] >= JWT_TTL:
            self._jwt = (now, es256_jwt(self._load_key(), self.cfg.apns_key_id, self.cfg.apns_team_id, int(now)))
        return self._jwt[1]

    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            # APNs speaks only HTTP/2: prior knowledge on http:// (tests), ALPN h2 on https://.
            self._client = httpx.AsyncClient(http1=False, http2=True, timeout=PUSH_TIMEOUT, transport=self.transport)
        return self._client

    async def send(self, device_token: str, payload: dict[str, Any], collapse_id: str | None = None) -> tuple[int, str]:
        """(status, Apple's reason or "") for one push."""
        headers = {"authorization": "bearer " + self.token(), "apns-topic": self.cfg.apns_topic,
                   "apns-push-type": "alert", "apns-priority": "10"}
        if collapse_id and len(collapse_id.encode()) <= 64:
            headers["apns-collapse-id"] = collapse_id
        r = await self.client().post(f"{self.base}/3/device/{device_token}", json=payload, headers=headers)
        reason = ""
        if r.status_code != 200:
            try:
                reason = str(r.json().get("reason") or "")
            except ValueError:
                reason = r.text[:200]
        if r.status_code == 403 and reason in ("ExpiredProviderToken", "InvalidProviderToken"):
            self._jwt = None        # made again for the next push
        return r.status_code, reason

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


# ---------- devices ----------

def device_view(row: dict) -> dict[str, Any]:
    """A devices row as the API shows it: never the token."""
    return {"id": row["id"], "platform": row["platform"], "name": row["name"], "created_at": row["created_at"],
            "last_push": row["last_push"]}


def devices(db) -> list[dict[str, Any]]:
    return [device_view(r) for r in db.query("SELECT * FROM devices ORDER BY id")]


def register(db, token: Any, platform: Any, name: Any = None) -> dict[str, Any]:
    """Add a device, or update the one with this token (same id). ValueError saying what's wrong."""
    token = str(token or "").strip().lower() if isinstance(token, str) else ""
    if not TOKEN_RE.match(token):
        raise ValueError("token: the APNs device token, as hex")
    if platform not in PLATFORMS:
        raise ValueError("platform: ios or macos")
    name = " ".join(str(name).split()) if isinstance(name, str) else ""
    name = "".join(c for c in name if unicodedata.category(c) not in ("Cc", "Zl", "Zp"))[:NAME_LIMIT].strip()
    name = name or PLATFORMS[platform]
    db.execute("INSERT INTO devices (token, platform, name, created_at) VALUES (?, ?, ?, ?) ON CONFLICT (token) "
               "DO UPDATE SET platform = excluded.platform, name = excluded.name", (token, platform, name, time.time()))
    return device_view(db.one("SELECT * FROM devices WHERE token = ?", (token,)))


def remove(db, device_id: int) -> bool:
    return bool(db.execute("DELETE FROM devices WHERE id = ?", (device_id,)).rowcount)


# ---------- the notifier ----------

class Notifier:
    """control: the app's Control (cfg, db, defs). `apns_transport` / `webhook_transport`: httpx transports for
    tests."""

    def __init__(self, control: Any, apns_transport: httpx.AsyncBaseTransport | None = None,
                 webhook_transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.control = control
        self.apns = Apns(control.cfg, apns_transport)
        self.webhook_transport = webhook_transport
        self._warned: set[str] = set()

    @property
    def db(self):
        return self.control.db

    @property
    def link(self) -> str:
        return f"{self.control.cfg.public_url}/#/inbox"

    def webhook_url(self) -> str | None:
        return self.db.get_setting(WEBHOOK_SETTING) or None

    def set_webhook_url(self, url: str | None) -> None:
        """ValueError if it isn't one (webhook_url_error); None or "" removes it."""
        if not url:
            self.db.execute("DELETE FROM settings WHERE key = ?", (WEBHOOK_SETTING,))
            return
        if err := webhook_url_error(url):
            raise ValueError(err)
        self.db.set_setting(WEBHOOK_SETTING, url)

    def settings(self) -> dict[str, Any]:
        """GET /api/notifications."""
        return {"webhook_url": self.webhook_url(),
                "push": {"configured": self.apns.configured, "error": self.apns.key_error(),
                         "topic": self.control.cfg.apns_topic, "sandbox": self.control.cfg.apns_sandbox,
                         "devices": len(devices(self.db))}}

    def _who(self, agent: str) -> str:
        try:
            return self.control.defs.definition(agent).display_name or agent
        except (DefinitionError, FileNotFoundError, ValueError):
            return agent

    # ---- sending ----
    async def item_created(self, item: dict[str, Any]) -> None:
        """A new pending InboxItem: push it and call the webhook, both at once. Never raises."""
        try:
            who = self._who(item["agent"])
            title, body = describe(item, who)
            payload = push_payload(item, title, body)
            body_json = {"event": "inbox", **_text(line(item, who, self.link)), **item_fields(item), "url": self.link}
            await asyncio.gather(self.push(payload, collapse_id=item["id"]), self.webhook(body_json))
        except Exception:
            log.exception("notifying inbox item %s failed", item.get("id"))

    async def shared_added(self, item: dict[str, Any]) -> None:
        """An agent added a note or a file to the shared space (space.py): a push (no answers to offer, so not
        time-sensitive and no category) and the webhook. Never the note's text or the file's bytes. Never raises."""
        try:
            who = self._who(item["added_by"])
            link = f"{self.control.cfg.public_url}/#/shared/{item['id']}"
            payload = {"aps": {"alert": {"title": _clip(f"{who} added to Shared", 100),
                                         "body": _clip(item.get("title") or "", 240)},
                               "sound": "default", "thread-id": "shared"},
                       "kind": "shared", "shared_id": item["id"], "agent": item["added_by"]}
            body_json = {"event": "shared", **_text(shared_line(item, who, link)), "shared_id": item["id"],
                         "kind": item["kind"], "title": item.get("title"), "added_by": item["added_by"], "url": link}
            await asyncio.gather(self.push(payload, collapse_id=f"shared-{item['id']}"), self.webhook(body_json))
        except Exception:
            log.exception("notifying shared item %s failed", item.get("id"))

    async def push(self, payload: dict[str, Any], collapse_id: str | None = None) -> dict[str, Any] | None:
        """To every device: {"sent", "failed", "removed", "error"}, or None when APNs isn't configured. Never
        raises."""
        if not self.apns.configured:
            return None
        out = {"sent": 0, "failed": 0, "removed": 0, "error": None}
        rows = self.db.query("SELECT * FROM devices ORDER BY id")
        if not rows:
            return out
        if err := self.apns.key_error():
            self._warn_once(err)
            return {**out, "failed": len(rows), "error": err}
        for d in rows:
            try:
                status, reason = await self.apns.send(d["token"], payload, collapse_id)
            except Exception as e:
                out["failed"] += 1
                out["error"] = f"can't reach APNs: {type(e).__name__}"
                log.warning("push to %s failed: %s", d["name"], type(e).__name__)
                continue
            if status == 200:
                out["sent"] += 1
                self.db.execute("UPDATE devices SET last_push = ? WHERE id = ?", (time.time(), d["id"]))
            elif status == 410 or reason == "BadDeviceToken":
                out["removed"] += 1
                remove(self.db, d["id"])
                log.info("push: APNs says %s's token is dead (%s); removed it", d["name"], reason or status)
            else:
                out["failed"] += 1
                out["error"] = f"APNs answered {status}" + (f" {reason}" if reason else "")
                log.warning("push to %s: APNs answered %s %s", d["name"], status, reason)
        return out

    async def webhook(self, body: dict[str, Any], url: str | None = None) -> dict[str, Any] | None:
        """POST `body` to the owner's webhook (or `url`): {"ok", "error"}, or None when none is set. Never raises,
        never logs the URL."""
        url = url or self.webhook_url()
        if not url:
            return None
        why = None
        try:
            async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT, transport=self.webhook_transport) as client:
                r = await client.post(url, json=body)
            if r.status_code >= 300:
                why = f"{_host(url)} answered {r.status_code}"
        except Exception as e:      # unreachable, timed out, a bad URL from an older version...
            why = f"can't reach {_host(url)}: {type(e).__name__}"
        if why:
            log.warning("webhook (%s) failed: %s", body.get("event"), why)
            return {"ok": False, "error": why}
        return {"ok": True, "error": None}

    async def test(self) -> dict[str, Any]:
        """POST /api/notifications/test: a test line to the webhook and a test push to every device.
        ValueError if neither is set up."""
        url = self.webhook_url()
        if not url and not self.apns.configured:
            raise ValueError("Set a webhook URL, or configure push (VALET_APNS_*), first.")
        text = "✅ Valet: notifications reach this webhook."
        payload = {"aps": {"alert": {"title": "Valet", "body": "Notifications reach this device."},
                           "sound": "default"}, "kind": "test"}
        hook, push = await asyncio.gather(self.webhook({"event": "test", **_text(text), "url": self.link}, url),
                                          self.push(payload))
        return {"webhook": hook, "push": push}

    def _warn_once(self, message: str) -> None:
        if message not in self._warned:
            self._warned.add(message)
            log.warning("push is off: %s", message)

    async def close(self) -> None:
        await self.apns.close()


def _text(text: str) -> dict[str, str]:
    """Slack reads `text`, Discord `content`."""
    return {"text": text, "content": text}
