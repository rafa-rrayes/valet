"""The ACP Client side Valet implements for a harness (acp.interfaces.Client). session/update and
session/request_permission become runner->control messages; fs and terminal methods aren't offered (the
capabilities say so) and raise acp.RequestError.method_not_found."""
from typing import TYPE_CHECKING, Any

import acp
from acp.meta import CLIENT_METHODS

if TYPE_CHECKING:
    from .agent import AgentProcess


def acp_json(model: Any) -> Any:
    """An acp.schema model as ACP wire JSON (camelCase, nulls dropped), which is what crosses the WebSocket."""
    return model.model_dump(by_alias=True, exclude_none=True, mode="json")


class ValetClient:
    """Bound to one AgentProcess. Method signatures are acp.interfaces.Client's (keyword arguments, snake_case,
    pydantic models from acp.schema).

    acp runs every incoming notification and request as its own task, so ordering only holds if each handler
    hands its message to the AgentProcess before its first await: on_update is synchronous and on_permission
    enqueues before it waits."""

    def __init__(self, agent: "AgentProcess") -> None:
        self._agent = agent

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        self._agent.on_update(session_id, acp_json(update))

    async def request_permission(self, session_id: str, tool_call: Any, options: list[Any],
                                 **kwargs: Any) -> acp.RequestPermissionResponse:
        outcome = await self._agent.on_permission(session_id, acp_json(tool_call), [acp_json(o) for o in options])
        return acp.RequestPermissionResponse.model_validate({"outcome": outcome})

    async def read_text_file(self, **kwargs: Any) -> Any:
        raise acp.RequestError.method_not_found(CLIENT_METHODS["fs_read_text_file"])

    async def write_text_file(self, **kwargs: Any) -> Any:
        raise acp.RequestError.method_not_found(CLIENT_METHODS["fs_write_text_file"])

    async def create_terminal(self, **kwargs: Any) -> Any:
        raise acp.RequestError.method_not_found(CLIENT_METHODS["terminal_create"])

    async def terminal_output(self, **kwargs: Any) -> Any:
        raise acp.RequestError.method_not_found(CLIENT_METHODS["terminal_output"])

    async def release_terminal(self, **kwargs: Any) -> Any:
        raise acp.RequestError.method_not_found(CLIENT_METHODS["terminal_release"])

    async def wait_for_terminal_exit(self, **kwargs: Any) -> Any:
        raise acp.RequestError.method_not_found(CLIENT_METHODS["terminal_wait_for_exit"])

    async def kill_terminal(self, **kwargs: Any) -> Any:
        raise acp.RequestError.method_not_found(CLIENT_METHODS["terminal_kill"])
