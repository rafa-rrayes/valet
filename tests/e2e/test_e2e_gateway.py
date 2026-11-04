"""The MCP gateway end to end: the real control plane and runner, the fake ACP agent as the harness (it calls the
gateway the way Claude Code does, from the "valet" MCP server its session got), and tests/fake_mcp.py as the
upstream server, run by the gateway over stdio (or served over HTTP for the remote case)."""
import asyncio
import contextlib
import json
import logging
import socket
import threading
from dataclasses import replace

import httpx
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from tests import fake_mcp
from tests.e2e.stack import ControlServer, Stack, end_of, eventually, kinds, texts

SECRET = "s3cret!"

AGENT = """\
name: {name}
harness: claude
computer:
  level: host
permissions: {level}
tools:
  mcp: [{servers}]
"""

REGISTRY = f"""\
servers:
  fake:
    description: The test upstream
    command: {json.dumps(fake_mcp.COMMAND[0])}
    args: [{json.dumps(fake_mcp.COMMAND[1])}]
    env:
      FAKE_SECRET: ${{env:VALET_TEST_FAKE_SECRET}}
"""

FAKE_TOOLS = ["fake__echo", "fake__add_note", "fake__delete_note", "fake__poke", "fake__secret_length",
              "fake__slow", "fake__fail"]
VALET_READ = ["valet_agents__list_agents", "valet_agents__agent_status"]
VALET_TOOLS = VALET_READ + ["valet_agents__send_message"]


@pytest.fixture(autouse=True)
def secret_env(monkeypatch):
    monkeypatch.setenv("VALET_TEST_FAKE_SECRET", SECRET)


async def put_registry(stack: Stack, text: str = REGISTRY) -> dict:
    r = await stack.http.put("/api/gateway/registry", json={"text": text})
    assert r.status_code == 200, r.text
    return r.json()


async def gw_agent(stack: Stack, name: str = "gw", level: str = "ask", servers: str = "valet.agents, fake",
                   registry: bool = True) -> str:
    """A running agent with gateway servers; returns a new session's id."""
    if registry:
        await put_registry(stack)
    r = await stack.http.post("/api/agents", json={"name": name, "agent_yaml": AGENT.format(
        name=name, level=level, servers=servers)})
    assert r.status_code == 201, r.text
    await stack.start_agent(name)
    return (await stack.new_session(name))["id"]


async def set_level(stack: Stack, name: str, level: str, servers: str = "valet.agents, fake") -> None:
    r = await stack.http.put(f"/api/agents/{name}/files/agent.yaml",
                             json={"text": AGENT.format(name=name, level=level, servers=servers)})
    assert r.status_code == 200, r.text


def access(stack: Stack, name: str = "gw"):
    """The gateway url and token the runner got in agent.start for this agent."""
    return stack.runner.supervisor._agents[name].gateway


@contextlib.asynccontextmanager
async def mcp_client(url: str, token: str, mode: str = "auto"):
    async with create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}) as http:
        async with Client(streamable_http_client(url, http_client=http), mode=mode) as client:
            yield client


@contextlib.asynccontextmanager
async def agent_client(stack: Stack, name: str = "gw"):
    g = access(stack, name)
    async with mcp_client(g.url, g.token) as client:
        yield client


async def say(stack: Stack, sid: str, text: str, name: str = "gw") -> str:
    """One turn; the agent's reply."""
    evs = await stack.run_turn(name, sid, text)
    assert end_of(evs)["error"] is None, evs
    return texts(evs)


async def calls(stack: Stack, **params) -> list[dict]:
    r = await stack.http.get("/api/gateway/calls", params=params)
    assert r.status_code == 200, r.text
    return r.json()["calls"]


def result_text(result) -> str:
    return " ".join(c.text for c in result.content if c.type == "text")


# ---------- the harness gets the gateway ----------

async def test_session_gets_the_valet_server(stack):
    sid = await gw_agent(stack)
    [server] = json.loads(await say(stack, sid, "mcp-servers"))
    assert server == {"name": "valet", "type": "http", "url": f"{stack.base}/mcp/agents/gw",
                      "headers": ["Authorization"]}
    assert access(stack).token.startswith("vagt_")
    # An agent without MCP servers gets no gateway, and no token.
    await stack.ready_agent("plain")
    sid2 = (await stack.new_session("plain"))["id"]
    assert json.loads(await say(stack, sid2, "mcp-servers", name="plain")) == []
    assert access(stack, "plain") is None


async def test_harness_calls_through_the_gateway(stack):
    sid = await gw_agent(stack)
    assert (await say(stack, sid, "mcp-list")).split(",") == VALET_TOOLS + FAKE_TOOLS
    assert await say(stack, sid, 'mcp fake__echo {"text": "hi"}') == "ok: echo: hi"
    # The upstream got its credential from the control plane's env; the agent never saw it.
    assert await say(stack, sid, "mcp fake__secret_length {}") == f"ok: {len(SECRET)}"
    listed = json.loads((await say(stack, sid, "mcp valet_agents__list_agents {}")).removeprefix("ok: "))
    assert listed["you"] == "gw" and [a["name"] for a in listed["agents"]] == ["gw"]
    assert listed["agents"][0]["status"] == "working"
    status = json.loads((await say(stack, sid, 'mcp valet_agents__agent_status {"name": "gw"}')).removeprefix("ok: "))
    assert status["name"] == "gw" and status["sessions"][0]["state"] == "working"
    assert (await say(stack, sid, "mcp fake__fail {}")).startswith("error: ")
    assert (await say(stack, sid, "mcp fake__nope {}")).startswith("error: There's no tool called fake__nope")

    rows = await calls(stack, agent="gw")
    assert [(c["server"], c["tool"], c["decision"], c["outcome"]) for c in rows] == [
        ("", "fake__nope", None, "unknown"),
        ("fake", "fail", "allow", "error"),
        ("valet.agents", "agent_status", "allow", "ok"),
        ("valet.agents", "list_agents", "allow", "ok"),
        ("fake", "secret_length", "allow", "ok"),
        ("fake", "echo", "allow", "ok"),
    ]
    echo = rows[-1]
    assert echo["arguments"] == {"text": "hi"} and echo["agent"] == "gw"
    assert echo["duration"] >= 0 and echo["ended_at"] >= echo["started_at"]
    assert await calls(stack, agent="gw", server="valet.agents") == rows[2:4]
    page = (await stack.http.get("/api/gateway/calls", params={"agent": "gw", "limit": 2})).json()
    assert page["more"] is True and [c["id"] for c in page["calls"]] == [r["id"] for r in rows[:2]]
    older = await calls(stack, agent="gw", before=rows[1]["id"])
    assert [c["id"] for c in older] == [r["id"] for r in rows[2:]]


# ---------- listing follows the definition and the level ----------

async def test_listing_follows_definition_and_level(stack):
    await gw_agent(stack, level="locked")
    async with agent_client(stack) as client:
        names = [t.name for t in (await client.list_tools()).tools]
        # locked: only what's read-only; the rest would always be denied, so it isn't offered.
        assert names == VALET_READ + ["fake__echo", "fake__secret_length", "fake__slow", "fake__fail"]
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert tools["fake__echo"].annotations.read_only_hint is True
        assert tools["fake__echo"].input_schema["required"] == ["text"]

        await set_level(stack, "gw", "ask")             # saved definitions apply at the next request
        assert [t.name for t in (await client.list_tools()).tools] == VALET_TOOLS + FAKE_TOOLS

        await set_level(stack, "gw", "trusted", servers="valet.agents")
        assert [t.name for t in (await client.list_tools()).tools] == VALET_TOOLS
        result = await client.call_tool("fake__echo", {"text": "x"})     # not one of its servers any more
        assert result.is_error and "no tool called fake__echo" in result_text(result)

    await set_level(stack, "gw", "trusted")
    r = await stack.http.get("/api/agents/gw/tools")
    assert r.status_code == 200, r.text
    view = r.json()
    assert view["permissions"] == "trusted" and view["url"] == f"{stack.base}/mcp/agents/gw"
    assert [s["server"] for s in view["servers"]] == ["valet.agents", "fake"]
    decisions = {t["name"]: t["decision"] for s in view["servers"] for t in s["tools"]}
    assert decisions == {"valet_agents__list_agents": "allow", "valet_agents__agent_status": "allow",
                         "valet_agents__send_message": "allow",
                         "fake__echo": "allow", "fake__add_note": "allow", "fake__delete_note": "ask",
                         "fake__poke": "ask", "fake__secret_length": "allow", "fake__slow": "allow",
                         "fake__fail": "allow"}


async def test_tools_preview_another_level_without_saving_it(stack):
    """The settings page shows what a level change does before it's saved: ?permissions= decides at that level."""
    await gw_agent(stack, level="ask")
    decisions = {}
    for level in ("locked", "trusted", "unrestricted"):
        r = await stack.http.get("/api/agents/gw/tools", params={"permissions": level})
        assert r.status_code == 200, r.text
        assert r.json()["permissions"] == level
        decisions[level] = {t["name"]: t["decision"] for s in r.json()["servers"] for t in s["tools"]}
    assert decisions["locked"]["fake__echo"] == "allow"                  # read-only runs at every level
    assert decisions["locked"]["fake__add_note"] == "deny" and decisions["locked"]["fake__delete_note"] == "deny"
    assert decisions["trusted"]["fake__add_note"] == "allow" and decisions["trusted"]["fake__delete_note"] == "ask"
    assert set(decisions["unrestricted"].values()) == {"allow"}
    saved = (await stack.http.get("/api/agents/gw/tools")).json()           # the saved level is untouched
    assert saved["permissions"] == "ask"
    assert {t["name"]: t["decision"] for s in saved["servers"] for t in s["tools"]}["fake__add_note"] == "ask"
    assert (await stack.http.get("/api/agents/gw")).json()["definition"]["permissions"] == "ask"
    r = await stack.http.get("/api/agents/gw/tools", params={"permissions": "root"})
    assert r.status_code == 422


async def test_unavailable_servers_are_reported_not_fatal(stack):
    await gw_agent(stack, servers="valet.agents, ghost, fake")      # every server DESIGN.md names is built now
    view = (await stack.http.get("/api/agents/gw/tools")).json()
    status = {s["server"]: (s["ok"], s["error"]) for s in view["servers"]}
    assert status["ghost"] == (False, "no server named ghost in mcp.yaml")
    assert status["valet.agents"] == (True, None) and status["fake"] == (True, None)
    async with agent_client(stack) as client:
        assert [t.name for t in (await client.list_tools()).tools] == VALET_TOOLS + FAKE_TOOLS


# ---------- decisions ----------

async def test_ask_lands_in_the_inbox_and_allow_runs_it(stack):
    feed = await stack.feed()
    sid = await gw_agent(stack, level="ask")
    turn = await stack.prompt("gw", sid, 'mcp fake__add_note {"text": "milk"}')
    [item] = await stack.wait_pending()
    assert item["kind"] == "tool" and item["agent"] == "gw" and item["status"] == "pending"
    assert item["session_id"] == sid and item["turn_id"] == turn     # its only running turn
    assert item["title"] == "fake: add_note"
    assert item["tool"] == {"server": "fake", "name": "add_note", "title": None, "description": "Add a note.",
                            "arguments": {"text": "milk"}, "annotations": {"readOnlyHint": False,
                                                                           "destructiveHint": False}}
    assert item["tool_call"]["rawInput"] == {"text": "milk"} and item["tool_call"]["title"] == "fake: add_note"
    assert [o["optionId"] for o in item["options"]] == ["allow_once", "allow_hour", "allow_always", "reject_once"]
    assert (await stack.agent("gw"))["status"] == "waiting"
    await feed.wait(lambda f: f["type"] == "inbox" and f["item"]["id"] == item["id"], what="inbox frame")
    req = await feed.wait(lambda f: f["type"] == "event" and f["event"]["kind"] == "permission_request",
                          what="permission_request event")
    assert req["event"]["session_id"] == sid and req["event"]["data"]["requestId"] == item["id"]
    assert req["event"]["data"]["tool"] == item["tool"]     # the conversation shows it without the inbox item

    r = await stack.http.post(f"/api/inbox/{item['id']}/answer", json={"option_id": "allow_once"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "answered"
    evs = await stack.turn("gw", sid, turn)
    assert texts(evs) == "ok: noted: milk"
    assert kinds(evs) == ["user_message", "permission_request", "permission_answer", "agent_message", "turn_end"]
    [row] = await calls(stack, agent="gw")
    assert (row["decision"], row["outcome"]) == ("ask", "ok")
    assert await stack.pending() == []
    assert (await stack.agent("gw"))["status"] == "idle"


async def test_ask_rejected_never_reaches_the_upstream(stack):
    sid = await gw_agent(stack, level="ask")
    turn = await stack.prompt("gw", sid, 'mcp fake__delete_note {"name": "todo"}')
    [item] = await stack.wait_pending()
    r = await stack.http.post(f"/api/inbox/{item['id']}/answer", json={"option_id": "reject_once"})
    assert r.status_code == 200 and r.json()["answer"] == {"outcome": "selected", "optionId": "reject_once"}
    assert texts(await stack.turn("gw", sid, turn)) == "error: The owner rejected this call: delete_note was not run."
    [row] = await calls(stack, agent="gw")
    assert (row["decision"], row["outcome"]) == ("ask", "rejected")
    r = await stack.http.post(f"/api/inbox/{item['id']}/answer", json={"option_id": "allow_once"})
    assert r.status_code == 409


async def test_trusted_asks_only_before_destructive_tools(stack):
    await gw_agent(stack, level="trusted")
    async with agent_client(stack) as client:
        assert result_text(await client.call_tool("fake__add_note", {"text": "a"})) == "noted: a"
        assert await stack.pending() == []
        for tool, args in (("fake__delete_note", {"name": "a"}), ("fake__poke", {})):
            task = asyncio.create_task(client.call_tool(tool, args))
            [item] = await stack.wait_pending()
            assert item["tool"]["name"] == tool.removeprefix("fake__")
            assert item["session_id"] is None        # no turn was running: it's in the inbox only
            r = await stack.http.post(f"/api/inbox/{item['id']}/answer", json={"option_id": "allow_once"})
            assert r.status_code == 200, r.text
            result = await task
            assert not result.is_error, result
    rows = await calls(stack, agent="gw")
    assert [(c["tool"], c["decision"], c["outcome"]) for c in rows] == [
        ("poke", "ask", "ok"), ("delete_note", "ask", "ok"), ("add_note", "allow", "ok")]


async def test_locked_denies_and_unrestricted_allows(stack):
    await gw_agent(stack, level="locked")
    async with agent_client(stack) as client:
        result = await client.call_tool("fake__delete_note", {"name": "x"})   # not listed, called anyway
        assert result.is_error and "doesn't allow fake delete_note" in result_text(result)
        assert result_text(await client.call_tool("fake__echo", {"text": "ro"})) == "echo: ro"
        await set_level(stack, "gw", "unrestricted")
        assert result_text(await client.call_tool("fake__delete_note", {"name": "x"})) == "deleted x"
    assert await stack.pending() == []
    rows = await calls(stack, agent="gw")
    assert [(c["tool"], c["decision"], c["outcome"]) for c in rows] == [
        ("delete_note", "allow", "ok"), ("echo", "allow", "ok"), ("delete_note", "deny", "denied")]


async def test_ask_expires_when_nobody_answers(tmp_path):
    stack = Stack(tmp_path, "load")
    stack.ccfg = replace(stack.ccfg, gateway_ask_timeout=1.0)
    stack.control = ControlServer(stack.ccfg)
    await stack.start()
    try:
        sid = await gw_agent(stack, level="ask")
        reply = await say(stack, sid, "mcp fake__poke {}")
        assert reply.startswith("error: The owner didn't answer in time, so poke was not run.")
        r = await stack.http.get("/api/inbox", params={"status": "all"})
        [item] = r.json()
        assert item["status"] == "expired"
        r = await stack.http.post(f"/api/inbox/{item['id']}/answer", json={"option_id": "allow_once"})
        assert r.status_code == 409
        [row] = await calls(stack, agent="gw")
        assert (row["decision"], row["outcome"]) == ("ask", "expired") and row["duration"] >= 1.0
    finally:
        await stack.close()


async def test_stopping_the_agent_expires_its_ask_and_its_token(stack):
    sid = await gw_agent(stack, level="ask")
    g = access(stack)
    await stack.prompt("gw", sid, "mcp fake__poke {}")
    [item] = await stack.wait_pending()
    r = await stack.http.post("/api/agents/gw/stop")
    assert r.status_code == 200, r.text
    assert (await stack.http.get(f"/api/inbox/{item['id']}")).json()["status"] == "expired"
    async with httpx.AsyncClient() as http:
        r = await http.post(g.url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                            headers={"Authorization": f"Bearer {g.token}"})
    assert r.status_code == 401


async def test_control_restart_expires_pending_asks(stack):
    await gw_agent(stack, level="ask")
    g = access(stack)

    async def call():
        async with mcp_client(g.url, g.token) as client:
            return await client.call_tool("fake__poke", {})
    task = asyncio.create_task(call())
    [item] = await stack.wait_pending()
    await stack.restart_control()
    with contextlib.suppress(BaseException):
        await asyncio.wait_for(task, 10)        # the call died with the old process
    r = await stack.http.get(f"/api/inbox/{item['id']}")
    assert r.json()["status"] == "expired"
    r = await stack.http.post(f"/api/inbox/{item['id']}/answer", json={"option_id": "allow_once"})
    assert r.status_code == 409
    [row] = await calls(stack, agent="gw")
    assert row["outcome"] in ("interrupted", "cancelled")
    # The agent's token survived the restart: it keeps its tools.
    async with mcp_client(g.url, g.token) as client:
        assert result_text(await client.call_tool("fake__echo", {"text": "back"})) == "echo: back"


# ---------- tokens ----------

async def test_tokens_open_only_their_own_agents_endpoint(stack):
    await gw_agent(stack, name="gw")
    await gw_agent(stack, name="other", registry=False)
    mine, theirs = access(stack, "gw"), access(stack, "other")
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}

    async def post(path: str, token: str | None):
        headers = {"Accept": "application/json, text/event-stream"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        async with httpx.AsyncClient(base_url=stack.base) as http:
            return await http.post(path, json=body, headers=headers)

    assert (await post("/mcp/agents/gw", theirs.token)).status_code == 403
    assert (await post("/mcp/agents/gw", None)).status_code == 401
    r = await post("/mcp/agents/gw", "vagt_bogus")
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Bearer")
    assert (await post("/mcp/agents/gw", stack.owner_token)).status_code == 401    # an owner token isn't one
    assert (await post("/mcp/nope", mine.token)).status_code == 404
    with pytest.raises(BaseException):
        async with mcp_client(f"{stack.base}/mcp/agents/other", mine.token) as client:
            await client.list_tools()
    # Restarting issues a new token; the old one stops working.
    await stack.restart_agent("gw")
    assert access(stack, "gw").token != mine.token
    assert (await post("/mcp/agents/gw", mine.token)).status_code == 401


# ---------- servers ----------

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.asynccontextmanager
async def remote_upstream(token: str):
    """tests/fake_mcp.py served over streamable HTTP, behind a bearer token, on a free port."""
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(fake_mcp.http_app(token), host="127.0.0.1", port=port,
                                           log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    await eventually(lambda: server.started, what="remote upstream")
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        await asyncio.to_thread(thread.join, 10)


async def test_remote_server_with_a_header_from_the_control_plane_env(stack, monkeypatch):
    async with remote_upstream("remote-t0ken") as url:
        await put_registry(stack, REGISTRY + f"""\
  remote:
    url: {url}
    headers:
      Authorization: Bearer ${{env:VALET_TEST_REMOTE_TOKEN}}
""")
        await gw_agent(stack, servers="remote", registry=False)
        view = (await stack.http.get("/api/agents/gw/tools")).json()
        [server] = view["servers"]
        assert not server["ok"] and "VALET_TEST_REMOTE_TOKEN isn't set" in server["error"]
        monkeypatch.setenv("VALET_TEST_REMOTE_TOKEN", "wrong")
        view = (await stack.http.get("/api/agents/gw/tools")).json()
        assert not view["servers"][0]["ok"]                     # the remote refuses the wrong token
        monkeypatch.setenv("VALET_TEST_REMOTE_TOKEN", "remote-t0ken")
        async with agent_client(stack) as client:
            assert [t.name for t in (await client.list_tools()).tools] == [
                n.replace("fake__", "remote__") for n in FAKE_TOOLS]
            assert result_text(await client.call_tool("remote__echo", {"text": "far"})) == "echo: far"


async def test_registry_credentials_from_valets_secrets(stack, caplog):
    """`${secret:NAME}` in mcp.yaml: decrypted from Valet's secrets when the gateway connects; a new value
    reconnects at the next call, a deleted one cuts the server off. No agent restart is involved (the gateway holds
    the credential, not the agent), and the value is in no API response, audit row or log line."""
    caplog.set_level(logging.INFO)
    first, second = "vault-TEST-value-0001", "vault-TEST-value-000002"
    await put_registry(stack, REGISTRY.replace("${env:VALET_TEST_FAKE_SECRET}", "${secret:FAKE_TOKEN}"))
    sid = await gw_agent(stack, servers="fake", registry=False)
    [server] = (await stack.http.get("/api/agents/gw/tools")).json()["servers"]
    assert not server["ok"] and "FAKE_TOKEN isn't set in Valet's secrets" in server["error"]
    assert (await stack.http.get("/api/secrets")).json() == [
        {"name": "FAKE_TOKEN", "set": False, "updated_at": None, "used_by": [], "servers": ["fake"]}]

    r = await stack.http.put("/api/secrets/FAKE_TOKEN", json={"value": first})
    assert r.status_code == 200 and r.json()["servers"] == ["fake"]
    assert await say(stack, sid, "mcp fake__secret_length {}") == f"ok: {len(first)}"
    await stack.http.put("/api/secrets/FAKE_TOKEN", json={"value": second})
    assert await say(stack, sid, "mcp fake__secret_length {}") == f"ok: {len(second)}"
    assert (await stack.agent("gw"))["restart_needed"] is False

    assert (await stack.http.delete("/api/secrets/FAKE_TOKEN")).status_code == 200
    out = await say(stack, sid, "mcp fake__secret_length {}")
    assert out.startswith("error: ") and "FAKE_TOKEN isn't set in Valet's secrets" in out
    servers = {s["name"]: s for s in (await stack.http.get("/api/gateway/servers")).json()["servers"]}
    assert servers["fake"]["connected"] is False and servers["fake"]["secrets"] == ["FAKE_TOKEN"]

    seen = [json.dumps(servers), (await stack.http.get("/api/gateway/registry")).text,
            json.dumps(await calls(stack)), (await stack.http.get("/api/secrets")).text,
            (await stack.http.get("/api/agents/gw/tools")).text, (await stack.http.get("/api/agents/gw")).text,
            caplog.text]
    raw = b"".join(f.read_bytes() for f in stack.ccfg.data.glob("valet.db*"))
    for value in (first, second):
        assert all(value not in text for text in seen) and value.encode() not in raw


async def test_upstream_errors_never_quote_a_credential(stack):
    """A server that fails with the resolved credential in its error (here: the url, with a secret in its query
    string, unreachable) has it replaced by "[redacted]" in what the API, the agent and the audit log see."""
    r = await stack.http.put("/api/secrets/URL_KEY", json={"value": "k3y-TEST-in-the-url"})
    assert r.status_code == 200
    await put_registry(stack, REGISTRY + f"""\
  remote:
    url: http://127.0.0.1:{free_port()}/mcp?key=${{secret:URL_KEY}}
""")
    sid = await gw_agent(stack, servers="remote", registry=False)
    [server] = (await stack.http.get("/api/agents/gw/tools")).json()["servers"]
    assert not server["ok"] and "can't connect" in server["error"]
    out = await say(stack, sid, "mcp remote__echo {}")
    assert out.startswith("error: ")
    for text in (server["error"], out, json.dumps(await calls(stack))):
        assert "k3y-TEST-in-the-url" not in text


async def test_registry_api(stack):
    r = await stack.http.get("/api/gateway/registry")
    assert r.json()["text"] == ""
    r = await stack.http.put("/api/gateway/registry", json={"text": "servers:\n  valet-x: {command: a}\n"})
    assert r.status_code == 422 and "reserved" in r.json()["detail"]
    assert (await stack.http.get("/api/gateway/registry")).json()["text"] == ""
    out = await put_registry(stack)
    assert out["commit"] and out["error"] is None
    await gw_agent(stack, registry=False)
    assert (await stack.http.get("/api/gateway/registry")).json()["text"] == REGISTRY
    servers = {s["name"]: s for s in (await stack.http.get("/api/gateway/servers")).json()["servers"]}
    assert list(servers) == ["valet.agents", "valet.browser", "valet.memory", "valet.owner", "valet.shared", "fake"]
    assert servers["valet.owner"]["builtin"] and servers["valet.owner"]["agents"] == []
    assert servers["valet.agents"]["builtin"] and servers["valet.agents"]["agents"] == ["gw"]
    assert servers["valet.browser"]["builtin"] and servers["valet.browser"]["agents"] == []
    fake = servers["fake"]
    assert fake["kind"] == "stdio" and fake["env"] == ["FAKE_SECRET"] and fake["agents"] == ["gw"]
    assert SECRET not in json.dumps(fake) and "VALET_TEST_FAKE_SECRET" not in json.dumps(fake)
    assert fake["connected"] is False
    async with agent_client(stack) as client:
        await client.list_tools()
    servers = {s["name"]: s for s in (await stack.http.get("/api/gateway/servers")).json()["servers"]}
    assert servers["fake"]["connected"] is True
    # The definitions repo keeps its history.
    history = (await stack.http.get("/api/agents/gw/history")).json()
    assert history and (stack.ccfg.definitions / "mcp.yaml").read_text() == REGISTRY
