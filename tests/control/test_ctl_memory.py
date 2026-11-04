"""Memory from the control plane's side: the owner's API (files, history, older versions, edits as commits, `memory`
frames), what a start carries (memory.md, the gateway for a memory-only agent), the environment's Memory section,
the preview's states, and valet.memory through the gateway endpoint: every call goes through, at any level."""
import json
import subprocess
import time

from valet.control import memory

YAML = "name: {name}\nharness: claude\ncomputer:\n  level: host\npermissions: {level}\n"
ACCEPT = {"Accept": "application/json, text/event-stream"}


def create(client, name="scout", level="ask", mem=True, directives="[persona.md, environment, memory]",
           extra=""):
    text = YAML.format(name=name, level=level) + f"memory: {'true' if mem else 'false'}\ndirectives: {directives}\n"
    r = client.post("/api/agents", json={"name": name, "persona": "You are Scout.\n", "agent_yaml": text + extra})
    assert r.status_code == 201, r.text


def _wait(predicate, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not reached")


def start(rpc, runner, client, name="scout") -> dict:
    r, cmd = rpc(runner, client.post, f"/api/agents/{name}/start", expect="agent.start")
    assert r.status_code == 200, r.text
    runner.status(name, "running", capabilities={"mcpCapabilities": {"http": True}})
    _wait(lambda: client.get(f"/api/agents/{name}").json()["state"] == "running")
    return cmd


def rpc_call(client, token, method, params=None, agent="scout") -> dict:
    r = client.post(f"/mcp/agents/{agent}", json={"jsonrpc": "2.0", "id": 1, "method": method,
                                                  "params": params or {}},
                    headers={**ACCEPT, "Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    if r.headers["content-type"].startswith("application/json"):
        return r.json()["result"]
    return json.loads(next(x[5:] for x in r.text.splitlines() if x.startswith("data:")))["result"]


def call(client, token, tool, args, agent="scout") -> dict:
    return rpc_call(client, token, "tools/call", {"name": tool, "arguments": args}, agent)


def test_owner_api(client, cfg):
    create(client)
    r = client.get("/api/agents/scout/memory")
    assert r.json() == {"agent": "scout", "enabled": True, "layer": True, "files": [], "size": 0,
                        "limits": memory.LIMITS}
    with client.websocket_connect("/ws/client") as ws:
        assert ws.receive_json()["type"] == "hello"
        r = client.put("/api/agents/scout/memory/notes.md", json={"text": "# Notes\n\nAlice likes tea."})
        assert r.status_code == 200, r.text
        f = r.json()["file"]
        assert (f["name"], f["title"], f["size"]) == ("notes", "Notes", 26)
        frame = ws.receive_json()
        assert frame == {"type": "memory", "agent": "scout", "name": "notes", "file": f}
        assert client.delete("/api/agents/scout/memory/notes").status_code == 200
        assert ws.receive_json() == {"type": "memory", "agent": "scout", "name": "notes", "file": None}
    client.put("/api/agents/scout/memory/notes", json={"text": "one"})
    client.put("/api/agents/scout/memory/notes", json={"text": "two"})
    listing = client.get("/api/agents/scout/memory").json()
    assert [x["name"] for x in listing["files"]] == ["notes"] and listing["size"] == 4
    got = client.get("/api/agents/scout/memory/notes").json()
    assert got["text"] == "two\n" and got["file"]["name"] == "notes" and got["commit"] is None

    h = client.get("/api/agents/scout/memory/notes/history").json()["history"]
    assert [e["message"] for e in h] == [
        "Memory: owner wrote scout's notes.md", "Memory: owner wrote scout's notes.md",
        "Memory: owner deleted scout's notes.md", "Memory: owner wrote scout's notes.md"]
    old = client.get("/api/agents/scout/memory/notes", params={"at": h[1]["commit"]}).json()
    assert old["text"] == "one\n" and old["file"] is None and old["commit"] == h[1]["commit"]
    deleted_at = next(e["commit"] for e in h if "deleted" in e["message"])
    assert client.get("/api/agents/scout/memory/notes", params={"at": deleted_at}).status_code == 404
    assert client.get("/api/agents/scout/memory/notes", params={"at": "nope"}).status_code == 422
    # Memory stays out of the agent's settings history.
    assert not any(e["message"].startswith("Memory:") for e in client.get("/api/agents/scout/history").json())

    assert client.get("/api/agents/scout/memory/ghost").status_code == 404
    assert client.delete("/api/agents/scout/memory/ghost").status_code == 404
    assert client.get("/api/agents/scout/memory/Bad Name").status_code == 422
    assert client.put("/api/agents/scout/memory/notes", json={"text": " "}).status_code == 422
    r = client.put("/api/agents/scout/memory/big", json={"text": "x" * (memory.FILE_MAX + 1)})
    assert r.status_code == 422 and "64 KiB" in r.json()["detail"]
    assert client.get("/api/agents/ghost/memory").status_code == 404
    # An agent without memory still shows its files (and says it's off).
    create(client, "plain", mem=False, directives="[persona.md]")
    assert client.get("/api/agents/plain/memory").json()["enabled"] is False


def test_owner_only(client):
    create(client)
    client.cookies.clear()
    assert client.get("/api/agents/scout/memory").status_code == 401
    assert client.put("/api/agents/scout/memory/x", json={"text": "x"}).status_code == 401


def test_start_carries_the_memory_layer_and_the_gateway(client, connect, rpc, cfg):
    create(client, level="locked")
    client.put("/api/agents/scout/memory/people", json={"text": "# People\n\nAlice's cat is Bartolomeu."})
    with connect() as runner:
        cmd = start(rpc, runner, client)
        files = cmd["files"]
        assert set(files) == {"persona.md", "environment.md", "memory.md"}
        assert files["memory.md"].startswith("# Your memory\n\n")
        assert "## Memory file: people\n\n# People\n\nAlice's cat is Bartolomeu." in files["memory.md"]
        env = files["environment.md"]
        assert "## Memory\n\nYou have a memory in Valet" in env and "(\"Your memory\")" in env
        assert "- valet.memory: Your memory:" in env and "Runs: list, read, write, append, delete, search." in env
        # A memory-only agent gets the gateway.
        assert cmd["gateway"]["url"].endswith("/mcp/agents/scout") and cmd["gateway"]["token"].startswith("vagt_")
        token = cmd["gateway"]["token"]

        # Memory changes never ask for a restart.
        client.put("/api/agents/scout/memory/people", json={"text": "# People\n\nChanged."})
        assert client.get("/api/agents/scout").json()["restart_needed"] is False

        # Locked, and still: every memory tool is listed and runs.
        names = [t["name"] for t in rpc_call(client, token, "tools/list")["tools"]]
        assert names == [f"valet_memory__{t}" for t in ("list", "read", "write", "append", "delete", "search")]
        r = call(client, token, "valet_memory__write", {"name": "plans", "text": "# Plans\n\nShip M4."})
        assert not r.get("isError"), r
        assert r["structuredContent"]["name"] == "plans"
        r = call(client, token, "valet_memory__append", {"name": "plans", "text": "Then M5."})
        assert not r.get("isError")
        r = call(client, token, "valet_memory__read", {"name": "plans"})
        assert r["content"][0]["text"] == "# Plans\n\nShip M4.\nThen M5.\n"
        r = call(client, token, "valet_memory__search", {"query": "m5"})
        assert r["structuredContent"]["hits"] == [{"name": "plans", "line": 4, "text": "Then M5."}]
        r = call(client, token, "valet_memory__list", {})
        assert [f["name"] for f in r["structuredContent"]["files"]] == ["people", "plans"]
        assert r["structuredContent"]["limits"] == memory.LIMITS
        r = call(client, token, "valet_memory__delete", {"name": "plans"})
        assert not r.get("isError")
        r = call(client, token, "valet_memory__read", {"name": "plans"})
        assert r["isError"] and "no memory file named plans" in r["content"][0]["text"]
        r = call(client, token, "valet_memory__write", {"name": "Bad", "text": "x"})
        assert r["isError"] and "lowercase letters" in r["content"][0]["text"]
        log = subprocess.run(["git", "-C", str(cfg.definitions), "log", "--format=%s", "-3"], capture_output=True,
                             text=True, check=True).stdout.splitlines()
        assert log == ["Memory: scout deleted plans.md", "Memory: scout appended to plans.md",
                       "Memory: scout wrote plans.md"]
        rows = client.get("/api/gateway/calls", params={"agent": "scout"}).json()["calls"]
        assert {(c["server"], c["decision"]) for c in rows} == {("valet.memory", "allow")}
        assert {c["tool"] for c in rows} == {"write", "append", "read", "search", "list", "delete"}

        # Memory off: no valet.memory, no gateway, memory.md empty (its layer still listed).
        d = client.get("/api/agents/scout").json()["definition"]
        client.put("/api/agents/scout/definition", json={"definition": {**d, "memory": False}})
        assert client.get("/api/agents/scout").json()["restart_needed"] is True
        cmd = start(rpc, runner, client)
        assert cmd["gateway"] is None and cmd["files"]["memory.md"] == ""
        assert "## Memory" not in cmd["files"]["environment.md"]
        assert "Valet gives you no MCP tools." in cmd["files"]["environment.md"]


def test_gateway_servers_listing_and_settings(client):
    create(client)
    servers = {s["name"]: s for s in client.get("/api/gateway/servers").json()["servers"]}
    assert servers["valet.memory"]["comes_with"] == "memory" and servers["valet.memory"]["agents"] == ["scout"]
    assert servers["valet.agents"]["comes_with"] is None
    assert "valet.shared" in servers and servers["valet.shared"]["comes_with"] is None
    view = client.get("/api/agents/scout/tools").json()
    assert [s["server"] for s in view["servers"]] == ["valet.memory"]
    assert {t["decision"] for t in view["servers"][0]["tools"]} == {"allow"}
    # tools.mcp can't list it: it comes with memory.
    d = client.get("/api/agents/scout").json()["definition"]
    r = client.put("/api/agents/scout/definition",
                   json={"definition": {**d, "tools": {**d["tools"], "mcp": ["valet.memory"]}}})
    assert r.status_code == 422 and "comes with `memory: true`" in r.text


def test_preview_states(client):
    create(client)
    layers = {x["name"]: x for x in client.get("/api/agents/scout/directives").json()["layers"]}
    assert layers["memory"]["state"] == "ok" and layers["memory"]["file"] == "memory.md"
    assert layers["memory"]["text"].startswith("# Your memory") and "no memory files yet" in layers["memory"]["text"]
    d = client.get("/api/agents/scout").json()["definition"]
    p = client.post("/api/agents/scout/directives/preview", json={"definition": {**d, "memory": False}}).json()
    assert {x["name"]: x["state"] for x in p["layers"]}["memory"] == "off"
    assert "# Your memory" not in p["text"]


def test_the_name_owner_is_taken(client):
    r = client.post("/api/agents", json={"name": "owner"})
    assert r.status_code == 422
