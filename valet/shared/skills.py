"""Skills: instructions an agent loads when a task calls for them, in the Agent Skills format: a directory named for
the skill, with a SKILL.md that starts with YAML frontmatter (`name`, the directory's name, and `description`, what
it's for and when to use it) followed by the instructions, plus any other text files the instructions refer to.

In the definitions repo the shared library is skills/<name>/ and an agent's own skills are
agents/<agent>/skills/<name>/. An agent gets all of its own, and the shared ones its definition picks in
`tools.skills` (by name, or "*" for every one); its own wins a name both have. The control plane sends them to the
runner with the agent's files (agent.start), as skills/<name>/<path>, and each harness adapter makes its harness
load them from the agent's read-only config dir, and no others."""
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import yaml

SKILLS_DIR = "skills"           # the library at the repo's root, an agent's own in its dir, and the files' prefix
SKILL_FILE = "SKILL.md"
ALL = "*"                       # tools.skills: every shared skill
# Lowercase letters and digits in words joined by single dashes: the Agent Skills rule, and what both harnesses take.
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
NAME_MAX = 64
DESCRIPTION_MAX = 1024
# A file's path inside its skill: segments of letters, digits, '.', '_' and '-', never starting with '.' (no hidden
# files, no '..').
SEGMENT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,99}$")
PATH_MAX = 200
DEPTH_MAX = 5
FILE_MAX = 256 * 1024           # bytes (UTF-8) per file
FILES_MAX = 50                  # files per skill
SKILL_MAX = 1024 * 1024         # bytes per skill
AGENT_MAX = 16 * 1024 * 1024    # bytes of skills per agent: what agent.start carries
FRONT_RE = re.compile(r"\A﻿?---[ \t]*\r?\n(.*?)^---[ \t]*(?:\r?\n|\Z)(.*)\Z", re.DOTALL | re.MULTILINE)


class SkillError(ValueError):
    """A skill that isn't valid; the message says what to fix and is shown to the owner as is."""


def name_error(name: object) -> str | None:
    """Why `name` can't name a skill, or None."""
    if not isinstance(name, str) or len(name) > NAME_MAX or not NAME_RE.match(name):
        return f"lowercase letters and digits, words joined by single dashes, at most {NAME_MAX}"
    return None


def description_error(text: object) -> str | None:
    if not isinstance(text, str) or not text.strip():
        return "is required: what the skill is for and when to use it"
    if len(text) > DESCRIPTION_MAX:
        return f"is at most {DESCRIPTION_MAX} characters"
    if any(unicodedata.category(c) in ("Cc", "Zl", "Zp") and c not in "\n\t" for c in text):
        return "can't have control characters"
    return None


def path_error(path: object) -> str | None:
    """Why `path` can't be a file of a skill (relative to its directory, '/'-separated), or None."""
    if not isinstance(path, str) or not path or len(path) > PATH_MAX:
        return f"a relative path, at most {PATH_MAX} characters"
    parts = path.split("/")
    if len(parts) > DEPTH_MAX or not all(SEGMENT_RE.match(p) for p in parts):
        return ("a relative path of names made of letters, digits, '.', '_' and '-', not starting with '.', "
                f"at most {DEPTH_MAX} deep")
    return None


def split(text: str) -> tuple[dict, str]:
    """SKILL.md -> (frontmatter, instructions). The file opens with a line `---`, the YAML, and another `---`."""
    m = FRONT_RE.match(text)
    if not m:
        raise SkillError("SKILL.md must start with YAML frontmatter: a line ---, name: and description:, then ---")
    try:
        meta = yaml.safe_load(m.group(1))
    except yaml.YAMLError as e:
        raise SkillError(f"SKILL.md: the frontmatter isn't valid YAML: {e}") from None
    if meta is None:
        meta = {}
    if not isinstance(meta, dict):
        raise SkillError("SKILL.md: the frontmatter must be a mapping (name: ..., description: ...)")
    return meta, m.group(2).strip("\r\n")


def render(name: str, description: str, instructions: str, extra: dict | None = None) -> str:
    """SKILL.md from its parts: name and description first, then `extra` frontmatter (kept from the file it
    replaces), then the instructions. split(render(...)) gives them back."""
    meta = {"name": name, "description": description.strip(),
            **{k: v for k, v in (extra or {}).items() if k not in ("name", "description")}}
    front = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=1000)
    body = instructions.strip("\r\n")
    return f"---\n{front}---\n" + (f"\n{body}\n" if body else "")


def _size(path: str, text: object) -> int:
    if not isinstance(text, str) or "\0" in text:
        raise SkillError(f"{path}: skills are text files only")
    try:
        return len(text.encode())
    except UnicodeEncodeError:
        raise SkillError(f"{path}: not valid text") from None


def check(name: str, files: dict[str, str]) -> dict:
    """Validate a whole skill, its files by path (SKILL.md included); return SKILL.md's frontmatter."""
    if err := name_error(name):
        raise SkillError(f"{name!r}: {err}")
    if SKILL_FILE not in files:
        raise SkillError(f"a skill needs a {SKILL_FILE}")
    if len(files) > FILES_MAX:
        raise SkillError(f"at most {FILES_MAX} files in a skill")
    total, seen = 0, set()
    for path, text in files.items():
        if err := path_error(path):
            raise SkillError(f"{path!r}: {err}")
        if path.lower() in seen:            # one file on a case-insensitive disk
            raise SkillError(f"{path}: another file has this name in other letter case")
        seen.add(path.lower())
        size = _size(path, text)
        if size > FILE_MAX:
            raise SkillError(f"{path}: at most {FILE_MAX // 1024} KiB a file")
        total += size
    if total > SKILL_MAX:
        raise SkillError(f"at most {SKILL_MAX // 1024 // 1024} MiB in a skill")
    meta, _ = split(files[SKILL_FILE])
    if meta.get("name") != name:
        raise SkillError(f"SKILL.md: name must be {name}, the skill's own name")
    if err := description_error(meta.get("description")):
        raise SkillError(f"SKILL.md: the description {err}")
    return meta


def names(base: Path) -> list[str]:
    """The skill directories in `base` (a library), by name; their contents aren't checked."""
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir()
                  if p.is_dir() and not p.is_symlink() and name_error(p.name) is None)


def read(skill_dir: Path) -> dict[str, str]:
    """A skill directory's files, by path relative to it. SkillError if one isn't a text file, is a link, or the
    skill has too many or too big; FileNotFoundError if there's no such directory. The contents (SKILL.md's
    frontmatter) are check()'s job."""
    if not skill_dir.is_dir() or skill_dir.is_symlink():
        raise FileNotFoundError(skill_dir.name)
    out: dict[str, str] = {}
    for p in sorted(skill_dir.rglob("*")):
        rel = p.relative_to(skill_dir).as_posix()
        if p.is_symlink():
            raise SkillError(f"{rel}: links aren't allowed in a skill")
        if p.is_dir():
            continue
        if not p.is_file():
            raise SkillError(f"{rel}: not a regular file")
        if err := path_error(rel):
            raise SkillError(f"{rel!r}: {err}")
        if len(out) >= FILES_MAX:
            raise SkillError(f"at most {FILES_MAX} files in a skill")
        if p.stat().st_size > FILE_MAX:
            raise SkillError(f"{rel}: at most {FILE_MAX // 1024} KiB a file")
        try:
            out[rel] = p.read_bytes().decode()
        except UnicodeDecodeError:
            raise SkillError(f"{rel}: skills are text files only (UTF-8)") from None
    return out


def load(base: Path, name: str) -> dict[str, str]:
    """read() and check() the skill `name` of the library `base`."""
    files = read(base / name)
    check(name, files)
    return files


@dataclass
class Selection:
    """What one agent gets: its own skills and the shared ones it picks."""
    skills: dict[str, dict[str, str]] = field(default_factory=dict)   # name -> files
    sources: dict[str, str] = field(default_factory=dict)             # name -> "agent" | "shared"
    missing: list[str] = field(default_factory=list)                  # picked by name, not in the library
    problems: list[str] = field(default_factory=list)                 # skills left out, and why

    def files(self) -> dict[str, str]:
        """As agent.start's files: skills/<name>/<path> -> text."""
        return {f"{SKILLS_DIR}/{n}/{p}": t for n, fs in sorted(self.skills.items()) for p, t in sorted(fs.items())}


def select(own: Path, shared: Path, chosen: list[str]) -> Selection:
    """The agent's skills: every one in `own` (its own library), then those of `shared` that `chosen` names, or all
    of them with "*". Its own wins a name both have. A skill that doesn't check out, or doesn't fit in AGENT_MAX, is
    left out and said in `problems`; a name picked that the library doesn't have is in `missing`."""
    out = Selection()
    total = 0

    def take(base: Path, n: str, source: str) -> None:
        nonlocal total
        try:
            files = load(base, n)
        except (SkillError, FileNotFoundError, OSError) as e:
            out.problems.append(f"{n}{' (its own)' if source == 'agent' else ''}: {e}")
            return
        size = sum(len(t.encode()) for t in files.values())
        if total + size > AGENT_MAX:
            out.problems.append(f"{n}: left out, the agent's skills would pass {AGENT_MAX // 1024 // 1024} MiB")
            return
        total += size
        out.skills[n] = files
        out.sources[n] = source

    mine = names(own)
    for n in mine:
        take(own, n, "agent")
    library = names(shared)
    for n in library if ALL in chosen else dict.fromkeys(c for c in chosen if c != ALL):
        if n in mine:
            continue
        if n not in library:
            out.missing.append(n)
            continue
        take(shared, n, "shared")
    return out


def by_skill(files: dict[str, str]) -> dict[str, dict[str, str]]:
    """agent.start's files -> the skills in them: name -> {path inside the skill: text}. The runner's side of
    Selection.files; entries whose name or path isn't a skill's are left out."""
    out: dict[str, dict[str, str]] = {}
    for key, text in files.items():
        parts = key.split("/", 2)
        if len(parts) == 3 and parts[0] == SKILLS_DIR and not name_error(parts[1]) and not path_error(parts[2]):
            out.setdefault(parts[1], {})[parts[2]] = text
    return out
