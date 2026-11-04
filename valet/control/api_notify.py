"""REST routes for notifications (docs/API.md, "Notifications"): the owner's webhook, the native apps' devices for
push, and a test of both. What is sent, and when, is notify.py's."""
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from . import auth, notify

router = APIRouter(prefix="/api", dependencies=[Depends(auth.require_owner)])


def _notifier(request: Request) -> notify.Notifier:
    return request.app.state.control.notifier


class NotificationSettings(BaseModel):
    webhook_url: Any = None     # checked by notify.webhook_url_error: a URL, or null / "" for none


@router.get("/notifications")
def get_notifications(request: Request):
    return _notifier(request).settings()


@router.put("/notifications")
def put_notifications(body: NotificationSettings, request: Request):
    n = _notifier(request)
    url = body.webhook_url.strip() if isinstance(body.webhook_url, str) else body.webhook_url
    try:
        n.set_webhook_url(url)
    except ValueError as e:
        raise HTTPException(422, f"webhook_url: {e}") from None
    return n.settings()


@router.post("/notifications/test")
async def test_notifications(request: Request):
    """A test line to the webhook and a test push to every device; each says how it went."""
    try:
        return await _notifier(request).test()
    except ValueError as e:
        raise HTTPException(422, str(e)) from None


class Device(BaseModel):
    token: Any = None           # checked by notify.register
    platform: Any = None
    name: Any = None


@router.get("/devices")
def list_devices(request: Request):
    return notify.devices(request.app.state.control.db)


@router.post("/devices")
def add_device(body: Device, request: Request):
    """Register a device for push, or update the one with this token."""
    try:
        return notify.register(request.app.state.control.db, body.token, body.platform, body.name)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None


@router.delete("/devices/{device_id}")
def remove_device(device_id: int, request: Request):
    if not notify.remove(request.app.state.control.db, device_id):
        raise HTTPException(404, "no such device")
    return {"ok": True}
