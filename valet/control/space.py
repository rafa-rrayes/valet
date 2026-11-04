"""The shared space: notes and files the owner and one agent leave for each other. Each agent has its own, newest change
first, and only it and the owner see it: every item belongs to one agent (`shared.agent`), and an agent reaching for
another's item by id finds nothing (_row, the one place that rule is applied). Items from before (when one list was
everyone's) whose agent couldn't be told are unassigned: only the owner sees them, and can move one to an agent. A note
is a title and Markdown text; a file is its bytes, in <data>/shared/<id>/<name>, its name as the title and an
optional description. The index is the `shared` table.

The owner uses it through the API (api_shared.py) and the Shared part of an agent's page, agents through valet.shared
(gateway/builtin_shared.py). What an agent adds or changes stays unseen until the owner looks at it, and each item an agent
adds goes through shared_added(), the one place a notification about it starts from. Every change is a `shared`
client frame with the new unseen counts (in all, and per agent)."""
import logging
import mimetypes
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from valet.shared.definition import OWNER_NAME

from .db import SHARED_ASSIGN

log = logging.getLogger(__name__)

OWNER = OWNER_NAME
ANY = "*"                       # listing(): every item, whoever's ("*" is never an agent's name: NAME_RE)
MAX_TEXT = 1_000_000            # characters of a note's text or a file's description
MAX_TITLE = 200
NAME_MAX = 120                  # characters of a file's name
NAME_BYTES = 240                # ... and its bytes in UTF-8 (a path segment is at most 255 on Linux)
PREVIEW_MAX = 200
PAGE = 50                       # items a listing returns by default
PAGE_MAX = 200
INCOMING = ".incoming"          # <data>/shared/.incoming/: files on their way in, emptied at every start
UNSEEN = "updated_by != 'owner' AND (seen_at IS NULL OR seen_at < updated_at)"
KINDS = ("note", "file")
# Types a browser may show in place (?inline=1); anything else downloads, and nothing is served as HTML or SVG.
INLINE_TYPES = re.compile(r"^(image/(png|jpeg|gif|webp|avif|bmp)|application/pdf|text/plain|audio/.+|video/.+)$")


class SharedError(ValueError):
    """Something the shared space refuses; the message says why and is shown as is."""


class NotFound(LookupError):
    pass


def safe_name(name: object) -> str:
    """A file name to keep and show: its last path segment, without control characters or leading dots."""
    name = (name if isinstance(name, str) else "").replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip().lstrip(".").strip()
    return name[:NAME_MAX].encode()[:NAME_BYTES].decode(errors="ignore") or "file"


def mime_of(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def first_line(text: str) -> str:
    return next((x.strip() for x in text.splitlines() if x.strip()), "")


def preview(text: str, title: str = "") -> str:
    """What a list shows under the title: the text's first line that has anything, past the heading the title came
    from, without Markdown's heading, quote or list mark and its bold and code marks."""
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    if lines and lines[0].lstrip("#").strip()[:MAX_TITLE] == title:
        lines = lines[1:]
    line = re.sub(r"^(#{1,6}|>|[-*+]|\d+[.)])\s+", "", lines[0]) if lines else ""
    return re.sub(r"\*\*|__|`", "", line).strip()[:PREVIEW_MAX]


def note_title(title: object, text: str) -> str:
    """The given title, else the text's first line without its Markdown heading marks."""
    title = re.sub(r"[\x00-\x1f\x7f]", " ", title if isinstance(title, str) else "").strip()
    return (title or first_line(text).lstrip("#").strip() or "Note")[:MAX_TITLE]


def check_text(text: object, what: str = "the text") -> str:
    if not isinstance(text, str):
        raise SharedError(f"{what} must be a string")
    if len(text) > MAX_TEXT:
        raise SharedError(f"{what} is over {MAX_TEXT:,} characters; share it as a file instead")
    return text.strip()


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def assigned_to(row: dict[str, Any], agents: set[str]) -> str | None:
    """Whose space an item from the one everyone shared goes to: the agent that added it, else the one that last
    changed it, if it's in `agents` (those that exist); None: unassigned. The database migration decides by it."""
    return next((x for x in (row["added_by"], row["updated_by"]) if x in agents), None)


def for_agent(item: dict[str, Any]) -> dict[str, Any]:
    """An item as valet.shared shows it: dates in ISO 8601, no seen state (that's the owner's), no agent (its own)."""
    out = {k: item[k] for k in ("id", "kind", "title", "added_by", "updated_by")}
    if item["kind"] == "file":
        out.update(name=item["title"], mime=item["mime"], size=item["size"])
    out.update(created=iso(item["created_at"]), updated=iso(item["updated_at"]))
    if "text" in item:
        out["text"] = item["text"]
    else:
        out["preview"] = item["preview"]
    return out


class SharedSpace:
    def __init__(self, control: Any) -> None:
        """control: the app's Control (cfg, db, hub)."""
        self.control = control

    @property
    def db(self):
        return self.control.db

    @property
    def root(self) -> Path:
        return self.control.cfg.shared_dir

    @property
    def max_bytes(self) -> int:
        return self.control.cfg.max_file_mb * 1024 * 1024

    def start(self) -> None:
        """At startup: the directory (owner-only), no half-arrived files left from before, and an old database's
        items given their agents (assign_old), once."""
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)
        shutil.rmtree(self.root / INCOMING, ignore_errors=True)
        if self.db.get_setting(SHARED_ASSIGN):
            n = self.assign_old(set(self.control.defs.names()))
            self.db.execute("DELETE FROM settings WHERE key = ?", (SHARED_ASSIGN,))
            log.info("shared: one space per agent now; %s item(s) went to their agent", n)

    def assign_old(self, agents: set[str]) -> int:
        """The items from when the space was everyone's (agent NULL): each goes to the agent that added it, else to
        the one that last changed it, when that agent is in `agents` (it has a definitions folder); the rest stay
        unassigned. How many were assigned."""
        n = 0
        for r in self.db.query("SELECT id, added_by, updated_by FROM shared WHERE agent IS NULL"):
            if who := assigned_to(r, agents):
                self.db.execute("UPDATE shared SET agent = ? WHERE id = ?", (who, r["id"]))
                n += 1
        return n

    def incoming(self) -> Path:
        """A new path to receive a file into, before it's added (add_file / replace_file move it into place)."""
        d = self.root / INCOMING
        d.mkdir(parents=True, exist_ok=True)
        return d / uuid.uuid4().hex

    def too_big(self, name: str) -> SharedError:
        return SharedError(f"{name} is over the {self.control.cfg.max_file_mb} MiB limit (VALET_MAX_FILE_MB)")

    # ---------- reading ----------

    def _row(self, item_id: int, who: str = OWNER) -> dict:
        """The item, as `who` may reach it: the owner (OWNER) any, an agent only its own. Anything else is NotFound, the
        same as an id that was never there."""
        r = self.db.one("SELECT * FROM shared WHERE id = ?", (item_id,))
        if not r or (who != OWNER and r["agent"] != who):
            raise NotFound(f"there's no shared item {item_id}")
        return r

    @staticmethod
    def view(r: dict, full: bool = False) -> dict[str, Any]:
        """SharedItem (docs/API.md); `full` adds `text`."""
        v = {k: r[k] for k in ("id", "agent", "kind", "title", "mime", "size", "added_by", "updated_by",
                               "created_at", "updated_at")}
        v["unseen"] = r["updated_by"] != OWNER and (r["seen_at"] or 0) < r["updated_at"]
        v["preview"] = preview(r["text"], r["title"])
        if full:
            v["text"] = r["text"]
        return v

    def get(self, item_id: int, who: str = OWNER) -> dict[str, Any]:
        """One item, its text included, as `who` may reach it (_row). NotFound."""
        return self.view(self._row(item_id, who), full=True)

    @staticmethod
    def _scope(agent: str | None) -> tuple[str, tuple]:
        """An `agent` as SQL: that agent's items, the unassigned (None), or every item (ANY)."""
        if agent == ANY:
            return "1", ()
        return ("agent IS NULL", ()) if agent is None else ("agent = ?", (agent,))

    def listing(self, agent: str | None, q: str = "", by: str = "", kind: str = "", before: float | None = None,
                limit: int = PAGE, offset: int = 0) -> dict[str, Any]:
        """{"items", "more"}: `agent`'s items (None: the unassigned; ANY: everyone's), most recently changed first,
        `limit` of them; page back with before=<the last one's updated_at> (or `offset`). `q`: in the title or text
        (ignoring ASCII case); `by`: owner, agents, or one agent's name (who added it); `kind`: note or file."""
        scope, scope_args = self._scope(agent)
        where, args = [scope], list(scope_args)
        if q and q.strip():
            like = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            where.append("(title LIKE ? ESCAPE '\\' OR text LIKE ? ESCAPE '\\')")
            args += [like, like]
        if by == OWNER:
            where.append("added_by = 'owner'")
        elif by == "agents":
            where.append("added_by != 'owner'")
        elif by:
            where.append("added_by = ?")
            args.append(by)
        if kind:
            if kind not in KINDS:
                raise SharedError("kind is note or file")
            where.append("kind = ?")
            args.append(kind)
        if before is not None:
            where.append("updated_at < ?")
            args.append(before)
        limit = max(1, min(PAGE_MAX, int(limit)))
        rows = self.db.query(f"SELECT * FROM shared WHERE {' AND '.join(where)} "
                             "ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?", (*args, limit + 1, max(0, offset)))
        return {"items": [self.view(r) for r in rows[:limit]], "more": len(rows) > limit}

    def unseen_count(self, agent: str | None = ANY) -> int:
        """What's unseen among `agent`'s items (None: the unassigned; ANY: all of them)."""
        scope, args = self._scope(agent)
        return self.db.one(f"SELECT COUNT(*) AS n FROM shared WHERE {scope} AND {UNSEEN}", args)["n"]

    def counts(self) -> dict[str, Any]:
        """{"unseen": in all, "agents": {agent: unseen}, "unassigned": how many items have no agent, seen or not}
        (an agent with nothing unseen left out; the unassigned only in the total): what the badges show, in every
        `shared` frame."""
        rows = self.db.query(f"SELECT agent, COUNT(*) AS n FROM shared WHERE {UNSEEN} GROUP BY agent")
        return {"unseen": sum(r["n"] for r in rows), "agents": {r["agent"]: r["n"] for r in rows if r["agent"]},
                "unassigned": self.db.one("SELECT COUNT(*) AS n FROM shared WHERE agent IS NULL")["n"]}

    def file_path(self, item_id: int, who: str = OWNER) -> tuple[dict[str, Any], Path]:
        """A file item (its view, text included) and where its bytes are, as `who` may reach it (_row). NotFound
        for a note or a missing file."""
        r = self._row(item_id, who)
        p = (self.root / (r["path"] or "")).resolve()
        if r["kind"] != "file" or not p.is_relative_to(self.root.resolve()) or not p.is_file():
            raise NotFound(f"shared item {item_id} has no file")
        return self.view(r, full=True), p

    # ---------- changing ----------

    async def _changed(self, item_id: int) -> dict[str, Any]:
        item = self.get(item_id)
        await self.control.hub.broadcast({"type": "shared", "item": self.view(self._row(item_id)), **self.counts()})
        return item

    def _insert(self, agent: str, kind: str, by: str, title: str, text: str) -> int:
        if by not in (OWNER, agent):
            raise SharedError("an agent adds only to its own shared space")
        if not self.control.defs.exists(agent):
            # Deleted while an upload to it was still arriving (its or the owner's): an item now would outlive it, and
            # go to the next agent made with its name. Checked here, with no await before the row is in: a delete's
            # forget_agent comes after its definition is gone, so it either finds the row or there is none.
            raise NotFound(f"there's no agent {agent}")
        now = time.time()
        cur = self.db.execute("INSERT INTO shared (agent, kind, title, text, added_by, updated_by, created_at, "
                              "updated_at, seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                              (agent, kind, title, text, by, by, now, now, now if by == OWNER else None))
        return cur.lastrowid

    def _place(self, item_id: int, name: str, src: Path) -> None:
        """Move a received file into the item's folder, in place of what was there (a replaced file's old bytes go
        only once the new file is in: a move that fails leaves the item as it was)."""
        folder, staged = self.root / str(item_id), self.incoming()
        staged.mkdir()
        src.replace(staged / name)
        shutil.rmtree(folder, ignore_errors=True)
        staged.rename(folder)
        dest = folder / name
        self.db.execute("UPDATE shared SET mime = ?, size = ?, path = ? WHERE id = ?",
                        (mime_of(name), dest.stat().st_size, f"{item_id}/{name}", item_id))

    def _touch(self, item_id: int, by: str, **fields: Any) -> None:
        now = time.time()
        fields.update(updated_by=by, updated_at=now, **({"seen_at": now} if by == OWNER else {}))
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(f"UPDATE shared SET {sets} WHERE id = ?", (*fields.values(), item_id))

    async def add_note(self, agent: str, by: str, title: object, text: object) -> dict[str, Any]:
        """A new note in `agent`'s space, from `by` (OWNER or that agent)."""
        text = check_text(text)
        if not text:
            raise SharedError("a note needs some text")
        item = await self._changed(self._insert(agent, "note", by, note_title(title, text), text))
        if by != OWNER:
            await self.shared_added(item)
        return item

    async def add_file(self, agent: str, by: str, name: object, src: Path, text: object = "") -> dict[str, Any]:
        """A new file item in `agent`'s space, from `by` (as add_note), from the file at `src` (received with
        incoming(); moved into place). Within max_bytes, or SharedError (and `src` is removed)."""
        try:
            name, text = safe_name(name), check_text(text if text is not None else "", "the description")
            if src.stat().st_size > self.max_bytes:
                raise self.too_big(name)
            item_id = self._insert(agent, "file", by, name, text)
            try:
                self._place(item_id, name, src)
            except BaseException:
                self._forget(item_id)
                raise
        finally:
            src.unlink(missing_ok=True)
        item = await self._changed(item_id)
        if by != OWNER:
            await self.shared_added(item)
        return item

    async def update(self, item_id: int, by: str, title: object = None, text: object = None) -> dict[str, Any]:
        """A note's title and text, a file's name and description: what's given (None: unchanged). `by` reaches
        the item as _row says."""
        r = self._row(item_id, by)
        fields: dict[str, Any] = {}
        if text is not None:
            fields["text"] = check_text(text)
            if r["kind"] == "note" and not fields["text"]:
                raise SharedError("a note needs some text")
        if title is not None:
            if not isinstance(title, str):
                raise SharedError("the title must be a string")
            if r["kind"] == "note":
                fields["title"] = note_title(title, fields.get("text", r["text"]))
            elif title.strip():
                fields["title"] = safe_name(title)
        if not fields:
            raise SharedError("nothing to change: give a title or text")
        self._touch(item_id, by, **fields)
        return await self._changed(item_id)

    async def replace_file(self, item_id: int, by: str, src: Path, name: object = None) -> dict[str, Any]:
        """A file item's bytes, from `src` (as add_file). `name`: the new file's name; the title follows it when
        the title was the old file's name. `by` reaches the item as _row says."""
        try:
            r = self._row(item_id, by)
            if r["kind"] != "file":
                raise SharedError(f"shared item {item_id} is a note; change its text instead")
            old = (r["path"] or "").rsplit("/", 1)[-1]
            new = safe_name(name) if isinstance(name, str) and name.strip() else (old or r["title"])
            if src.stat().st_size > self.max_bytes:
                raise self.too_big(new)
            self._place(item_id, new, src)
        finally:
            src.unlink(missing_ok=True)
        self._touch(item_id, by, **({"title": new} if r["title"] == old else {}))
        return await self._changed(item_id)

    def _forget(self, item_id: int) -> None:
        shutil.rmtree(self.root / str(item_id), ignore_errors=True)
        self.db.execute("DELETE FROM shared WHERE id = ?", (item_id,))

    async def delete(self, item_id: int, by: str) -> dict[str, Any]:
        """Remove an item and its file (`by` reaches it as _row says). Returns what it was. NotFound."""
        item = self.get(item_id, by)
        self._forget(item_id)
        log.info("shared: %s deleted #%s %s", by, item_id, item["title"])
        await self.control.hub.broadcast({"type": "shared", "deleted": item_id, "agent": item["agent"],
                                          **self.counts()})
        return item

    async def move(self, item_id: int, agent: str) -> dict[str, Any]:
        """The owner puts an item (an unassigned one, or another agent's) in `agent`'s space. NotFound."""
        before = self._row(item_id)["agent"]
        self.db.execute("UPDATE shared SET agent = ? WHERE id = ?", (agent, item_id))
        log.info("shared: owner moved #%s from %s to %s", item_id, before or "unassigned", agent)
        return await self._changed(item_id)

    def forget_agent(self, agent: str) -> int:
        """The agent is going away (Delete agent): its items and their files go with it. No frames: the caller
        says the agent is gone, and clients look again. A transfer it had under way finds nothing, as its
        definition is gone too (builtin_shared.transfer). How many items there were."""
        ids = [r["id"] for r in self.db.query("SELECT id FROM shared WHERE agent = ?", (agent,))]
        for item_id in ids:
            self._forget(item_id)
        if ids:
            log.info("shared: %s's %s item(s) deleted with it", agent, len(ids))
        return len(ids)

    async def mark_seen(self, ids: list[int] | None = None) -> dict[str, Any]:
        """The owner has seen these items (all of them: None). The unseen counts after (counts())."""
        where, args = UNSEEN, ()
        if ids is not None:
            ids = [int(i) for i in ids][:1000]
            where, args = f"{UNSEEN} AND id IN ({','.join('?' * len(ids)) or 'NULL'})", tuple(ids)
        self.db.execute(f"UPDATE shared SET seen_at = ? WHERE {where}", (time.time(), *args))
        counts = self.counts()
        await self.control.hub.broadcast({"type": "shared", "seen": "all" if ids is None else ids, **counts})
        return counts

    async def shared_added(self, item: dict[str, Any]) -> None:
        """An agent added `item`: the one place a notification about it starts from (a push to the owner's devices, the
        webhook: Notifier.shared_added, in the background, so the agent's call never waits on Apple or the webhook).
        The client frame has already gone out."""
        log.info("shared: %s added %s #%s %s", item["added_by"], item["kind"], item["id"], item["title"])
        notifier = getattr(self.control, "notifier", None)
        if notifier is not None:
            self.control.hub.spawn(notifier.shared_added(item))
