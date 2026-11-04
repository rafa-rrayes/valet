"""The control plane's half of the MCP gateway, with the test playing the runner: agent.start carries the gateway
and a fresh agent token; the endpoint takes only that agent's token while it runs; a gateway inbox item nobody is
waiting on any more can't be answered."""
import json
import time

from valet.control import auth
from valet.control.db import Database

YAML = "name: {name}\nharness: claude\ncomputer:\n  level: host\n"
MCP = "tools:\n  mcp: [valet.agents]\n"
LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
ACCEPT = {"Accept": "application/json, text/event-stream"}


def start(client, rpc, runner, name="scout", mcp=True):
    """Create (or keep) the agent, start it, report it running; returns the agent.start command."""
    client.post("/api/agents", json={"name": name, "agent_yaml": YAML.format(name=name) + (MCP if mcp else "")})
    r, cmd = rpc(runner, client.post, f"/api/agents/{name}/start", expect="agent.start")
    assert r.status_code == 200, r.text
    runner.status(name, "running", capabilities={"mcpCapabilities": {"http": True}})
    return cmd


def control(client):
    return client.app.state.control


def post(client, path, token=None):
    headers = dict(ACCEPT, **({"Authorization": f"Bearer {token}"} if token else {}))
    return client.post(path, json=LIST, headers=headers)


def test_agent_start_carries_the_gateway(client, connect, rpc, cfg):
    with connect() as runner:
        cmd = start(client, rpc, runner)
        gw = cmd["gateway"]
        assert gw["url"] == f"{cfg.public_url}/mcp/agents/scout"
        assert gw["token"].startswith("vagt_")
        assert gw["timeout"] == cfg.gateway_ask_timeout + cfg.gateway_call_timeout     # how long a call may be held
        db = control(client).db
        stored = db.one("SELECT token_hash FROM agents WHERE name = 'scout'")["token_hash"]
        assert stored == auth.token_hash(gw["token"])
        assert not any(gw["token"].encode() in f.read_bytes() for f in cfg.data.glob("valet.db*"))   # never stored
        assert auth.agent_for_token(db, f"Bearer {gw['token']}") == "scout"

        # A restart issues a new token; the old one is dead.
        again = start(client, rpc, runner)
        assert again["gateway"]["token"] != gw["token"]
        assert auth.agent_for_token(db, f"Bearer {gw['token']}") is None

        # Without MCP servers: no gateway, no token.
        client.put("/api/agents/scout/files/agent.yaml", json={"text": YAML.format(name="scout")})
        plain = start(client, rpc, runner)
        assert plain["gateway"] is None
        assert db.one("SELECT token_hash FROM agents WHERE name = 'scout'")["token_hash"] is None


def test_gateway_url_setting(tmp_path):
    from valet.shared.config import ControlConfig
    env = {"VALET_DATA": str(tmp_path), "VALET_PUBLIC_URL": "https://valet.example.com/",
           "VALET_GATEWAY_URL": "http://host.docker.internal:8780/"}
    c = ControlConfig.from_env(env)
    assert c.gateway_url == "http://host.docker.internal:8780" and c.public_url == "https://valet.example.com"
    assert ControlConfig.from_env({"VALET_DATA": str(tmp_path)}).gateway_url == "http://127.0.0.1:8780"


def test_endpoint_auth(client, connect, rpc):
    with connect() as runner:
        token = start(client, rpc, runner)["gateway"]["token"]
        other = start(client, rpc, runner, name="other")["gateway"]["token"]
        assert post(client, "/mcp/agents/scout", token).status_code == 200
        r = post(client, "/mcp/agents/scout")
        assert r.status_code == 401 and r.headers["www-authenticate"] == 'Bearer realm="valet"'
        assert post(client, "/mcp/agents/scout", "vagt_nope").status_code == 401
        assert post(client, "/mcp/agents/scout", other).status_code == 403
        assert post(client, "/mcp/agents/other", token).status_code == 403
        assert post(client, "/mcp/agents/Scout", token).status_code == 404
        assert post(client, "/mcp/whatever", token).status_code == 404
        # The owner's session cookie isn't an agent token.
        assert client.post("/mcp/agents/scout", json=LIST, headers=ACCEPT).status_code == 401

        # Only while it runs.
        runner.status("scout", "failed", error="boom")
        r = post(client, "/mcp/agents/scout", token)
        assert r.status_code == 403 and "isn't running" in r.json()["detail"]
        runner.status("scout", "running")
        assert post(client, "/mcp/agents/scout", token).status_code == 200

        # Stopping revokes it.
        r, _ = rpc(runner, client.post, "/api/agents/scout/stop", expect="agent.stop")
        assert r.status_code == 200
        assert post(client, "/mcp/agents/scout", token).status_code == 401


def test_listing_through_the_endpoint(client, connect, rpc):
    with connect() as runner:
        token = start(client, rpc, runner)["gateway"]["token"]
        r = post(client, "/mcp/agents/scout", token)
        body = r.json() if r.headers["content-type"].startswith("application/json") else _sse(r.text)
        names = [t["name"] for t in body["result"]["tools"]]
        assert names == ["valet_agents__list_agents", "valet_agents__agent_status", "valet_agents__send_message"]


def _sse(text: str) -> dict:
    return json.loads(next(line[5:] for line in text.splitlines() if line.startswith("data:")))


def test_answer_with_nobody_waiting_is_gone(client, connect, rpc):
    """A gateway item still pending but with no call waiting on it (it can only be left so by a crash between the
    two): answering it expires it and says so."""
    with connect() as runner:
        start(client, rpc, runner)
        db: Database = control(client).db
        db.execute("INSERT INTO inbox (id, kind, agent, runner, title, payload, created_at) "
                   "VALUES ('g1', 'tool', 'scout', NULL, 'valet.agents: x', ?, ?)",
                   (json.dumps({"tool_call": {}, "options": [{"optionId": "allow_once", "kind": "allow_once",
                                                              "name": "Allow once"}],
                                "tool": {"server": "valet.agents", "name": "x"}}), time.time()))
        r = client.post("/api/inbox/g1/answer", json={"option_id": "allow_once"})
        assert r.status_code == 410 and r.json()["item"]["status"] == "expired"
        assert r.json()["item"]["tool"] == {"server": "valet.agents", "name": "x"}
        assert client.post("/api/inbox/g1/answer", json={"option_id": "allow_once"}).status_code == 409


def test_old_inbox_table_is_migrated(tmp_path):
    """Databases from before the gateway had inbox.runner NOT NULL; opening one relaxes it and keeps the rows."""
    import sqlite3
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE inbox (id TEXT PRIMARY KEY, kind TEXT NOT NULL, agent TEXT NOT NULL, session_id TEXT,
            turn_id TEXT, runner TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', title TEXT NOT NULL,
            payload TEXT NOT NULL, answer TEXT, created_at REAL NOT NULL, answered_at REAL);
        INSERT INTO inbox (id, kind, agent, runner, title, payload, created_at)
            VALUES ('p1', 'permission', 'scout', 'rr', 't', '{}', 1.0);
    """)
    con.commit()
    con.close()
    db = Database(path)
    try:
        assert db.one("SELECT runner FROM inbox WHERE id = 'p1'")["runner"] == "rr"
        db.execute("INSERT INTO inbox (id, kind, agent, runner, title, payload, created_at) "
                   "VALUES ('g1', 'tool', 'scout', NULL, 't', '{}', 2.0)")
        assert db.one("SELECT 1 FROM tool_calls") is None
    finally:
        db.close()


# ---------- secrets and the gateway in one start ----------

BOTH = "name: scout\nharness: claude\ncomputer:\n  level: host\nsecrets: [GITHUB_TOKEN]\ntools:\n  mcp: [valet.agents]\n"
VALUE = "ghp_TEST_both_0123456789"


def _wait(fn, timeout=5.0):
    end = time.monotonic() + timeout
    while not fn():
        assert time.monotonic() < end, "timed out"
        time.sleep(0.02)


def test_start_carries_secrets_and_the_gateway_and_records_what_it_started_with(client, connect, rpc):
    """agent.start has both; `started` is recorded once the runner took it, and its fingerprint covers the secrets'
    updated_at and the gateway's endpoint, never a value or the token."""
    from dataclasses import replace

    from valet.control import views
    c = control(client)
    client.post("/api/agents", json={"name": "scout", "agent_yaml": BOTH})
    r = client.post("/api/agents/scout/start")
    assert r.status_code == 422 and r.json()["missing_secrets"] == ["GITHUB_TOKEN"]
    assert c.db.one("SELECT token_hash FROM agents WHERE name = 'scout' AND token_hash IS NOT NULL") is None
    assert client.put("/api/secrets/GITHUB_TOKEN", json={"value": VALUE}).status_code == 200
    with connect() as runner:
        r, cmd = rpc(runner, client.post, "/api/agents/scout/start", expect="agent.start")
        assert r.status_code == 200, r.text
        assert cmd["secrets"] == {"GITHUB_TOKEN": VALUE} and cmd["gateway"]["token"].startswith("vagt_")
        d, files = c.defs.load("scout")
        stamps = c.secrets.stamps(["GITHUB_TOKEN"])
        started = c.db.one("SELECT started FROM agents WHERE name = 'scout'")["started"]
        assert started == views.fingerprint(d, files, stamps, cmd["gateway"]["url"])
        assert started != views.fingerprint(d, files, stamps)       # the endpoint counts
        runner.status("scout", "running", capabilities={"mcpCapabilities": {"http": True}})
        _wait(lambda: client.get("/api/agents/scout").json()["state"] == "running")
        assert client.get("/api/agents/scout").json()["restart_needed"] is False

        # The secret set again: restart to apply. The gateway's endpoint moved: likewise.
        time.sleep(0.01)
        client.put("/api/secrets/GITHUB_TOKEN", json={"value": VALUE + "2"})
        assert client.get("/api/agents/scout").json()["restart_needed"] is True
        rpc(runner, client.post, "/api/agents/scout/start", expect="agent.start")
        assert client.get("/api/agents/scout").json()["restart_needed"] is False
        old_cfg = c.cfg
        c.cfg = replace(old_cfg, gateway_url="http://host.docker.internal:9999")
        try:
            assert client.get("/api/agents/scout").json()["restart_needed"] is True
        finally:
            c.cfg = old_cfg
        assert client.get("/api/agents/scout").json()["restart_needed"] is False


def test_a_start_the_runner_refused_keeps_the_running_agents_token(client, connect, rpc):
    """The token is issued before agent.start goes out; a start the runner surely didn't take (it said no, or it
    wasn't connected) puts the old one back, so the process still running there keeps the gateway, and `started`
    keeps the old fingerprint."""
    c = control(client)
    with connect() as runner:
        token = start(client, rpc, runner)["gateway"]["token"]
        before = c.db.one("SELECT started FROM agents WHERE name = 'scout'")["started"]
        client.put("/api/agents/scout/files/persona.md", json={"text": "You are Scout, changed."})
        r, cmd = rpc(runner, client.post, "/api/agents/scout/start", expect="agent.start", error="bad command")
        assert r.status_code == 502
        assert auth.agent_for_token(c.db, f"Bearer {token}") == "scout"
        assert auth.agent_for_token(c.db, f"Bearer {cmd['gateway']['token']}") is None
        assert c.db.one("SELECT started FROM agents WHERE name = 'scout'")["started"] == before
        assert client.get("/api/agents/scout").json()["restart_needed"] is True
        assert post(client, "/mcp/agents/scout", token).status_code == 200
