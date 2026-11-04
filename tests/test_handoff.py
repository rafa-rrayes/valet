"""A hand-off message (valet/shared/handoff.py): what it says it is, the newest entries within its budget, the
owner's note at the end; a Valet session's events as a conversation."""
from valet.shared import handoff

SOURCE = 'a Claude Code session ("Auth", 0000) that ran in /srv/app on runner mac'


def entries(n: int, size: int = 100) -> list[dict]:
    return [{"role": "user" if i % 2 == 0 else "assistant", "text": f"#{i} " + "x" * size} for i in range(n)]


def test_header_entries_and_ending():
    convo = [{"role": "user", "text": "fix the bug"}, {"role": "assistant", "text": "Looking."},
             {"role": "tool", "text": "Bash: pytest -q"}, {"role": "assistant", "text": "Fixed."},
             {"role": "summary", "text": "Earlier: set up the repo."}]
    out = handoff.compose(SOURCE, convo)
    text = out["text"]
    assert text.startswith("[Valet hand-off] What follows is an earlier conversation, " + SOURCE)
    assert "Valet condensed it without a model" in text and "left out" in text
    assert "[User]\nfix the bug\n\n[Assistant]\nLooking.\n\n  > Bash: pytest -q\n\n[Assistant]\nFixed." in text
    assert "[Summary of the conversation before it]\nEarlier: set up the repo." in text
    assert text.endswith("--- End of the earlier conversation. ---\n\nNothing new has been asked yet: say in a few "
                         "lines where things stand, then wait for the user.")
    assert out["entries"] == 5 and out["omitted"] == 0
    noted = handoff.compose(SOURCE, convo, note="  Now add a test for it.  ")["text"]
    assert noted.endswith("--- End of the earlier conversation. ---\n\nNow add a test for it.")


def test_the_newest_fit_in_the_budget():
    convo = entries(2000)
    out = handoff.compose(SOURCE, convo, note="go on")
    assert len(out["text"].encode()) <= handoff.MAX_BYTES
    assert out["entries"] + out["omitted"] == 2000 and out["omitted"] > 0
    assert f"The {out['omitted']} earliest entries didn't fit and are left out." in out["text"]
    assert "#1999 " in out["text"] and "#0 " not in out["text"]
    kept = [int(line[1:].split()[0]) for line in out["text"].splitlines() if line.startswith("#")]
    assert kept == list(range(2000 - out["entries"], 2000))          # in order, the newest
    # Dropped earlier (the runner sends only the newest), and counted here.
    assert "The 42 earliest entries didn't fit" in handoff.compose(SOURCE, entries(3), omitted=42)["text"]
    assert "The earliest entry didn't fit" in handoff.compose(SOURCE, entries(3), omitted=1)["text"]


def test_one_long_entry_is_clipped_and_kept():
    out = handoff.compose(SOURCE, [{"role": "user", "text": "y" * 200_000}])
    assert out["entries"] == 1 and len(out["text"].encode()) < handoff.MAX_BYTES
    assert "y" * handoff.ENTRY_MAX in out["text"] and "y" * (handoff.ENTRY_MAX + 1) not in out["text"]


def test_newest_counts_bytes_not_characters():
    convo = [{"role": "user", "text": "é" * 1000}] * 10      # 2 bytes each
    kept, dropped = handoff.newest(convo, 5000)
    assert len(kept) == 2 and dropped == 8


def ev(kind, data, turn="t1"):
    return {"kind": kind, "turn_id": turn, "data": data}


def text(t):
    return {"content": {"type": "text", "text": t}}


def test_a_valet_session_as_a_conversation():
    events = [ev("user_message", text("what's in /srv?")), ev("agent_thought", text("hmm")),
              ev("agent_message", text("Let me ")), ev("agent_message", text("look.")),
              ev("tool_call", {"toolCallId": "c1", "title": "ls /srv", "kind": "execute",
                               "rawInput": {"command": "ls /srv"}}),
              ev("tool_call_update", {"toolCallId": "c1", "status": "completed"}),
              ev("permission_request", {"requestId": "r"}),
              ev("agent_message", text("Two files.")), ev("turn_end", {"stopReason": "end_turn"}),
              ev("user_message", text("thanks"), "t2"), ev("agent_message", text("Sure."), "t2"),
              ev("agent_message", text(" Anything else?"), "t2"),
              ev("tool_call", {"toolCallId": "c2", "title": "Read"}, "t2")]
    assert handoff.from_events(events) == [
        {"role": "user", "text": "what's in /srv?"}, {"role": "assistant", "text": "Let me look."},
        {"role": "tool", "text": "ls /srv: ls /srv"}, {"role": "assistant", "text": "Two files."},
        {"role": "user", "text": "thanks"}, {"role": "assistant", "text": "Sure. Anything else?"},
        {"role": "tool", "text": "Read"}]
