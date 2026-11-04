"""The definitions repo: a git working tree of agents/<name>/{agent.yaml,persona.md}, plus, at its root, mcp.yaml (the
MCP gateway's server registry), browser.yaml (the browser's profiles and site rules), owner.md (the owner profile),
authority/<preset>.md (standing authority presets; Valet writes its own when there's no authority/ yet) and
skills: the shared library in skills/<name>/, an agent's own in agents/<name>/skills/<name>/ (shared/skills.py).
An agent's memory is agents/<name>/memory/*.md (control/memory.py). Every write is a commit (author "Valet
<valet@localhost>"), so the history is the audit trail and rollback is `git revert`."""
import os
import re
import shutil
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

from valet.control.directives import preset_names, shipped_presets
from valet.control.gateway.registry import REGISTRY_FILE
from valet.control.gateway.registry import parse as parse_registry
from valet.control.memory import MEMORY_DIR
from valet.shared import skills
from valet.shared.browser import BROWSER_FILE
from valet.shared.browser import parse as parse_browser
from valet.shared.definition import (
    AGENT_FILE, PERSONA_FILE, PERSONA_TEMPLATE, TEMPLATE, AgentDefinition, agent_dir, load_agent, list_agents,
    parse_agent,
)
from valet.shared.directives import AUTHORITY_DIR, OWNER_FILE, preset_path
from valet.shared.owner import say

FILES = (AGENT_FILE, PERSONA_FILE)
COMMIT_RE = re.compile(r"^[0-9a-f]{7,40}$")
_IDENTITY = {"GIT_AUTHOR_NAME": "Valet", "GIT_AUTHOR_EMAIL": "valet@localhost",
             "GIT_COMMITTER_NAME": "Valet", "GIT_COMMITTER_EMAIL": "valet@localhost"}


class DefinitionsRepo:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = threading.Lock()       # one git operation at a time (writes run in worker threads)

    def _git(self, *args: str, strip: bool = True) -> str:
        # -c overrides keep a user's global config (signing, hooks dir) from breaking unattended commits.
        cmd = ["git", "-C", str(self.root), "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args]
        r = subprocess.run(cmd, capture_output=True, text=True, env={**os.environ, **_IDENTITY})
        if r.returncode != 0:
            raise RuntimeError(f"git {args[0]} failed: {r.stderr.strip() or r.stdout.strip()}")
        return r.stdout.strip() if strip else r.stdout

    def init(self) -> None:
        """Create the directory and `git init` it if it isn't a repo yet (the first commit holds Valet's authority
        presets). A repo without authority/ (one from before M2a) gets the presets in a commit of their own."""
        self.root.mkdir(parents=True, exist_ok=True)
        with self._lock:
            if not (self.root / ".git").exists():
                self._git("init", "-q", "-b", "main")
                paths = self._seed_presets()
                if paths:
                    self._git("add", "--", *paths)
                self._git("commit", "-q", "--allow-empty", "-m", "Definitions repo")
            elif not (self.root / AUTHORITY_DIR).exists():
                paths = self._seed_presets()
                if paths:
                    self._git("add", "--", *paths)
                    self._git("commit", "-q", "-m", "Add Valet's authority presets", "--", *paths)

    def _seed_presets(self) -> list[str]:
        """Write Valet's shipped presets into authority/; the paths written."""
        paths = []
        for name, text in shipped_presets().items():
            p = preset_path(self.root, name)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
            paths.append(str(p))
        return paths

    def names(self) -> list[str]:
        return list_agents(self.root)

    def load(self, name: str) -> tuple[AgentDefinition, dict[str, str]]:
        """(definition, files) via shared.definition.load_agent, the files with the agent's skills added as
        skills/<name>/<path> (skills.Selection.files): what agent.start carries and the restart fingerprint covers.
        DefinitionError / FileNotFoundError pass through."""
        d, files = load_agent(self.root, name)
        return d, {**files, **self.skills_of(d).files()}

    def definition(self, name: str) -> AgentDefinition:
        """Just the parsed agent.yaml (no files read). DefinitionError / FileNotFoundError pass through."""
        return parse_agent((agent_dir(self.root, name) / AGENT_FILE).read_text(), name)

    # ---------- skills ----------
    def skills_base(self, agent: str | None = None) -> Path:
        """The shared library (agent None), or that agent's own skills."""
        return self.root / skills.SKILLS_DIR if agent is None else agent_dir(self.root, agent) / skills.SKILLS_DIR

    def skill_names(self, agent: str | None = None) -> list[str]:
        return skills.names(self.skills_base(agent))

    def skill(self, name: str, agent: str | None = None) -> dict[str, str]:
        """A skill's files as on disk (skills.read: SkillError if one isn't text; FileNotFoundError)."""
        if skills.name_error(name):
            raise FileNotFoundError(name)
        return skills.read(self.skills_base(agent) / name)

    def skills_of(self, d: AgentDefinition) -> skills.Selection:
        """What the agent `d` gets: its own skills, and the shared ones its tools.skills picks."""
        return skills.select(self.skills_base(d.name), self.skills_base(), d.tools.skills)

    def write_skill(self, name: str, files: dict[str, str], message: str, agent: str | None = None) -> str:
        """Make the skill exactly `files` (path -> text; a file not in it is removed): validated first (SkillError,
        nothing written), then committed. Returns the commit hash (HEAD if nothing changed)."""
        skills.check(name, files)
        d = self.skills_base(agent) / name
        with self._lock:
            if d.is_dir():
                for p in sorted(d.rglob("*"), reverse=True):    # files before their directories
                    rel = p.relative_to(d).as_posix()
                    if p.is_file() or p.is_symlink():
                        if rel not in files:
                            p.unlink()
                    elif p.is_dir() and not any(p.iterdir()):
                        p.rmdir()
            for rel, text in files.items():
                path = d / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
            return self._commit_dir(d, message)

    def delete_skill(self, name: str, message: str, agent: str | None = None) -> str:
        """Remove the skill and commit. FileNotFoundError if there's none by that name."""
        if skills.name_error(name):
            raise FileNotFoundError(name)
        d = self.skills_base(agent) / name
        if not d.is_dir():
            raise FileNotFoundError(name)
        with self._lock:
            shutil.rmtree(d)
            return self._commit_dir(d, message)

    def copy_skills(self, src: str, dst: str, message: str) -> str:
        """Copy agent `src`'s own skills to agent `dst` (a copy of the agent), and commit."""
        a, b = self.skills_base(src), self.skills_base(dst)
        if not self.skill_names(src):
            return self.head()
        with self._lock:
            shutil.copytree(a, b, symlinks=True, dirs_exist_ok=True)
            return self._commit_dir(b, message)

    def _commit_dir(self, d: Path, message: str) -> str:
        """Commit what changed under `d` (added, edited, removed); HEAD if nothing did. Holds the lock."""
        self._git("add", "-A", "--", str(d))
        if not self._git("status", "--porcelain", "--", str(d)):
            return self._git("rev-parse", "HEAD")
        self._git("commit", "-q", "-m", message, "--", str(d))
        return self._git("rev-parse", "HEAD")

    def raw(self, name: str) -> dict[str, str]:
        """{"agent.yaml": text, "persona.md": text} as on disk, valid or not ("" for a missing persona)."""
        d = agent_dir(self.root, name)
        if not (d / AGENT_FILE).is_file():
            raise FileNotFoundError(name)
        return {f: (d / f).read_text() if (d / f).is_file() else "" for f in FILES}

    def exists(self, name: str) -> bool:
        return (agent_dir(self.root, name) / AGENT_FILE).is_file()

    def write(self, name: str, files: dict[str, str], message: str) -> str:
        """Write files (keys: agent.yaml and/or persona.md only), validate agent.yaml if given (DefinitionError,
        nothing written), commit, and return the commit hash. A write that changes nothing returns HEAD."""
        unknown = set(files) - set(FILES)
        if unknown:
            raise ValueError(f"not a definition file: {', '.join(sorted(unknown))}")
        d = agent_dir(self.root, name)
        if AGENT_FILE in files:
            parse_agent(files[AGENT_FILE], name)
        with self._lock:
            d.mkdir(parents=True, exist_ok=True)
            for f, text in files.items():
                (d / f).write_text(text)
            paths = [str(d / f) for f in files]
            self._git("add", "--", *paths)
            if not self._git("status", "--porcelain", "--", *paths):
                return self._git("rev-parse", "HEAD")
            self._git("commit", "-q", "-m", message, "--", *paths)
            return self._git("rev-parse", "HEAD")

    def create(self, name: str, agent_yaml: str | None = None, persona: str | None = None) -> str:
        """New agent from shared.definition.TEMPLATE / PERSONA_TEMPLATE unless given. FileExistsError if it exists."""
        if self.exists(name):
            raise FileExistsError(name)
        files = {AGENT_FILE: agent_yaml if agent_yaml is not None else TEMPLATE.format(name=name),
                 PERSONA_FILE: persona if persona is not None else say(PERSONA_TEMPLATE.replace("{name}", name))}
        return self.write(name, files, f"Create agent {name}")

    def delete(self, name: str) -> str:
        """Remove agents/<name>/ (its definition, skills and memory) as one commit, `Delete agent <name>`; the
        history keeps it. FileNotFoundError if there's no such agent."""
        if not self.exists(name):
            raise FileNotFoundError(name)
        d = agent_dir(self.root, name)
        with self._lock:
            self._git("rm", "-r", "-f", "-q", "--ignore-unmatch", "--", str(d))
            shutil.rmtree(d, ignore_errors=True)        # what git didn't track
            if not self._git("status", "--porcelain", "--", str(d)):
                return self._git("rev-parse", "HEAD")
            self._git("commit", "-q", "-m", f"Delete agent {name}", "--", str(d))
            return self._git("rev-parse", "HEAD")

    def registry_text(self) -> str:
        """mcp.yaml as on disk ("" if there's none)."""
        p = self.root / REGISTRY_FILE
        return p.read_text() if p.is_file() else ""

    def write_registry(self, text: str, message: str = "Edit mcp.yaml") -> str:
        """Validate (RegistryError, nothing written), write and commit mcp.yaml; returns the commit hash."""
        parse_registry(text)
        p = self.root / REGISTRY_FILE
        with self._lock:
            p.write_text(text)
            self._git("add", "--", str(p))
            if not self._git("status", "--porcelain", "--", str(p)):
                return self._git("rev-parse", "HEAD")
            self._git("commit", "-q", "-m", message, "--", str(p))
            return self._git("rev-parse", "HEAD")

    def browser_text(self) -> str:
        """browser.yaml (the browser's profiles and site rules, shared/browser.py) as on disk ("" if there's none)."""
        p = self.root / BROWSER_FILE
        return p.read_text() if p.is_file() else ""

    def write_browser(self, text: str, message: str = "Edit browser.yaml") -> str:
        """Validate (BrowserError, nothing written), write and commit browser.yaml; returns the commit hash."""
        parse_browser(text)
        return self._commit_file(self.root / BROWSER_FILE, text, message)

    def head(self) -> str:
        with self._lock:
            return self._git("rev-parse", "HEAD")

    def history(self, name: str, limit: int = 50) -> list[dict]:
        """The commits that changed agents/<name>/, newest first: [{"commit", "message", "at"}]. Its memory's aren't
        among them: the agent writes it all the time (file_history has them)."""
        path = agent_dir(self.root, name).relative_to(self.root)
        return self._log(str(path), f":(exclude){path / MEMORY_DIR}", limit=limit)

    def file_history(self, path: Path, limit: int = 50) -> list[dict]:
        """The commits that changed one file of the repo (`path`, absolute), newest first, as history()'s."""
        return self._log(str(path.relative_to(self.root)), limit=limit)

    def _log(self, *pathspecs: str, limit: int) -> list[dict]:
        with self._lock:
            out = self._git("log", f"-{limit}", "--format=%H%x1f%s%x1f%ct", "--", *pathspecs)
        rows = [line.split("\x1f") for line in out.splitlines() if line]
        return [{"commit": c, "message": m, "at": int(t)} for c, m, t in rows]

    def file_at(self, path: Path, commit: str) -> str | None:
        """One file of the repo (`path`, absolute) as of `commit`; None if it wasn't there (or the commit isn't
        known). ValueError if `commit` isn't a commit hash."""
        if not COMMIT_RE.match(commit):
            raise ValueError("not a commit hash")
        with self._lock:
            try:
                return self._git("show", f"{commit}:{path.relative_to(self.root).as_posix()}", strip=False)
            except RuntimeError:
                return None

    def at(self, name: str, commit: str) -> dict[str, str]:
        """The agent's files as of `commit` ("" for a persona that wasn't there). FileNotFoundError if the agent
        had no agent.yaml then, or the commit isn't known; ValueError if `commit` isn't a commit hash."""
        if not COMMIT_RE.match(commit):
            raise ValueError("not a commit hash")
        rel = agent_dir(self.root, name).relative_to(self.root)
        files = {}
        with self._lock:
            for f in FILES:
                try:
                    files[f] = self._git("show", f"{commit}:{rel / f}", strip=False)
                except RuntimeError:
                    files[f] = None
        if files[AGENT_FILE] is None:
            raise FileNotFoundError(f"{name} has no definition at {commit[:7]}")
        return {f: t or "" for f, t in files.items()}

    # ---------- the owner profile and authority presets (shared directive layers) ----------

    def _commit_file(self, path: Path, text: str | None, message: str) -> str:
        """Write `path` (None: remove it) and commit it; HEAD when that changes nothing."""
        with self._lock:
            return self._write_commit(path, text, message)

    def update_file(self, path: Path, change: Callable[[str | None], str | None], message: str) -> str:
        """Change one file as one commit, with nothing else writing in between: `change` gets its text now (None if
        there's none) and returns what to write (None: remove it). An exception from `change` writes nothing.
        Returns the commit hash (HEAD when nothing changed)."""
        with self._lock:
            new = change(path.read_text() if path.is_file() else None)
            return self._write_commit(path, new, message)

    def _write_commit(self, path: Path, text: str | None, message: str) -> str:
        """_commit_file's work; the caller holds the lock."""
        if text is None:
            self._git("rm", "-q", "--ignore-unmatch", "--", str(path))
            path.unlink(missing_ok=True)        # never committed: just a file
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            self._git("add", "--", str(path))
        if not self._git("status", "--porcelain", "--", str(path)):
            return self._git("rev-parse", "HEAD")
        self._git("commit", "-q", "-m", message, "--", str(path))
        return self._git("rev-parse", "HEAD")

    def owner_text(self) -> str:
        """owner.md ("" if there's none)."""
        p = self.root / OWNER_FILE
        return p.read_text() if p.is_file() else ""

    def write_owner(self, text: str) -> str:
        return self._commit_file(self.root / OWNER_FILE, text, "Edit the owner profile")

    def presets(self) -> list[str]:
        return preset_names(self.root)

    def preset_text(self, name: str) -> str:
        """authority/<name>.md. FileNotFoundError if there's no such preset, ValueError for a bad name."""
        p = preset_path(self.root, name)
        if not p.is_file():
            raise FileNotFoundError(name)
        return p.read_text()

    def write_preset(self, name: str, text: str) -> str:
        """Create or replace authority/<name>.md. ValueError for a bad name."""
        return self._commit_file(preset_path(self.root, name), text, f"Edit authority/{name}")

    def delete_preset(self, name: str) -> str:
        """FileNotFoundError if there's no such preset, ValueError for a bad name."""
        p = preset_path(self.root, name)
        if not p.is_file():
            raise FileNotFoundError(name)
        return self._commit_file(p, None, f"Delete authority/{name}")
