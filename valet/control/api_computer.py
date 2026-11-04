"""Routes for the agent's computer and attachments (docs/API.md, "The agent's computer" and "Attachments"): the
terminal's WebSocket /ws/terminal/<agent>, files, volumes, the audit, and the files the owner attaches to a message.
Thin: validation and auth here, the work in control/computer.py and control/attachments.py."""
import logging
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from valet.shared.definition import NAME_RE

from . import auth
from .attachments import AttachmentError, Attachments
from .computer import AUDIT_PAGE, AUDIT_PAGE_MAX, Computer, ComputerError
from .space import INLINE_TYPES, mime_of, safe_name

log = logging.getLogger(__name__)

router = APIRouter()
owner = APIRouter(prefix="/api", dependencies=[Depends(auth.require_owner)])
CLOSE_FORBIDDEN = 4403
CLOSE_UNAUTHORIZED = 4401
CLOSE_NOT_FOUND = 4404


def _computer(request: Request) -> Computer:
    return request.app.state.control.computer


def _attachments(request: Request) -> Attachments:
    return request.app.state.control.attachments


def _agent_or_404(request: Request, name: str) -> None:
    if not NAME_RE.match(name) or not request.app.state.control.defs.exists(name):
        raise HTTPException(404, f"no agent named {name}")


def _http(e: ComputerError | AttachmentError) -> HTTPException:
    return HTTPException(e.status, str(e))


def _declared(request: Request, limit: int) -> None:
    n = request.headers.get("content-length")
    if n and n.isdigit() and int(n) > limit:
        raise HTTPException(413, f"over the {limit // 1024 // 1024} MiB limit (VALET_MAX_FILE_MB)")


def _limit(request: Request) -> int:
    return request.app.state.control.cfg.max_file_mb * 1024 * 1024


def _disposition(name: str, inline: bool) -> str:
    name = safe_name(name)
    ascii_name = name.encode("ascii", "replace").decode().replace('"', "_").replace("?", "_")
    return f"{'inline' if inline else 'attachment'}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"


# ---------- the terminal ----------

@router.websocket("/ws/terminal/{agent}")
async def terminal_socket(ws: WebSocket, agent: str):
    c = ws.app.state.control
    await ws.accept()
    if not auth.origin_ok(ws.headers.get("origin"), c.cfg.public_url):
        log.warning("terminal socket from origin %s refused (VALET_PUBLIC_URL is %s)", ws.headers.get("origin"),
                    c.cfg.public_url)
        await ws.close(CLOSE_FORBIDDEN)
        return
    if not auth.websocket_is_owner(ws):
        await ws.close(CLOSE_UNAUTHORIZED)
        return
    if not NAME_RE.match(agent) or not c.defs.exists(agent):
        await ws.close(CLOSE_NOT_FOUND)
        return

    def size(key: str, default: int) -> int:
        v = ws.query_params.get(key, "")
        return min(max(int(v), 2), 1000) if v.isdigit() else default
    await c.computer.serve_terminal(ws, agent, size("cols", 80), size("rows", 24))


# ---------- files ----------

@owner.get("/agents/{name}/computer/files")
async def list_files(name: str, request: Request, path: str | None = None):
    """What's at `path` (a folder's entries, or a file's details); no path: the first root. With the roots."""
    _agent_or_404(request, name)
    try:
        return await _computer(request).list(name, path)
    except ComputerError as e:
        raise _http(e) from None


@owner.get("/agents/{name}/computer/files/text")
async def file_text(name: str, request: Request, path: str):
    _agent_or_404(request, name)
    try:
        return await _computer(request).text(name, path)
    except ComputerError as e:
        raise _http(e) from None


@owner.get("/agents/{name}/computer/files/download")
async def download_file(name: str, request: Request, path: str, inline: bool = False):
    """The file's bytes as they're read: an attachment, or with ?inline=1 shown in place when it's a type browsers
    show safely (the shared space's INLINE_TYPES). Never run as a page: CSP sandbox, nosniff."""
    _agent_or_404(request, name)
    try:
        size, chunks = await _computer(request).download(name, path)
    except ComputerError as e:
        raise _http(e) from None
    file_name = path.rstrip("/").rsplit("/", 1)[-1] or "file"
    mime = mime_of(file_name)
    shown = inline and bool(INLINE_TYPES.match(mime))
    return StreamingResponse(chunks, media_type=mime if shown else "application/octet-stream", headers={
        "content-length": str(size), "content-disposition": _disposition(file_name, shown),
        "content-security-policy": "sandbox"})


@owner.post("/agents/{name}/computer/files/upload", status_code=201)
async def upload_file(name: str, request: Request, folder: str = Query(alias="dir"), file: str = Query(alias="name"),
                      exists: Literal["fail", "replace", "rename"] = "fail"):
    """The raw body as `dir`/`name`: within VALET_MAX_FILE_MB as it streams (413), renamed into place at the end.
    exists: what to do if the name is taken (fail: 409)."""
    _agent_or_404(request, name)
    if not file or "/" in file or file in (".", "..") or "\0" in file or len(file.encode()) > 255:
        raise HTTPException(422, "not a file name")
    limit = _limit(request)
    _declared(request, limit)
    try:
        return await _computer(request).upload(name, folder, file, exists, request.stream(), limit)
    except ComputerError as e:
        raise _http(e) from None


class Folder(BaseModel):
    path: str


@owner.post("/agents/{name}/computer/files/folder", status_code=201)
async def make_folder(name: str, body: Folder, request: Request):
    _agent_or_404(request, name)
    try:
        return await _computer(request).mkdir(name, body.path)
    except ComputerError as e:
        raise _http(e) from None


# ---------- volumes and the audit ----------

@owner.get("/agents/{name}/computer/volumes")
async def list_volumes(name: str, request: Request):
    _agent_or_404(request, name)
    try:
        return await _computer(request).volumes(name)
    except ComputerError as e:
        raise _http(e) from None


@owner.delete("/agents/{name}/computer/volumes")
async def delete_volumes(name: str, request: Request):
    """Its Docker volumes (home and workspace) on its runner, for good. 409 while it runs."""
    _agent_or_404(request, name)
    try:
        return await _computer(request).remove_volumes(name)
    except ComputerError as e:
        raise _http(e) from None


@owner.get("/agents/{name}/computer/audit")
def computer_audit(name: str, request: Request, before: int | None = None,
                   limit: int = Query(AUDIT_PAGE, ge=1, le=AUDIT_PAGE_MAX)):
    _agent_or_404(request, name)
    return _computer(request).audit_list(name, before, limit)


# ---------- attachments ----------

@owner.post("/agents/{name}/attachments", status_code=201)
async def add_attachment(name: str, request: Request, file: str = Query(alias="name")):
    """The raw file as an attachment for a message to this agent, sent with the prompt's `attachments`."""
    _agent_or_404(request, name)
    if not file.strip():
        raise HTTPException(422, "name the file: ?name=<file name>")
    limit = _limit(request)
    _declared(request, limit)
    try:
        return await _attachments(request).add(name, file, request.stream(), limit)
    except AttachmentError as e:
        raise _http(e) from None


@owner.get("/agents/{name}/attachments/{att_id}")
def get_attachment(name: str, att_id: str, request: Request):
    """Its bytes: an image shown in place (CSP sandbox), anything else as a download. 404 once a sent file's copy
    went (it's in the agent's computer, at its `path`)."""
    _agent_or_404(request, name)
    a = _attachments(request)
    try:
        row = a.get(name, att_id)
    except AttachmentError as e:
        raise _http(e) from None
    path = a.path(att_id)
    if not path.is_file():
        raise HTTPException(404, f"it's in {name}'s computer now: {row['path']}" if row["path"] else "it's gone")
    shown = row["kind"] == "image" and bool(INLINE_TYPES.match(row["mime"]))
    return FileResponse(path, media_type=row["mime"] if shown else "application/octet-stream", headers={
        "content-disposition": _disposition(row["name"], shown), "content-security-policy": "sandbox"})


@owner.delete("/agents/{name}/attachments/{att_id}")
def delete_attachment(name: str, att_id: str, request: Request):
    _agent_or_404(request, name)
    try:
        _attachments(request).delete(name, att_id)
    except AttachmentError as e:
        raise _http(e) from None
    return {"ok": True}


router.include_router(owner)
