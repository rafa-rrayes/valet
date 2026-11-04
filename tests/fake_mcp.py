"""A small MCP server standing in for an upstream behind Valet's gateway (the `mcp` SDK's MCPServer).

stdio (the default): `[sys.executable, <abs path of this file>]`, as a registry `command`.
HTTP: `http_app(token)` is a streamable-HTTP ASGI app at /mcp that answers 401 without `Authorization: Bearer
<token>`, for a registry `url` with a `headers` entry.

Tools, one per kind of annotation the gateway's rule reads:
  echo(text)          readOnlyHint      -> "echo: <text>"
  add_note(text)      destructiveHint: false (additive) -> "noted: <text>"
  delete_note(name)   destructiveHint: true -> "deleted <name>"
  poke()              no annotations (so, per the MCP spec, destructive) -> "poked"
  secret_length()     readOnlyHint      -> the length of $FAKE_SECRET (shows the env reached the server, not its value)
  slow(seconds)       readOnlyHint      -> sleeps, then "slept"
  fail()              readOnlyHint      -> an error result"""
import asyncio
import os
import sys

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

COMMAND = [sys.executable, os.path.abspath(__file__)]

mcp = MCPServer("fake-upstream")


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
def echo(text: str) -> str:
    """Echo the text back."""
    return f"echo: {text}"


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False))
def add_note(text: str) -> str:
    """Add a note."""
    return f"noted: {text}"


@mcp.tool(annotations=ToolAnnotations(destructive_hint=True))
def delete_note(name: str) -> str:
    """Delete a note."""
    return f"deleted {name}"


@mcp.tool()
def poke() -> str:
    """Do something unannotated."""
    return "poked"


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
def secret_length() -> int:
    """How long FAKE_SECRET is."""
    return len(os.environ.get("FAKE_SECRET", ""))


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
async def slow(seconds: float) -> str:
    """Sleep, then answer."""
    await asyncio.sleep(seconds)
    return "slept"


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
def fail() -> str:
    """Always fails."""
    raise ValueError("fake failure")


def http_app(token: str):
    """streamable HTTP at /mcp, behind a bearer token."""
    app = mcp.streamable_http_app()

    async def guarded(scope, receive, send):
        if scope["type"] == "http":
            auth = dict(scope["headers"]).get(b"authorization", b"").decode()
            if auth != f"Bearer {token}":
                await send({"type": "http.response.start", "status": 401, "headers": []})
                await send({"type": "http.response.body", "body": b"no"})
                return
        await app(scope, receive, send)
    return guarded


if __name__ == "__main__":
    mcp.run()
