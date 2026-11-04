"""An agent's memory: Markdown files it keeps for itself across sessions and restarts, agents/<name>/memory/<file>.md
in the definitions repo. The agent writes them through Valet's gateway (valet.memory, gateway/builtin_memory.py),
the owner through the API (api_memory.py); every change is one commit, so the repo's history is the memory's.

File names are slugs (the skill name rule: `project-notes`), given with or without `.md`. Limits: FILE_MAX a file,
AGENT_MAX and FILES_MAX an agent. The `memory` directive layer (layer()) is what the agent finds in its
instructions at each start: how memory works, an index, and the files themselves while they fit in LAYER_MAX.
Memory is left out of the restart fingerprint: it changes all the time, and the agent reads it fresh with its tools."""
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from valet.shared.definition import agent_dir
from valet.shared.owner import say

if TYPE_CHECKING:
    from .definitions import DefinitionsRepo

MEMORY_DIR = "memory"           # agents/<name>/memory/
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*\Z")     # \Z: `$` would let a final newline through
NAME_MAX = 64
SUFFIX = ".md"
FILE_MAX = 64 * 1024            # bytes (UTF-8) a file
AGENT_MAX = 1024 * 1024         # bytes an agent, all its files
FILES_MAX = 200                 # files an agent
LAYER_MAX = 32 * 1024           # bytes of files' text the memory layer carries; the rest it names
TITLE_MAX = 80
SEARCH_HITS = 50
HIT_MAX = 300                   # characters of a matching line
OWNER = "owner"                 # `by` for the owner's changes (commit messages)
NAME_RULE = (f"memory file names are lowercase letters and digits, words joined by single dashes, like project-notes "
             f"(at most {NAME_MAX})")
LIMITS = {"file": FILE_MAX, "total": AGENT_MAX, "files": FILES_MAX, "layer": LAYER_MAX}


class MemoryError(ValueError):
    """A name, text or limit the memory refuses; the message says what to do and is shown to the agent or owner."""


def file_name(name: object) -> str:
    """`notes` or `notes.md` -> `notes`. MemoryError for anything that isn't a memory file's name."""
    if not isinstance(name, str):
        raise MemoryError(NAME_RULE)
    n = name.strip()
    n = n[:-len(SUFFIX)] if n.endswith(SUFFIX) else n
    if len(n) > NAME_MAX or not NAME_RE.match(n):
        raise MemoryError(f"{name!r}: {NAME_RULE}")
    return n


def directory(root: Path, agent: str) -> Path:
    return agent_dir(root, agent) / MEMORY_DIR


def path(root: Path, agent: str, name: str) -> Path:
    return directory(root, agent) / (file_name(name) + SUFFIX)


@dataclass(frozen=True)
class MemoryFile:
    name: str
    title: str
    size: int                   # bytes
    updated_at: float           # when it was last written (the file's mtime)

    def view(self) -> dict[str, Any]:
        """MemoryFile (docs/API.md)."""
        return {"name": self.name, "title": self.title, "size": self.size, "updated_at": self.updated_at}

    def for_agent(self) -> dict[str, Any]:
        """What valet.memory's list shows: the date as ISO 8601 (models read that better than epoch seconds)."""
        return {"name": self.name, "title": self.title, "size": self.size, "updated": iso(self.updated_at)}


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def title_of(text: str) -> str:
    """Its first Markdown heading, else its first line that has anything, on one line."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    heading = next((m.group(1) for line in lines if (m := re.match(r"^#{1,6}\s+(.*?)\s*#*$", line))), None)
    t = heading if heading is not None else (lines[0].lstrip("#>*-+ ").strip() if lines else "")
    t = re.sub(r"\s+", " ", t)
    return t if len(t) <= TITLE_MAX else t[:TITLE_MAX - 1] + "…"


def _file(p: Path, text: str) -> MemoryFile:
    return MemoryFile(p.stem, title_of(text), len(text.encode()), p.stat().st_mtime)


def _readable(p: Path) -> bool:
    return p.is_file() and not p.is_symlink()


def _entries(root: Path, agent: str) -> list[tuple[Path, str]]:
    """(path, text) of each memory file, by name. Links, other files and ones that aren't UTF-8 are skipped: only
    Valet writes here, and a hand-made oddity mustn't break the agent's memory."""
    d = directory(root, agent)
    if not d.is_dir() or d.is_symlink():
        return []
    out = []
    for p in sorted(d.glob("*" + SUFFIX)):
        if not _readable(p) or not NAME_RE.match(p.stem) or len(p.stem) > NAME_MAX:
            continue
        try:
            out.append((p, p.read_bytes().decode()))
        except (UnicodeDecodeError, OSError):
            continue
    return out


def files(root: Path, agent: str) -> list[MemoryFile]:
    """The agent's memory files, by name."""
    return [_file(p, t) for p, t in _entries(root, agent)]


def read(root: Path, agent: str, name: str) -> tuple[MemoryFile, str]:
    """(file, text). MemoryError for a bad name, FileNotFoundError if there's no such file."""
    p = path(root, agent, name)
    if not _readable(p):
        raise FileNotFoundError(file_name(name))
    text = p.read_bytes().decode(errors="replace")
    return _file(p, text), text


def check_text(text: object) -> str:
    """The text a file may hold, or MemoryError."""
    if not isinstance(text, str):
        raise MemoryError("the text must be a string")
    if "\0" in text:
        raise MemoryError("memory files are text: no NUL characters")
    try:
        size = len(text.encode())
    except UnicodeEncodeError:
        raise MemoryError("the text isn't valid Unicode") from None
    if size > FILE_MAX:
        raise MemoryError(f"a memory file holds at most {FILE_MAX // 1024} KiB (this would be {kib(size)}): split it "
                          "into files by subject, or trim what you no longer need")
    return text


def kib(n: int) -> str:
    return f"{n} B" if n < 1024 else f"{n / 1024:.1f} KiB"


def _message(agent: str, name: str, verb: str, by: str) -> str:
    """Memory: scout wrote notes.md / Memory: owner wrote scout's notes.md."""
    return f"Memory: {agent} {verb} {name}{SUFFIX}" if by == agent else f"Memory: {by} {verb} {agent}'s {name}{SUFFIX}"


def _put(defs: "DefinitionsRepo", agent: str, name: str, make, verb: str, by: str) -> MemoryFile:
    """Write the file to make(old text or None) as one commit, within the limits, nothing else writing meanwhile."""
    name = file_name(name)
    p = path(defs.root, agent, name)
    d = p.parent
    if d.is_symlink() or p.is_symlink() or (p.exists() and not p.is_file()):
        raise MemoryError(f"{name}: not a regular file in the definitions repo; fix it there")

    def change(old: str | None) -> str:
        text = check_text(make(old))
        others = [(q, t) for q, t in _entries(defs.root, agent) if q.name != p.name]
        if old is None and len(others) >= FILES_MAX:
            raise MemoryError(f"memory holds at most {FILES_MAX} files: delete or merge some first")
        total = sum(len(t.encode()) for _, t in others) + len(text.encode())
        if total > AGENT_MAX:
            raise MemoryError(f"memory holds at most {AGENT_MAX // 1024 // 1024} MiB in all (this would be "
                              f"{total / 1024 / 1024:.2f} MiB): delete or trim files first")
        return text

    defs.update_file(p, change, _message(agent, name, verb, by))
    return read(defs.root, agent, name)[0]


def _ending(text: str) -> str:
    return text if not text or text.endswith("\n") else text + "\n"


def write(defs: "DefinitionsRepo", agent: str, name: str, text: str, by: str) -> MemoryFile:
    """Create or replace the file. `by`: the agent itself, or OWNER."""
    if isinstance(text, str) and not text.strip():
        raise MemoryError("the text is empty: to remove a file, delete it")
    return _put(defs, agent, name, lambda old: _ending(text) if isinstance(text, str) else text, "wrote", by)


def append(defs: "DefinitionsRepo", agent: str, name: str, text: str, by: str) -> MemoryFile:
    """Add `text` at the end of the file, on a line of its own; a file that isn't there yet is created."""
    if not isinstance(text, str) or not text.strip():
        raise MemoryError("there's nothing to append")
    return _put(defs, agent, name, lambda old: _ending(old or "") + _ending(text), "appended to", by)


def delete(defs: "DefinitionsRepo", agent: str, name: str, by: str) -> None:
    """FileNotFoundError if there's no such file."""
    name = file_name(name)
    p = path(defs.root, agent, name)
    if not _readable(p):
        raise FileNotFoundError(name)

    def change(old: str | None) -> None:
        if old is None:
            raise FileNotFoundError(name)
        return None

    defs.update_file(p, change, _message(agent, name, "deleted", by))


def search(root: Path, agent: str, query: object) -> list[dict[str, Any]]:
    """Lines of the agent's files that contain `query` (ignoring case), in file order, at most SEARCH_HITS: [{name,
    line (from 1), text}]. A file whose name matches comes with line 0 and its title."""
    if not isinstance(query, str) or not query.strip():
        raise MemoryError("search for some text")
    q = query.strip().casefold()
    hits: list[dict[str, Any]] = []
    for p, text in _entries(root, agent):
        if q in p.stem.casefold():
            hits.append({"name": p.stem, "line": 0, "text": title_of(text)})
        for i, line in enumerate(text.splitlines(), 1):
            if q in line.casefold():
                s = line.strip()
                hits.append({"name": p.stem, "line": i, "text": s if len(s) <= HIT_MAX else s[:HIT_MAX - 1] + "…"})
        if len(hits) >= SEARCH_HITS:
            return hits[:SEARCH_HITS]
    return hits


def frame(agent: str, name: str, f: MemoryFile | None) -> dict[str, Any]:
    """The client frame for a change to one file: `file` is its MemoryFile now, null when it was deleted."""
    return {"type": "memory", "agent": agent, "name": name, "file": f.view() if f else None}


def history(defs: "DefinitionsRepo", agent: str, name: str, limit: int = 50) -> list[dict]:
    """The commits that changed the file, newest first (HistoryEntry)."""
    return defs.file_history(path(defs.root, agent, name), limit)


def at(defs: "DefinitionsRepo", agent: str, name: str, commit: str) -> str | None:
    """The file's text as of `commit`, None if it wasn't there then. ValueError for a bad commit hash."""
    return defs.file_at(path(defs.root, agent, name), commit)


# ---------- the memory layer ----------

PREAMBLE = ("These are your memory files: notes you keep for yourself in Valet, across sessions and restarts, as "
            "Markdown files, one per subject. This is them as they were when you started. Change them as you go with "
            "your valet.memory tools: write (a whole file), append, delete, and list, read and search to look things "
            "up. A change is kept at once, the next start loads it here, and none of these tools waits for "
            "approval. Keep what you'll need again: what you learn about {owner} and the work, decisions and why, how "
            "things are set up, what you're in the middle of. {Owner} can read and edit them too.")


def layer(root: Path, agent: str, budget: int = LAYER_MAX) -> str:
    """The `memory` layer's text: PREAMBLE, an index of the files (name, title, size, date), then their text, newest
    first, as long as they fit in `budget` bytes; the ones that don't are named, to be read with the tools."""
    entries = _entries(root, agent)
    out = ["# Your memory", say(PREAMBLE)]
    if not entries:
        out.append("You have no memory files yet.")
        return "\n\n".join(out) + "\n"
    infos = {p.stem: _file(p, t) for p, t in entries}
    total = sum(f.size for f in infos.values())
    out.append(f"Files: {len(infos)}, {kib(total)} of {AGENT_MAX // 1024 // 1024} MiB.\n\n" + "\n".join(
        f"- {f.name}: " + (f"{f.title} " if f.title else "") + f"({kib(f.size)}, updated {iso(f.updated_at)[:10]})"
        for f in infos.values()))
    left, skipped = budget, []
    for p, text in sorted(entries, key=lambda e: (-infos[e[0].stem].updated_at, e[0].stem)):
        size = infos[p.stem].size
        if size > left:
            skipped.append(p.stem)
            continue
        left -= size
        out.append(f"## Memory file: {p.stem}\n\n{text.strip()}")
    if skipped:
        out.append(f"Not loaded here, for room ({budget // 1024} KiB at most): {', '.join(sorted(skipped))}. Read them "
                   "with valet.memory's read when you need them.")
    return "\n\n".join(out) + "\n"
