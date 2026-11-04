"""What each working agent is doing right now (Agent.activity): the latest tool call of its running turn by title,
else "thinking" or "writing". Taken from the events as they're stored; an agent frame goes out when it changes, at
most once a second per agent (the last change always gets out), so a streaming turn can't flood the clients."""
import asyncio
from typing import Any

THROTTLE = 1.0          # seconds between activity frames for one agent
TITLE = 120             # characters of a tool call's title kept


class Activity:
    def __init__(self, emit) -> None:
        """emit(agent): a coroutine function that broadcasts the agent's frame (Hub.emit_agent)."""
        self._emit = emit
        self._what: dict[str, str] = {}
        self._tool: dict[str, str] = {}             # agent -> the toolCallId its activity names
        self._last: dict[str, float] = {}           # agent -> loop time of its last activity frame
        self._timers: dict[str, asyncio.TimerHandle] = {}
        self._tasks: set[asyncio.Task] = set()

    def get(self, agent: str) -> str | None:
        return self._what.get(agent)

    def note(self, agent: str, kind: str, data: dict[str, Any]) -> None:
        """An event of the agent's running turn was stored."""
        what = None
        if kind == "tool_call":
            self._tool[agent] = str(data.get("toolCallId") or "")
            what = _title(data) or "using a tool"
        elif kind == "tool_call_update" and str(data.get("toolCallId") or "") == self._tool.get(agent):
            if data.get("status") in ("completed", "failed"):
                what = "thinking"
            elif _title(data):
                what = _title(data)
        elif kind == "agent_thought":
            what = "thinking"
        elif kind == "agent_message":
            what = "writing"
        if what and what != self._what.get(agent):
            self._what[agent] = what
            self._changed(agent)

    def clear(self, agent: str) -> None:
        """Its turn ended (the caller emits the agent frame that says so)."""
        self._what.pop(agent, None)
        self._tool.pop(agent, None)
        timer = self._timers.pop(agent, None)
        if timer is not None:
            timer.cancel()

    def _changed(self, agent: str) -> None:
        loop = asyncio.get_running_loop()
        if agent in self._timers:
            return                              # a frame is already due; it will carry this change
        wait = self._last.get(agent, -THROTTLE) + THROTTLE - loop.time()
        if wait <= 0:
            self._send(agent)
        else:
            self._timers[agent] = loop.call_later(wait, self._send, agent)

    def _send(self, agent: str) -> None:
        self._timers.pop(agent, None)
        self._last[agent] = asyncio.get_running_loop().time()
        task = asyncio.ensure_future(self._emit(agent))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


def _title(data: dict[str, Any]) -> str:
    title = " ".join(str(data.get("title") or "").split())
    return title if len(title) <= TITLE else title[:TITLE - 1] + "…"
