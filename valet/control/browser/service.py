"""The browser service (`control.browser`): what the actions, the `valet.browser` server, the live view and the API
share. It reads the rules (browser.yaml and the agent's `browser.rules`, at every call), holds hour grants, writes
"always" rules into the asking agent's definition, records the audit log and its screenshots, and asks the owner
through the inbox (Hub.ask_item, kind `browser`).

Runtime state lives under `<data>/browser/`: `profiles/<name>` (Chrome's user-data-dirs) and `shots/` (the
screenshots asks and the audit log show, pruned after SHOT_DAYS)."""
import asyncio
import logging
import os
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio

from valet.shared import browser as rules
from valet.shared.browser import BrowserError, BrowserFile, Profile, host_of
from valet.shared.owner import say
from valet.shared.definition import AGENT_FILE, AgentDefinition, DefinitionError, to_yaml

from ..gateway import scopes
from .actions import mask, secret_forms
from .backend import Backend
from .policy import Verdict, decide

log = logging.getLogger(__name__)

HOUR = 3600.0
SHOT_DAYS = 30
SHOT_RE_CHARS = set("0123456789abcdef")
TARGET_LIMIT = 300
NOTE_LIMIT = 500
ASK_OPTIONS = [{"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"},
               {"optionId": "allow_hour", "name": "Allow for an hour", "kind": "allow_once"},
               {"optionId": "allow_always", "name": "Always allow", "kind": "allow_always"},
               {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"}]
HUMAN_OPTIONS = [{"optionId": "done", "name": "Done", "kind": "allow_once"},
                 {"optionId": "cant", "name": "I can't", "kind": "reject_once"}]


@dataclass
class Caller:
    """Who is acting: the agent, its definition as read for this call, and the profile."""
    agent: str
    definition: AgentDefinition
    profile: str


def valid_shot(name: str) -> bool:
    return len(name) == 20 and name.endswith(".jpg") and set(name[:16]) <= SHOT_RE_CHARS


class BrowserService:
    def __init__(self, control: Any, backend_factory: Callable[[], Backend] | None = None) -> None:
        self.control = control
        self._factory = backend_factory
        self._backend: Backend | None = None
        self._shots = 0
        from .actions import Actions        # actions.py uses this module's names
        self.actions = Actions(self)

    @property
    def cfg(self):
        return self.control.cfg

    @property
    def dir(self) -> Path:
        return self.cfg.data / "browser"

    @property
    def shots_dir(self) -> Path:
        return self.dir / "shots"

    @property
    def backend(self) -> Backend:
        """The browsers (Chrome, chrome.py), made on first use."""
        if self._backend is None:
            if self._factory is not None:
                self._backend = self._factory()
            else:
                from .chrome import ChromeBackend
                self._backend = ChromeBackend(self.dir, chrome=self.cfg.chrome, headless_mode=self.cfg.headless,
                                              idle_seconds=self.cfg.browser_idle,
                                              no_sandbox=self.cfg.chrome_no_sandbox)
        return self._backend

    @backend.setter
    def backend(self, value: Backend) -> None:
        self._backend = value

    async def close(self) -> None:
        if self._backend is not None:
            await self._backend.close()

    # ---------- browser.yaml ----------
    def file(self) -> BrowserFile:
        """browser.yaml, parsed. BrowserError if it doesn't validate."""
        return rules.parse(self.control.defs.browser_text())

    def profile(self, name: str) -> Profile | None:
        """The profile, or None if browser.yaml doesn't have it (or doesn't validate)."""
        try:
            return self.file().profiles.get(name)
        except BrowserError:
            return None

    def missing_profiles(self, d: AgentDefinition) -> list[str]:
        """The profiles `d` lists that browser.yaml doesn't have (all of them if it doesn't validate)."""
        if not d.browser.profiles:
            return []
        try:
            have = self.file().profiles
        except BrowserError:
            have = {}
        return [p for p in d.browser.profiles if p not in have]

    def check(self, d: AgentDefinition) -> None:
        """DefinitionError if `d` lists a profile browser.yaml doesn't have: such an agent doesn't start."""
        if missing := self.missing_profiles(d):
            raise DefinitionError(f"browser profile {missing[0]}: there's no such profile in browser.yaml; add it "
                                  "or take it out of the agent's browser profiles")

    def users(self, profile: str) -> list[str]:
        """Agents whose (valid) definition lists the profile, by name."""
        out = []
        for agent in self.control.defs.names():
            try:
                d = self.control.defs.definition(agent)
            except (DefinitionError, FileNotFoundError):
                continue
            if profile in d.browser.profiles:
                out.append(agent)
        return out

    # ---------- the decision ----------
    def caller(self, agent: str, profile: str) -> Caller:
        """The agent's definition, read now. DefinitionError / FileNotFoundError pass through; PermissionError if
        the agent may not use the profile."""
        d = self.control.defs.definition(agent)
        if profile not in d.browser.profiles:
            allowed = ", ".join(d.browser.profiles) or "none"
            raise PermissionError(f"this agent may not use the browser profile {profile!r}; its profiles: {allowed}")
        if self.profile(profile) is None:
            raise PermissionError(f"there's no browser profile {profile!r} in Valet (browser.yaml)")
        return Caller(agent, d, profile)

    def verdict(self, c: Caller, kind: str, url: str) -> Verdict:
        p = self.profile(c.profile)
        return decide(c.definition.permissions, c.definition.browser.rules, p.sites if p else {}, kind, url,
                      self.live_grants(c.agent, c.profile))

    # ---------- grants ----------
    def live_grants(self, agent: str, profile: str) -> set[tuple[str, str]]:
        rows = self.control.db.query("SELECT host, kind FROM browser_grants WHERE agent = ? AND profile = ? "
                                     "AND expires_at > ?", (agent, profile, time.time()))
        return {(r["host"], r["kind"]) for r in rows}

    def grant(self, agent: str, profile: str, host: str, kind: str, seconds: float = HOUR) -> int:
        now = time.time()
        return self.control.db.execute(
            "INSERT INTO browser_grants (agent, profile, host, kind, expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (agent, profile, host, kind, now + seconds, now)).lastrowid

    def grants(self, agent: str | None = None, profile: str | None = None) -> list[dict]:
        """Live grants, newest first."""
        where, params = ["expires_at > ?"], [time.time()]
        for col, val in (("agent", agent), ("profile", profile)):
            if val is not None:
                where.append(f"{col} = ?")
                params.append(val)
        return self.control.db.query(f"SELECT * FROM browser_grants WHERE {' AND '.join(where)} ORDER BY id DESC",
                                     tuple(params))

    def revoke(self, grant_id: int) -> bool:
        return bool(self.control.db.execute("DELETE FROM browser_grants WHERE id = ?", (grant_id,)).rowcount)

    async def always(self, agent: str, host: str, kind: str) -> bool:
        """Write `host: {kind: allow}` into the agent's browser.rules, as a commit. False if it can't (the
        definition doesn't validate, or the host isn't a pattern Valet can write)."""
        try:
            await asyncio.to_thread(self._write_always, agent, host, kind)
        except (DefinitionError, FileNotFoundError, ValueError) as e:
            log.warning("browser: couldn't write %s's rule for %s %s: %s", agent, kind, host, e)
            return False
        await self.control.hub.emit_agent(agent)
        return True

    def _write_always(self, agent: str, host: str, kind: str) -> None:
        """always()'s read, add and write back, in one piece: under the lock the gateway's "always" takes too
        (scopes.always_allow), so two answers at once can't each write agent.yaml without the other's rule.
        Blocking (git)."""
        with scopes._always:
            d = self.control.defs.definition(agent)
            pattern = rules.normalize_pattern(host)
            new = dict(d.browser.rules)
            new[pattern] = {**new.get(pattern, {}), kind: "allow"}
            d = AgentDefinition.model_validate({**d.model_dump(mode="json"),
                                                "browser": {**d.browser.model_dump(mode="json"), "rules": new}})
            self.control.defs.write(agent, {AGENT_FILE: to_yaml(d)},
                                    f"Inbox: always allow {kind} on {host} for {agent}")

    def vault_forms(self) -> list[str]:
        """The vault's secrets as masking replaces them (actions.secret_forms); [] if the vault can't be read."""
        try:
            return secret_forms(self.control.secrets.vault.secrets())
        except Exception:
            return []

    # ---------- asks ----------
    async def _wait(self, kind: str, c: Caller, title: str, extra: dict, options: list[dict]) -> str | None:
        """An inbox item; the chosen optionId, or None once it expires (timeout, or the agent or its turn went
        away). The item never outlives the wait."""
        hub = self.control.hub
        row, fut = await hub.ask_item(kind, c.agent, title, {kind: extra}, options,
                                      raw_input=extra.get("detail") or {})
        try:
            with anyio.move_on_after(self.cfg.gateway_ask_timeout):
                outcome = await fut
                return outcome.get("optionId") if outcome and outcome.get("outcome") == "selected" else None
            return None
        finally:
            with anyio.CancelScope(shield=True):
                if await hub.expire_item(row):
                    await hub.emit_agent(c.agent)

    async def ask(self, c: Caller, *, tab: str | None, url: str, kind: str, summary: str, detail: dict,
                  shot: str | None) -> str | None:
        """Ask the owner about one action -> allow_once | allow_hour | allow_always | reject_once | None (expired).
        allow_hour grants the agent that kind on that host for an hour; allow_always writes the rule."""
        host = host_of(url) or url
        # Masked: the URL may carry a vault password, and the title is the phone's push and the webhook's line.
        extra = mask({"profile": c.profile, "tab": tab, "url": url, "host": host, "kind": kind, "summary": summary,
                      "detail": detail, "shot": shot}, self.vault_forms())
        choice = await self._wait("browser", c, f"{extra['host']}: {extra['summary']}", extra, ASK_OPTIONS)
        if choice == "allow_hour":
            self.grant(c.agent, c.profile, host, kind)
        elif choice == "allow_always":
            await self.always(c.agent, host, kind)
        return choice

    async def human(self, c: Caller, *, tab: str, url: str, reason: str, shot: str | None) -> str | None:
        """request_human: done | cant | None (expired)."""
        host = host_of(url) or url
        extra = mask({"profile": c.profile, "tab": tab, "url": url, "host": host, "kind": "human",
                      "summary": f"take over this tab: {reason}", "detail": {"reason": reason}, "shot": shot},
                     self.vault_forms())
        return await self._wait("browser", c, f"{extra['host']}: take over this tab", extra, HUMAN_OPTIONS)

    # ---------- the audit log ----------
    def audit(self, agent: str | None, profile: str, action: str, *, kind: str | None = None, host: str | None = None,
              target: str | None = None, decision: str | None = None, outcome: str = "ok", note: str | None = None,
              shot: str | None = None) -> int:
        """One row of the activity log; a vault secret in its target or note (a URL) is masked."""
        forms = self.vault_forms()
        target, note = mask(target, forms), mask(say(note) if note else note, forms)
        return self.control.db.execute(
            "INSERT INTO browser_audit (ts, agent, profile, action, kind, host, target, decision, outcome, note, "
            "shot) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), agent, profile, action, kind, host, (target or "")[:TARGET_LIMIT] or None, decision,
             outcome, (note or "")[:NOTE_LIMIT] or None, shot)).lastrowid

    def audit_rows(self, agent: str | None = None, profile: str | None = None, before: int | None = None,
                   limit: int = 100) -> tuple[list[dict], bool]:
        where, params = [], []
        for col, val in (("agent", agent), ("profile", profile)):
            if val is not None:
                where.append(f"{col} = ?")
                params.append(val)
        if before is not None:
            where.append("id < ?")
            params.append(before)
        sql = ("SELECT * FROM browser_audit" + (" WHERE " + " AND ".join(where) if where else "")
               + " ORDER BY id DESC LIMIT ?")
        rows = self.control.db.query(sql, (*params, limit + 1))
        return rows[:limit], len(rows) > limit

    # ---------- screenshots ----------
    def save_shot(self, data: bytes) -> str:
        """Store a JPEG; its name (16 hex digits + .jpg)."""
        self.shots_dir.mkdir(parents=True, exist_ok=True)
        name = secrets.token_hex(8) + ".jpg"
        fd = os.open(self.shots_dir / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        self._shots += 1
        if self._shots % 200 == 1:
            self.prune_shots()
        return name

    def shot_path(self, name: str) -> Path | None:
        if not valid_shot(name):
            return None
        p = self.shots_dir / name
        return p if p.is_file() else None

    def prune_shots(self, days: float = SHOT_DAYS) -> int:
        cutoff, n = time.time() - days * 86400, 0
        if not self.shots_dir.is_dir():
            return 0
        for p in self.shots_dir.glob("*.jpg"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    n += 1
            except OSError:
                pass
        return n

    # ---------- what the API shows ----------
    def profile_view(self, name: str, p: Profile) -> dict[str, Any]:
        running = self._backend.running() if self._backend is not None else {}
        return {"name": name, "title": p.title, "note": p.note, "sites": p.sites, "used_by": self.users(name),
                "vault": self.control.secrets.vault.count(name), "running": name in running}

    def running(self) -> list[dict[str, Any]]:
        if self._backend is None:
            return []
        out = []
        for name, s in sorted(self._backend.running().items()):
            out.append({"profile": name, "tabs": len(s.tabs()), "started_at": s.started_at,
                        "idle": round(time.monotonic() - s.last_used, 1), "busy": s.lock.locked(),
                        "viewers": s.viewers})
        return out

    async def tabs(self, profile: str) -> list[dict[str, Any]]:
        """A running profile's tabs, newest first ([] if its browser isn't running)."""
        s = self._backend.running().get(profile) if self._backend is not None else None
        if s is None:
            return []
        return [{"tab": t.id, "url": t.url, "title": await t.title()} for t in reversed(s.tabs())]
