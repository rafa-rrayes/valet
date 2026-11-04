"""DELETE /api/agents/{name}: the agent stops, its runner forgets it, its folder leaves the definitions repo, and
everything the control plane kept under its name goes, so an agent made again with that name starts clean."""
import subprocess
import time

import pytest

from valet.control.__main__ import revoke_runner
from valet.control.removal import TABLES
from valet.shared.definition import DefinitionError

HOST = "name: {name}\nharness: claude\nmodel: m1\ncomputer:\n  level: host\n"
BOX = "name: {name}\nharness: claude\nmodel: m1\ncomputer:\n  level: container\n"
FORGET = ["terminal", "files", "volumes", "forget"]


def _control(client):
    return client.app.state.control


def _create(client, name, yaml=HOST):
    r = client.post("/api/agents", json={"name": name, "agent_yaml": yaml.format(name=name)})
    assert r.status_code == 201, r.text


def _running(client, rpc, runner, name, yaml=HOST):
    _create(client, name, yaml)
    r, _ = rpc(runner, client.post, f"/api/agents/{name}/start", expect="agent.start")
    assert r.status_code == 200, r.text
    runner.status(name, "running", capabilities={"loadSession": True})
    _wait(lambda: client.get(f"/api/agents/{name}").json()["status"] == "idle")


def _wait(predicate, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not reached")


def _seed(c, name):
    """One row of everything the control plane keeps per agent, an attachment's file, and an audit row."""
    db = c.db
    db.execute("INSERT OR IGNORE INTO agents (name, updated_at) VALUES (?, 1)", (name,))
    db.execute("UPDATE agents SET token_hash = ? WHERE name = ?", ("hash-" + name, name))
    db.execute("INSERT INTO sessions (agent, id, created_at, updated_at) VALUES (?, 'S9', 1, 1)", (name,))
    db.execute("INSERT INTO turns (id, agent, session_id, state, started_at) VALUES (?, ?, 'S9', 'done', 1)",
               (name + "-t", name))
    db.execute("INSERT INTO events (agent, session_id, turn_id, kind, data, ts) "
               "VALUES (?, 'S9', ?, 'agent_message', '{}', 1)", (name, name + "-t"))
    db.execute("INSERT INTO inbox (id, kind, agent, title, payload, created_at) "
               "VALUES (?, 'message', ?, 'hi', '{}', 1)",
               (name + "-i", name))
    db.execute("INSERT INTO prompt_queue (agent, session_id, pos, text, source, created_at) "
               "VALUES (?, 'S9', 1, 'later', 'owner', 1)", (name,))
    db.execute("INSERT INTO trigger_runs (agent, trigger, source, fired_at, outcome) "
               "VALUES (?, 'schedule:0 8 * * *', 'schedule', 1, 'started')", (name,))
    db.execute("INSERT INTO trigger_state (agent, trigger, last) VALUES (?, 'schedule:0 8 * * *', 1)", (name,))
    db.execute("INSERT INTO webhook_keys (agent, name, key, created_at) VALUES (?, 'gh', x'00', 1)", (name,))
    db.execute("INSERT INTO wakeups (agent, prompt, due_at, created_at) VALUES (?, 'wake', 4102444800, 1)", (name,))
    db.execute("INSERT INTO tool_grants (agent, server, tool, expires_at, created_at) "
               "VALUES (?, 'github', 'push', 4102444800, 1)", (name,))
    db.execute("INSERT INTO browser_grants (agent, profile, host, kind, expires_at, created_at) "
               "VALUES (?, 'main', 'x.org', 'read', 4102444800, 1)", (name,))
    db.execute("INSERT INTO attachments (id, agent, kind, name, mime, size, created_at) "
               "VALUES (?, ?, 'image', 'a.png', 'image/png', 3, 1)", (name + "-a", name))
    c.attachments.path(name + "-a").write_bytes(b"png")
    db.execute("INSERT INTO tool_calls (agent, server, tool, outcome, started_at) "
               "VALUES (?, 'github', 'push', 'ok', 1)",
               (name,))


def _counts(c, name):
    out = {t: c.db.one(f"SELECT COUNT(*) AS n FROM {t} WHERE agent = ?", (name,))["n"] for t in TABLES}
    out["agents"] = c.db.one("SELECT COUNT(*) AS n FROM agents WHERE name = ?", (name,))["n"]
    return out


def _git(cfg, *args):
    return subprocess.run(["git", "-C", str(cfg.definitions), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def test_delete_a_stopped_agent_forgets_everything_of_it(client, cfg):
    c = _control(client)
    _create(client, "helper")
    _create(client, "other")
    for name in ("helper", "other"):
        _seed(c, name)
    assert set(_counts(c, "helper").values()) == {1}

    with client.websocket_connect("/ws/client") as live:
        live.receive_json()
        r = client.delete("/api/agents/helper")
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True, "volumes": [], "kept": None}       # never placed: no runner to tell
        assert live.receive_json() == {"type": "agent_deleted", "agent": "helper"}

    assert set(_counts(c, "helper").values()) == {0}
    assert set(_counts(c, "other").values()) == {1}                         # another agent's stay
    assert not c.attachments.path("helper-a").exists() and c.attachments.path("other-a").exists()
    assert c.db.one("SELECT COUNT(*) AS n FROM tool_calls WHERE agent = 'helper'")["n"] == 1    # the audit stays
    assert client.get("/api/agents/helper").status_code == 404
    assert [a["name"] for a in client.get("/api/agents").json()] == ["other"]
    assert not (cfg.definitions / "agents" / "helper").exists()
    assert _git(cfg, "log", "-1", "--format=%an <%ae>|%s") == "Valet <valet@localhost>|Delete agent helper"
    assert _git(cfg, "status", "--porcelain") == ""
    assert "agent.yaml" in _git(cfg, "show", "HEAD~1:agents/helper/")      # the history keeps it
    assert client.delete("/api/agents/helper").status_code == 404


def test_refusals(client, cfg):
    assert client.delete("/api/agents/nobody").status_code == 404
    assert client.delete("/api/agents/Not_A_Name").status_code == 404
    r = client.delete("/api/agents/owner")
    assert r.status_code == 422 and "stands for you" in r.json()["detail"]
    # even a folder someone made by hand under that name: `owner` is never an agent's, so nothing of it is touched
    (cfg.definitions / "agents" / "owner").mkdir(parents=True)
    (cfg.definitions / "agents" / "owner" / "agent.yaml").write_text("name: owner\n")
    assert client.delete("/api/agents/owner").status_code == 422
    assert (cfg.definitions / "agents" / "owner" / "agent.yaml").exists()
    client.cookies.clear()
    assert client.delete("/api/agents/nobody").status_code == 401


def test_a_folder_git_never_had_goes_too(client, cfg):
    d = cfg.definitions / "agents" / "byhand"
    d.mkdir(parents=True)
    (d / "agent.yaml").write_text(HOST.format(name="byhand"))
    assert client.delete("/api/agents/byhand").status_code == 200
    assert not d.exists()


def test_delete_a_running_agent(client, connect, rpc, pool):
    with client.websocket_connect("/ws/client") as live, connect(features=FORGET) as runner:
        live.receive_json()
        _running(client, rpc, runner, "helper")
        r, _ = rpc(runner, client.post, "/api/agents/helper/sessions", expect="session.new",
                   result={"session_id": "S1"})
        assert r.status_code == 201
        r, _ = rpc(runner, client.post, "/api/agents/helper/sessions/S1/prompt", json={"text": "go"},
                   expect="session.prompt")
        turn = r.json()["turn_id"]
        runner.send(type="permission.request", agent="helper", session_id="S1", turn_id=turn, request_id="R1",
                    tool_call={"title": "rm"}, options=[])
        _wait(lambda: client.get("/api/inbox").json())

        fut = pool.submit(client.delete, "/api/agents/helper")
        cmd = runner.recv("agent.forget")
        assert cmd["agent"] == "helper" and cmd["volumes"] is False
        runner.status("helper", "stopped")          # a runner reports the stop before it replies
        runner.reply(cmd, {"volumes": [], "kept": "/runner/agents/helper/work"})
        r = fut.result(timeout=10)
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True, "volumes": [], "kept": "/runner/agents/helper/work"}

        frames = []
        while not frames or frames[-1]["type"] != "agent_deleted":
            frames.append(live.receive_json())
        assert frames[-1] == {"type": "agent_deleted", "agent": "helper"}
        ends = [f["event"] for f in frames if f["type"] == "event" and f["event"]["kind"] == "turn_end"]
        assert [e["data"]["error"] for e in ends] == ["interrupted"]          # its turn ended cleanly
        assert any(f["type"] == "inbox" and f["item"]["id"] == "R1" and f["item"]["status"] == "expired"
                   for f in frames)

        # The runner speaking of it afterwards changes nothing, and no frame brings it back to the lists.
        runner.status("helper", "running")
        _create(client, "other")
        while (f := live.receive_json())["type"] != "agent" or f["agent"]["name"] != "other":
            assert not (f["type"] == "agent" and f["agent"]["name"] == "helper"), f
        assert [a["name"] for a in client.get("/api/agents").json()] == ["other"]
        assert client.get("/api/inbox?status=all").json() == []
        assert set(_counts(_control(client), "helper").values()) == {0}


def test_a_container_agent_and_its_volumes(client, connect, rpc, pool):
    with connect(features=FORGET) as runner:
        _running(client, rpc, runner, "box", BOX)
        r, cmd = rpc(runner, client.delete, "/api/agents/box?volumes=1", expect="agent.forget",
                     result={"volumes": ["valet-agent-box-home", "valet-agent-box-work"], "kept": None})
        assert cmd["volumes"] is True
        assert r.status_code == 200 and r.json()["volumes"] == ["valet-agent-box-home", "valet-agent-box-work"]

        _running(client, rpc, runner, "box2", BOX)
        client.post("/api/agents/box2/stop")        # stopped: agent.forget still goes (its container, its files)
        runner.recv("agent.stop")
        r, cmd = rpc(runner, client.delete, "/api/agents/box2", expect="agent.forget")
        assert r.status_code == 200 and cmd["volumes"] is False


def test_while_it_is_being_deleted_nothing_starts_it(client, connect, rpc, pool):
    """Between agent.forget and its folder going, a Start would run it again on the runner, an orphan nothing knows
    of: refused, as is a second delete."""
    with connect(features=FORGET) as runner:
        _running(client, rpc, runner, "helper")
        fut = pool.submit(client.delete, "/api/agents/helper")
        cmd = runner.recv("agent.forget")
        r = client.post("/api/agents/helper/start")
        assert r.status_code == 409 and "being deleted" in r.json()["detail"]
        assert client.delete("/api/agents/helper").status_code == 409
        c = _control(client)
        assert c.hub.deleting == {"helper"}
        # A start already under way when the delete began (a reconcile's, a Start past its check) isn't sent either.
        with pytest.raises(DefinitionError, match="being deleted"):
            client.portal.call(c.hub.start_agent, "helper", "rr")
        runner.reply(cmd, {"volumes": [], "kept": None})
        assert fut.result(timeout=10).status_code == 200
        assert c.hub.deleting == set()


def test_the_runner_must_be_there_unless_its_gone_for_good(client, connect, rpc, cfg):
    with connect(features=FORGET) as runner:
        _running(client, rpc, runner, "helper")
    _wait(lambda: client.get("/api/agents/helper").json()["connected"] is False)
    r = client.delete("/api/agents/helper")
    assert r.status_code == 503 and "runner rr is not connected" in r.json()["detail"]
    assert client.get("/api/agents/helper").status_code == 200          # nothing deleted

    revoke_runner(cfg, "rr")                    # it won't come back: there's nothing to reach
    assert client.delete("/api/agents/helper").status_code == 200
    assert client.get("/api/agents/helper").status_code == 404


def test_an_old_runner_and_a_runner_error(client, connect, rpc):
    with connect() as runner:                   # no features: from before agent.forget
        _running(client, rpc, runner, "helper")
        r = client.delete("/api/agents/helper")
        assert r.status_code == 501 and "update it" in r.json()["detail"]
    with connect(features=FORGET, agents=[{"agent": "helper", "state": "running"}]) as runner:
        r, _ = rpc(runner, client.delete, "/api/agents/helper", expect="agent.forget",
                   error="can't remove its files on the runner: busy")
        assert r.status_code == 502 and "can't remove its files" in r.json()["detail"]
        a = client.get("/api/agents/helper").json()         # still there, and stopped for good
        assert a["desired"] == "stopped"


def test_an_agent_made_again_with_the_name_starts_clean(client, connect, rpc, cfg):
    c = _control(client)
    with connect(features=FORGET) as runner:
        _running(client, rpc, runner, "helper")
        _seed(c, "helper")
        assert client.put("/api/agents/helper/memory/notes", json={"text": "remember the milk"}).status_code == 200
        assert (cfg.definitions / "agents" / "helper" / "memory").is_dir()
        rpc(runner, client.delete, "/api/agents/helper", expect="agent.forget")

        yaml = HOST + "triggers:\n  - schedule: '0 8 * * *'\n    prompt: hi\n  - webhook: gh\n    prompt: hi\n"
        _create(client, "helper", yaml)
        a = client.get("/api/agents/helper").json()
        assert a["state"] == "stopped" and a["desired"] == "stopped" and a["runner"] is None
        assert client.get("/api/agents/helper/sessions").json() == []
        assert client.get("/api/inbox?status=all").json() == []
        assert client.get("/api/agents/helper/trigger-runs").json()["runs"] == []
        assert client.get("/api/agents/helper/wakeups?status=all").json()["wakeups"] == []
        assert client.get("/api/gateway/grants?agent=helper").json() == []
        assert not (cfg.definitions / "agents" / "helper" / "memory").exists()
        hooks = [t for t in client.get("/api/agents/helper/triggers").json()["triggers"] if t.get("webhook")]
        assert hooks[0]["key"] and hooks[0]["key_error"] is None and hooks[0]["key_created_at"] > 1    # a new key
        counts = _counts(c, "helper")
        assert counts["sessions"] == counts["inbox"] == counts["turns"] == counts["events"] == 0


def test_its_shared_items_and_their_files_go_with_it(client):
    c = _control(client)
    _create(client, "helper")
    _create(client, "other")
    note = client.post("/api/agents/helper/shared/notes", json={"text": "for helper"}).json()
    [f] = client.post("/api/agents/helper/shared/files", files=[("file", ("a.txt", b"a", "text/plain"))]).json()
    keep = client.post("/api/agents/other/shared/notes", json={"text": "for other"}).json()
    folder = c.shared.root / str(f["id"])
    assert folder.is_dir()

    assert client.delete("/api/agents/helper").status_code == 200
    assert c.db.one("SELECT COUNT(*) AS n FROM shared WHERE agent = 'helper'")["n"] == 0
    assert not folder.exists()
    for item in (note, f):
        assert client.get(f"/api/shared/{item['id']}").status_code == 404
    assert client.get(f"/api/shared/{keep['id']}").status_code == 200            # another agent's stay
    assert [i["id"] for i in client.get("/api/shared").json()["items"]] == [keep["id"]]

    _create(client, "helper")                                                     # made again: an empty space
    assert client.get("/api/agents/helper/shared").json()["items"] == []
