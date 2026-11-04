"""Claude Code's session transcripts, read the way its Agent SDK reads them: pure functions over a path and a
transcript's lines (the runner's claude_sessions.py does the reading).

Claude Code keeps each session as `~/.claude/projects/<folder>/<session id>.jsonl`, one JSON record per line:
`user` and `assistant` messages (an assistant message is split into one record per content block, sharing
`message.id`), each with `uuid` and `parentUuid`, so a transcript is a tree whose current branch runs back from its
last message; `cwd`, `timestamp`, `isSidechain` (a subagent's), `isMeta` (text Claude Code added),
`isCompactSummary` (the summary that replaced the conversation before a compaction); and records that only carry
state: `custom-title`, `ai-title`, `last-prompt`, `summary`, and others this module ignores.

The folder is the session's cwd with every character but an ASCII letter or digit as "-", cut at 200 characters
with a hash of the whole path after it (folder_name)."""
import json
import re
from typing import Any, Iterable

FOLDER_MAX = 200
SESSION_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
# A project folder's name, as folder_name makes them: nothing that could leave ~/.claude/projects.
FOLDER_RE = re.compile(r"^[A-Za-z0-9-]{1,255}$")
TITLE_MAX = 200
# Text Claude Code wrapped in a tag of its own (a command's output, a reminder) or an interruption marker: not
# something the owner typed, so never a title.
_WRAPPED = re.compile(r"^(?:\s*<[a-z][\w-]*[\s>]|\[Request interrupted by user[^\]]*\])")
_COMMAND = re.compile(r"<command-name>(.*?)</command-name>")
_COMMAND_ARGS = re.compile(r"<command-args>([\s\S]*?)</command-args>")
_BASH = re.compile(r"<bash-input>([\s\S]*?)</bash-input>")
_REMINDER = re.compile(r"<system-reminder>[\s\S]*?</system-reminder>")


def _java_hash(text: str) -> int:
    """The SDK's string hash: Java's hashCode over UTF-16 code units, as a signed 32-bit int."""
    data = text.encode("utf-16-le")
    h = 0
    for i in range(0, len(data), 2):
        h = (h * 31 + int.from_bytes(data[i:i + 2], "little")) & 0xFFFFFFFF
    return h - (1 << 32) if h >= 1 << 31 else h


def _base36(n: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while True:
        n, r = divmod(n, 36)
        out = digits[r] + out
        if not n:
            return out


def folder_name(cwd: str) -> str:
    """The project folder Claude Code keeps a cwd's sessions in. Every UTF-16 code unit that isn't an ASCII letter
    or digit becomes "-" (a character outside the BMP, two); a name longer than FOLDER_MAX is cut there and gets "-"
    and the base-36 hash of the whole path (the SDK's; the CLI may hash differently: claude_sessions also matches
    on the prefix and the transcripts' own cwd)."""
    name = "".join(ch if ch.isascii() and ch.isalnum() else ("--" if ord(ch) > 0xFFFF else "-") for ch in cwd)
    if len(name) <= FOLDER_MAX:
        return name
    return name[:FOLDER_MAX] + "-" + _base36(abs(_java_hash(cwd)))


def records(lines: Iterable[str | bytes]) -> list[dict[str, Any]]:
    """The JSON objects among the lines; anything else (a line cut in half, a blank) is skipped."""
    out = []
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict):
            out.append(r)
    return out


def _texts(content: Any) -> list[str] | None:
    """A user message's text parts, or None if it carries a tool result (it's the tool's turn, not the owner's)."""
    if isinstance(content, str):
        return [content]
    out = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_result":
            return None
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            out.append(block["text"])
    return out


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _first_prompt(head: list[dict[str, Any]]) -> str:
    """The first thing the owner typed (the SDK's rule): not meta, not a compaction summary, not a tool result;
    newlines as spaces; `! <command>` for a shell escape; a slash command's name only if nothing else comes."""
    command = ""
    for r in head:
        if r.get("type") != "user" or r.get("isMeta") is True or r.get("isCompactSummary") is True:
            continue
        texts = _texts((r.get("message") or {}).get("content"))
        if texts is None:
            continue
        for t in texts:
            t = t.replace("\n", " ").strip()
            if not t:
                continue
            if m := _COMMAND.search(t):
                command = command or m.group(1)
                continue
            if m := _BASH.search(t):
                return "! " + m.group(1).strip()
            if _WRAPPED.match(t):
                continue
            return clip(t, TITLE_MAX)
    return command


def _last(rs: list[dict[str, Any]], kind: str, key: str) -> str:
    for r in reversed(rs):
        if r.get("type") == kind and isinstance(r.get(key), str) and r[key].strip():
            return r[key].strip()
    return ""


def summary(head: list[dict[str, Any]], tail: list[dict[str, Any]]) -> dict[str, Any] | None:
    """What a session list shows of a transcript, from its first and last records (the SDK reads 64 KB of each
    end): {"title", "cwd"}, or None for a subagent's transcript (its first record isSidechain). The title is the
    SDK's: the latest custom title, else the AI's title, else the last prompt, else a `summary` line, else the
    first prompt; "" if none."""
    if head and head[0].get("isSidechain") is True:
        return None
    title = (_last(tail, "custom-title", "customTitle") or _last(head, "custom-title", "customTitle")
             or _last(tail, "ai-title", "aiTitle") or _last(head, "ai-title", "aiTitle")
             or _last(tail, "last-prompt", "lastPrompt") or _last(tail, "summary", "summary")
             or _first_prompt(head))
    cwd = next((r["cwd"] for r in head if isinstance(r.get("cwd"), str)), None)
    return {"title": clip(" ".join(title.split()), TITLE_MAX), "cwd": cwd}


# ---------- the conversation, for a hand-off ----------

# A tool call's arguments worth naming, first found wins: what it ran, read, searched or delegated.
KEY_ARGS = ("command", "file_path", "path", "notebook_path", "pattern", "url", "query", "skill", "description",
            "prompt", "subject")
ARG_MAX = 300


def tool_line(name: str, args: Any) -> str:
    """`name: <its key argument>` (KEY_ARGS), else `name <its arguments as JSON>`, clipped to ARG_MAX."""
    if isinstance(args, dict):
        for key in KEY_ARGS:
            v = args.get(key)
            if isinstance(v, str) and v.strip():
                return f"{name}: {clip(' '.join(v.split()), ARG_MAX)}"
        if args:
            return f"{name} {clip(json.dumps(args, ensure_ascii=False), ARG_MAX)}"
    return name


def _branch(rs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The current branch: from the last user or assistant record back through parentUuid (and, across a
    compaction, logicalParentUuid) to the start, in order. A transcript without uuids is taken in file order."""
    by_id = {r["uuid"]: r for r in rs if isinstance(r.get("uuid"), str)}
    leaf = next((r for r in reversed(rs) if r.get("type") in ("user", "assistant") and r.get("uuid")
                 and r.get("isSidechain") is not True), None)
    if leaf is None:
        return [r for r in rs if r.get("isSidechain") is not True]
    chain, seen, r = [], set(), leaf
    while r is not None and r["uuid"] not in seen:
        seen.add(r["uuid"])
        chain.append(r)
        parent = r.get("parentUuid") or r.get("logicalParentUuid")
        r = by_id.get(parent) if isinstance(parent, str) else None
    return chain[::-1]


def _user_text(text: str) -> str:
    """What the owner typed, as they'd recognise it: reminders out, `/command args` and `! command` for Claude
    Code's wrappers, and "" for text Claude Code wrote (a command's output, a caveat)."""
    text = _REMINDER.sub("", text).strip()
    if m := _COMMAND.search(text):
        args = _COMMAND_ARGS.search(text)
        return (m.group(1) + (" " + args.group(1).strip() if args and args.group(1).strip() else "")).strip()
    if m := _BASH.search(text):
        return "! " + m.group(1).strip()
    if text.startswith("<") and _WRAPPED.match(text):
        return ""
    return text


def conversation(rs: list[dict[str, Any]]) -> list[dict[str, str]]:
    """A transcript's current branch as entries {"role": "user" | "assistant" | "tool" | "summary", "text"}: what
    the owner typed (images as "[image]"), what the assistant wrote (consecutive text joined), each tool call as
    tool_line, and a compaction's summary. Left out: thinking, tool results, meta and subagent records."""
    out: list[dict[str, str]] = []

    def add(role: str, text: str) -> None:
        if not text.strip():
            return
        if role == "assistant" and out and out[-1]["role"] == "assistant":
            out[-1]["text"] += "\n\n" + text.strip()
        else:
            out.append({"role": role, "text": text.strip()})

    for r in _branch(rs):
        msg = r.get("message") or {}
        content = msg.get("content")
        if r.get("type") == "user":
            if r.get("isMeta") is True or r.get("isSidechain") is True:
                continue
            if r.get("isCompactSummary") is True:
                add("summary", "\n".join(_texts(content) or []))
                continue
            texts = _texts(content)
            if texts is None:
                continue
            images = sum(1 for b in content if isinstance(b, dict) and b.get("type") == "image") \
                if isinstance(content, list) else 0
            add("user", "\n".join(filter(None, [_user_text(t) for t in texts] + ["[image]"] * images)))
        elif r.get("type") == "assistant" and r.get("isSidechain") is not True:
            blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content or []
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and isinstance(b.get("text"), str):
                    add("assistant", b["text"])
                elif b.get("type") in ("tool_use", "server_tool_use", "mcp_tool_use"):
                    add("tool", tool_line(str(b.get("name") or "tool"), b.get("input")))
    return out
