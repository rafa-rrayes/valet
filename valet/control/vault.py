"""The vault: saved logins for the browser's profiles. Each entry has a unique name (`github/alice`), the profile it
signs in and the site it's for, a username, and its secrets: a password and a TOTP secret (base32, or the
otpauth:// URI a QR code holds). `login()` fills them into the page and nowhere else; an agent whose definition
lists `- vault: <entry>` gets the password as an env var (secrets.py). No route or tool returns a value.

Encrypted like Valet's secrets (secrets.py): AES-256-GCM with the control plane's
master key, a fresh 12-byte nonce, the entry's name as associated data (`vault:<name>`, distinct from a secret's
`secret:<NAME>`), so a ciphertext can't be moved to another entry or into the secrets table."""
import base64
import binascii
import json
import os
import time
from collections.abc import Callable
from typing import Any

import pyotp
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from valet.shared.browser import matches, normalize_pattern, profile_name_error, specificity
from valet.shared.definition import vault_name_error

from .db import Database

NONCE_BYTES = 12
MAX_PASSWORD = 4096
MIN_REDACT = 4          # shorter passwords aren't masked in page text (they'd mask ordinary words)
FIELDS = ("name", "profile", "site", "label", "username")


class VaultError(Exception):
    """An entry that can't be stored or opened. str() is shown to the owner; it never repeats a value."""


def clean_totp(secret: str | None) -> str | None:
    """A base32 secret (spaces and case don't matter) or an otpauth:// URI -> the base32 secret, or, when the URI
    asks for codes other than 6 digits every 30 seconds with SHA1, `otpauth://totp/?secret=...` keeping its digits,
    period and algorithm; None for none. VaultError if it's neither."""
    if not secret or not secret.strip():
        return None
    secret = secret.strip()
    params = ""
    try:
        if secret.lower().startswith("otpauth://"):
            otp = pyotp.parse_uri(secret)
            if type(otp) is not pyotp.TOTP:         # HOTP (a counter), Steam's codes: not what login() types
                raise VaultError("the 2FA link must be a time-based one (otpauth://totp/...)")
            algorithm = otp.digest().name.upper()
            if (otp.digits, otp.interval, algorithm) != (6, 30, "SHA1"):
                params = f"&digits={otp.digits}&period={otp.interval}&algorithm={algorithm}"
            secret = otp.secret
        secret = secret.replace(" ", "").replace("-", "").upper()
        base64.b32decode(secret + "=" * (-len(secret) % 8))
    except (ValueError, binascii.Error):
        raise VaultError("the 2FA secret must be the base32 key or the otpauth:// link of its QR code") from None
    if not secret:
        raise VaultError("the 2FA secret is empty")
    return f"otpauth://totp/?secret={secret}{params}" if params else secret


def totp_seed(totp: str) -> str:
    """A clean_totp() value's base32 secret."""
    return pyotp.parse_uri(totp).secret if totp.startswith("otpauth://") else totp


def totp_code(secret: str) -> str:
    """The current code of a clean_totp() value."""
    return (pyotp.parse_uri(secret) if secret.startswith("otpauth://") else pyotp.TOTP(secret)).now()


def clean_site(site: str) -> str:
    """What people type or paste ("https://www.instagram.com/accounts/login/") -> "instagram.com/accounts/login".
    VaultError if it isn't a site."""
    try:
        p = normalize_pattern(site)
    except ValueError as e:
        raise VaultError(str(e)) from None
    return p[4:] if p.startswith("www.") else p


def covers(site: str, url: str) -> bool:
    """A login saved for a domain also covers its subdomains (instagram.com -> www.instagram.com), like a password
    manager, with or without a path prefix (clean_site drops "www.", so instagram.com/accounts/login must cover
    www.instagram.com/accounts/login); a host with wildcards matches exactly as written."""
    host, sep, path = site.partition("/")
    return matches((host if "*" in host or "?" in host else "*." + host) + sep + path, url)


def _password_error(value: object) -> str | None:
    if not isinstance(value, str):
        return "the password must be text"
    if "\0" in value:
        return "the password can't contain a NUL character"
    if len(value) > MAX_PASSWORD:
        return f"the password is longer than {MAX_PASSWORD} characters"
    try:
        value.encode()
    except UnicodeEncodeError:
        return "the password isn't valid text"
    return None


def _line(value: object, what: str, limit: int) -> str:
    if not isinstance(value, str):
        raise VaultError(f"the {what} must be text")
    value = value.strip()
    if len(value) > limit or any(ord(c) < 32 for c in value):
        raise VaultError(f"the {what}: one line, at most {limit} characters")
    return value


class Vault:
    """The `vault` table. Only open() decrypts; everything the API shows comes from public()."""

    def __init__(self, db: Database, cipher: Callable[[], AESGCM]) -> None:
        """cipher: the secrets store's AES-GCM (the master key, loaded on first use)."""
        self.db = db
        self._cipher = cipher

    # ---------- crypto ----------
    def _seal(self, name: str, data: dict[str, Any]) -> bytes:
        nonce = os.urandom(NONCE_BYTES)
        return nonce + self._cipher().encrypt(nonce, json.dumps(data).encode(), f"vault:{name}".encode())

    def open(self, row: dict) -> dict[str, Any]:
        """{"password": str, "totp": str|None} of an entry's row. VaultError if it doesn't decrypt."""
        blob = row["secret"]
        try:
            return json.loads(self._cipher().decrypt(blob[:NONCE_BYTES], blob[NONCE_BYTES:],
                                                     f"vault:{row['name']}".encode()))
        except InvalidTag:
            raise VaultError(f"vault entry {row['name']} doesn't decrypt with this master key: set it again") from None

    # ---------- reading ----------
    def row(self, name: str) -> dict | None:
        return self.db.one("SELECT * FROM vault WHERE name = ?", (name,))

    def public(self, row: dict) -> dict[str, Any]:
        """What the owner's apps may show: never the password or the TOTP secret. An entry that doesn't decrypt
        is still listed (has_password and has_totp null, `error` saying so), so the others show and it can be set
        again or deleted."""
        try:
            s, error = self.open(row), None
        except VaultError as e:
            s, error = None, str(e)
        out = {"name": row["name"], "profile": row["profile"], "site": row["site"], "label": row["label"],
               "username": row["username"], "has_password": bool(s.get("password")) if s else None,
               "has_totp": bool(s.get("totp")) if s else None,
               "created_at": row["created_at"], "updated_at": row["updated_at"], "used_at": row["used_at"]}
        return {**out, "error": error} if error else out

    def listing(self, profile: str | None = None) -> list[dict[str, Any]]:
        if profile is None:
            rows = self.db.query("SELECT * FROM vault ORDER BY name")
        else:
            rows = self.db.query("SELECT * FROM vault WHERE profile = ? ORDER BY name", (profile,))
        return [self.public(r) for r in rows]

    def count(self, profile: str) -> int:
        return self.db.one("SELECT COUNT(*) AS n FROM vault WHERE profile = ?", (profile,))["n"]

    def stamps(self, names: list[str]) -> dict[str, float | None]:
        """{name: when its password last changed, or None if there's no such entry, or it has no password an agent
        could get (or doesn't open)}: what restart awareness compares, and what missing_secrets lists."""
        if not names:
            return {}
        marks = ",".join("?" * len(names))
        rows = self.db.query(f"SELECT * FROM vault WHERE name IN ({marks})", tuple(names))
        have = {}
        for r in rows:
            try:
                have[r["name"]] = r["password_at"] if self.open(r).get("password") else None
            except VaultError:
                have[r["name"]] = None
        return {n: have.get(n) for n in names}

    def find(self, profile: str, url: str, name: str | None = None) -> dict | None:
        """The entry login() uses on `url` in `profile`: `name`'s if given (and it covers the URL), else the most
        specific site that covers it (ties: the first by name)."""
        rows = self.db.query("SELECT * FROM vault WHERE profile = ? ORDER BY name", (profile,))
        rows = [r for r in rows if covers(r["site"], url) and (name is None or r["name"] == name)]
        if not rows:
            return None
        return max(rows, key=lambda r: specificity(r["site"]))      # max keeps the first of equals

    def sites(self, profile: str) -> list[str]:
        """The sites with saved logins in a profile (what an agent may know: login() works there)."""
        return sorted({r["site"] for r in self.db.query("SELECT site FROM vault WHERE profile = ?", (profile,))})

    def reveal(self, row: dict) -> dict[str, Any]:
        """Internal only (login()): {username, password, totp} and marks it used. VaultError if it can't open."""
        s = self.open(row)
        self.db.execute("UPDATE vault SET used_at = ? WHERE name = ?", (time.time(), row["name"]))
        return {"username": row["username"] or "", "password": s.get("password") or "", "totp": s.get("totp")}

    def secrets(self) -> list[str]:
        """Every password (of MIN_REDACT characters or more) and TOTP secret in the vault, longest first: what page
        text is masked with, and what type_text refuses to type. Entries that don't decrypt are skipped."""
        out: set[str] = set()
        for r in self.db.query("SELECT * FROM vault"):
            try:
                s = self.open(r)
            except VaultError:
                continue
            if s.get("password") and len(s["password"]) >= MIN_REDACT:
                out.add(s["password"])
            if s.get("totp"):
                out.add(totp_seed(s["totp"]))
        return sorted(out, key=len, reverse=True)

    def passwords(self, names: list[str]) -> tuple[dict[str, str], list[str]]:
        """({name: password}, problems) for entries given to an agent as env vars; a problem names an entry that
        doesn't exist, has no password, or doesn't open."""
        values, problems = {}, []
        for n in names:
            row = self.row(n)
            if row is None:
                problems.append(f"vault entry {n} doesn't exist: add it to Valet's vault")
                continue
            try:
                pw = self.open(row).get("password")
            except VaultError as e:
                problems.append(str(e))
                continue
            if not pw:
                problems.append(f"vault entry {n} has no password")
                continue
            values[n] = pw
        return values, problems

    # ---------- writing ----------
    def put(self, name: str, *, profile: str | None = None, site: str | None = None, label: str | None = None,
            username: str | None = None, password: str | None = None, totp: str | None = None,
            create: bool) -> dict[str, Any]:
        """Create (create=True: profile and site required) or update an entry; a field left None stays as it is
        (password or totp "" clears it). Returns its public view. VaultError for bad input, FileExistsError /
        FileNotFoundError for create / update of a name that is / isn't there. The caller checks the profile
        exists in browser.yaml."""
        if err := vault_name_error(name):
            raise VaultError(f"name: {err}")
        old = self.row(name)
        if create and old is not None:
            raise FileExistsError(name)
        if not create and old is None:
            raise FileNotFoundError(name)
        if create and (profile is None or site is None):
            raise VaultError("a new entry needs a profile and a site")
        if profile is not None and (err := profile_name_error(profile)):
            raise VaultError(f"profile: {err}")
        fields = dict(old or {})
        if profile is not None:
            fields["profile"] = profile
        if site is not None:
            fields["site"] = clean_site(site)
        if label is not None:
            fields["label"] = _line(label, "label", 80) or None
        if username is not None:
            fields["username"] = _line(username, "username", 200) or None
        try:
            secret = self.open(old) if old else {"password": "", "totp": None}
        except VaultError:
            if password is None:        # what it held can't be kept: setting it again means giving the password
                raise
            secret = {"password": "", "totp": None}
        now = time.time()
        password_at = (old or {}).get("password_at") or now
        if password is not None:
            if err := _password_error(password):
                raise VaultError(err)
            if password != (secret.get("password") or ""):
                password_at = now
            secret["password"] = password
        if totp is not None:
            secret["totp"] = clean_totp(totp)
        self.db.execute(
            "INSERT INTO vault (name, profile, site, label, username, secret, created_at, updated_at, password_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (name) DO UPDATE SET profile = excluded.profile, "
            "site = excluded.site, label = excluded.label, username = excluded.username, secret = excluded.secret, "
            "updated_at = excluded.updated_at, password_at = excluded.password_at",
            (name, fields["profile"], fields["site"], fields.get("label"), fields.get("username"),
             self._seal(name, {"password": secret.get("password") or "", "totp": secret.get("totp")}),
             (old or {}).get("created_at") or now, now, password_at))
        return self.public(self.row(name))

    def delete(self, name: str) -> bool:
        return bool(self.db.execute("DELETE FROM vault WHERE name = ?", (name,)).rowcount)
