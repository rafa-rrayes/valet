"""Connections from the gateway to the MCP servers behind it: registry servers (remote streamable HTTP, or stdio
processes the gateway runs) through the `mcp` SDK's Client, and Valet's own servers in process (builtin.py).

One connection per registry server, shared by every agent that has it: credentials belong to the server, not the
agent. A connection is opened on first use, held by a background task (the SDK's context managers must be entered
and left in one task), and reopened on the next use after it fails."""
import asyncio
import contextlib
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Iterable
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import anyio
import mcp_types as types
from mcp import Client, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from valet.shared.owner import say_all

from .registry import ServerSpec

log = logging.getLogger(__name__)

CONNECT_TIMEOUT = 30.0      # seconds to start a stdio server or reach a remote one, and initialize
LIST_TIMEOUT = 30.0
MAX_PAGES = 20              # tools/list pages followed per server
TOOLS_TTL = 60.0            # seconds a listing answers list_tools(fresh=False) (a call looking up its tool)


def say_tools(tools: Iterable[types.Tool]) -> list[types.Tool]:
    """Valet's own servers' tools with the owner's name filled in ({owner}, shared/owner.py). Done at each listing,
    not once at import: the name comes from the config, which is read after the modules load."""
    return [types.Tool.model_validate(say_all(t.model_dump(by_alias=True, exclude_none=True))) for t in tools]


class UpstreamError(Exception):
    """A server that can't be reached, listed or called; str() is shown to the owner and to the agent."""


class Upstream(ABC):
    name: str
    # True for a server that decides its own calls (valet.browser: its site rules and its own asks; valet.memory:
    # the agent's own notebook, which never asks; valet.owner: calls that never reach past the owner and the calling
    # agent, with its own limits): the gateway lists every tool it has at every level and forwards every call
    # without its level and annotation rule (a tools.rules deny still hides one), so nothing asks twice. Only
    # Valet's own servers can say so.
    decides_calls: bool = False

    @abstractmethod
    async def list_tools(self, fresh: bool = True) -> list[types.Tool]:
        """fresh=False may answer from a recent listing (TOOLS_TTL)."""

    @abstractmethod
    async def call_tool(self, tool: str, arguments: dict[str, Any] | None, timeout: float,
                        caller: str) -> types.CallToolResult:
        """`caller`: the agent making the call (Valet's own servers use it; registry servers never see it)."""

    async def close(self) -> None:
        return None


class Connection(Upstream):
    """An MCP client session to one registry server. `spec` is already resolved (credentials substituted); `redact`
    holds the substituted values, which every message it reports or logs has replaced by "[redacted]" (an HTTP
    error can quote the url, a stdio server's error its env)."""

    def __init__(self, name: str, spec: ServerSpec, fingerprint: str, workdir: Path,
                 redact: Iterable[str] = ()) -> None:
        self.name = name
        self.spec = spec
        self.fingerprint = fingerprint
        self.workdir = workdir
        self._redact = sorted({v for v in redact if v}, key=len, reverse=True)
        self._client: Client | None = None
        self._holder: asyncio.Task | None = None
        self._closing = asyncio.Event()
        self._lock = asyncio.Lock()
        self._tools: tuple[float, list[types.Tool]] | None = None     # (when, listing)

    @property
    def connected(self) -> bool:
        return self._client is not None and self._holder is not None and not self._holder.done()

    def _transport(self, stack: AsyncExitStack) -> Any:
        spec = self.spec
        if spec.url is not None:
            http = create_mcp_http_client(headers=dict(spec.headers))
            stack.push_async_callback(http.aclose)
            return streamable_http_client(spec.url, http_client=http)
        self.workdir.mkdir(parents=True, exist_ok=True)
        params = StdioServerParameters(command=spec.command, args=list(spec.args), env=dict(spec.env),
                                       cwd=str(self.workdir))
        return params

    async def _hold(self, ready: asyncio.Future) -> None:
        """Open the client, publish it, keep it open until close() or until it fails."""
        try:
            async with AsyncExitStack() as stack:
                client = Client(self._transport(stack), client_info=types.Implementation(name="valet-gateway",
                                                                                          version="1"))
                await stack.enter_async_context(client)
                if not ready.done():
                    ready.set_result(client)
                await self._closing.wait()
        except BaseException as e:      # noqa: BLE001 (anything that ends the connection ends it)
            if not ready.done():
                ready.set_exception(UpstreamError(f"{self.name}: can't connect: {self._describe(e)}"))
            elif not self._closing.is_set():
                log.warning("gateway: connection to %s ended: %s", self.name, self._describe(e))
            if isinstance(e, asyncio.CancelledError):
                raise
        finally:
            self._client = None

    async def _session(self) -> Client:
        async with self._lock:
            if self.connected:
                return self._client
            ready: asyncio.Future = asyncio.get_running_loop().create_future()
            ready.add_done_callback(lambda f: f.cancelled() or f.exception())    # retrieved even if we gave up
            self._holder = asyncio.create_task(self._hold(ready), name=f"mcp-upstream-{self.name}")
            try:
                self._client = await asyncio.wait_for(asyncio.shield(ready), CONNECT_TIMEOUT)
            except TimeoutError:
                self._holder.cancel()
                raise UpstreamError(f"{self.name}: no answer within {CONNECT_TIMEOUT:.0f}s") from None
            return self._client

    def _drop(self) -> None:
        """Forget a client that failed; the next use reconnects."""
        self._client = None
        if self._holder is not None and not self._holder.done():
            self._holder.cancel()

    async def list_tools(self, fresh: bool = True) -> list[types.Tool]:
        if not fresh and self._tools and time.monotonic() - self._tools[0] < TOOLS_TTL:
            return list(self._tools[1])
        for attempt in (1, 2):              # a dropped connection is reopened once
            client = await self._session()
            try:
                with anyio.fail_after(LIST_TIMEOUT):
                    tools, cursor = [], None
                    for _ in range(MAX_PAGES):
                        page = await client.list_tools(cursor=cursor)
                        tools += page.tools
                        cursor = page.next_cursor
                        if not cursor:
                            break
                self._tools = (time.monotonic(), tools)
                return list(tools)
            except TimeoutError:
                raise UpstreamError(f"{self.name}: tools/list timed out") from None
            except Exception as e:
                self._drop()
                if attempt == 2:
                    raise UpstreamError(f"{self.name}: tools/list failed: {self._describe(e)}") from None
        raise AssertionError("unreachable")

    async def call_tool(self, tool: str, arguments: dict[str, Any] | None, timeout: float,
                        caller: str) -> types.CallToolResult:
        client = await self._session()
        try:
            with anyio.fail_after(timeout):
                return await client.call_tool(tool, arguments)
        except TimeoutError:
            raise UpstreamError(f"{self.name}: {tool} didn't finish within {timeout:.0f}s") from None
        except Exception as e:
            if _connection_lost(e):
                self._drop()
            raise UpstreamError(f"{self.name}: {tool} failed: {self._describe(e)}") from None

    def _describe(self, e: BaseException) -> str:
        return _describe(e, self._redact)

    async def close(self) -> None:
        self._closing.set()
        if self._holder is not None:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(self._holder, 5.0)
            if not self._holder.done():
                self._holder.cancel()


def _connection_lost(e: BaseException) -> bool:
    from mcp.shared.exceptions import MCPError
    if isinstance(e, MCPError):
        return "closed" in str(e).lower()
    return isinstance(e, (anyio.ClosedResourceError, anyio.BrokenResourceError, ConnectionError, OSError))


def _describe(e: BaseException, redact: Iterable[str] = ()) -> str:
    """The innermost useful message (anyio wraps failures in exception groups), each of `redact` replaced by
    "[redacted]" before it's cut to 300 characters."""
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]
    text = str(e) or type(e).__name__
    for value in redact:
        text = text.replace(value, "[redacted]")
    return text if len(text) <= 300 else text[:300] + "..."
