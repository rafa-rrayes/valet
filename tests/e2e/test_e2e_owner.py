"""The owner's side of the stack: signing in (web cookie and native token), defining an agent, editing it."""
import asyncio

import httpx
from websockets.asyncio.client import connect

from tests.e2e.stack import PASSWORD, AGENT_YAML, texts


async def test_web_login_cookie_and_socket(stack):
    async with httpx.AsyncClient(base_url=stack.base) as web:
        r = await web.post("/api/login", json={"password": PASSWORD, "client": "web"})
        assert r.status_code == 200 and r.json() == {"ok": True}
        cookie = r.headers["set-cookie"].lower()
        assert "valet_session=" in cookie and "httponly" in cookie and "samesite=strict" in cookie
        assert "secure" not in cookie                       # public_url is http
        assert (await web.get("/api/me")).json()["owner"] is True
        assert (await web.get("/api/agents")).status_code == 200
        assert r.headers.get("cache-control") == "no-store"

        # The browser's socket authenticates with the same cookie.
        session = web.cookies.get("valet_session")
        async with connect(f"ws://127.0.0.1:{stack.ccfg.port}/ws/client",
                           additional_headers={"Cookie": f"valet_session={session}"}) as ws:
            assert '"hello"' in await asyncio.wait_for(ws.recv(), 5)

        assert (await web.post("/api/logout")).json() == {"ok": True}
        web.cookies.clear()
        assert (await web.get("/api/agents")).status_code == 401


async def test_login_rate_limit(stack):
    async with httpx.AsyncClient(base_url=stack.base) as anon:
        for _ in range(5):
            r = await anon.post("/api/login", json={"password": "not the password", "client": "native"})
            assert r.status_code == 401
        r = await anon.post("/api/login", json={"password": PASSWORD, "client": "native"})
        assert r.status_code == 429


async def test_ui_and_headers(stack):
    async with httpx.AsyncClient(base_url=stack.base) as anon:
        r = await anon.get("/")
        assert r.status_code == 200 and "/static/app.js" in r.text
        assert "default-src 'self'" in r.headers["content-security-policy"]
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["x-content-type-options"] == "nosniff"
        assert (await anon.get("/docs")).status_code == 404
        assert (await anon.get("/openapi.json")).status_code == 404


async def test_define_edit_run(stack):
    r = await stack.http.post("/api/agents", json={"name": "Bad Name"})
    assert r.status_code == 422
    detail = await stack.create_agent("scout", persona="First persona.")
    assert detail["files"]["agent.yaml"] == AGENT_YAML.format(name="scout")
    assert detail["files"]["persona.md"] == "First persona."
    assert detail["definition"]["computer"]["level"] == "host" and detail["commit"]
    assert detail["status"] == "stopped" and detail["desired"] == "stopped"
    r = await stack.http.post("/api/agents", json={"name": "scout"})
    assert r.status_code == 409

    # A broken agent.yaml is refused and nothing changes.
    r = await stack.http.put("/api/agents/scout/files/agent.yaml", json={"text": "name: scout\ncolour: red\n"})
    assert r.status_code == 422 and "colour" in r.json()["detail"]
    assert (await stack.agent("scout"))["files"]["agent.yaml"] == AGENT_YAML.format(name="scout")
    r = await stack.http.put("/api/agents/scout/files/notes.txt", json={"text": "x"})
    assert r.status_code in (404, 422)
    r = await stack.http.put("/api/agents/ghost/files/persona.md", json={"text": "x"})
    assert r.status_code == 404

    r = await stack.http.put("/api/agents/scout/files/persona.md", json={"text": "Second persona."})
    assert r.status_code == 200
    commit = r.json()["commit"]
    assert commit != detail["commit"] and (await stack.agent("scout"))["commit"] == commit

    # The edited persona is what the harness gets on start.
    await stack.start_agent("scout")
    sid = (await stack.new_session("scout"))["id"]
    evs = await stack.run_turn("scout", sid, "meta")
    assert "Second persona." in texts(evs)

    r = await stack.http.get("/api/agents")
    assert [a["name"] for a in r.json()] == ["scout"]
