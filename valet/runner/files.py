"""The agent's files, for the owner (files.list / read / write / cancel / mkdir, docs/PROTOCOL.md). What an agent's files are
(its roots) follows from its computer: a container's volumes and shared folders, a host agent's working folder; an
agent at level `none` has none.

Every path is absolute and inside a root as written, with no `.` or `..` parts (check_path, here), and again once
its links are resolved, where the files are: on the runner's machine for a host agent (HostFiles, in Python), inside
the container for the others (ContainerFiles: computer.cjs run with `docker exec ... node -e`). An upload is written
next to its target as `.valet-upload-<id>` and renamed into place at the end, so nothing is ever written through a
link and a half-done upload never shows under its name. On the host the agent runs as the runner's user and could
swap a link between the check and the use: the host level gives it that user's reach anyway."""
import asyncio
import base64
import errno
import json
import logging
import os
import re
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import acp

from valet.shared.config import RunnerConfig
from valet.shared.definition import AgentDefinition

from .egress import docker_env
from .launch import host_workdir

log = logging.getLogger(__name__)

HELPER = Path(__file__).with_name("computer.cjs").read_text()
MAX_ENTRIES = 2000
UPLOAD_RE = re.compile(r"^[0-9a-f]{8,64}$")
UPLOAD_PREFIX = ".valet-upload-"
STALE_UPLOAD = 15 * 60          # seconds an upload may sit without a chunk before its partial file is dropped
HELPER_TIMEOUT = 60.0
ERRNO = {"ENOENT": "no such file or folder", "ENOTDIR": "not a folder", "EISDIR": "it's a folder",
         "EACCES": "permission denied", "EPERM": "permission denied", "ELOOP": "it's a link", "EROFS": "read-only",
         "ENOSPC": "no space left", "EEXIST": "exists", "EDQUOT": "no space left",
         "ENAMETOOLONG": "the name is too long"}


class FilesError(Exception):
    """Refused or failed; the message is shown to the owner as is."""


@dataclass(frozen=True)
class Root:
    path: str           # absolute, as the agent sees it
    label: str
    writable: bool

    def view(self) -> dict[str, Any]:
        return {"path": self.path, "label": self.label, "writable": self.writable}


def roots_of(definition: AgentDefinition, cfg: RunnerConfig) -> list[Root]:
    """container: /work (its workspace volume), /home/agent (its home volume), then its shared folders; none: nothing
    (it has no computer); host: its working folder, absolute."""
    computer = definition.computer
    if computer.level == "none":
        return []
    if computer.level == "host":
        return [Root(os.path.abspath(host_workdir(definition, cfg)), "Working folder", True)]
    out = [Root("/work", "Workspace", True), Root("/home/agent", "Home", True)]
    out += [Root(_norm(m.path), f"Shared folder {m.host}", not m.readonly) for m in computer.mounts]
    return out


def _norm(path: object) -> str:
    """An absolute path with no `.` or `..` parts, written plainly (no double or trailing slashes)."""
    if not isinstance(path, str) or not path.startswith("/") or "\0" in path:
        raise FilesError("a path must be absolute")
    parts = [p for p in path.split("/") if p]
    if any(p in (".", "..") for p in parts):
        raise FilesError("a path can't have . or .. in it")
    return "/" + "/".join(parts)


def _within(path: str, root: str) -> bool:
    return root == "/" or path == root or path.startswith(root + "/")


def check_path(path: object, roots: list[Root]) -> str:
    """The path written plainly, if it's inside one of the roots as written. FilesError otherwise."""
    p = _norm(path)
    if not any(_within(p, r.path) for r in roots):
        raise FilesError(f"{p} is outside its folders")
    return p


def check_name(name: object) -> str:
    if (not isinstance(name, str) or not name or "/" in name or "\0" in name or name in (".", "..")
            or len(name.encode()) > 255):
        raise FilesError("not a file name")
    if name.startswith(UPLOAD_PREFIX):
        raise FilesError("that name is Valet's own")
    return name


def _why(e: OSError) -> str:
    return ERRNO.get(errno.errorcode.get(e.errno or 0, ""), e.strerror or str(e))


def _type(st: os.stat_result) -> str:
    return "dir" if stat.S_ISDIR(st.st_mode) else "file" if stat.S_ISREG(st.st_mode) else "other"


def _sort_key(name: str) -> tuple[str, str]:
    return (name.lower(), name)


class Uploads:
    """Uploads in progress (upload id -> (agent, folder, last chunk's time)), so one left behind (the control plane
    went away mid-upload) has its partial file dropped after STALE_UPLOAD."""

    def __init__(self) -> None:
        self._open: dict[str, tuple[str, str, float]] = {}

    def touch(self, agent: str, upload: str, folder: str) -> None:
        self._open[upload] = (agent, folder, time.monotonic())

    def done(self, upload: str) -> None:
        self._open.pop(upload, None)

    def stale(self) -> list[tuple[str, str, str]]:
        """(agent, upload, folder) of the ones idle too long; forgotten here (the caller drops their files)."""
        now = time.monotonic()
        out = [(a, u, f) for u, (a, f, t) in self._open.items() if now - t > STALE_UPLOAD]
        for _, u, _ in out:
            self._open.pop(u, None)
        return out


class HostFiles:
    """A host agent's files: its working folder on the runner's machine, as the runner's user."""

    def __init__(self, roots: list[Root]) -> None:
        self.roots = roots

    def _real_roots(self) -> list[tuple[str, Root]]:
        out = []
        for r in self.roots:
            try:
                out.append((os.path.realpath(r.path, strict=True), r))
            except OSError:
                pass        # a root that isn't there holds nothing
        return out

    def _root_of(self, real: str) -> Root | None:
        best: tuple[str, Root] | None = None
        for rp, r in self._real_roots():
            if _within(real, rp) and (best is None or len(rp) > len(best[0])):
                best = (rp, r)
        return best[1] if best else None

    def _resolve(self, path: object) -> tuple[str, str, Root]:
        p = check_path(path, self.roots)
        try:
            real = os.path.realpath(p, strict=True)
        except OSError as e:
            raise FilesError(_why(e)) from None
        root = self._root_of(real)
        if root is None:
            raise FilesError(f"{p} leads outside its folders")
        return p, real, root

    async def list(self, path: str | None) -> dict[str, Any]:
        return await asyncio.to_thread(self._list, path or self.roots[0].path)

    def _entry(self, folder: str, name: str) -> dict[str, Any] | None:
        full = os.path.join(folder, name)
        try:
            st = os.lstat(full)
        except OSError:
            return None
        link = stat.S_ISLNK(st.st_mode)
        kind = _type(st)
        if link:
            kind = "other"
            try:
                real = os.path.realpath(full, strict=True)
                if self._root_of(real) is not None:
                    st = os.stat(real)
                    kind = _type(st)
            except OSError:
                pass
        return {"name": name, "type": kind, "link": link, "size": st.st_size, "mtime": st.st_mtime}

    def _list(self, path: str) -> dict[str, Any]:
        p, real, _ = self._resolve(path)
        try:
            st = os.stat(real)
            out = {"path": p, "type": _type(st), "size": st.st_size, "mtime": st.st_mtime, "entries": [],
                   "truncated": False}
            if out["type"] != "dir":
                return out
            names = [n for n in os.listdir(real) if not n.startswith(UPLOAD_PREFIX)]
        except OSError as e:
            raise FilesError(_why(e)) from None
        if len(names) > MAX_ENTRIES:
            names = sorted(names, key=_sort_key)[:MAX_ENTRIES]
            out["truncated"] = True
        entries = [e for e in (self._entry(real, n) for n in names) if e]
        out["entries"] = sorted(entries, key=lambda e: (e["type"] != "dir", *_sort_key(e["name"])))
        return out

    async def read(self, path: str, offset: int, length: int) -> tuple[dict[str, Any], bytes]:
        return await asyncio.to_thread(self._read, path, offset, length)

    def _read(self, path: str, offset: int, length: int) -> tuple[dict[str, Any], bytes]:
        _, real, _ = self._resolve(path)
        try:
            fd = os.open(real, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError as e:
            raise FilesError(_why(e)) from None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise FilesError("not a file")
            data = os.pread(fd, max(0, min(length, st.st_size - offset)), offset) if offset < st.st_size else b""
        except OSError as e:
            raise FilesError(_why(e)) from None
        finally:
            os.close(fd)
        return {"size": st.st_size, "eof": offset + len(data) >= st.st_size}, data

    async def write(self, upload: str, folder: str, name: str, offset: int, data: bytes, final: bool,
                    exists: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._write, upload, folder, name, offset, data, final, exists)

    def _write(self, upload: str, folder: str, name: str, offset: int, data: bytes, final: bool,
               exists: str) -> dict[str, Any]:
        p, real, root = self._resolve(folder)
        if not os.path.isdir(real):
            raise FilesError("not a folder")
        if not root.writable:
            raise FilesError(f"{root.path} is read-only")
        check_name(name)
        if not UPLOAD_RE.match(upload):
            raise FilesError("not an upload id")
        tmp = os.path.join(real, UPLOAD_PREFIX + upload)
        try:
            flags = os.O_WRONLY | os.O_NOFOLLOW | (os.O_CREAT | os.O_EXCL if offset == 0 else os.O_APPEND)
            fd = os.open(tmp, flags, 0o644)
            try:
                if os.fstat(fd).st_size != offset:
                    raise FilesError("the upload is out of step")
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view):]
            finally:
                os.close(fd)
        except OSError as e:
            raise FilesError(_why(e)) from None
        size = offset + len(data)
        if not final:
            return {"size": size}
        try:
            placed = self._place(real, tmp, name, exists)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return {"path": (p.rstrip("/") or "") + "/" + placed, "size": size}

    @staticmethod
    def _place(folder: str, tmp: str, name: str, exists: str) -> str:
        try:
            if exists == "rename":
                stem, dot, ext = name.rpartition(".")
                stem, ext = (stem, dot + ext) if stem else (name, "")
                candidate, k = name, 2
                while os.path.lexists(os.path.join(folder, candidate)):
                    candidate, k = f"{stem}-{k}{ext}", k + 1
                os.rename(tmp, os.path.join(folder, candidate))
                return candidate
            target = os.path.join(folder, name)
            if os.path.lexists(target):
                if exists != "replace":
                    raise FilesError("exists")
                if not stat.S_ISREG(os.lstat(target).st_mode):
                    raise FilesError(f"{name} is a folder or a link: it can't be replaced")
            os.rename(tmp, target)      # rename(2) replaces the entry itself: never writes through a link
            return name
        except OSError as e:
            raise FilesError(_why(e)) from None

    async def cancel(self, upload: str, folder: str) -> None:
        await asyncio.to_thread(self._cancel, upload, folder)

    def _cancel(self, upload: str, folder: str) -> None:
        _, real, _ = self._resolve(folder)
        if not UPLOAD_RE.match(upload):
            raise FilesError("not an upload id")
        try:
            os.unlink(os.path.join(real, UPLOAD_PREFIX + upload))
        except FileNotFoundError:
            pass
        except OSError as e:
            raise FilesError(_why(e)) from None

    async def mkdir(self, path: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._mkdir, path)

    def _mkdir(self, path: str) -> dict[str, Any]:
        """Each missing folder is made inside its parent's real (resolved, checked) path."""
        p = check_path(path, self.roots)
        top = max((r for r in self.roots if _within(p, r.path)), key=lambda r: len(r.path))
        cur = top.path
        _, real, root = self._resolve(cur)
        for part in [x for x in p[len(top.path):].split("/") if x]:
            if not root.writable:
                raise FilesError(f"{root.path} is read-only")
            try:
                os.mkdir(os.path.join(real, part), 0o755)
            except FileExistsError:
                pass
            except OSError as e:
                raise FilesError(_why(e)) from None
            cur = (cur if cur != "/" else "") + "/" + part
            _, real, root = self._resolve(cur)
            if not os.path.isdir(real):
                raise FilesError(f"{cur} is not a folder")
        return {"path": p}


class ContainerFiles:
    """A container agent's files, reached inside its container: computer.cjs under `node -e`, after `exec` (`docker
    exec -i <container>`; tests run it with no prefix, on the runner's own node, against local folders)."""

    def __init__(self, roots: list[Root], exec_prefix: list[str], env: dict[str, str] | None = None) -> None:
        self.roots = roots
        self.exec_prefix = exec_prefix
        self.env = env

    async def helper(self, op: str, args: dict[str, Any], stdin: bytes | None = None) -> tuple[dict[str, Any], bytes]:
        """Run one op -> (its JSON line, the bytes after it). FilesError for its error, or when it can't run."""
        argv = [*self.exec_prefix, "node", "-e", HELPER, op,
                json.dumps({**args, "roots": [{"path": r.path, "writable": r.writable} for r in self.roots]})]
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=self.env if self.env is not None else {**acp.default_environment(), **docker_env()})
        except OSError as e:
            raise FilesError(f"can't reach its files: {e}") from None
        try:
            out, err = await asyncio.wait_for(proc.communicate(stdin), HELPER_TIMEOUT)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise FilesError("its computer didn't answer in time") from None
        line, _, rest = out.partition(b"\n")
        try:
            result = json.loads(line)
        except ValueError:
            text = err.decode(errors="replace").strip().splitlines()
            log.warning("files helper %s failed (exit %s): %s", op, proc.returncode, text[-1:] or "no output")
            raise FilesError("can't reach its files" + (f": {text[-1]}" if text else "")) from None
        if not isinstance(result, dict):
            raise FilesError("can't reach its files")
        if "error" in result:
            raise FilesError(str(result["error"]))
        return result, rest

    async def list(self, path: str | None) -> dict[str, Any]:
        p = check_path(path or self.roots[0].path, self.roots)
        result, _ = await self.helper("list", {"path": p})
        return result

    async def read(self, path: str, offset: int, length: int) -> tuple[dict[str, Any], bytes]:
        p = check_path(path, self.roots)
        result, data = await self.helper("read", {"path": p, "offset": offset, "length": length})
        return {"size": result.get("size", 0), "eof": bool(result.get("eof"))}, data

    async def write(self, upload: str, folder: str, name: str, offset: int, data: bytes, final: bool,
                    exists: str) -> dict[str, Any]:
        p = check_path(folder, self.roots)
        check_name(name)
        if not UPLOAD_RE.match(upload):
            raise FilesError("not an upload id")
        result, _ = await self.helper("write", {"upload": upload, "dir": p, "name": name, "offset": offset,
                                                "final": final, "exists": exists}, stdin=data)
        return result

    async def cancel(self, upload: str, folder: str) -> None:
        await self.helper("cancel", {"upload": upload, "dir": check_path(folder, self.roots)})

    async def mkdir(self, path: str) -> dict[str, Any]:
        result, _ = await self.helper("mkdir", {"path": check_path(path, self.roots)})
        return result


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def unb64(text: str) -> bytes:
    try:
        return base64.b64decode(text, validate=True)
    except ValueError:
        raise FilesError("the data isn't base64") from None
