"""A scripted ACP agent for the runner's own tests (the shared tests/fake_agent.py belongs to the TESTS builder).

Prompt text picks the behaviour, first match wins: "env A B" (those variables of its env, as JSON), "pwd" (its
working directory), permission, slow, fail, crash, mcp (the MCP servers the session got, as JSON), meta, else echo.
`--caps load|resume|none` picks what it advertises for continuing sessions; `--slow-load` makes session/load
take a second, `--load-fails` makes it fail; it replays a Claude Code transcript where Claude Code keeps the cwd's
sessions ($HOME/.claude/projects/<folder>/<id>.jsonl), else "(replayed)". `--no-mcp-http` leaves
mcpCapabilities.http out. `--modes a,b,c` offers session modes on
session/new, load and resume, every session starting in the first; session/set_mode switches it and is recorded in
the session's meta as "modes_set"; `--set-mode-fails` makes it an error."""
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import acp
from valet.shared import transcripts
from acp.schema import (
    AgentCapabilities, Implementation, InitializeResponse, LoadSessionResponse, McpCapabilities, NewSessionResponse,
    PermissionOption, PromptResponse, ResumeSessionResponse, SessionCapabilities, SessionMode, SessionModeState,
    SessionResumeCapabilities, SetSessionModeResponse, ToolCallUpdate,
)


def _arg(flag: str) -> str | None:
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else None


class FakeAgent:
    def __init__(self, caps: str) -> None:
        self.caps = caps
        self.mode_ids = _arg("--modes").split(",") if "--modes" in sys.argv else []
        self.conn: Any = None
        self.meta: dict[str, dict] = {}
        self.mcp: dict[str, list] = {}          # session id -> MCP servers from new/load/resume, as ACP JSON
        self.cancelled: dict[str, asyncio.Event] = {}

    def on_connect(self, conn: Any) -> None:
        self.conn = conn

    async def initialize(self, protocol_version: int, **kw: Any) -> InitializeResponse:
        caps = AgentCapabilities(load_session=self.caps == "load",
                                 mcp_capabilities=McpCapabilities(http="--no-mcp-http" not in sys.argv))
        if self.caps == "resume":
            caps.session_capabilities = SessionCapabilities(resume=SessionResumeCapabilities())
        return InitializeResponse(protocol_version=protocol_version, agent_capabilities=caps,
                                  agent_info=Implementation(name="fake-agent", version="0"))

    async def new_session(self, cwd: str, mcp_servers: Any = None, additional_directories: Any = None,
                          **meta: Any) -> NewSessionResponse:
        sid = "fake-" + uuid.uuid4().hex[:8]
        self.meta[sid] = dict(meta, cwd=cwd)
        self._servers(sid, mcp_servers)
        return NewSessionResponse(session_id=sid, modes=self.modes())

    def _servers(self, sid: str, servers: Any) -> None:
        self.mcp[sid] = [s.model_dump(by_alias=True, exclude_none=True, mode="json") for s in servers or []]

    async def load_session(self, cwd: str, session_id: str, mcp_servers: Any = None,
                           additional_directories: Any = None, **meta: Any):
        self.meta[session_id] = dict(meta, cwd=cwd)
        self._servers(session_id, mcp_servers)
        if "--slow-load" in sys.argv:
            await asyncio.sleep(1.0)
        if "--load-fails" in sys.argv:
            raise acp.RequestError.internal_error({"reason": "fake load failure"})
        transcript = Path.home() / ".claude" / "projects" / transcripts.folder_name(cwd) / f"{session_id}.jsonl"
        if transcript.is_file():        # a Claude Code session: its conversation, as Claude Code's adapter replays it
            for n, e in enumerate(transcripts.conversation(transcripts.records(transcript.read_bytes().splitlines()))):
                if e["role"] == "user":
                    await self.conn.session_update(session_id, acp.update_user_message_text(e["text"]))
                elif e["role"] == "tool":
                    await self.conn.session_update(session_id, acp.start_tool_call(f"t{n}", e["text"], status="completed"))
                else:
                    await self.conn.session_update(session_id, acp.update_agent_message_text(e["text"]))
            return LoadSessionResponse(modes=self.modes())
        await self.conn.session_update(session_id, acp.update_user_message_text("(replayed)"))
        await self.conn.session_update(session_id, acp.update_agent_message_text("(replayed)"))
        return LoadSessionResponse(modes=self.modes())

    async def resume_session(self, session_id: str, cwd: str, mcp_servers: Any = None, **kw: Any):
        self.meta[session_id] = {"cwd": cwd, "resumed": True}
        self._servers(session_id, mcp_servers)
        return ResumeSessionResponse(modes=self.modes())

    def modes(self) -> SessionModeState | None:
        if not self.mode_ids:
            return None
        return SessionModeState(current_mode_id=self.mode_ids[0],
                                available_modes=[SessionMode(id=m, name=m) for m in self.mode_ids])

    async def set_session_mode(self, session_id: str, mode_id: str, **kw: Any) -> SetSessionModeResponse:
        if "--set-mode-fails" in sys.argv:
            raise acp.RequestError.internal_error({"reason": "fake set_mode failure"})
        if mode_id not in self.mode_ids:
            raise acp.RequestError.invalid_params({"reason": f"no mode {mode_id}"})
        self.meta.setdefault(session_id, {}).setdefault("modes_set", []).append(mode_id)
        return SetSessionModeResponse()

    async def cancel(self, session_id: str, **kw: Any) -> None:
        self.cancelled.setdefault(session_id, asyncio.Event()).set()

    async def say(self, sid: str, text: str) -> None:
        await self.conn.session_update(sid, acp.update_agent_message_text(text))

    async def prompt(self, session_id: str, prompt: list[Any], **kw: Any) -> PromptResponse:
        sid = session_id
        text = next((b.text for b in prompt if getattr(b, "type", None) == "text"), "")
        self.cancelled[sid] = asyncio.Event()
        if text.startswith("env "):     # the named variables of this process's env, as JSON
            await self.say(sid, json.dumps({k: os.environ.get(k) for k in text.split()[1:]}, sort_keys=True))
            return PromptResponse(stop_reason="end_turn")
        if text == "pwd":
            await self.say(sid, os.getcwd())
            return PromptResponse(stop_reason="end_turn")
        if "permission" in text:
            call_id = "call-" + uuid.uuid4().hex[:6]
            await self.conn.session_update(sid, acp.start_tool_call(call_id, "Write hello.txt", kind="edit",
                                                                     status="pending"))
            options = [PermissionOption(option_id="allow", name="Allow", kind="allow_once"),
                       PermissionOption(option_id="allow-always", name="Always allow", kind="allow_always"),
                       PermissionOption(option_id="reject", name="Reject", kind="reject_once")]
            r = await self.conn.request_permission(sid, ToolCallUpdate(tool_call_id=call_id, title="Write hello.txt"),
                                                   options)
            if r.outcome.outcome == "cancelled":
                await self.say(sid, "cancelled")
                return PromptResponse(stop_reason="cancelled")
            allowed = r.outcome.option_id.startswith("allow")
            await self.conn.session_update(sid, acp.update_tool_call(call_id,
                                                                     status="completed" if allowed else "failed"))
            await self.say(sid, "allowed" if allowed else "rejected")
            return PromptResponse(stop_reason="end_turn")
        if "slow" in text:
            await self.say(sid, "working")
            try:
                await asyncio.wait_for(self.cancelled[sid].wait(), 30)
                return PromptResponse(stop_reason="cancelled")
            except TimeoutError:
                return PromptResponse(stop_reason="end_turn")
        if "fail" in text:
            raise acp.RequestError.internal_error({"reason": "fake failure"})
        if "crash" in text:
            await self.say(sid, "crashing")
            print("fake agent: crashing on purpose", file=sys.stderr, flush=True)
            await asyncio.sleep(0.2)
            os._exit(3)
        if "mcp" in text:
            await self.say(sid, json.dumps(self.mcp.get(sid, []), sort_keys=True))
            return PromptResponse(stop_reason="end_turn")
        if "meta" in text:
            await self.say(sid, json.dumps(self.meta.get(sid, {}), sort_keys=True))
            return PromptResponse(stop_reason="end_turn")
        await self.conn.session_update(sid, acp.update_agent_thought_text("thinking"))
        await self.say(sid, "echo: ")
        await self.say(sid, text)
        return PromptResponse(stop_reason="end_turn")


if __name__ == "__main__":
    caps = _arg("--caps") or "load"
    if "--exit-early" in sys.argv:
        print("fake agent: refusing to start", file=sys.stderr, flush=True)
        sys.exit(7)
    asyncio.run(acp.run_agent(FakeAgent(caps), use_unstable_protocol=True))   # session/resume is "unstable" in acp
