"""The owner's side of the shared space (control/space.py; docs/API.md): an agent's list with search and filters, a
new note or upload (a raw body, or multipart/form-data) in it, under /api/agents/{name}/shared; an item by its id
under /api/shared/{id} (downloads, edits, deletes, moving it to another agent); the unassigned items; what's
unseen."""
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.datastructures import UploadFile

from valet.shared.definition import NAME_RE

from . import auth
from .space import ANY, INLINE_TYPES, OWNER, PAGE, PAGE_MAX, NotFound, SharedError, SharedSpace, safe_name

router = APIRouter(prefix="/api", dependencies=[Depends(auth.require_owner)])
CHUNK = 1024 * 1024
FORM_SLACK = 64 * 1024          # a multipart body's bytes besides the files: boundaries, headers, the description


def _space(request: Request) -> SharedSpace:
    return request.app.state.control.shared


def _refused(e: Exception) -> HTTPException:
    return HTTPException(404 if isinstance(e, NotFound) else 422, str(e))


def _agent(request: Request, name: str) -> str:
    """An agent that exists (its definitions folder): its name, or 404."""
    if not NAME_RE.match(name) or not request.app.state.control.defs.exists(name):
        raise HTTPException(404, f"no agent named {name}")
    return name


class TooLarge(Exception):
    pass


def _limited(receive: Callable[[], Awaitable[dict]], limit: int) -> Callable[[], Awaitable[dict]]:
    """`receive` that raises TooLarge once more than `limit` bytes of body have come: an upload over the limit stops
    as it streams, not once it's all on disk."""
    seen = 0

    async def wrapped() -> dict:
        nonlocal seen
        message = await receive()
        if message["type"] == "http.request":
            seen += len(message.get("body", b""))
            if seen > limit:
                raise TooLarge()
        return message
    return wrapped


def _declared(request: Request, limit: int) -> None:
    """413 at once for a body that says it's too big."""
    n = request.headers.get("content-length")
    if n and n.isdigit() and int(n) > limit:
        raise HTTPException(413, f"over the {limit // 1024 // 1024} MiB limit (VALET_MAX_FILE_MB)")


async def _receive_raw(request: Request, space: SharedSpace) -> Path:
    """The raw body into a new incoming path, within the limit (413)."""
    limit = space.max_bytes
    _declared(request, limit)
    dest, size = space.incoming(), 0
    try:
        with dest.open("wb") as out:
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    raise HTTPException(413, f"over the {space.control.cfg.max_file_mb} MiB limit "
                                             "(VALET_MAX_FILE_MB)")
                out.write(chunk)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    return dest


async def _receive_form(request: Request, space: SharedSpace) -> tuple[list[tuple[str, Path]], str | None]:
    """A multipart body's files (each `file` part: (its file name, an incoming path)) and its `text` field."""
    limit = space.max_bytes + FORM_SLACK
    _declared(request, limit)
    form_request = Request(request.scope, _limited(request.receive, limit))
    got: list[tuple[str, Path]] = []
    try:
        async with form_request.form(max_files=100, max_fields=20) as form:
            text = form.get("text")
            for part in form.getlist("file"):
                if not isinstance(part, UploadFile):
                    continue
                dest = space.incoming()
                got.append((part.filename or "file", dest))
                with dest.open("wb") as out:
                    while chunk := await part.read(CHUNK):
                        out.write(chunk)
    except TooLarge:
        for _, p in got:
            p.unlink(missing_ok=True)
        raise HTTPException(413, f"over the {space.control.cfg.max_file_mb} MiB limit (VALET_MAX_FILE_MB)") from None
    except BaseException:
        for _, p in got:
            p.unlink(missing_ok=True)
        raise
    return got, text if isinstance(text, str) else None


def _multipart(request: Request) -> bool:
    return request.headers.get("content-type", "").lower().startswith("multipart/form-data")


def _listing(request: Request, agent: str | None, q: str, by: str, kind: str, before: float | None,
             limit: int) -> dict:
    """{"items": [SharedItem], "more", "unseen"}: `agent`'s items (None: the unassigned; ANY: all), newest change
    first; `unseen` counts the same items."""
    space = _space(request)
    try:
        out = space.listing(agent, q=q, by=by, kind=kind, before=before, limit=limit)
    except SharedError as e:
        raise _refused(e) from None
    return {**out, "unseen": space.unseen_count(agent)}


@router.get("/agents/{name}/shared")
def list_agent_shared(name: str, request: Request, q: str = "", by: str = "", kind: str = "",
                      before: float | None = None, limit: int = Query(PAGE, ge=1, le=PAGE_MAX)):
    """The agent's shared space."""
    return _listing(request, _agent(request, name), q, by, kind, before, limit)


@router.get("/shared")
def list_shared(request: Request, q: str = "", by: str = "", kind: str = "", before: float | None = None,
                limit: int = Query(PAGE, ge=1, le=PAGE_MAX)):
    """Every item, whoever's (each says its agent)."""
    return _listing(request, ANY, q, by, kind, before, limit)


@router.get("/shared/unassigned")
def list_unassigned(request: Request, q: str = "", by: str = "", kind: str = "", before: float | None = None,
                    limit: int = Query(PAGE, ge=1, le=PAGE_MAX)):
    """Items from when the space was everyone's, whose agent couldn't be told: only the owner sees them."""
    return _listing(request, None, q, by, kind, before, limit)


@router.get("/shared/unseen")
def shared_unseen(request: Request):
    """{"unseen": in all, "agents": {name: unseen}}."""
    return _space(request).counts()


class Seen(BaseModel):
    ids: list[int] | None = None        # None: everything


@router.post("/shared/seen")
async def shared_seen(body: Seen, request: Request):
    return await _space(request).mark_seen(body.ids)


class NewNote(BaseModel):
    title: str = ""
    text: str


@router.post("/agents/{name}/shared/notes", status_code=201)
async def add_note(name: str, body: NewNote, request: Request):
    agent = _agent(request, name)
    try:
        return await _space(request).add_note(agent, OWNER, body.title, body.text)
    except (SharedError, NotFound) as e:        # NotFound: the agent was deleted meanwhile
        raise _refused(e) from None


@router.post("/agents/{agent}/shared/files", status_code=201)
async def add_files(agent: str, request: Request, name: str = "", text: str = ""):
    """Upload: multipart/form-data (each `file` part an item, `text` the description), or the raw file as the body
    with ?name= (and ?text=). [SharedItem]."""
    agent = _agent(request, agent)
    space = _space(request)
    if _multipart(request):
        files, form_text = await _receive_form(request, space)
        text = form_text if form_text is not None else text
        if not files:
            raise HTTPException(422, "no file: send each one as a `file` part")
    else:
        if not name.strip():
            raise HTTPException(422, "name the file: ?name=<file name>")
        files = [(name, await _receive_raw(request, space))]
    out = []
    try:
        for i, (n, p) in enumerate(files):
            out.append(await space.add_file(agent, OWNER, n, p, text))
    except (SharedError, NotFound) as e:        # NotFound: the agent was deleted while its files arrived
        for _, p in files[i:]:
            p.unlink(missing_ok=True)
        raise _refused(e) from None
    return out


@router.get("/shared/{item_id}")
def get_shared(item_id: int, request: Request):
    try:
        return _space(request).get(item_id)
    except NotFound as e:
        raise _refused(e) from None


class Edit(BaseModel):
    title: str | None = None
    text: str | None = None


@router.patch("/shared/{item_id}")
async def edit_shared(item_id: int, body: Edit, request: Request):
    """A note's title and text; a file's name and description."""
    try:
        return await _space(request).update(item_id, OWNER, body.title, body.text)
    except (NotFound, SharedError) as e:
        raise _refused(e) from None


class Move(BaseModel):
    agent: str


@router.post("/shared/{item_id}/move")
async def move_shared(item_id: int, body: Move, request: Request):
    """The item into this agent's space (an unassigned one, or another agent's): SharedItem."""
    agent = _agent(request, body.agent)
    try:
        return await _space(request).move(item_id, agent)
    except NotFound as e:
        raise _refused(e) from None


@router.delete("/shared/{item_id}")
async def delete_shared(item_id: int, request: Request):
    try:
        await _space(request).delete(item_id, OWNER)
    except NotFound as e:
        raise _refused(e) from None
    return {"ok": True}


@router.get("/shared/{item_id}/file")
def download(item_id: int, request: Request, inline: bool = False):
    """The file's bytes: an attachment, or with ?inline=1 shown in place when it's a type browsers show safely
    (INLINE_TYPES: images, PDF, plain text, audio, video). Never run as a page: CSP sandbox (and SecurityHeaders'
    nosniff)."""
    try:
        item, path = _space(request).file_path(item_id)
    except NotFound as e:
        raise _refused(e) from None
    return file_response(item, path, inline)


def file_response(item: dict[str, Any], path: Path, inline: bool = False) -> FileResponse:
    """A shared file as a download (the gateway's transfer route serves it the same way)."""
    mime = item["mime"] or "application/octet-stream"
    shown = inline and bool(INLINE_TYPES.match(mime))
    name = safe_name(item["title"])
    ascii_name = name.encode("ascii", "replace").decode().replace('"', "_").replace("?", "_")
    disposition = f"{'inline' if shown else 'attachment'}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"
    return FileResponse(path, media_type=mime if shown else "application/octet-stream",
                        headers={"content-disposition": disposition, "content-security-policy": "sandbox"})


@router.put("/shared/{item_id}/file")
async def replace_file(item_id: int, request: Request, name: str = ""):
    """A file item's new bytes: the raw body (?name= to rename the file), or multipart with one `file` part."""
    space = _space(request)
    try:
        space.file_path(item_id)
    except NotFound as e:
        raise _refused(e) from None
    if _multipart(request):
        files, _ = await _receive_form(request, space)
        if len(files) != 1:
            for _, p in files:
                p.unlink(missing_ok=True)
            raise HTTPException(422, "send exactly one `file` part")
        name, src = name or files[0][0], files[0][1]
    else:
        src = await _receive_raw(request, space)
    try:
        return await space.replace_file(item_id, OWNER, src, name or None)
    except (NotFound, SharedError) as e:
        raise _refused(e) from None
