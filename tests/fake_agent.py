"""A scripted ACP agent for offline tests: the real agent side of the `acp` package over stdio, so the runner talks to
it exactly as it talks to a harness. Run as `[sys.executable, <abs path of this file>]`.

Behaviour is picked by the first text block of each prompt (first match wins):
  later       waits 1.5s, then as `permission` (so the request can fall in a window where the runner is offline)
  ticks       messages "tick 0 " ... "tick 19 ", one every 0.1s, then end_turn
  permission  tool_call "Write hello.txt" (pending), then session/request_permission with options
              allow (allow_once), allow-always (allow_always), reject (reject_once); reports the outcome:
              allow* -> tool_call_update completed + "allowed"; reject -> failed + "rejected"; cancelled -> "cancelled"
  slow        message "working", then waits for session/cancel (stop reason cancelled) or 30s (end_turn)
  fail        raises an internal error
  crash       message "crashing", then exits with code 3
  meta        message = the session's _meta as JSON (sort_keys)
  env A B     message = {"A": <its value in the agent's env, or null>, ...} as JSON (the prompt starts with "env ")
  tool        a tool_call (execute, in_progress) with updates: content "output", then completed; message "done"
  blocks      message = JSON of the prompt's blocks: text as is, an image as {type, mimeType, bytes (its size)},
              a resource link as {type, uri, name}
  otherwise   thought "thinking", then messages "echo: " and "<text>"

Checked first, MCP through the session's "valet" server (Valet's gateway), as a harness would use it:
  mcp-servers        message = JSON of the MCP servers the session got: [{name, type, url, headers: [names]}]
  mcp-list           message = the gateway's tool names, comma-separated
  mcp <tool> <json>  calls the tool with those arguments; message = "ok: <text>" or "error: <text>"
It advertises mcpCapabilities.http unless started with `--no-mcp-http`, and promptCapabilities.image unless started
with `--no-image`.

Session continuity is chosen with `--sessions load|resume|none` (or env FAKE_AGENT_SESSIONS; default load): which of
session/load or session/resume the agent advertises at initialize. session/load replays a Claude Code transcript of
the session when there is one where Claude Code would keep it ($HOME/.claude/projects/<the cwd's folder>/<id>.jsonl:
each user message, assistant text and tool call, as Claude Code's ACP adapter does; a summary line is its title,
sent as session_info_update), else "(replayed)"; and a session it loaded that way is known to it afterwards."""
import argparse
import asyncio
import base64
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import acp
from acp import schema

from valet.shared import transcripts

# How tests spawn it: RunnerConfig(harness_commands={"claude": COMMAND}) or VALET_HARNESS_CMD_CLAUDE.
COMMAND = [sys.executable, os.path.abspath(__file__)]


def _block(block: Any) -> dict[str, Any]:
    kind = getattr(block, "type", None)
    if kind == "text":
        return {"type": "text", "text": block.text}
    if kind == "image":
        return {"type": "image", "mimeType": block.mime_type, "bytes": len(base64.b64decode(block.data))}
    if kind == "resource_link":
        return {"type": "resource_link", "uri": block.uri, "name": block.name}
    return {"type": kind}


def _text(prompt: list[Any]) -> str:
    for block in prompt:
        if getattr(block, "type", None) == "text":
            return block.text
    return ""


class FakeAgent:
    def __init__(self, sessions: str, mcp_http: bool = True, image: bool = True) -> None:
        self.sessions_mode = sessions
        self.mcp_http = mcp_http
        self.image = image
        self.conn: Any = None
        self.meta: dict[str, dict[str, Any]] = {}       # session id -> _meta it was created/loaded with
        self.mcp: dict[str, list[Any]] = {}             # session id -> the MCP servers it was given
        self.cancels: dict[str, asyncio.Event] = {}     # session id -> set by session/cancel during a turn

    def on_connect(self, conn: Any) -> None:
        self.conn = conn

    async def initialize(self, protocol_version: int, client_capabilities=None, client_info=None, **kw):
        caps = schema.AgentCapabilities(load_session=self.sessions_mode == "load",
                                        mcp_capabilities=schema.McpCapabilities(http=self.mcp_http),
                                        prompt_capabilities=schema.PromptCapabilities(image=self.image))
        if self.sessions_mode == "resume":
            caps.session_capabilities = schema.SessionCapabilities(resume=schema.SessionResumeCapabilities())
        return acp.InitializeResponse(protocol_version=acp.PROTOCOL_VERSION, agent_capabilities=caps,
                                      agent_info=schema.Implementation(name="fake-agent", version="0"))

    async def new_session(self, cwd: str, additional_directories=None, mcp_servers=None, **meta):
        sid = "fake-" + uuid.uuid4().hex[:8]
        self.meta[sid] = meta
        self.mcp[sid] = list(mcp_servers or [])
        return acp.NewSessionResponse(session_id=sid)

    async def load_session(self, cwd: str, session_id: str, mcp_servers=None, additional_directories=None, **meta):
        if self.sessions_mode != "load":
            raise acp.RequestError.method_not_found("session/load")
        self.meta[session_id] = meta
        self.mcp[session_id] = list(mcp_servers or [])
        transcript = Path.home() / ".claude" / "projects" / transcripts.folder_name(cwd) / f"{session_id}.jsonl"
        if not transcript.is_file():
            await self._send(session_id, acp.update_user_message_text("(replayed)"))
            await self._send(session_id, acp.update_agent_message_text("(replayed)"))
            return acp.LoadSessionResponse()
        rs = transcripts.records(transcript.read_bytes().splitlines())
        for n, entry in enumerate(transcripts.conversation(rs)):
            if entry["role"] == "user":
                await self._send(session_id, acp.update_user_message_text(entry["text"]))
            elif entry["role"] == "tool":
                call = f"replayed-{n}"
                await self._send(session_id, acp.start_tool_call(call, entry["text"], kind="other", status="pending"))
                await self._send(session_id, acp.update_tool_call(call, status="completed"))
            else:
                await self._send(session_id, acp.update_agent_message_text(entry["text"]))
        title = (transcripts.summary(rs, rs) or {}).get("title")
        if title:
            await self._send(session_id, schema.SessionInfoUpdate(session_update="session_info_update", title=title))
        return acp.LoadSessionResponse()

    async def resume_session(self, session_id: str, cwd: str, additional_directories=None, mcp_servers=None, **meta):
        if self.sessions_mode != "resume":
            raise acp.RequestError.method_not_found("session/resume")
        self.meta[session_id] = meta
        self.mcp[session_id] = list(mcp_servers or [])
        return schema.ResumeSessionResponse()

    async def authenticate(self, method_id: str, **kw):
        return None

    async def cancel(self, session_id: str, **kw) -> None:
        if ev := self.cancels.get(session_id):
            ev.set()

    async def prompt(self, session_id: str, prompt: list[Any], **kw):
        if session_id not in self.meta:
            raise acp.RequestError.invalid_params({"reason": f"unknown session {session_id}"})
        text = _text(prompt)
        if text.startswith("blocks"):
            await self._say(session_id, json.dumps([_block(b) for b in prompt]))
            return acp.PromptResponse(stop_reason="end_turn")
        self.cancels[session_id] = cancelled = asyncio.Event()
        try:
            stop = await self._turn(session_id, text, cancelled)
        finally:
            self.cancels.pop(session_id, None)
        return acp.PromptResponse(stop_reason=stop)

    async def _turn(self, sid: str, text: str, cancelled: asyncio.Event) -> str:
        if text.startswith("mcp"):
            await self._say(sid, await self._mcp(sid, text))
            return "end_turn"
        if text.startswith("env "):
            await self._say(sid, json.dumps({k: os.environ.get(k) for k in text.split()[1:]}, sort_keys=True))
            return "end_turn"
        if "later" in text:
            await asyncio.sleep(1.5)
            return await self._permission(sid)
        if "ticks" in text:
            for i in range(20):
                await self._say(sid, f"tick {i} ")
                await asyncio.sleep(0.1)
            return "end_turn"
        if "permission" in text:
            return await self._permission(sid)
        if "slow" in text:
            await self._say(sid, "working")
            try:
                await asyncio.wait_for(cancelled.wait(), 30)
                return "cancelled"
            except TimeoutError:
                return "end_turn"
        if "fail" in text:
            raise acp.RequestError.internal_error({"reason": "fake failure"})
        if "crash" in text:
            await self._say(sid, "crashing")
            await asyncio.sleep(0.2)    # let the message reach the client before dying
            os._exit(3)
        if "meta" in text:
            await self._say(sid, json.dumps(self.meta.get(sid, {}), sort_keys=True))
            return "end_turn"
        if "tool" in text:
            call = "call-" + uuid.uuid4().hex[:8]
            await self._send(sid, acp.start_tool_call(call, "Run ls", kind="execute", status="in_progress",
                                                      raw_input={"command": "ls"}))
            await self._send(sid, acp.update_tool_call(call, content=[acp.tool_content(acp.text_block("output"))]))
            await self._send(sid, acp.update_tool_call(call, status="completed"))
            await self._say(sid, "done")
            return "end_turn"
        await self._send(sid, acp.update_agent_thought_text("thinking"))
        await self._say(sid, "echo: ")
        await self._say(sid, text)
        return "end_turn"

    async def _permission(self, sid: str) -> str:
        call = "call-" + uuid.uuid4().hex[:8]
        await self._send(sid, acp.start_tool_call(call, "Write hello.txt", kind="edit", status="pending",
                                                  raw_input={"path": "hello.txt", "content": "hello"}))
        options = [
            schema.PermissionOption(option_id="allow", name="Allow", kind="allow_once"),
            schema.PermissionOption(option_id="allow-always", name="Always allow", kind="allow_always"),
            schema.PermissionOption(option_id="reject", name="Reject", kind="reject_once"),
        ]
        tool_call = schema.ToolCallUpdate(tool_call_id=call, title="Write hello.txt", kind="edit", status="pending")
        resp = await self.conn.request_permission(session_id=sid, tool_call=tool_call, options=options)
        outcome = resp.outcome
        if getattr(outcome, "outcome", None) != "selected":
            await self._say(sid, "cancelled")
            return "cancelled"
        if outcome.option_id.startswith("allow"):
            await self._send(sid, acp.update_tool_call(call, status="completed"))
            await self._say(sid, "allowed")
        else:
            await self._send(sid, acp.update_tool_call(call, status="failed"))
            await self._say(sid, "rejected")
        return "end_turn"

    async def _mcp(self, sid: str, text: str) -> str:
        servers = self.mcp.get(sid, [])
        if text == "mcp-servers":
            return json.dumps([{"name": s.name, "type": getattr(s, "type", "stdio"), "url": getattr(s, "url", None),
                                "headers": [h.name for h in getattr(s, "headers", [])]} for s in servers])
        valet = next((s for s in servers if s.name == "valet"), None)
        if valet is None:
            return "error: no valet server"
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client
        from mcp.shared._httpx_utils import create_mcp_http_client
        try:
            async with create_mcp_http_client(headers={h.name: h.value for h in valet.headers}) as http:
                # mode="legacy": the initialize handshake, as Claude Code and dsh speak it today.
                async with Client(streamable_http_client(valet.url, http_client=http), mode="legacy") as client:
                    if text == "mcp-list":
                        return ",".join(t.name for t in (await client.list_tools()).tools)
                    _, tool, args = (text.split(" ", 2) + ["{}"])[:3]
                    result = await client.call_tool(tool, json.loads(args))
                    out = " ".join(c.text for c in result.content if getattr(c, "type", None) == "text")
                    return ("error: " if result.is_error else "ok: ") + out
        except Exception as e:
            while isinstance(e, BaseExceptionGroup) and e.exceptions:
                e = e.exceptions[0]
            return f"error: {type(e).__name__}: {e}"

    async def _say(self, sid: str, text: str) -> None:
        await self._send(sid, acp.update_agent_message_text(text))

    async def _send(self, sid: str, update: Any) -> None:
        await self.conn.session_update(session_id=sid, update=update)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--sessions", choices=["load", "resume", "none"],
                   default=os.environ.get("FAKE_AGENT_SESSIONS") or "load")
    p.add_argument("--no-mcp-http", action="store_true")
    p.add_argument("--no-image", action="store_true")
    args = p.parse_args()
    agent = FakeAgent(args.sessions, mcp_http=not args.no_mcp_http, image=not args.no_image)
    asyncio.run(acp.run_agent(agent, use_unstable_protocol=True))


if __name__ == "__main__":
    main()
