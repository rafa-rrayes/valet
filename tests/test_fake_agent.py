"""The fake agent itself, driven by a bare acp client: if these fail, the runner and e2e tests can't be trusted."""
import asyncio
import json

import acp
import pytest
from acp import schema

from tests.fake_agent import COMMAND


class Client:
    """Records updates; answers permission requests with `answer` (an option id, or None to wait for cancel)."""

    def __init__(self, answer: str | None = "allow") -> None:
        self.updates: list[dict] = []
        self.answer = answer
        self.asked = asyncio.Event()
        self.release = asyncio.Event()

    async def session_update(self, session_id, update, **kw):
        self.updates.append(update.model_dump(by_alias=True, exclude_none=True, mode="json"))

    async def request_permission(self, session_id, tool_call, options, **kw):
        assert [o.kind for o in options] == ["allow_once", "allow_always", "reject_once"]
        self.asked.set()
        if self.answer is None:
            await self.release.wait()
            return acp.RequestPermissionResponse(outcome=schema.DeniedOutcome(outcome="cancelled"))
        return acp.RequestPermissionResponse(outcome=schema.AllowedOutcome(outcome="selected", option_id=self.answer))

    def texts(self, kind="agent_message_chunk"):
        return [u["content"]["text"] for u in self.updates if u["sessionUpdate"] == kind]


async def start(client, *args):
    cm = acp.spawn_agent_process(client, *COMMAND, *args)
    conn, proc = await cm.__aenter__()
    init = await conn.initialize(protocol_version=acp.PROTOCOL_VERSION,
                                 client_capabilities=schema.ClientCapabilities(),
                                 client_info=schema.Implementation(name="test", version="0"))
    return cm, conn, proc, init


@pytest.fixture
async def agent():
    opened = []

    async def _open(client, *args):
        cm, conn, proc, init = await start(client, *args)
        opened.append(cm)
        return conn, proc, init

    yield _open
    for cm in reversed(opened):
        await cm.__aexit__(None, None, None)


async def test_echo(agent):
    c = Client()
    conn, _, init = await agent(c)
    assert init.agent_capabilities.load_session is True
    assert init.agent_info.name == "fake-agent"
    s = await conn.new_session(cwd="/tmp", mcp_servers=[])
    assert s.session_id.startswith("fake-")
    r = await conn.prompt(session_id=s.session_id, prompt=[acp.text_block("hello there")])
    assert r.stop_reason == "end_turn"
    assert c.texts("agent_thought_chunk") == ["thinking"]
    assert c.texts() == ["echo: ", "hello there"]


@pytest.mark.parametrize("answer,status,text", [("allow", "completed", "allowed"),
                                                ("allow-always", "completed", "allowed"),
                                                ("reject", "failed", "rejected")])
async def test_permission(agent, answer, status, text):
    c = Client(answer)
    conn, _, _ = await agent(c)
    s = await conn.new_session(cwd="/tmp", mcp_servers=[])
    r = await conn.prompt(session_id=s.session_id, prompt=[acp.text_block("permission")])
    assert r.stop_reason == "end_turn"
    start_, update = c.updates[0], c.updates[1]
    assert start_["sessionUpdate"] == "tool_call" and start_["title"] == "Write hello.txt"
    assert start_["kind"] == "edit" and start_["status"] == "pending"
    assert update == {"sessionUpdate": "tool_call_update", "toolCallId": start_["toolCallId"], "status": status}
    assert c.texts() == [text]


async def test_permission_cancelled(agent):
    c = Client(answer=None)
    conn, _, _ = await agent(c)
    s = await conn.new_session(cwd="/tmp", mcp_servers=[])
    turn = asyncio.create_task(conn.prompt(session_id=s.session_id, prompt=[acp.text_block("permission")]))
    await asyncio.wait_for(c.asked.wait(), 5)
    await conn.cancel(session_id=s.session_id)
    c.release.set()
    r = await asyncio.wait_for(turn, 5)
    assert r.stop_reason == "cancelled"
    assert c.texts() == ["cancelled"]


async def test_slow_cancel(agent):
    c = Client()
    conn, _, _ = await agent(c)
    s = await conn.new_session(cwd="/tmp", mcp_servers=[])
    turn = asyncio.create_task(conn.prompt(session_id=s.session_id, prompt=[acp.text_block("slow")]))
    for _ in range(100):
        if c.texts() == ["working"]:
            break
        await asyncio.sleep(0.05)
    await conn.cancel(session_id=s.session_id)
    assert (await asyncio.wait_for(turn, 5)).stop_reason == "cancelled"


async def test_tool_fail_meta(agent):
    c = Client()
    conn, _, _ = await agent(c)
    s = await conn.new_session(cwd="/tmp", mcp_servers=[], systemPrompt={"append": "be brief"})
    await conn.prompt(session_id=s.session_id, prompt=[acp.text_block("tool")])
    kinds = [u["sessionUpdate"] for u in c.updates]
    assert kinds == ["tool_call", "tool_call_update", "tool_call_update", "agent_message_chunk"]
    assert c.updates[2]["status"] == "completed"
    with pytest.raises(acp.RequestError):
        await conn.prompt(session_id=s.session_id, prompt=[acp.text_block("fail")])
    c.updates.clear()
    await conn.prompt(session_id=s.session_id, prompt=[acp.text_block("meta")])
    assert json.loads(c.texts()[0]) == {"systemPrompt": {"append": "be brief"}}


async def test_crash(agent):
    c = Client()
    conn, proc, _ = await agent(c)
    s = await conn.new_session(cwd="/tmp", mcp_servers=[])
    with pytest.raises(Exception):
        await asyncio.wait_for(conn.prompt(session_id=s.session_id, prompt=[acp.text_block("crash")]), 5)
    assert await asyncio.wait_for(proc.wait(), 5) == 3
    assert c.texts() == ["crashing"]


async def test_load_session(agent):
    c = Client()
    conn, _, _ = await agent(c)
    await conn.load_session(cwd="/tmp", session_id="old-1", mcp_servers=[])
    assert c.texts("user_message_chunk") == ["(replayed)"] and c.texts() == ["(replayed)"]
    r = await conn.prompt(session_id="old-1", prompt=[acp.text_block("hi")])
    assert r.stop_reason == "end_turn"


async def test_resume_and_none(agent):
    c = Client()
    conn, _, init = await agent(c, "--sessions", "resume")
    caps = init.agent_capabilities
    assert caps.load_session is False and caps.session_capabilities.resume is not None
    await conn.resume_session(cwd="/tmp", session_id="old-2", mcp_servers=[])
    assert c.updates == []
    assert (await conn.prompt(session_id="old-2", prompt=[acp.text_block("hi")])).stop_reason == "end_turn"

    conn2, _, init2 = await agent(Client(), "--sessions", "none")
    assert init2.agent_capabilities.load_session is False
    assert init2.agent_capabilities.session_capabilities.resume is None
    with pytest.raises(acp.RequestError):
        await conn2.prompt(session_id="old-3", prompt=[acp.text_block("hi")])
