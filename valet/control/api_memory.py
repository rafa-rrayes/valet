"""The owner's side of an agent's memory (control/memory.py), under /api/agents/{name}/memory (docs/API.md): the files,
each one's text, its history and older versions, and editing and deleting them. Every change is a commit, like the
agent's own, and a `memory` client frame."""
import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from valet.shared.definition import DefinitionError
from valet.shared.directives import MEMORY

from . import auth, memory
from .api import _agent_or_404, _control

router = APIRouter(prefix="/api", dependencies=[Depends(auth.require_owner)])


class MemoryText(BaseModel):
    text: str


def _name(file: str) -> str:
    try:
        return memory.file_name(file)
    except memory.MemoryError as e:
        raise HTTPException(422, str(e)) from None


@router.get("/agents/{name}/memory")
async def list_memory(name: str, request: Request):
    """Whether the agent has memory (`memory: true`) and the `memory` layer, and its files, by name."""
    c = _control(request)
    _agent_or_404(c, name)
    try:
        d = c.defs.definition(name)
        enabled, layer = d.memory, MEMORY in d.directives
    except DefinitionError:
        enabled = layer = None          # agent.yaml doesn't validate: the files are still there to see
    files = await asyncio.to_thread(memory.files, c.defs.root, name)
    return {"agent": name, "enabled": enabled, "layer": layer, "files": [f.view() for f in files],
            "size": sum(f.size for f in files), "limits": memory.LIMITS}


@router.get("/agents/{name}/memory/{file}")
async def read_memory(name: str, file: str, request: Request, at: str | None = Query(None)):
    """One file's text, now, or as of commit `at` (one of its history's)."""
    c = _control(request)
    _agent_or_404(c, name)
    n = _name(file)
    if at is not None:
        try:
            text = await asyncio.to_thread(memory.at, c.defs, name, n, at)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        if text is None:
            raise HTTPException(404, f"{n}.md wasn't there as of {at[:12]}")
        return {"name": n, "commit": at, "file": None, "text": text}
    try:
        f, text = await asyncio.to_thread(memory.read, c.defs.root, name, n)
    except FileNotFoundError:
        raise HTTPException(404, f"{name} has no memory file {n}.md") from None
    return {"name": n, "commit": None, "file": f.view(), "text": text}


@router.put("/agents/{name}/memory/{file}")
async def write_memory(name: str, file: str, body: MemoryText, request: Request):
    """Create or replace a file, as one commit ("Memory: owner wrote <agent>'s <file>.md")."""
    c = _control(request)
    _agent_or_404(c, name)
    n = _name(file)
    try:
        f = await asyncio.to_thread(memory.write, c.defs, name, n, body.text, memory.OWNER)
    except memory.MemoryError as e:
        raise HTTPException(422, str(e)) from None
    await c.hub.broadcast(memory.frame(name, n, f))
    return {"file": f.view()}


@router.delete("/agents/{name}/memory/{file}")
async def delete_memory(name: str, file: str, request: Request):
    c = _control(request)
    _agent_or_404(c, name)
    n = _name(file)
    try:
        await asyncio.to_thread(memory.delete, c.defs, name, n, memory.OWNER)
    except FileNotFoundError:
        raise HTTPException(404, f"{name} has no memory file {n}.md") from None
    await c.hub.broadcast(memory.frame(name, n, None))
    return {"ok": True}


@router.get("/agents/{name}/memory/{file}/history")
async def memory_history(name: str, file: str, request: Request, limit: int = Query(50, ge=1, le=200)):
    """The commits that changed the file, newest first (a deleted file's included)."""
    c = _control(request)
    _agent_or_404(c, name)
    n = _name(file)
    return {"name": n, "history": await asyncio.to_thread(memory.history, c.defs, name, n, limit)}
