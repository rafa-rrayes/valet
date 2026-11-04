"""The Claude Code sessions on the runner's machine, for the control plane to list, continue (session.import) or
hand off (claude.transcript): the transcripts under ~/.claude/projects of the user the runner runs as, the same
user and home its host agents have (shared/transcripts.py reads them).

Read-only, and only there: a folder is a name as Claude Code makes them (transcripts.FOLDER_RE), a session an id
(transcripts.SESSION_RE), so no request reaches a path outside that directory. Blocking file reads: callers run
these in a thread."""
import os
from pathlib import Path
from typing import Any

from valet.shared import transcripts

END = 65_536            # bytes read from each end of a transcript to list it (as the SDK does)
SESSIONS_MAX = 200      # newest first
FOLDERS_MAX = 100


class NotFound(Exception):
    """No such folder or session; the message says which."""


def projects_dir() -> Path:
    return Path.home() / ".claude" / "projects"


def _ends(path: Path, size: int) -> tuple[list[dict], list[dict]]:
    """The records in the first and last END bytes (a line cut by either edge is dropped)."""
    with path.open("rb") as f:
        head = f.read(END)
        if size > END:
            f.seek(size - END)
            tail = f.read()
        else:
            tail = head
    head_lines, tail_lines = head.split(b"\n"), tail.split(b"\n")
    if size > END:
        head_lines, tail_lines = head_lines[:-1], tail_lines[1:]
    return transcripts.records(head_lines), transcripts.records(tail_lines)


def session_info(path: Path) -> dict[str, Any] | None:
    """A transcript as a list shows it: {"id", "title", "cwd", "updated_at", "size"}; None for a subagent's, or a
    file it can't read."""
    try:
        st = path.stat()
        head, tail = _ends(path, st.st_size)
    except OSError:
        return None
    s = transcripts.summary(head, tail)
    if s is None:
        return None
    return {"id": path.stem, "title": s["title"], "cwd": s["cwd"], "updated_at": st.st_mtime, "size": st.st_size}


def _transcripts(folder: Path) -> list[Path]:
    try:
        found = [p for p in folder.iterdir() if p.suffix == ".jsonl" and transcripts.SESSION_RE.match(p.stem)
                 and p.is_file()]
    except OSError:
        return []
    return sorted(found, key=_mtime, reverse=True)


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def folder_path(folder: str) -> Path:
    """A project folder by name. Raises NotFound."""
    if not transcripts.FOLDER_RE.match(folder or ""):
        raise NotFound(f"no Claude Code project folder {folder!r}")
    path = projects_dir() / folder
    if not path.is_dir():
        raise NotFound(f"no Claude Code project folder {folder!r}")
    return path


def sessions_in(folder: str, cwd: str | None = None) -> list[dict[str, Any]]:
    """A folder's sessions, newest first (at most SESSIONS_MAX): {"id", "title", "cwd", "updated_at", "size"}.
    Subagents' transcripts aren't sessions; with `cwd`, only the sessions that ran there are listed."""
    out = []
    for p in _transcripts(folder_path(folder)):
        s = session_info(p)
        if s is not None and (cwd is None or s["cwd"] in (None, cwd)):
            out.append(s)
        if len(out) >= SESSIONS_MAX:
            break
    return out


def folders_for(cwd: str) -> list[str]:
    """The project folders that can hold a cwd's sessions: its folder_name, and its real path's (macOS's /tmp is
    /private/tmp, and Claude Code names the folder after the cwd it sees); for a name past FOLDER_MAX, any folder
    with the same first FOLDER_MAX characters (the hash after them is the CLI's)."""
    names = []
    for path in dict.fromkeys([cwd, os.path.realpath(cwd)]):
        name = transcripts.folder_name(path)
        names.append(name)
        if len(name) > transcripts.FOLDER_MAX:
            prefix = name[:transcripts.FOLDER_MAX] + "-"
            try:
                names += sorted(p.name for p in projects_dir().iterdir() if p.name.startswith(prefix))
            except OSError:
                pass
    return [n for n in dict.fromkeys(names) if (projects_dir() / n).is_dir()]


def sessions_for(cwd: str) -> list[dict[str, Any]]:
    """The sessions Claude Code ran in `cwd`, newest first: what an agent working there can continue."""
    found: dict[str, dict[str, Any]] = {}
    paths = {cwd, os.path.realpath(cwd)}
    for name in folders_for(cwd):
        for s in sessions_in(name):
            if s["cwd"] is None or s["cwd"] in paths:
                found.setdefault(s["id"], s)
    return sorted(found.values(), key=lambda s: s["updated_at"], reverse=True)[:SESSIONS_MAX]


def find(cwd: str, session_id: str) -> Path:
    """A session of `cwd`'s by id. Raises NotFound."""
    if transcripts.SESSION_RE.match(session_id or ""):
        for name in folders_for(cwd):
            path = projects_dir() / name / f"{session_id}.jsonl"
            if path.is_file():
                return path
    raise NotFound(f"no Claude Code session {session_id} in {cwd}")


def folders() -> list[dict[str, Any]]:
    """The project folders that have sessions, newest first (at most FOLDERS_MAX): {"folder", "cwd" (from its
    newest session that says), "sessions" (how many), "updated_at"}."""
    try:
        dirs = [p for p in projects_dir().iterdir() if p.is_dir() and transcripts.FOLDER_RE.match(p.name)]
    except OSError:
        return []
    out = []
    for d in dirs:
        ts = _transcripts(d)
        if not ts:
            continue
        cwd = None
        for p in ts[:5]:
            s = session_info(p)
            if s and s["cwd"]:
                cwd = s["cwd"]
                break
        out.append({"folder": d.name, "cwd": cwd, "sessions": len(ts), "updated_at": _mtime(ts[0])})
    out.sort(key=lambda f: f["updated_at"], reverse=True)
    return out[:FOLDERS_MAX]


def read(folder: str, session_id: str) -> dict[str, Any]:
    """A session's conversation (transcripts.conversation) with its title and cwd: {"title", "cwd", "entries"}.
    Raises NotFound."""
    if not transcripts.SESSION_RE.match(session_id or ""):
        raise NotFound(f"no Claude Code session {session_id!r}")
    path = folder_path(folder) / f"{session_id}.jsonl"
    try:
        with path.open("rb") as f:
            rs = transcripts.records(f)
    except FileNotFoundError:
        raise NotFound(f"no Claude Code session {session_id} in {folder}") from None
    s = transcripts.summary(rs[:200], rs[-200:]) or {"title": "", "cwd": None}
    return {"title": s["title"], "cwd": s["cwd"], "entries": transcripts.conversation(rs)}
