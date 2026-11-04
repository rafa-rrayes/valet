"""Files the owner attaches to a message (docs/API.md, "Attachments").

An attachment is uploaded first (POST /api/agents/<name>/attachments, the raw file) and kept in
<data>/attachments/<id> until a prompt carries it ({"text", "attachments": [ids]}); one that's never sent goes after
UNSENT_TTL. The prompt route readies them (place(), before anything is recorded), and the prompt then owns them
(claim(), whether its turn starts now or it waits in the session's queue; control/prompts.py builds the blocks):
- an image (PNG, JPEG, GIF or WebP by its bytes, at most IMAGE_MAX) for a harness that takes images
  (promptCapabilities.image, from its initialize) goes in the prompt as an ACP image block, and stays here: the
  session's stored user_message event is the block with a `uri` to it (/api/agents/<name>/attachments/<id>) instead
  of its bytes;
- anything else (an image too, for a harness that takes none) is put in the agent's computer, attachments/<name> in
  its first writable folder (renamed on a clash, like name-2.ext), and the prompt gets a text block naming the path;
  the stored event is a resource_link to it (file://<path>), and the copy here goes.
Placing files needs the agent's computer (control/computer.py): an agent at level `none` takes images only."""
import asyncio
import base64
import logging
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from .computer import ComputerError
from .space import mime_of, safe_name

log = logging.getLogger(__name__)

IMAGE_MAX = 5 * 1024 * 1024
UNSENT_TTL = 24 * 3600
PER_PROMPT = 10                 # attachments one message may carry
FOLDER = "attachments"          # in the agent's workspace
CHUNK = 1024 * 1024
INCOMING = ".incoming"


class AttachmentError(Exception):
    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


def sniff_image(head: bytes) -> str | None:
    """The image type its first bytes say, among those every harness that takes images takes."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def human(n: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return f"{n} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1000
    return str(n)


class Attachments:
    def __init__(self, control: Any) -> None:
        self.control = control

    @property
    def db(self):
        return self.control.db

    @property
    def root(self) -> Path:
        return self.control.cfg.data / "attachments"

    def start(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)
        for p in (self.root / INCOMING).glob("*"):
            p.unlink(missing_ok=True)

    def path(self, att_id: str) -> Path:
        return self.root / att_id

    @staticmethod
    def url(agent: str, att_id: str) -> str:
        return f"/api/agents/{agent}/attachments/{att_id}"

    def view(self, row: dict) -> dict[str, Any]:
        return {"id": row["id"], "agent": row["agent"], "name": row["name"], "mime": row["mime"],
                "size": row["size"], "image": row["kind"] == "image", "sent": row["session_id"] is not None,
                "path": row["path"], "url": self.url(row["agent"], row["id"]), "created_at": row["created_at"]}

    def prune(self) -> None:
        """Unsent ones older than UNSENT_TTL go."""
        for row in self.db.query("SELECT id FROM attachments WHERE session_id IS NULL AND created_at < ?",
                                 (time.time() - UNSENT_TTL,)):
            self._forget(row["id"])

    def _forget(self, att_id: str) -> None:
        self.path(att_id).unlink(missing_ok=True)
        self.db.execute("DELETE FROM attachments WHERE id = ?", (att_id,))

    async def add(self, agent: str, name: str, body: AsyncIterator[bytes], limit: int) -> dict[str, Any]:
        """The body as a new unsent attachment of `agent`, within `limit` bytes (413 as it streams)."""
        self.prune()
        name = safe_name(name)
        att_id = uuid.uuid4().hex
        incoming = self.root / INCOMING
        incoming.mkdir(parents=True, exist_ok=True)
        tmp, size, head = incoming / att_id, 0, b""
        try:
            with tmp.open("wb") as out:
                async for chunk in body:
                    size += len(chunk)
                    if size > limit:
                        raise AttachmentError(f"over the {limit // 1024 // 1024} MiB limit (VALET_MAX_FILE_MB)", 413)
                    if len(head) < 16:
                        head += chunk[:16]
                    out.write(chunk)
            image = sniff_image(head)
            kind = "image" if image and size <= IMAGE_MAX else "file"
            tmp.rename(self.path(att_id))
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        self.db.execute("INSERT INTO attachments (id, agent, session_id, kind, name, mime, size, path, created_at) "
                        "VALUES (?, ?, NULL, ?, ?, ?, ?, NULL, ?)",
                        (att_id, agent, kind, name, image or mime_of(name), size, time.time()))
        return self.view(self.get(agent, att_id))

    def get(self, agent: str, att_id: str) -> dict:
        row = self.db.one("SELECT * FROM attachments WHERE id = ? AND agent = ?", (att_id, agent))
        if not row:
            raise AttachmentError("no such attachment", 404)
        return row

    def delete(self, agent: str, att_id: str) -> None:
        """An unsent one (the owner took it off the message)."""
        row = self.get(agent, att_id)
        if row["session_id"] is not None:
            raise AttachmentError("it was sent: it's part of the conversation now", 409)
        self._forget(att_id)

    def pending(self, agent: str, ids: list[str]) -> list[dict]:
        """The rows of a prompt's attachments, in order: the agent's own, unsent, each once."""
        if len(ids) > PER_PROMPT:
            raise AttachmentError(f"at most {PER_PROMPT} attachments per message", 422)
        if len(set(ids)) != len(ids):
            raise AttachmentError("the same attachment twice", 422)
        rows = []
        for att_id in ids:
            row = self.get(agent, att_id)
            if row["session_id"] is not None:
                raise AttachmentError(f"{row['name']} was sent already: attach it again", 409)
            if not self.path(att_id).is_file():
                raise AttachmentError(f"{row['name']} is gone: attach it again", 409)
            rows.append(row)
        return rows

    async def place(self, agent: str, rows: list[dict], images: bool) -> None:
        """Ready a prompt's attachments, before it's recorded or queued: each one that won't go as an image (`images`:
        the harness takes image blocks) is put in the agent's computer, once (a row placed already is skipped).
        Raises ComputerError (placing failed: nothing is sent; what was placed already stays)."""
        folder = None
        for row in rows:
            if row["path"] is not None or (row["kind"] == "image" and images):
                continue
            if folder is None:
                folder = await self._folder(agent)
            out = await self.control.computer.upload(agent, folder, row["name"], "rename", self._chunks(row["id"]),
                                                     row["size"], action="attachment")
            path = out.get("path") or f"{folder}/{row['name']}"
            self.db.execute("UPDATE attachments SET path = ?, kind = 'file' WHERE id = ?", (path, row["id"]))
            row["path"], row["kind"] = path, "file"

    def stored_blocks(self, agent: str, rows: list[dict]) -> list[dict]:
        """The blocks the session's user_message events keep: an image by its uri here, a file as a link to it in
        the agent's computer."""
        out = []
        for row in rows:
            if row["kind"] == "image":
                out.append({"type": "image", "mimeType": row["mime"], "uri": self.url(agent, row["id"]),
                            "name": row["name"], "size": row["size"]})
            else:
                out.append({"type": "resource_link", "uri": "file://" + row["path"],
                            "name": row["path"].rsplit("/", 1)[-1], "mimeType": row["mime"], "size": row["size"]})
        return out

    async def prompt_blocks(self, agent: str, rows: list[dict]) -> list[dict]:
        """The blocks the harness gets: an image's bytes, a text line naming a file's path."""
        out = []
        for row in rows:
            if row["kind"] == "image":
                try:
                    data = await asyncio.to_thread(self.path(row["id"]).read_bytes)
                except OSError:
                    out.append({"type": "text", "text": f"[Attached image {row['name']}: no longer available]"})
                    continue
                out.append({"type": "image", "mimeType": row["mime"], "data": base64.b64encode(data).decode()})
            else:
                out.append({"type": "text",
                            "text": f"[Attached file: {row['path']} ({row['mime']}, {human(row['size'])})]"})
        return out

    async def _folder(self, agent: str) -> str:
        """attachments/ in the agent's first writable folder, made if it's missing."""
        listing = await self.control.computer.list(agent, None)
        root = next((r["path"] for r in listing.get("roots") or [] if r.get("writable")), None)
        if root is None:
            raise ComputerError(f"{agent} has no folder to put files in", 409)
        folder = f"{root.rstrip('/')}/{FOLDER}"
        await self.control.computer.mkdir(agent, folder, audit=False)
        return folder

    async def _chunks(self, att_id: str) -> AsyncIterator[bytes]:
        with self.path(att_id).open("rb") as f:
            while chunk := await asyncio.to_thread(f.read, CHUNK):
                yield chunk

    def claim(self, rows: list[dict], session_id: str | None) -> None:
        """A prompt carries them now (a turn, or the queue): an image stays (its event names it), a placed file's
        copy goes."""
        for row in rows:
            self.db.execute("UPDATE attachments SET session_id = ? WHERE id = ?", (session_id or "", row["id"]))
            row["session_id"] = session_id or ""
            if row["kind"] == "file":
                self.path(row["id"]).unlink(missing_ok=True)

    def rows(self, agent: str, ids: list[str]) -> list[dict]:
        """The rows of these ids, in order (those still here)."""
        rows = [self.db.one("SELECT * FROM attachments WHERE id = ? AND agent = ?", (i, agent)) for i in ids]
        return [r for r in rows if r]

    def discard(self, rows: list[dict]) -> None:
        """A queued prompt the owner took back: its attachments go."""
        for row in rows:
            self._forget(row["id"])
