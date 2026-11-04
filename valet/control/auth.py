"""Owner auth: one argon2 password; a signed session (itsdangerous, 30 days) carried as an httponly cookie for the
web UI or as `Authorization: Bearer vown_<signed>` for API clients. Runner auth: `Authorization: Bearer vrun_...`
checked against the sha256 in the runners table. Agent auth (the MCP gateway): `Authorization: Bearer vagt_...`,
one token per agent, issued when it starts, its sha256 in agents.token_hash."""
import contextlib
import hashlib
import secrets
import threading
import time
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import HTTPException, Request, WebSocket
from itsdangerous import BadSignature, URLSafeTimedSerializer

from valet.shared.definition import NAME_RE

from .db import Database

COOKIE = "valet_session"
OWNER_PREFIX = "vown_"
RUNNER_PREFIX = "vrun_"
AGENT_PREFIX = "vagt_"
SESSION_DAYS = 30

_ph = PasswordHasher()


def _secret(db: Database) -> str:
    s = db.get_setting("session_secret")
    if not s:
        s = secrets.token_urlsafe(48)
        db.set_setting("session_secret", s)
    return s


def _signer(db: Database) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_secret(db), salt="valet-owner")


def set_password(db: Database, password: str) -> None:
    """Hash and store; rotate session_secret. ValueError under 12 characters."""
    if len(password) < 12:
        raise ValueError("use at least 12 characters")
    db.set_setting("owner_password_hash", _ph.hash(password))
    db.set_setting("session_secret", secrets.token_urlsafe(48))      # signs everyone out


def check_password(db: Database, password: str) -> bool:
    h = db.get_setting("owner_password_hash")
    if not h:
        return False
    try:
        return _ph.verify(h, password)
    except (VerificationError, InvalidHashError):
        return False


def issue_session(db: Database) -> str:
    """A signed owner session (without the vown_ prefix)."""
    return _signer(db).dumps({"owner": True, "t": time.time()})


def is_owner_token(db: Database, token: str | None) -> bool:
    """True for a valid signed session, with or without `Bearer ` / `vown_` prefixes."""
    if not token:
        return False
    token = token.strip().removeprefix("Bearer ").strip().removeprefix(OWNER_PREFIX)
    try:
        data = _signer(db).loads(token, max_age=SESSION_DAYS * 86400)
    except BadSignature:
        return False
    return isinstance(data, dict) and data.get("owner") is True


def _bearer(headers) -> str | None:
    auth = headers.get("authorization") or ""
    return auth if auth.startswith("Bearer " + OWNER_PREFIX) else None


def require_owner(request: Request) -> None:
    """FastAPI dependency for /api routes: cookie or bearer, else 401. A cookie changing state must also come
    from Valet's own pages (require_same_origin)."""
    db = request.app.state.control.db
    if is_owner_token(db, _bearer(request.headers)):
        return
    if not is_owner_token(db, request.cookies.get(COOKIE)):
        raise HTTPException(401, "sign in first")
    require_same_origin(request)


def origin_ok(origin: str | None, public_url: str) -> bool:
    """Native apps send no Origin; a browser must be on Valet itself (SameSite alone admits sibling subdomains,
    which are same-site)."""
    if not origin:
        return True
    pub = urlsplit(public_url)
    return origin == f"{pub.scheme}://{pub.netloc}"


def require_same_origin(request: Request) -> None:
    """CSRF guard for cookie-carrying unsafe requests: browsers send Origin (or at least Sec-Fetch-Site) on
    every POST/PUT/DELETE, so a page elsewhere is refused; a client sending neither isn't a browser."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    origin = request.headers.get("origin")
    if origin:
        ok = origin_ok(origin, request.app.state.control.cfg.public_url)
    else:
        ok = request.headers.get("sec-fetch-site", "same-origin") in ("same-origin", "none")
    if not ok:
        raise HTTPException(403, "cross-origin request refused")


def websocket_is_owner(ws: WebSocket) -> str | None:
    """Cookie, bearer header, or ?token=<vown_...> (browsers can't set headers on WebSockets): the owner's session
    it carries, or None. The socket checks it again while it's open (is_owner_token): a new password or the
    session's age signs it out too."""
    db = ws.app.state.control.db
    candidates = (_bearer(ws.headers), ws.cookies.get(COOKIE), ws.query_params.get("token"))
    return next((t for t in candidates if t and is_owner_token(db, t)), None)


LOOPBACK = ("127.0.0.1", "::1")


def client_ip(request: Request, trust_cf: bool = False) -> str:
    """The caller's address for the login limit. Behind a Cloudflare tunnel (VALET_TRUST_CF_IP) every request
    comes from loopback and the real one is in cf-connecting-ip; anywhere else that header is the client's own
    say-so, so it's ignored."""
    peer = request.client.host if request.client else "?"
    if trust_cf and peer in LOOPBACK:
        return request.headers.get("cf-connecting-ip") or peer
    return peer


class LoginLimiter:
    """Failed-login windows: 5 attempts within 15 minutes lock that IP out until the oldest ages out, and
    GLOBAL_LIMIT across all IPs locks everyone out (so rotating addresses can't buy unlimited guesses).
    An attempt counts from the moment it starts (begin) and is forgiven on success, so parallel requests can't
    all slip past the check while argon2 runs. In memory on purpose: a restart forgives."""
    LIMIT = 5
    GLOBAL_LIMIT = 100
    WINDOW = 15 * 60

    def __init__(self) -> None:
        self._fails: dict[str, list[float]] = {}
        self._all: list[float] = []
        self._lock = threading.Lock()       # login runs in the threadpool

    def _recent(self, ip: str) -> list[float]:
        cutoff = time.time() - self.WINDOW
        self._all = [t for t in self._all if t > cutoff]
        recent = [t for t in self._fails.get(ip, []) if t > cutoff]
        if recent:
            self._fails[ip] = recent
        else:
            self._fails.pop(ip, None)
        return recent

    def begin(self, ip: str) -> float | None:
        """Count an attempt; None if the IP (or everyone) is locked out. Pass the result to succeed()."""
        with self._lock:
            recent = self._recent(ip)
            if len(recent) >= self.LIMIT or len(self._all) >= self.GLOBAL_LIMIT:
                return None
            t = time.time()
            self._fails[ip] = recent + [t]
            self._all.append(t)
            return t

    def succeed(self, ip: str, attempt: float) -> None:
        """The password was right: forgive this IP's failures and this attempt."""
        with self._lock:
            self._fails.pop(ip, None)
            with contextlib.suppress(ValueError):
                self._all.remove(attempt)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def add_runner(db: Database, name: str) -> str:
    """Create or re-key a runner; returns the plaintext vrun_ token."""
    if not NAME_RE.match(name):
        raise ValueError("runner names are lowercase letters, digits and dashes, at most 31")
    token = RUNNER_PREFIX + secrets.token_urlsafe(32)
    db.execute("INSERT INTO runners (name, token_hash, created_at) VALUES (?, ?, ?) "
               "ON CONFLICT (name) DO UPDATE SET token_hash = excluded.token_hash, revoked = 0",
               (name, token_hash(token), time.time()))
    return token


def runner_for_token(db: Database, authorization: str | None) -> str | None:
    """`Bearer vrun_...` -> runner name, or None if missing, unknown or revoked."""
    token = (authorization or "").strip().removeprefix("Bearer ").strip()
    if not token.startswith(RUNNER_PREFIX):
        return None
    row = db.one("SELECT name FROM runners WHERE token_hash = ? AND revoked = 0", (token_hash(token),))
    return row["name"] if row else None


def issue_agent_token(db: Database, agent: str) -> str:
    """A new gateway token for `agent` (its agents row must exist), replacing any earlier one; returns the
    plaintext vagt_ token, which only the runner (in agent.start) and the agent's harness ever hold."""
    token = AGENT_PREFIX + secrets.token_urlsafe(32)
    db.execute("UPDATE agents SET token_hash = ? WHERE name = ?", (token_hash(token), agent))
    return token


def revoke_agent_token(db: Database, agent: str) -> None:
    db.execute("UPDATE agents SET token_hash = NULL WHERE name = ?", (agent,))


def agent_for_token(db: Database, authorization: str | None) -> str | None:
    """`Bearer vagt_...` -> agent name, or None if missing, unknown or revoked."""
    token = (authorization or "").strip().removeprefix("Bearer ").strip()
    if not token.startswith(AGENT_PREFIX):
        return None
    row = db.one("SELECT name FROM agents WHERE token_hash = ?", (token_hash(token),))
    return row["name"] if row else None
