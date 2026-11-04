"""Cross-site requests riding the owner's cookie: SameSite=Strict admits sibling subdomains (same site), so the
client socket and cookie-authenticated state changes also check the Origin."""
import pytest
from starlette.websockets import WebSocketDisconnect

EVIL = "https://blog.example.com"
OWN = "http://127.0.0.1:8780"       # the default VALET_PUBLIC_URL in these tests


def test_client_socket_refuses_foreign_origin(client):
    with client.websocket_connect("/ws/client", headers={"origin": EVIL}) as ws:
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_json()
        assert e.value.code == 4403
    with client.websocket_connect("/ws/client", headers={"origin": OWN}) as ws:
        assert ws.receive_json()["type"] == "hello"
    with client.websocket_connect("/ws/client") as ws:         # native clients send no Origin
        assert ws.receive_json()["type"] == "hello"


def test_cookie_posts_need_same_origin(client):
    client.post("/api/agents", json={"name": "scout"})
    assert client.post("/api/agents/scout/start").status_code == 503    # no runner; desired = running
    assert client.post("/api/agents/scout/stop", headers={"origin": EVIL}).status_code == 403
    assert client.post("/api/agents/scout/stop", headers={"sec-fetch-site": "same-site"}).status_code == 403
    assert client.post("/api/logout", headers={"origin": EVIL}).status_code == 403
    assert client.get("/api/agents", headers={"origin": EVIL}).status_code == 200       # reads aren't state
    assert client.get("/api/agents/scout").json()["desired"] == "running"
    assert client.post("/api/agents/scout/stop", headers={"origin": OWN}).status_code == 200
    assert client.post("/api/agents/scout/stop", headers={"sec-fetch-site": "same-origin"}).status_code == 200


def test_bearer_needs_no_origin(client):
    token = client.post("/api/login", json={"password": "correct horse battery", "client": "native"}).json()["token"]
    client.cookies.clear()
    client.post("/api/agents", json={"name": "scout"}, headers={"Authorization": f"Bearer {token}"})
    r = client.post("/api/agents/scout/stop", headers={"origin": EVIL, "Authorization": f"Bearer {token}"})
    assert r.status_code == 200
