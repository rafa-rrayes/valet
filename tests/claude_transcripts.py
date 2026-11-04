"""Fake Claude Code transcripts for tests, laid out as Claude Code lays them out under a (temporary) HOME:
~/.claude/projects/<folder of the cwd>/<session id>.jsonl, one record per line, each message chained to the one
before by parentUuid. Nothing here comes from a real transcript."""
import json
import os
import uuid
from pathlib import Path
from typing import Any

from valet.shared.transcripts import folder_name


class Chain:
    """Records of one session, each user or assistant message's parentUuid the previous one's uuid."""

    def __init__(self, cwd: str, session_id: str | None = None) -> None:
        self.cwd = cwd
        self.session_id = session_id or str(uuid.uuid4())
        self.records: list[dict[str, Any]] = []
        self.last: str | None = None

    def _msg(self, kind: str, content: Any, parent: str | None = "last", **kw: Any) -> dict[str, Any]:
        r = {"type": kind, "uuid": str(uuid.uuid4()), "parentUuid": self.last if parent == "last" else parent,
             "sessionId": self.session_id, "cwd": self.cwd, "isSidechain": False,
             "timestamp": "2026-10-31T12:00:00.000Z", "message": {"role": kind, "content": content}, **kw}
        self.records.append(r)
        self.last = r["uuid"]
        return r

    def user(self, content: Any, **kw: Any) -> dict[str, Any]:
        return self._msg("user", content, **kw)

    def assistant(self, *blocks: dict[str, Any] | str, **kw: Any) -> dict[str, Any]:
        """One record per block, sharing message.id, as Claude Code writes them."""
        mid = "msg_" + uuid.uuid4().hex[:12]
        r = {}
        for b in blocks:
            block = {"type": "text", "text": b} if isinstance(b, str) else b
            r = self._msg("assistant", [block], **kw)
            r["message"]["id"] = mid
        return r

    def tool(self, name: str, args: dict[str, Any], output: str = "ok") -> None:
        """A tool call and its result."""
        tid = "toolu_" + uuid.uuid4().hex[:12]
        self.assistant({"type": "tool_use", "id": tid, "name": name, "input": args})
        self.user([{"type": "tool_result", "tool_use_id": tid, "content": output}])

    def line(self, kind: str, **fields: Any) -> None:
        """A record that carries state, not a message (ai-title, custom-title, last-prompt, summary, ...)."""
        self.records.append({"type": kind, "sessionId": self.session_id, **fields})

    def write(self, home: Path, mtime: float | None = None, folder: str | None = None) -> Path:
        path = home / ".claude" / "projects" / (folder or folder_name(self.cwd)) / f"{self.session_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r) + "\n" for r in self.records))
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path


def simple(cwd: str, *exchanges: tuple[str, str], session_id: str | None = None) -> Chain:
    """A session of plain exchanges (what the user typed, what the assistant answered)."""
    c = Chain(cwd, session_id)
    for said, answer in exchanges:
        c.user(said)
        c.assistant(answer)
    return c
