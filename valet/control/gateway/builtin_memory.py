"""valet.memory: an agent's own memory (control/memory.py) as gateway tools. Every agent with `memory: true` gets it,
without listing it in tools.mcp (AgentDefinition.gateway_servers). It decides its own calls (decides_calls):
memory is the agent's notebook, so no call waits for approval or is refused at any level. An agent only ever reaches
its own files: the caller is the agent the gateway authenticated."""
import asyncio
import json
from typing import Any

import mcp_types as types

from .. import memory
from .upstream import Upstream, UpstreamError

READ = types.ToolAnnotations(read_only_hint=True, open_world_hint=False)
ADD = types.ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
CHANGE = types.ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)
NAME = {"type": "string", "description": "The file's name, a slug like project-notes (.md is optional)"}


def _schema(**props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def _result(data: Any, text: str | None = None) -> types.CallToolResult:
    """`text`: what the model reads (default the data as JSON)."""
    shown = text if text is not None else json.dumps(data, indent=1, ensure_ascii=False)
    return types.CallToolResult(content=[types.TextContent(text=shown)], structured_content=data)


def _error(text: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(text=text)], is_error=True)


class MemoryServer(Upstream):
    name = "valet.memory"
    description = ("Your memory: Markdown files you keep for yourself across sessions and restarts. Its calls never "
                   "wait for approval.")
    comes_with = "memory"       # the definition setting that gives it (GET /api/gateway/servers: `comes_with`)
    decides_calls = True

    TOOLS = [
        types.Tool(name="list", title="List memory files",
                   description="Your memory files: name, title (the first heading), size in bytes and when each was "
                               "last updated, and how much room is left.",
                   input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                   annotations=READ),
        types.Tool(name="read", title="Read a memory file", description="One memory file's whole text.",
                   input_schema=_schema(name=NAME), annotations=READ),
        types.Tool(name="write", title="Write a memory file",
                   description=f"Create a memory file, or replace one's whole text (Markdown). One file per subject; "
                               f"at most {memory.FILE_MAX // 1024} KiB a file, {memory.FILES_MAX} files and "
                               f"{memory.AGENT_MAX // 1024 // 1024} MiB in all. Kept at once, as a new version.",
                   input_schema=_schema(name=NAME, text={"type": "string", "description": "The file's new text"}),
                   annotations=CHANGE),
        types.Tool(name="append", title="Append to a memory file",
                   description="Add text at the end of a memory file, on a new line; the file is created if it isn't "
                               "there yet. Good for logs and running notes.",
                   input_schema=_schema(name=NAME, text={"type": "string", "description": "What to add"}),
                   annotations=ADD),
        types.Tool(name="delete", title="Delete a memory file",
                   description="Remove a memory file (it stays in Valet's history).",
                   input_schema=_schema(name=NAME), annotations=CHANGE),
        types.Tool(name="search", title="Search memory",
                   description=f"The lines of your memory files that contain some text (ignoring case), with file "
                               f"and line number, at most {memory.SEARCH_HITS}.",
                   input_schema=_schema(query={"type": "string", "description": "The text to look for"}),
                   annotations=READ),
    ]

    def __init__(self, control: Any) -> None:
        self.control = control

    async def list_tools(self, fresh: bool = True) -> list[types.Tool]:
        return list(self.TOOLS)

    async def call_tool(self, tool: str, arguments: dict[str, Any] | None, timeout: float,
                        caller: str) -> types.CallToolResult:
        args = arguments or {}
        defs = self.control.defs
        try:
            if tool == "list":
                files = await asyncio.to_thread(memory.files, defs.root, caller)
                used = sum(f.size for f in files)
                return _result({"files": [f.for_agent() for f in files], "used": used, "limits": memory.LIMITS})
            if tool == "read":
                f, text = await asyncio.to_thread(memory.read, defs.root, caller, args.get("name"))
                return _result({**f.for_agent(), "text": text}, text)
            if tool == "search":
                hits = await asyncio.to_thread(memory.search, defs.root, caller, args.get("query"))
                return _result({"hits": hits})
            if tool in ("write", "append"):
                op = memory.write if tool == "write" else memory.append
                f = await asyncio.to_thread(op, defs, caller, args.get("name"), args.get("text"), caller)
                await self.control.hub.broadcast(memory.frame(caller, f.name, f))
                return _result({"ok": True, **f.for_agent()})
            if tool == "delete":
                name = memory.file_name(args.get("name"))
                await asyncio.to_thread(memory.delete, defs, caller, name, caller)
                await self.control.hub.broadcast(memory.frame(caller, name, None))
                return _result({"ok": True, "deleted": name})
        except memory.MemoryError as e:
            return _error(str(e))
        except FileNotFoundError as e:
            return _error(f"There's no memory file named {e}: list shows the ones you have.")
        raise UpstreamError(f"valet.memory has no tool {tool!r}")
