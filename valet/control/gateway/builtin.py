"""Valet's own MCP servers, served in process by the gateway (no registry entry; names start with `valet.`).

`valet.agents` (here: the other agents, their status, and send_message to talk to one), `valet.owner` (owner.py:
The owner's inbox and the agent's own wake-ups), `valet.browser` (control/browser/server.py: the browser, which decides
its own calls), `valet.memory` (builtin_memory.py: the agent's own memory, given by `memory: true`) and
`valet.shared` (builtin_shared.py: the shared space). Every server DESIGN.md names is built."""
import json
import threading
import time
from collections import deque
from typing import Any

import mcp_types as types

from valet.shared.definition import NAME_RE
from valet.shared.owner import say

from .. import views
from ..prompts import MAIN, DeliveryError, header
from .owner import OwnerServer
from .upstream import Upstream, UpstreamError, say_tools

READ_ONLY = types.ToolAnnotations(read_only_hint=True, open_world_hint=False)
# send_message changes nothing outside Valet but starts another agent's turn: not read-only (so `locked` denies it
# and `ask` asks the owner), not destructive (so `trusted` runs it).
MESSAGE = types.ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
AGENT_FIELDS = ("name", "display_name", "harness", "model", "level", "status", "error")
MESSAGE_LIMIT = 20_000      # characters of a message to another agent
SEND_LIMIT = 20             # messages an agent may send ...
SEND_WINDOW = 10 * 60       # ... within this many seconds (two agents answering each other can't loop forever)


def _result(data: Any) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(text=json.dumps(data, indent=1))],
                                structured_content=data)


def _error(text: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(text=text)], is_error=True)


class AgentsServer(Upstream):
    """`valet.agents`: the other agents' status, and send_message, which prompts another agent's main session (source
    `agent:<sender>`) and returns at once: the answer, if any, comes back the same way."""
    name = "valet.agents"
    description = "Valet's agents, their status, and messages to them."

    TOOLS = [
        types.Tool(name="list_agents", title="List agents",
                   description="Every agent Valet runs for {owner}: name, display name, harness, model, computer level "
                               "and status (idle, working, waiting, starting, stopped, failed).",
                   input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                   annotations=READ_ONLY),
        types.Tool(name="agent_status", title="Agent status",
                   description="One agent's status and its most recent sessions (title, state, last update).",
                   input_schema={"type": "object", "properties": {"name": {"type": "string",
                                                                           "description": "The agent's name"}},
                                 "required": ["name"], "additionalProperties": False},
                   annotations=READ_ONLY),
        types.Tool(name="send_message", title="Message an agent",
                   description="Send another of {owner}'s agents a message. It arrives as a prompt in its main session, "
                               "starting with a `Valet:` line that names you; it starts a turn there, or waits for the "
                               "one running. Returns at once, without its reply: if it answers, it does so with "
                               "send_message to you, which reaches your main session the same way. The agent must be "
                               "running.",
                   input_schema={"type": "object",
                                 "properties": {"agent": {"type": "string", "description": "The agent's name"},
                                                "text": {"type": "string", "description": "The message"}},
                                 "required": ["agent", "text"], "additionalProperties": False},
                   annotations=MESSAGE),
    ]

    def __init__(self, control: Any) -> None:
        self.control = control
        self._sent: dict[str, deque] = {}
        self._lock = threading.Lock()

    async def list_tools(self, fresh: bool = True) -> list[types.Tool]:
        return say_tools(self.TOOLS)

    async def call_tool(self, tool: str, arguments: dict[str, Any] | None, timeout: float,
                        caller: str) -> types.CallToolResult:
        args = arguments or {}
        if tool == "list_agents":
            agents = [{k: a[k] for k in AGENT_FIELDS} for a in views.agents(self.control)]
            return _result({"agents": agents, "you": caller})
        if tool == "agent_status":
            name = args.get("name")
            if not isinstance(name, str) or not NAME_RE.match(name) or not self.control.defs.exists(name):
                return _error(f"no agent named {name!r}")
            agent = {k: v for k, v in views.agent_view(self.control, name).items() if k in AGENT_FIELDS}
            rows = self.control.db.query("SELECT * FROM sessions WHERE agent = ? ORDER BY updated_at DESC LIMIT 5",
                                         (name,))
            sessions = [{"title": r["title"], "state": r["state"], "updated_at": r["updated_at"]} for r in rows]
            return _result({**agent, "sessions": sessions})
        if tool == "send_message":
            return await self._send(caller, args)
        raise UpstreamError(f"valet.agents has no tool {tool!r}")

    def _room(self, sender: str) -> bool:
        """Count a message from `sender` if it's within SEND_LIMIT per SEND_WINDOW."""
        now = time.monotonic()
        with self._lock:
            q = self._sent.setdefault(sender, deque())
            while q and q[0] <= now - SEND_WINDOW:
                q.popleft()
            if len(q) >= SEND_LIMIT:
                return False
            q.append(now)
            return True

    async def _send(self, sender: str, args: dict[str, Any]) -> types.CallToolResult:
        target, text = args.get("agent"), args.get("text")
        if not isinstance(target, str) or not NAME_RE.match(target) or not self.control.defs.exists(target):
            return _error(f"no agent named {target!r} (list_agents names them)")
        if target == sender:
            return _error("that's you: to remind yourself later, use valet.owner's schedule_wakeup")
        if not isinstance(text, str) or not text.strip() or "\x00" in text:
            return _error("send_message needs `text`: the message")
        if len(text) > MESSAGE_LIMIT:
            return _error(f"That's {len(text)} characters; a message holds at most {MESSAGE_LIMIT}.")
        prompts = self.control.prompts
        if prompts.runner_of(target) is None:
            return _error(f"{target} can't take a message now: {prompts.why_not(target)}.")
        if not self._room(sender):
            return _error(f"You've sent {SEND_LIMIT} messages in the last {SEND_WINDOW // 60} minutes; wait before "
                          "sending more.")
        row = self.control.db.one("SELECT 1 FROM agents WHERE name = ?", (sender,))
        who = views.agent_view(self.control, sender).get("display_name") if row else None
        named = f"`{sender}`" + (f" ({who})" if who and who != sender else "")
        body = header(say(f"a message from agent {named}, another of {{owner}}'s agents. To reply, use valet.agents "
                           f"send_message to {sender}."), text)
        try:
            d = await prompts.deliver(target, MAIN, body, f"agent:{sender}", f"From {sender}",
                                      title=f"From {sender}: {text}")
        except DeliveryError as e:
            return _error(f"The message wasn't delivered: {e}")
        if d.queue_id is not None:
            return _result({"delivered": "queued", "agent": target,
                            "detail": f"{target} is busy; it gets the message when its current turn ends"})
        if d.error and not d.in_doubt:
            return _error(f"The message wasn't delivered: {d.error}")
        return _result({"delivered": "started", "agent": target, "detail": f"{target} is reading it now"})


def builtins(control: Any) -> dict[str, Upstream]:
    """Valet's own servers by name."""
    from ..browser.server import BrowserServer
    from .builtin_memory import MemoryServer
    from .builtin_shared import SharedServer
    return {s.name: s for s in (AgentsServer(control), BrowserServer(control), MemoryServer(control),
                                OwnerServer(control), SharedServer(control))}


# Named in DESIGN.md, not built yet: listed for an agent as "not available yet" rather than "unknown". (None left.)
PLANNED: dict[str, str] = {}
