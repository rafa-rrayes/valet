"""REST routes for the browser and the vault (docs/API.md, "The browser" and "The vault"), and the live view's
WebSocket, /ws/browser/<profile>. Thin, like api.py: validation and auth here, the work in control/browser/ and
control/vault.py."""
import asyncio
import logging
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ValidationError

from valet.shared import browser as rules
from valet.shared.browser import BrowserError, BrowserFile, Profile, profile_name_error
from valet.shared.definition import DefinitionError, validation_errors

from . import auth
from .browser import live
from .browser.backend import BrowserFailure
from .vault import VaultError

log = logging.getLogger(__name__)

router = APIRouter()
owner = APIRouter(prefix="/api", dependencies=[Depends(auth.require_owner)])
CLOSE_FORBIDDEN = 4403
CLOSE_UNAUTHORIZED = 4401


def _control(request: Request):
    return request.app.state.control


def _invalid(e: ValidationError | str) -> JSONResponse:
    if isinstance(e, str):
        return JSONResponse({"detail": e}, status_code=422)
    errors = validation_errors(e)
    return JSONResponse({"detail": "; ".join(f"{x['field']}: {x['msg']}" for x in errors), "errors": errors},
                        status_code=422)


def _file(c) -> BrowserFile:
    """browser.yaml, for a change: 409 if it doesn't validate now (fix it as text first)."""
    try:
        return c.browser.file()
    except BrowserError as e:
        raise HTTPException(409, f"{e}; fix it first (PUT /api/browser/yaml)") from None


async def _write(c, f: BrowserFile, message: str) -> str:
    return await asyncio.to_thread(c.defs.write_browser, rules.to_yaml(f), message)


# ---------- profiles and browser.yaml ----------
class ProfileBody(BaseModel):
    """A profile as the API takes it: everything but the name, which is the path's (or `name` on create)."""
    name: str | None = None
    title: str | None = None
    note: str | None = None
    sites: dict[str, Any] = {}


def _profile(body: ProfileBody) -> Profile:
    return Profile.model_validate(body.model_dump(exclude={"name"}))


def _profile_or_404(c, name: str) -> Profile:
    p = _file(c).profiles.get(name)
    if p is None:
        raise HTTPException(404, f"no browser profile named {name}")
    return p


@owner.get("/browser/profiles")
def list_profiles(request: Request):
    c = _control(request)
    try:
        f, error = c.browser.file(), None
    except BrowserError as e:
        f, error = BrowserFile(), str(e)
    return {"profiles": [c.browser.profile_view(n, p) for n, p in sorted(f.profiles.items())], "error": error}


@owner.post("/browser/profiles", status_code=201)
async def create_profile(body: ProfileBody, request: Request):
    c = _control(request)
    if err := profile_name_error(body.name):
        return _invalid(f"name: {err}")
    f = _file(c)
    if body.name in f.profiles:
        raise HTTPException(409, f"there's already a browser profile named {body.name}")
    try:
        p = _profile(body)
    except ValidationError as e:
        return _invalid(e)
    f.profiles[body.name] = p
    await _write(c, f, f"Add browser profile {body.name}")
    return c.browser.profile_view(body.name, p)


@owner.get("/browser/profiles/{name}")
def get_profile(name: str, request: Request):
    c = _control(request)
    return c.browser.profile_view(name, _profile_or_404(c, name))


@owner.put("/browser/profiles/{name}")
async def put_profile(name: str, body: ProfileBody, request: Request):
    """Replace the profile's title, note and site rules (a field left out is cleared)."""
    c = _control(request)
    f = _file(c)
    if name not in f.profiles:
        raise HTTPException(404, f"no browser profile named {name}")
    try:
        p = _profile(body)
    except ValidationError as e:
        return _invalid(e)
    f.profiles[name] = p
    await _write(c, f, f"Edit browser profile {name}")
    return c.browser.profile_view(name, p)


@owner.delete("/browser/profiles/{name}")
async def delete_profile(name: str, request: Request, wipe: bool = False):
    """409 while an agent lists it or the vault has logins for it. Stops its browser; `wipe` also deletes its
    browser data (cookies, sessions: every site signs out)."""
    c = _control(request)
    f = _file(c)
    if name not in f.profiles:
        raise HTTPException(404, f"no browser profile named {name}")
    if users := c.browser.users(name):
        raise HTTPException(409, f"browser profile {name} is used by {', '.join(users)}: take it out of "
                                 f"{'their' if len(users) > 1 else 'its'} browser profiles first")
    if n := c.secrets.vault.count(name):
        raise HTTPException(409, f"the vault has {n} login{'s' if n > 1 else ''} for {name}: delete "
                                 f"{'them' if n > 1 else 'it'} first")
    del f.profiles[name]
    await _write(c, f, f"Delete browser profile {name}")
    if wipe:
        await c.browser.backend.wipe(name)
    else:
        await c.browser.backend.stop(name)
    return {"ok": True}


class Text(BaseModel):
    text: str


@owner.get("/browser/yaml")
def get_yaml(request: Request):
    c = _control(request)
    return {"text": c.defs.browser_text(), "commit": c.defs.head()}


@owner.put("/browser/yaml")
async def put_yaml(body: Text, request: Request):
    """browser.yaml as text, validated and committed as written (comments kept). 422 if it doesn't validate."""
    c = _control(request)
    try:
        commit = await asyncio.to_thread(c.defs.write_browser, body.text)
    except BrowserError as e:
        return _invalid(str(e))
    return {"commit": commit, **list_profiles(request)}


# ---------- the running browsers ----------
@owner.get("/browser/running")
def running(request: Request):
    return _control(request).browser.running()


@owner.post("/browser/profiles/{name}/stop")
async def stop_browser(name: str, request: Request):
    c = _control(request)
    return {"ok": True, "was_running": await c.browser.backend.stop(name)}


@owner.get("/browser/profiles/{name}/tabs")
async def profile_tabs(name: str, request: Request):
    c = _control(request)
    _profile_or_404(c, name)
    return await c.browser.tabs(name)


class OpenTab(BaseModel):
    url: str = "about:blank"


@owner.post("/browser/profiles/{name}/tabs", status_code=201)
async def open_tab(name: str, body: OpenTab, request: Request):
    """Open a tab as the owner (the rules are the agents'), starting the profile's browser if it isn't running."""
    c = _control(request)
    _profile_or_404(c, name)
    url = body.url.strip() or "about:blank"
    if not re.match(r"^(https?://|about:blank)", url, re.I):
        url = "https://" + url
    try:
        s = await c.browser.backend.session(name)
        async with s.lock:
            tab = await s.new_tab()
            if url != "about:blank":
                await tab.goto(url)
    except BrowserFailure as e:
        raise HTTPException(502, str(e)) from None
    c.browser.audit(None, name, "open_tab", host=rules.host_of(url) or None, target=url)
    return {"tab": tab.id, "url": tab.url, "title": await tab.title()}


# ---------- grants, the audit log, screenshots ----------
@owner.get("/browser/grants")
def list_grants(request: Request, agent: str | None = None, profile: str | None = None):
    return _control(request).browser.grants(agent, profile)


@owner.delete("/browser/grants/{grant_id}")
def revoke_grant(grant_id: int, request: Request):
    if not _control(request).browser.revoke(grant_id):
        raise HTTPException(404, "no such grant")
    return {"ok": True}


@owner.get("/browser/audit")
def audit(request: Request, agent: str | None = None, profile: str | None = None,
          before: int | None = Query(None, ge=1), limit: int = Query(100, ge=1, le=500)):
    rows, more = _control(request).browser.audit_rows(agent, profile, before, limit)
    return {"actions": rows, "more": more}


@owner.get("/browser/shots/{name}")
def shot(name: str, request: Request):
    p = _control(request).browser.shot_path(name)
    if p is None:
        raise HTTPException(404, "no such screenshot")
    return FileResponse(p, media_type="image/jpeg")


# ---------- the vault ----------
class NewEntry(BaseModel):
    name: str
    profile: str
    site: str
    label: str | None = None
    username: str | None = None
    password: Any = None        # checked by the vault, whose messages never repeat it (pydantic's would)
    totp: Any = None


class EntryEdit(BaseModel):
    profile: str | None = None
    site: str | None = None
    label: str | None = None
    username: str | None = None
    password: Any = None        # "" clears it; left out: unchanged
    totp: Any = None


def _vault_users(c, name: str) -> list[str]:
    """Agents whose (valid) definition gets this entry's password as an env var."""
    out = []
    for agent in c.defs.names():
        try:
            d = c.defs.definition(agent)
        except (DefinitionError, FileNotFoundError):
            continue
        if any(v.vault == name for v in d.vault_secrets()):
            out.append(agent)
    return out


def _entry(c, public: dict) -> dict:
    return {**public, "used_by": _vault_users(c, public["name"])}


async def _emit_vault_users(c, name: str) -> None:
    for agent in _vault_users(c, name):
        await c.hub.emit_agent(agent)


def _secret_text(value: Any, what: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise VaultError(f"the {what} must be text")
    return value


@owner.get("/vault")
def list_vault(request: Request, profile: str | None = None):
    c = _control(request)
    try:
        return [_entry(c, e) for e in c.secrets.vault.listing(profile)]
    except VaultError as e:
        raise HTTPException(500, str(e)) from None


@owner.post("/vault", status_code=201)
async def create_entry(body: NewEntry, request: Request):
    c = _control(request)
    if c.browser.profile(body.profile) is None:
        return _invalid(f"profile: there's no browser profile named {body.profile}")
    try:
        e = c.secrets.vault.put(body.name, profile=body.profile, site=body.site, label=body.label or "",
                                username=body.username or "", password=_secret_text(body.password, "password") or "",
                                totp=_secret_text(body.totp, "2FA secret") or "", create=True)
    except FileExistsError:
        raise HTTPException(409, f"there's already a vault entry named {body.name}") from None
    except VaultError as e:
        return _invalid(str(e))
    await _emit_vault_users(c, body.name)
    return _entry(c, e)


def _entry_or_404(c, name: str) -> dict:
    row = c.secrets.vault.row(name)
    if row is None:
        raise HTTPException(404, f"no vault entry named {name}")
    try:
        return _entry(c, c.secrets.vault.public(row))
    except VaultError as e:
        raise HTTPException(500, str(e)) from None


@owner.get("/vault/{name:path}")
def get_entry(name: str, request: Request):
    return _entry_or_404(_control(request), name)


@owner.put("/vault/{name:path}")
async def edit_entry(name: str, body: EntryEdit, request: Request):
    """Change what's given; a field left out stays. A new password makes agents that get it say restart_needed."""
    c = _control(request)
    _entry_or_404(c, name)
    if body.profile is not None and c.browser.profile(body.profile) is None:
        return _invalid(f"profile: there's no browser profile named {body.profile}")
    try:
        e = c.secrets.vault.put(name, profile=body.profile, site=body.site, label=body.label, username=body.username,
                                password=_secret_text(body.password, "password"),
                                totp=_secret_text(body.totp, "2FA secret"), create=False)
    except FileNotFoundError:
        raise HTTPException(404, f"no vault entry named {name}") from None
    except VaultError as e:
        return _invalid(str(e))
    await _emit_vault_users(c, name)
    return _entry(c, e)


@owner.delete("/vault/{name:path}")
async def delete_entry(name: str, request: Request):
    c = _control(request)
    if not c.secrets.vault.delete(name):
        raise HTTPException(404, f"no vault entry named {name}")
    await _emit_vault_users(c, name)
    return {"ok": True}


# ---------- the live view ----------
@router.websocket("/ws/browser/{profile}")
async def live_socket(ws: WebSocket, profile: str):
    c = ws.app.state.control
    await ws.accept()
    if not auth.origin_ok(ws.headers.get("origin"), c.cfg.public_url):
        log.warning("live view socket from origin %s refused (VALET_PUBLIC_URL is %s)", ws.headers.get("origin"),
                    c.cfg.public_url)
        await ws.close(CLOSE_FORBIDDEN)
        return
    if not (token := auth.websocket_is_owner(ws)):
        await ws.close(CLOSE_UNAUTHORIZED)
        return
    if profile_name_error(profile):
        await ws.close(live.CLOSE_NOT_FOUND)
        return
    await live.serve(c.browser, ws, profile, ws.query_params.get("tab"), token)


router.include_router(owner)
