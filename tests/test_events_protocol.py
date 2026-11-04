"""The event normalization and the runner<->control wire models."""
import pytest
from pydantic import ValidationError

from valet.shared.events import KINDS, normalize_update, text_of
from valet.shared.protocol import (
    FILE_CHUNK, ONE_WAY, AgentStart, FilesRead, FilesWrite, Hello, PermissionAnswer, Reply, SessionUpdate,
    TerminalClosed, TerminalInput, TerminalOpen, TerminalOutput, parse_control_message, parse_runner_message,
)


def test_normalize_known_and_unknown():
    kind, data = normalize_update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "hi"}})
    assert kind == "agent_message" and data == {"content": {"type": "text", "text": "hi"}}
    assert text_of(data) == "hi"
    kind, data = normalize_update({"sessionUpdate": "tool_call_update", "toolCallId": "c1", "status": "completed"})
    assert kind == "tool_call_update" and data == {"toolCallId": "c1", "status": "completed"}
    assert normalize_update({"sessionUpdate": "something_new", "x": 1}) == ("something_new", {"x": 1})
    assert KINDS["available_commands_update"] == "commands"
    assert text_of({"content": {"type": "image", "data": ""}}) == "" and text_of({}) == ""


def test_normalize_does_not_mutate():
    update = {"sessionUpdate": "plan", "entries": []}
    normalize_update(update)
    assert update == {"sessionUpdate": "plan", "entries": []}


def test_runner_messages_roundtrip():
    hello = Hello(version="0.2.0")
    assert hello.dump() == {"type": "hello", "protocol": 1, "version": "0.2.0", "boot": "", "seq": 0, "agents": [],
                            "turns": [], "pending_permissions": [], "harnesses": [], "features": []}
    assert parse_runner_message(hello.dump()) == hello
    # A runner from before `harnesses` still says hello; it just can't say what it runs.
    assert parse_runner_message({"type": "hello", "protocol": 1, "version": "0.1.0"}).harnesses == []
    # Nor, from before `features`, what else it can do (an agent's terminal, files, volumes).
    assert parse_runner_message({"type": "hello", "protocol": 1, "version": "0.1.0"}).features == []
    up = SessionUpdate(agent="a", session_id="s", update={"sessionUpdate": "plan", "entries": []})
    assert parse_runner_message(up.dump()) == up
    r = parse_runner_message({"type": "reply", "id": "1", "ok": False, "error": "busy"})
    assert isinstance(r, Reply) and r.error == "busy"
    with pytest.raises(ValidationError):
        parse_runner_message({"type": "welcome", "runner": "x", "version": "0"})


def test_control_messages():
    start = parse_control_message({"type": "agent.start", "id": "c1", "agent": "a", "definition": {"name": "a"}})
    assert isinstance(start, AgentStart) and start.files == {}
    ans = parse_control_message({"type": "permission.answer", "id": "c2", "agent": "a", "request_id": "r",
                                 "outcome": {"outcome": "selected", "optionId": "allow"}})
    assert isinstance(ans, PermissionAnswer)
    with pytest.raises(ValidationError):
        parse_control_message({"type": "session.prompt", "id": "c3", "agent": "a"})
    with pytest.raises(ValidationError):
        parse_control_message({"type": "hello", "version": "0"})


def test_computer_frames():
    """The agent's computer: commands with ids, one-way frames without, terminal frames up without a seq."""
    out = parse_runner_message({"type": "terminal.output", "terminal": "t1", "data": "aGk="})
    assert isinstance(out, TerminalOutput) and not hasattr(out, "seq")
    closed = parse_runner_message({"type": "terminal.closed", "terminal": "t1", "code": 0})
    assert isinstance(closed, TerminalClosed) and closed.reason is None
    op = parse_control_message({"type": "terminal.open", "id": "c1", "agent": "a", "terminal": "t1"})
    assert isinstance(op, TerminalOpen) and (op.cols, op.rows) == (80, 24)
    inp = parse_control_message({"type": "terminal.input", "terminal": "t1", "data": "aGk="})
    assert isinstance(inp, TerminalInput) and isinstance(inp, ONE_WAY) and not isinstance(op, ONE_WAY)
    with pytest.raises(ValidationError):
        parse_control_message({"type": "terminal.resize", "terminal": "t1", "cols": 0, "rows": 10})
    with pytest.raises(ValidationError):
        parse_control_message({"type": "files.read", "id": "c2", "agent": "a", "path": "/w",
                               "length": FILE_CHUNK + 1})
    assert parse_control_message({"type": "files.read", "id": "c2", "agent": "a", "path": "/w"}) == FilesRead(
        id="c2", agent="a", path="/w")
    w = parse_control_message({"type": "files.write", "id": "c3", "agent": "a", "upload": "ab12cd34", "dir": "/w",
                               "name": "x"})
    assert isinstance(w, FilesWrite) and w.exists == "fail" and w.final is False
    with pytest.raises(ValidationError):
        parse_control_message({"type": "files.write", "id": "c3", "agent": "a", "upload": "u", "dir": "/w",
                               "name": "x", "exists": "overwrite"})
