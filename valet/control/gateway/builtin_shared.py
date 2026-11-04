"""valet.shared: the agent's shared space (control/space.py) as gateway tools, for an agent whose definition lists it
in tools.mcp. It is between the agent and the owner only: every call reaches the calling agent's own items and nothing
else (space._row with who=<the agent>), so another agent's item id is "no such item", by tool and by ticket alike.
Reading is read-only; adding is additive (destructiveHint false) and changing or deleting destructive, so
the gateway decides them like any other tool at the agent's level.

Files travel inline when they're small: a text file as text (INLINE_TEXT), an image as MCP image content
(INLINE_IMAGE), anything else as base64 (INLINE_BINARY); an agent sends one the same way, as `content` or
`content_base64` (INLINE_IN). Anything bigger goes through a transfer ticket: shared_get gives a download URL,
shared_add_file and shared_update with `upload: true` an upload URL to PUT the file to, both on the agent's own
gateway endpoint (/mcp/agents/<name>/shared/<ticket>, Gateway.asgi -> transfer()). A ticket is good for one file,
for TICKET_TTL, while the agent runs with the gateway token it had when the ticket was issued (a restart ends it),
and an upload's for one PUT. The URL is the credential: the agent's gateway token never has to reach its shell,
and a ticket only comes from a tool call the gateway has already decided (an add at `ask` waits for the owner first).
Each transfer is in the audit log, as valet.shared `download` or `upload`."""
import asyncio
import base64
import binascii
import json
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import mcp_types as types

from valet.shared.definition import DefinitionError

from .. import space as sp
from .upstream import Upstream, UpstreamError, say_tools

INLINE_TEXT = 128 * 1024        # bytes: a text file comes as text up to this
INLINE_IMAGE = 1024 * 1024      # an image as image content
INLINE_BINARY = 16 * 1024       # anything else as base64
INLINE_IN = 1024 * 1024         # bytes an agent may send inline (content / content_base64)
TICKET_TTL = 15 * 60            # seconds
IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")
TEXT_TYPE = re.compile(r"^(text/.+|application/(json|xml|yaml|x-yaml|toml|javascript|x-sh|sql|x-python|csv|"
                       r"ld\+json|x-ndjson|.+\+json|.+\+xml))$")
TEXT_SUFFIXES = (".md", ".markdown", ".txt", ".log", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".env", ".py", ".ts",
                 ".tsx", ".jsx", ".rs", ".go", ".swift", ".kt", ".rb", ".sh", ".sql", ".csv", ".tsv")
CHUNK = 1024 * 1024

READ = types.ToolAnnotations(read_only_hint=True, open_world_hint=False)
ADD = types.ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
CHANGE = types.ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)
ID = {"type": "integer", "description": "The item's id (shared_list)"}


def _schema(required: list[str], **props: dict) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


def _result(data: Any, extra: list | None = None) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(text=json.dumps(data, indent=1, ensure_ascii=False)),
                                         *(extra or [])], structured_content=data)


def _error(text: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(text=text)], is_error=True)


def is_text(mime: str | None, name: str) -> bool:
    return bool(TEXT_TYPE.match(mime or "")) or name.lower().endswith(TEXT_SUFFIXES)


@dataclass
class Ticket:
    agent: str
    token_hash: str             # the gateway token the agent had: a restart issues a new one, and the ticket ends
    kind: str                   # download | upload (a new file) | replace (an item's file)
    expires: float
    item_id: int | None = None
    name: str = ""
    text: str = ""


class SharedServer(Upstream):
    name = "valet.shared"
    description = ("Your shared space with {owner}: notes and files between you and {owner} only (no other agent sees "
                   "them), newest change first. {Owner} sees what you add or change as new.")

    TOOLS = [
        types.Tool(name="shared_list", title="List your shared space",
                   description="Notes and files in your shared space with {owner}, most recently changed first: id, "
                               "kind (note or file), title (a file's name), who added and last changed it (`owner` or "
                               "`me`), when, and the first line of its text.",
                   input_schema=_schema(
                       [], query={"type": "string", "description": "Only items whose title or text contain this"},
                       by={"type": "string", "enum": ["owner", "me"],
                           "description": "Who added them: owner (the person you share with) or me (you)"},
                       kind={"type": "string", "enum": ["note", "file"]},
                       limit={"type": "integer", "minimum": 1, "maximum": sp.PAGE_MAX,
                              "description": f"How many (default {sp.PAGE})"},
                       offset={"type": "integer", "minimum": 0, "description": "Skip this many (paging)"}),
                   annotations=READ),
        types.Tool(name="shared_get", title="Get a shared item",
                   description=f"One item: a note's text, or a file's description and its content. Small files come "
                               f"inline (text up to {INLINE_TEXT // 1024} KiB as text, images up to "
                               f"{INLINE_IMAGE // 1024 // 1024} MiB as images, anything else up to "
                               f"{INLINE_BINARY // 1024} KiB as base64); a bigger one, or any with download: true, "
                               f"comes as a download URL to fetch from your computer (curl -o <file> <url>), good "
                               f"for {TICKET_TTL // 60} minutes.",
                   input_schema=_schema(["id"], id=ID, download={
                       "type": "boolean", "description": "A download URL even for a small file"}),
                   annotations=READ),
        types.Tool(name="shared_add_note", title="Add a note to your shared space",
                   description="A new note for {owner}: Markdown text, and a title (else its first line).",
                   input_schema=_schema(["text"], title={"type": "string"}, text={"type": "string"}),
                   annotations=ADD),
        types.Tool(name="shared_add_file", title="Add a file to your shared space",
                   description=f"A new file for {{owner}}, named `name`. Give its content inline (`content` for text, "
                               f"`content_base64` for anything else, up to {INLINE_IN // 1024 // 1024} MiB), or "
                               f"upload: true for an upload URL to PUT the file to from your computer (curl -T "
                               f"<file> <url>), good for {TICKET_TTL // 60} minutes; the item appears once it's in.",
                   input_schema=_schema(
                       ["name"], name={"type": "string", "description": "The file's name, like report.pdf"},
                       content={"type": "string"}, content_base64={"type": "string"},
                       upload={"type": "boolean"},
                       description={"type": "string", "description": "What it is (optional)"}),
                   annotations=ADD),
        types.Tool(name="shared_update", title="Change a shared item",
                   description="Change a note's title or text, or a file's name, description or content (content, "
                               "content_base64, or upload: true for an upload URL, as shared_add_file): yours or "
                               "{owner}'s.",
                   input_schema=_schema(
                       ["id"], id=ID, title={"type": "string", "description": "A note's title, a file's name"},
                       text={"type": "string", "description": "A note's text, a file's description"},
                       content={"type": "string"}, content_base64={"type": "string"},
                       upload={"type": "boolean"}),
                   annotations=CHANGE),
        types.Tool(name="shared_delete", title="Delete a shared item",
                   description="Remove a note or file from your shared space, for {owner} too.",
                   input_schema=_schema(["id"], id=ID), annotations=CHANGE),
    ]

    def __init__(self, control: Any) -> None:
        self.control = control
        self.tickets: dict[str, Ticket] = {}

    @property
    def space(self) -> sp.SharedSpace:
        return self.control.shared

    async def list_tools(self, fresh: bool = True) -> list[types.Tool]:
        return say_tools(self.TOOLS)

    # ---------- tools ----------

    async def call_tool(self, tool: str, arguments: dict[str, Any] | None, timeout: float,
                        caller: str) -> types.CallToolResult:
        args = arguments or {}
        handler = {"shared_list": self._list, "shared_get": self._get, "shared_add_note": self._add_note,
                   "shared_add_file": self._add_file, "shared_update": self._update,
                   "shared_delete": self._delete}.get(tool)
        if handler is None:
            raise UpstreamError(f"valet.shared has no tool {tool!r}")
        try:
            return await handler(caller, args)
        except sp.NotFound as e:
            return _error(f"{e}: shared_list shows what's there.")
        except sp.SharedError as e:
            return _error(str(e))

    async def _list(self, agent: str, args: dict) -> types.CallToolResult:
        by = {"owner": sp.OWNER, "me": agent, "": ""}.get(_str(args.get("by")))
        if by is None:
            raise sp.SharedError("by is owner or me")
        out = self.space.listing(agent, q=_str(args.get("query")), by=by, kind=_str(args.get("kind")),
                                 limit=_int(args.get("limit"), sp.PAGE), offset=_int(args.get("offset"), 0))
        return _result({"items": [sp.for_agent(i) for i in out["items"]], "more": out["more"]})

    async def _get(self, agent: str, args: dict) -> types.CallToolResult:
        item = self.space.get(_id(args), agent)
        view = sp.for_agent(item)
        if item["kind"] == "note":
            return _result(view)
        _, path = self.space.file_path(item["id"], agent)
        size, mime = item["size"] or 0, item["mime"] or "application/octet-stream"
        if args.get("download") is True:
            return _result({**view, **self._download(agent, item["id"], asked=True)})
        if mime in IMAGE_TYPES and size <= INLINE_IMAGE:
            data = base64.b64encode(await asyncio.to_thread(path.read_bytes)).decode()
            return _result(view, [types.ImageContent(data=data, mime_type=mime)])
        if is_text(mime, item["title"]) and size <= INLINE_TEXT:
            raw = await asyncio.to_thread(path.read_bytes)
            try:
                return _result({**view, "content": raw.decode()})
            except UnicodeDecodeError:
                pass
        if size <= INLINE_BINARY:
            data = base64.b64encode(await asyncio.to_thread(path.read_bytes)).decode()
            return _result({**view, "content_base64": data})
        return _result({**view, **self._download(agent, item["id"])})

    async def _add_note(self, agent: str, args: dict) -> types.CallToolResult:
        item = await self.space.add_note(agent, agent, args.get("title"), args.get("text"))
        return _result({"ok": True, **sp.for_agent(item)})

    async def _add_file(self, agent: str, args: dict) -> types.CallToolResult:
        name = args.get("name")
        if not isinstance(name, str) or not name.strip():
            raise sp.SharedError("name the file (name: report.pdf)")
        text = args.get("description") or ""
        if not isinstance(text, str):
            raise sp.SharedError("the description must be a string")
        sp.check_text(text, "the description")
        how = _how(args)
        if how == "upload":
            return _result(self._upload(agent, "upload", name=sp.safe_name(name), text=text))
        src = await self._inline(args, how)
        item = await self.space.add_file(agent, agent, name, src, text)
        return _result({"ok": True, **sp.for_agent(item)})

    async def _update(self, agent: str, args: dict) -> types.CallToolResult:
        item_id = _id(args)
        item = self.space.get(item_id, agent)
        how = _how(args, required=False)
        if how and item["kind"] != "file":
            raise sp.SharedError(f"shared item {item_id} is a note: change its text instead")
        title, text = args.get("title"), args.get("text")
        if how is None and title is None and text is None:
            raise sp.SharedError("nothing to change: give a title, text, or a file's new content")
        out: dict[str, Any] = {}
        if title is not None or text is not None:
            item = await self.space.update(item_id, agent, title, text)
        if how == "upload":
            out = self._upload(agent, "replace", item_id=item_id)
        elif how is not None:
            item = await self.space.replace_file(item_id, agent, await self._inline(args, how))
        return _result({"ok": True, **sp.for_agent(item), **out})

    async def _delete(self, agent: str, args: dict) -> types.CallToolResult:
        item = await self.space.delete(_id(args), agent)
        return _result({"ok": True, "deleted": item["id"], "title": item["title"]})

    async def _inline(self, args: dict, how: str) -> Path:
        """content / content_base64 into an incoming file."""
        if how == "content":
            if not isinstance(args["content"], str):
                raise sp.SharedError("content must be a string (content_base64 for anything that isn't text)")
            data = args["content"].encode()
        else:
            try:
                data = base64.b64decode(_str(args["content_base64"]), validate=True)
            except (binascii.Error, ValueError):
                raise sp.SharedError("content_base64 isn't valid base64") from None
        if len(data) > INLINE_IN:
            raise sp.SharedError(f"inline content is at most {INLINE_IN // 1024 // 1024} MiB: use upload: true for "
                                 "a bigger file")
        if len(data) > self.space.max_bytes:
            raise self.space.too_big("the file")
        dest = self.space.incoming()
        await asyncio.to_thread(dest.write_bytes, data)
        return dest

    # ---------- tickets ----------

    def _has(self, agent: str) -> bool:
        """The agent's saved definition still gives it valet.shared."""
        try:
            return self.name in self.control.defs.definition(agent).gateway_servers()
        except (DefinitionError, FileNotFoundError):
            return False

    def _computer(self, agent: str) -> bool:
        return self.control.defs.definition(agent).computer.level != "none"

    def _issue(self, agent: str, kind: str, **fields: Any) -> tuple[str, Ticket]:
        row = self.control.db.one("SELECT token_hash FROM agents WHERE name = ?", (agent,))
        if not row or not row["token_hash"]:
            raise sp.SharedError("Valet can't hand out a transfer URL: this agent has no gateway token")
        now = time.time()
        for k in [k for k, t in self.tickets.items() if t.expires <= now]:
            del self.tickets[k]
        ticket = Ticket(agent=agent, token_hash=row["token_hash"], kind=kind, expires=now + TICKET_TTL, **fields)
        key = secrets.token_urlsafe(32)
        self.tickets[key] = ticket
        return f"{self.control.gateway.url(agent)}/shared/{key}", ticket

    def _download(self, agent: str, item_id: int, asked: bool = False) -> dict[str, Any]:
        item = self.space.get(item_id, agent)
        if not self._computer(agent):
            raise sp.SharedError("with no computer there's nowhere to download a file to: leave download out"
                                 if asked else f"{item['title']} is too big to hand over inline "
                                 f"({sp_size(item['size'])}), and with no computer there's nowhere to download it to")
        url, _ = self._issue(agent, "download", item_id=item_id)
        return {"download_url": url, "expires_in": TICKET_TTL,
                "how": f"curl -fsS -o {_shell_name(item['title'])} '{url}'"}

    def _upload(self, agent: str, kind: str, **fields: Any) -> dict[str, Any]:
        if not self._computer(agent):
            raise sp.SharedError("with no computer there's nothing to upload from: give the content inline")
        url, _ = self._issue(agent, kind, **fields)
        return {"upload_url": url, "expires_in": TICKET_TTL, "method": "PUT", "max_bytes": self.space.max_bytes,
                "how": f"curl -fsS -T <file> '{url}'"}

    async def transfer(self, scope, receive, send, agent: str, key: str) -> None:
        """/mcp/agents/<agent>/shared/<ticket>: GET a download ticket's file, PUT an upload ticket's."""
        from ..api_shared import file_response
        from . import _json
        gw = self.control.gateway
        started = time.time()
        t = self.tickets.get(key)
        row = self.control.db.one("SELECT token_hash, state FROM agents WHERE name = ?", (agent,))
        if (t is None or t.agent != agent or t.expires <= started or not row or row["token_hash"] != t.token_hash
                or row["state"] not in ("starting", "running") or not self._has(agent)):
            if t is not None and t.expires <= started:
                self.tickets.pop(key, None)
            return await _json(send, 404, {"detail": "no such transfer (it may have expired: ask for a new URL)"})
        method = scope.get("method", "GET")
        if t.kind == "download":
            if method not in ("GET", "HEAD"):
                return await _json(send, 405, {"detail": "a download URL takes GET"}, [(b"allow", b"GET, HEAD")])
            try:
                item, path = self.space.file_path(t.item_id, agent)
            except sp.NotFound as e:
                gw._audit(agent, self.name, "download", {"id": t.item_id}, "allow", "error", started, str(e))
                return await _json(send, 404, {"detail": str(e)})
            await file_response(item, path)(scope, receive, send)
            gw._audit(agent, self.name, "download", {"id": item["id"], "name": item["title"], "size": item["size"]},
                      "allow", "ok", started, None)
            return
        if method != "PUT":
            return await _json(send, 405, {"detail": "an upload URL takes PUT"}, [(b"allow", b"PUT")])
        self.tickets.pop(key, None)                 # one PUT per upload ticket, whatever comes of it
        args = {"id": t.item_id} if t.kind == "replace" else {"name": t.name}
        try:
            src = await self._receive(scope, receive)
            if t.kind == "replace":
                item = await self.space.replace_file(t.item_id, agent, src)
            else:
                item = await self.space.add_file(agent, agent, t.name, src, t.text)
        except (sp.SharedError, sp.NotFound) as e:
            status = 413 if isinstance(e, TooBig) else 404 if isinstance(e, sp.NotFound) else 422
            gw._audit(agent, self.name, "upload", args, "allow", "error", started, str(e))
            return await _json(send, status, {"detail": str(e)})
        gw._audit(agent, self.name, "upload", {**args, "size": item["size"]}, "allow", "ok", started, None)
        await _json(send, 201 if t.kind == "upload" else 200, {"ok": True, **sp.for_agent(item)})

    async def _receive(self, scope, receive) -> Path:
        """A PUT's body into an incoming file, within the limit (TooBig as soon as it's over)."""
        limit = self.space.max_bytes
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        n = headers.get("content-length", "")
        if n.isdigit() and int(n) > limit:
            raise TooBig(str(self.space.too_big("the file")))
        dest, size = self.space.incoming(), 0
        try:
            with dest.open("wb") as out:
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        raise sp.SharedError("the upload was cut off")
                    chunk = message.get("body", b"")
                    size += len(chunk)
                    if size > limit:
                        raise TooBig(str(self.space.too_big("the file")))
                    out.write(chunk)
                    if not message.get("more_body"):
                        break
        except BaseException:
            dest.unlink(missing_ok=True)
            raise
        return dest


class TooBig(sp.SharedError):
    pass


def _how(args: dict, required: bool = True) -> str | None:
    """Which of content / content_base64 / upload the call gives: exactly one (or none, when not `required`)."""
    given = [k for k in ("content", "content_base64") if args.get(k) is not None]
    if args.get("upload") is True:
        given.append("upload")
    if len(given) > 1:
        raise sp.SharedError("give the file one way: content, content_base64 or upload: true")
    if not given:
        if required:
            raise sp.SharedError("give the file's content (content or content_base64), or upload: true for an "
                                 "upload URL")
        return None
    return given[0]


def _id(args: dict) -> int:
    v = args.get("id")
    if isinstance(v, bool) or not isinstance(v, int | str) or not str(v).isdigit():
        raise sp.SharedError("id is a shared item's number (shared_list)")
    return int(v)


def _str(v: object) -> str:
    return v if isinstance(v, str) else ""


def _int(v: object, default: int) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) else default


def sp_size(n: int | None) -> str:
    n = n or 0
    return f"{n} B" if n < 1024 else f"{n / 1024:.0f} KiB" if n < 1024 * 1024 else f"{n / 1024 / 1024:.1f} MiB"


def _shell_name(name: str) -> str:
    """A file name safe to put in the curl line as is."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", name) or "file"
