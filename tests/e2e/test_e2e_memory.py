"""Memory and the shared space end to end: the real control plane and runner, the fake ACP agent as the harness,
calling valet.memory and valet.shared through the "valet" MCP server its session got, as Claude Code would.

An agent writes its memory, and after a restart finds it in its instructions; Alice sees and edits it. An agent leaves
a note and files in the shared space (inline, and through transfer URLs fetched and sent with plain HTTP, as curl
would), Alice sees them as new; a change at `ask` waits for Alice."""
import json

import httpx

from tests.e2e.stack import Stack, end_of, texts

AGENT = """\
name: {name}
harness: claude
computer:
  level: host
permissions: {level}
memory: {memory}
directives: [persona.md, environment, memory]
tools:
  mcp: [{servers}]
"""


async def make_agent(stack: Stack, name: str = "mem", level: str = "ask", memory: bool = True,
                     servers: str = "") -> str:
    r = await stack.http.post("/api/agents", json={"name": name, "persona": "You are a test agent.\n",
                                                   "agent_yaml": AGENT.format(name=name, level=level, servers=servers,
                                                                              memory=str(memory).lower())})
    assert r.status_code == 201, r.text
    await stack.start_agent(name)
    return (await stack.new_session(name))["id"]


async def say(stack: Stack, name: str, sid: str, text: str) -> str:
    evs = await stack.run_turn(name, sid, text)
    assert end_of(evs)["error"] is None, evs
    return texts(evs)


def ok(reply: str) -> dict:
    assert reply.startswith("ok: "), reply
    return json.loads(reply.removeprefix("ok: "))


async def test_an_agent_remembers_across_a_restart(stack):
    sid = await make_agent(stack)
    tools = (await say(stack, "mem", sid, "mcp-list")).split(",")
    assert tools == [f"valet_memory__{t}" for t in ("list", "read", "write", "append", "delete", "search")]
    feed = await stack.feed()
    wrote = ok(await say(stack, "mem", sid, 'mcp valet_memory__write {"name": "facts", "text": '
                                                 '"# Facts\\n\\nThe code word is BANANA."}'))
    assert wrote["name"] == "facts" and wrote["title"] == "Facts"
    frame = await feed.wait(lambda f: f["type"] == "memory")
    assert frame["agent"] == "mem" and frame["file"]["name"] == "facts"
    ok(await say(stack, "mem", sid, 'mcp valet_memory__append {"name": "facts", "text": "Alice drinks tea."}'))

    # Alice sees it, and nothing asks for a restart.
    r = await stack.http.get("/api/agents/mem/memory/facts")
    assert r.json()["text"] == "# Facts\n\nThe code word is BANANA.\nAlice drinks tea.\n"
    assert (await stack.agent("mem"))["restart_needed"] is False

    # After a restart, its instructions carry it.
    await stack.restart_agent("mem")
    sid = (await stack.new_session("mem"))["id"]
    prompt = json.loads(await say(stack, "mem", sid, "meta"))["systemPrompt"]["append"]
    assert prompt.startswith("You are a test agent.\n\n# Your environment")
    assert "## Memory\n\nYou have a memory in Valet" in prompt
    assert "# Your memory\n\n" in prompt and "## Memory file: facts\n\n# Facts\n\nThe code word is BANANA." in prompt

    # Alice's edit is what it reads next.
    await stack.http.put("/api/agents/mem/memory/facts", json={"text": "# Facts\n\nThe code word is MANGO."})
    got = await say(stack, "mem", sid, 'mcp valet_memory__read {"name": "facts"}')
    assert got == "ok: # Facts\n\nThe code word is MANGO.\n"          # the model reads the file as it is
    hits = ok(await say(stack, "mem", sid, 'mcp valet_memory__search {"query": "mango"}'))["hits"]
    assert hits == [{"name": "facts", "line": 3, "text": "The code word is MANGO."}]


async def test_the_shared_space(stack):
    sid = await make_agent(stack, "sh", level="unrestricted", memory=False, servers="valet.shared")
    feed = await stack.feed()
    note = ok(await say(stack, "sh", sid, 'mcp valet_shared__shared_add_note {"text": "# Done\\n\\nAll green."}'))
    assert note["title"] == "Done" and note["added_by"] == "sh"
    frame = await feed.wait(lambda f: f["type"] == "shared" and "item" in f)
    assert frame["item"]["unseen"] is True and frame["unseen"] == 1

    # A file inline, and a bigger one through an upload URL, PUT with plain HTTP as curl -T would.
    ok(await say(stack, "sh", sid, 'mcp valet_shared__shared_add_file {"name": "out.txt", "content": "small"}'))
    up = ok(await say(stack, "sh", sid, 'mcp valet_shared__shared_add_file {"name": "big.log", "upload": true}'))
    big = b"line\n" * 60_000
    async with httpx.AsyncClient() as shell:
        r = await shell.put(up["upload_url"], content=big)
        assert r.status_code == 201, r.text
        assert r.json()["name"] == "big.log" and r.json()["size"] == len(big)
        listed = (await stack.http.get("/api/agents/sh/shared")).json()
        assert [i["title"] for i in listed["items"]] == ["big.log", "out.txt", "Done"] and listed["unseen"] == 3

        # Alice's file reaches the agent: inline when small, by a download URL when big.
        r = await stack.http.post("/api/agents/sh/shared/files", params={"name": "brief.md"}, content=b"# Brief\n\nGo.")
        brief = r.json()[0]
        got = ok(await say(stack, "sh", sid, f'mcp valet_shared__shared_get {{"id": {brief["id"]}}}'))
        assert got["content"] == "# Brief\n\nGo."
        r = await stack.http.post("/api/agents/sh/shared/files", params={"name": "data.csv"}, content=big)
        data = r.json()[0]
        got = ok(await say(stack, "sh", sid, f'mcp valet_shared__shared_get {{"id": {data["id"]}}}'))
        assert "content" not in got
        d = await shell.get(got["download_url"])
        assert d.status_code == 200 and d.content == big

    await stack.http.post("/api/shared/seen", json={})
    assert (await stack.http.get("/api/shared/unseen")).json() == {"unseen": 0, "agents": {}, "unassigned": 0}


async def test_a_change_at_ask_waits_for_alice(stack):
    sid = await make_agent(stack, "asker", level="ask", memory=True, servers="valet.shared")
    # Memory never asks; the shared space follows the level.
    ok(await say(stack, "asker", sid, 'mcp valet_memory__write {"name": "x", "text": "y"}'))
    turn = await stack.prompt("asker", sid, 'mcp valet_shared__shared_add_note {"text": "please?"}')
    [item] = await stack.wait_pending()
    assert item["kind"] == "tool" and item["tool"]["server"] == "valet.shared"
    r = await stack.http.post(f"/api/inbox/{item['id']}/answer", json={"option_id": "allow_once"})
    assert r.status_code == 200, r.text
    evs = await stack.turn("asker", sid, turn)
    assert texts(evs).startswith("ok: ")
    assert [i["title"] for i in (await stack.http.get("/api/shared")).json()["items"]] == ["please?"]
