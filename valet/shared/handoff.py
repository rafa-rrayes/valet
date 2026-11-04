"""A hand-off: an earlier conversation, condensed into the first message of a new session, so any agent (any
harness, any computer level) can carry on from it. Valet runs no model, so the condensing is mechanical and says so:
what the owner and the assistant wrote, each tool call with its key argument, compactions' summaries; thinking and
tool output left out; the newest entries kept within MAX_BYTES, older ones dropped and counted.

A conversation is a list of entries {"role": "user" | "assistant" | "tool" | "summary", "text"}: a Claude Code
transcript's (shared/transcripts.conversation) or a Valet session's (from_events)."""
from typing import Any

from valet.shared.events import text_of
from valet.shared.transcripts import clip, tool_line

MAX_BYTES = 60_000          # the whole message, header included
ENTRY_MAX = 8_000           # characters of one entry: one long paste doesn't crowd out the rest
LABELS = {"user": "User", "assistant": "Assistant", "summary": "Summary of the conversation before it"}


def from_events(events: list[dict[str, Any]]) -> list[dict[str, str]]:
    """A Valet session's events (oldest first) as entries: the owner's messages, the agent's (its chunks of one turn
    joined), its tool calls (title, and the key argument of rawInput). A hand-off message the session started with
    stays one entry (it's the earlier conversation, already condensed)."""
    out: list[dict[str, str]] = []
    last: tuple[str, Any] | None = None
    for e in events:
        kind, data = e.get("kind"), e.get("data") or {}
        if kind in ("user_message", "agent_message"):
            role = "user" if kind == "user_message" else "assistant"
            text = text_of(data)
            if not text:
                continue
            if last == (role, e.get("turn_id")) and out:
                out[-1]["text"] += text
            else:
                out.append({"role": role, "text": text})
            last = (role, e.get("turn_id"))
            continue
        if kind == "tool_call":
            title = str(data.get("title") or data.get("kind") or "tool")
            raw = data.get("rawInput")
            out.append({"role": "tool", "text": tool_line(title, raw) if isinstance(raw, dict) and raw else title})
            last = None
    return [{"role": x["role"], "text": x["text"].strip()} for x in out if x["text"].strip()]


def render(entry: dict[str, str]) -> str:
    """One entry as text: a tool call on one indented line under the assistant's text, the rest under a label."""
    text = clip(entry["text"], ENTRY_MAX)
    if entry["role"] == "tool":
        return f"  > {text}"
    return f"[{LABELS.get(entry['role'], entry['role'])}]\n{text}"


def newest(entries: list[dict[str, str]], budget: int) -> tuple[list[dict[str, str]], int]:
    """The newest entries whose rendered text (and a blank line after each) fits in `budget` UTF-8 bytes, in order,
    and how many older ones were left out. At least the newest one, clipped, if anything is there at all."""
    kept: list[dict[str, str]] = []
    used = 0
    for entry in reversed(entries):
        size = len(render(entry).encode()) + 2
        if used + size > budget and kept:
            break
        kept.append(entry)
        used += size
    kept.reverse()
    return kept, len(entries) - len(kept)


def header(source: str, omitted: int) -> str:
    """What the message is, said first. `source` names where the conversation comes from (a sentence fragment:
    "a Claude Code session ... on runner mac")."""
    left_out = ("" if not omitted else " The earliest entry didn't fit and is left out." if omitted == 1
                else f" The {omitted} earliest entries didn't fit and are left out.")
    return ("[Valet hand-off] What follows is an earlier conversation, " + source + ", handed to you so you can carry "
            "on from where it ends. Valet condensed it without a model: the user's and the assistant's messages as "
            "they were written, each tool call on a line starting with \">\" with its main argument, and summaries "
            "of compacted parts. Thinking and tool output are left out." + left_out
            + " The files and state it talks about may have changed since, and may not be where you can reach them.")


def compose(source: str, entries: list[dict[str, str]], note: str = "", omitted: int = 0,
            budget: int = MAX_BYTES) -> dict[str, Any]:
    """The hand-off message: header, the newest entries that fit in `budget` bytes with it and the note, and the
    owner's note (what to do now) or, without one, a request to say where things stand. `omitted`: entries already
    dropped before these (the runner keeps a transcript's newest only). -> {"text", "entries" (kept), "omitted"}."""
    note = note.strip()
    ending = ("--- End of the earlier conversation. ---\n\n" + (note if note else
              "Nothing new has been asked yet: say in a few lines where things stand, then wait for the user."))
    room = budget - len(header(source, omitted + len(entries)).encode()) - len(ending.encode()) - 4
    kept, dropped = newest(entries, max(room, 0))
    omitted += dropped
    body = "\n\n".join(render(e) for e in kept)
    text = header(source, omitted) + "\n\n" + (body + "\n\n" if body else "") + ending
    return {"text": text, "entries": len(kept), "omitted": omitted}
