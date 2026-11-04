"""The harness-neutral event format: what the event store keeps and every client renders.

An event is an ACP session/update payload, lightly normalized: the `sessionUpdate` discriminator becomes `kind`
(renamed by KINDS below) and the rest of the payload, still ACP JSON with camelCase keys, becomes `data`. Valet adds
a few kinds of its own (VALET_KINDS) for things that aren't session/update: the owner's message, turn ends and
permission requests, so a session's history reads as one stream."""
from typing import Any, Literal, TypedDict

# ACP sessionUpdate -> event kind. Anything not listed passes through under its ACP name, so a newer harness
# never loses data; clients ignore kinds they don't know.
KINDS = {
    "user_message_chunk": "user_message",
    "agent_message_chunk": "agent_message",
    "agent_thought_chunk": "agent_thought",
    "tool_call": "tool_call",
    "tool_call_update": "tool_call_update",
    "plan": "plan",
    "available_commands_update": "commands",
    "current_mode_update": "mode",
    "config_option_update": "config_options",
    "session_info_update": "session_info",
    "usage_update": "usage",
}

# Kinds the control plane writes itself.
VALET_KINDS = (
    "user_message",        # the owner's prompt; data = {"content": <ACP ContentBlock>} (same as a chunk)
    "turn_end",            # data = {"stopReason": <ACP StopReason> | null, "error": str | null}
    "permission_request",  # data = {"requestId", "toolCall": <ACP ToolCallUpdate>, "options": [<ACP PermissionOption>]}
    "permission_answer",   # data = {"requestId", "outcome": <ACP outcome> | null, "status": "answered" | "expired"}
)

Kind = Literal[
    "user_message", "agent_message", "agent_thought", "tool_call", "tool_call_update", "plan", "commands", "mode",
    "config_options", "session_info", "usage", "turn_end", "permission_request", "permission_answer",
]


class Event(TypedDict):
    id: int                 # event store id, increasing across the whole store; clients page with ?after=<id>
    agent: str
    session_id: str         # the harness's ACP session id
    turn_id: str | None     # the Valet turn it happened in; None for updates outside a turn
    ts: float               # unix seconds, when the control plane stored it
    kind: str               # one of Kind, or an ACP sessionUpdate name Valet doesn't know yet
    data: dict[str, Any]


def normalize_update(update: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """ACP session/update `update` object (JSON, by alias) -> (kind, data). Doesn't validate the payload: the
    harness is the authority on its contents and clients render defensively."""
    data = dict(update)
    acp_kind = str(data.pop("sessionUpdate", "unknown"))
    return KINDS.get(acp_kind, acp_kind), data


def text_of(data: dict[str, Any]) -> str:
    """The text of a message-like event (user_message, agent_message, agent_thought), '' for other content."""
    content = data.get("content") or {}
    return content.get("text", "") if content.get("type") == "text" else ""
