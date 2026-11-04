"""REST routes for the gateway's hour grants (docs/API.md, "The MCP gateway"): what "Allow for an hour" on a gateway
ask left behind, listed and revoked. The rules "Always allow" writes are in the agent's definition (tools.rules)
and change with it (PUT /api/agents/{name}/definition)."""
from fastapi import APIRouter, Depends, HTTPException, Request

from . import auth
from .gateway import scopes

router = APIRouter(prefix="/api", dependencies=[Depends(auth.require_owner)])


@router.get("/gateway/grants")
def list_grants(request: Request, agent: str | None = None):
    """[ToolGrant] that are live, soonest to expire first; `agent`: one agent's."""
    return scopes.live(request.app.state.control.db, agent)


@router.delete("/gateway/grants/{grant_id}")
def revoke_grant(grant_id: int, request: Request):
    """The tool asks again from its next call. 404 if there's no live grant by that id."""
    if scopes.revoke(request.app.state.control.db, grant_id) is None:
        raise HTTPException(404, "no such grant (it may have expired)")
    return {"ok": True}
