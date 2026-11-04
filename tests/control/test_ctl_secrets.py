"""Secrets: the encrypted store, the name rule, the API (names only), and starting agents with them, with the test
playing the runner. VALUE is a stand-in secret; every test checks it shows up nowhere it shouldn't."""
import io
import json
import stat
import time

import pytest

from valet.control import views
from valet.control.__main__ import delete_secret, main, set_secret
from valet.control.app import open_database
from valet.control.secrets import KEY_BYTES, SecretError, SecretStore, _create_key, load_key
from valet.shared.config import ControlConfig
from valet.shared.definition import AgentDefinition, secret_name_error

VALUE = "ghp_TEST_not-a-real-token_0123456789"
YAML = "name: {name}\ncomputer:\n  level: host\nsecrets: [{secrets}]\n"


def agent(**kw) -> AgentDefinition:
    return AgentDefinition.model_validate({"name": "scout", **kw})


def raw_files(cfg: ControlConfig) -> bytes:
    """Every byte the database keeps on disk: the file and its WAL."""
    return b"".join(p.read_bytes() for p in cfg.data.glob("valet.db*"))


# ---------- the store ----------
def test_round_trip_and_the_database_never_holds_plaintext(cfg):
    db = open_database(cfg)
    store = SecretStore(db, cfg)
    at = store.set("GITHUB_TOKEN", VALUE)
    store.set("NPM_TOKEN", "npm_" + VALUE)
    values, stamps = store.resolve(agent(secrets=["GITHUB_TOKEN", "NPM_TOKEN"]))
    assert values == {"GITHUB_TOKEN": VALUE, "NPM_TOKEN": "npm_" + VALUE}
    assert stamps["GITHUB_TOKEN"] == at
    assert store.resolve(agent()) == ({}, {})
    assert [r["name"] for r in store.listing()] == ["GITHUB_TOKEN", "NPM_TOKEN"]
    assert VALUE.encode() not in raw_files(cfg)
    row = db.one("SELECT value FROM secrets WHERE name = 'GITHUB_TOKEN'")
    assert len(row["value"]) == 12 + len(VALUE) + 16           # nonce + ciphertext + tag

    # the key file: created on first use, 32 bytes, owner-only; a new store (the next start) reads the same one
    key = cfg.data / "master.key"
    assert len(key.read_bytes()) == KEY_BYTES and stat.S_IMODE(key.stat().st_mode) == 0o600
    assert SecretStore(db, cfg).resolve(agent(secrets=["GITHUB_TOKEN"]))[0] == {"GITHUB_TOKEN": VALUE}

    # replacing a value moves updated_at; deleting forgets it
    time.sleep(0.01)
    assert store.set("GITHUB_TOKEN", "new " + VALUE) > at
    assert store.stamps(["GITHUB_TOKEN", "ABSENT"])["ABSENT"] is None
    assert store.delete("NPM_TOKEN") and not store.delete("NPM_TOKEN")
    db.close()


def test_a_value_is_bound_to_its_name_and_key(cfg):
    db = open_database(cfg)
    store = SecretStore(db, cfg)
    store.set("A_TOKEN", VALUE)
    store.set("B_TOKEN", "something else")
    blob = db.one("SELECT value FROM secrets WHERE name = 'A_TOKEN'")["value"]
    db.execute("UPDATE secrets SET value = ? WHERE name = 'B_TOKEN'", (blob,))    # moved to another name
    with pytest.raises(SecretError, match="secret B_TOKEN doesn't decrypt") as e:
        store.resolve(agent(secrets=["B_TOKEN"]))
    assert VALUE not in str(e.value)
    other = ControlConfig(data=cfg.data, definitions=cfg.definitions, master_key=b"k" * 32)
    with pytest.raises(SecretError, match="A_TOKEN doesn't decrypt with this master key"):
        SecretStore(db, other).resolve(agent(secrets=["A_TOKEN"]))
    db.close()


def test_resolve_names_what_is_missing(cfg):
    db = open_database(cfg)
    store = SecretStore(db, cfg)
    store.set("GITHUB_TOKEN", VALUE)
    with pytest.raises(SecretError) as e:
        store.resolve(agent(secrets=["GITHUB_TOKEN", "NPM_TOKEN"]))
    assert str(e.value) == "secret NPM_TOKEN isn't set: add it to Valet's secrets" and e.value.missing == ["NPM_TOKEN"]
    with pytest.raises(SecretError) as e:
        store.resolve(agent(secrets=["A_KEY", "GITHUB_TOKEN", "B_KEY", {"vault": "aws/alice"}]))
    assert str(e.value) == ("secrets A_KEY, B_KEY aren't set: add them to Valet's secrets; "
                            "vault entry aws/alice doesn't exist: add it to Valet's vault")
    assert e.value.missing == ["A_KEY", "B_KEY", "vault:aws/alice"]
    with pytest.raises(SecretError, match="vault entry aws/alice"):
        store.resolve(agent(secrets=[{"vault": "aws/alice", "env": "AWS_KEY"}]))
    db.close()


def test_key_from_the_environment_or_a_file(tmp_path):
    key = bytes(range(32))
    cfg = ControlConfig.from_env({"VALET_DATA": str(tmp_path / "d"), "VALET_MASTER_KEY": key.hex()})
    assert cfg.master_key == key and "master_key" not in repr(cfg) and key.hex() not in repr(cfg)
    assert load_key(cfg) == key and not (tmp_path / "d" / "master.key").exists()
    for bad in ("abc", "zz" * 32, "00" * 16):
        with pytest.raises(ValueError, match="VALET_MASTER_KEY must be 64 hex characters"):
            ControlConfig.from_env({"VALET_MASTER_KEY": bad})

    cfg = ControlConfig.from_env({"VALET_DATA": str(tmp_path / "d"), "VALET_KEY_FILE": str(tmp_path / "k" / "key")})
    first = load_key(cfg)
    assert (tmp_path / "k" / "key").read_bytes() == first and len(first) == 32
    _create_key(tmp_path / "k" / "key")             # someone else got there first: theirs stays
    assert load_key(cfg) == first
    assert [p.name for p in (tmp_path / "k").iterdir()] == ["key"]     # no temp file left behind
    (tmp_path / "k" / "key").write_bytes(b"short")
    with pytest.raises(SecretError, match="isn't a 32-byte key"):
        load_key(cfg)


def test_cli_reads_the_value_from_stdin_never_argv(cfg, monkeypatch, capsys):
    monkeypatch.setenv("VALET_DATA", str(cfg.data))
    monkeypatch.delenv("VALET_MASTER_KEY", raising=False)
    monkeypatch.delenv("VALET_KEY_FILE", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO(VALUE + "\n"))
    main(["set-secret", "GITHUB_TOKEN"])
    assert VALUE not in capsys.readouterr().out
    db = open_database(cfg)
    assert SecretStore(db, cfg).resolve(agent(secrets=["GITHUB_TOKEN"]))[0] == {"GITHUB_TOKEN": VALUE}
    with pytest.raises(ValueError, match="reserved"):
        set_secret(cfg, "HOME", VALUE)
    delete_secret(cfg, "GITHUB_TOKEN")
    with pytest.raises(KeyError):
        delete_secret(cfg, "GITHUB_TOKEN")
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    with pytest.raises(SystemExit, match="empty"):
        main(["set-secret", "GITHUB_TOKEN"])
    db.close()


# ---------- the name rule ----------
@pytest.mark.parametrize("name", ["GITHUB_TOKEN", "A", "AWS_SECRET_ACCESS_KEY", "OPENAI_API_KEY", "X1_" + "Y" * 61])
def test_good_names(name):
    assert secret_name_error(name) is None


@pytest.mark.parametrize("name,why", [
    ("github_token", "env var name"), ("1TOKEN", "env var name"), ("_TOKEN", "env var name"),
    ("GH-TOKEN", "env var name"), ("A" * 65, "env var name"), ("", "env var name"), ("A B", "env var name"),
    ("HOME", "reserved"), ("PATH", "reserved"), ("NODE_OPTIONS", "reserved"), ("HTTPS_PROXY", "reserved"),
    ("CLAUDE_CODE_OAUTH_TOKEN", "reserved"), ("ANTHROPIC_API_KEY", "reserved"), ("ANTHROPIC_MODEL", "reserved"),
    ("CLAUDE_AGENT_ACP_PROTOCOL", "reserved"), ("DEEPSEEK_API_KEY", "reserved"), ("DSH_AGENTS_HOME", "reserved"),
    ("DOCKER_HOST", "reserved"), ("VALET_DATA", "reserved"), ("LD_PRELOAD", "reserved"),
])
def test_bad_names(name, why):
    assert why in secret_name_error(name)


def test_definition_checks_secret_names():
    assert agent(secrets=["GITHUB_TOKEN", {"vault": "aws/alice", "env": "AWS_KEY"}]).secret_names() == ["GITHUB_TOKEN"]
    for secrets, msg in [(["HOME"], "HOME: reserved"), (["lower"], "lower: an env var name"),
                         (["A_KEY", "A_KEY"], "A_KEY is listed twice"),
                         (["A_KEY", {"vault": "x", "env": "A_KEY"}], "A_KEY is listed twice"),
                         ([{"vault": "x", "env": "PATH"}], "reserved")]:
        with pytest.raises(ValueError, match=msg):
            agent(secrets=secrets)


def test_fingerprint_without_secrets_is_unchanged():
    """Agents running before secrets existed don't all turn restart_needed after the upgrade."""
    import hashlib
    d, files = agent(), {"persona.md": "x"}
    # M2's fingerprint: tools.rules, browser.rules and computer.workdir came after, and their defaults (workdir
    # unset) are left out of it.
    before = d.model_dump(mode="json", exclude={"display_name": True, "emoji": True, "tools": {"rules"},
                                                "browser": {"rules"}, "computer": {"workdir"}})
    old = hashlib.sha256(json.dumps({"definition": before, "files": files}, sort_keys=True).encode()).hexdigest()[:16]
    assert views.fingerprint(d, files) == views.fingerprint(d, files, {}) == old
    assert views.fingerprint(d, files, {"A": 1.0}) != views.fingerprint(d, files, {"A": 2.0})
    assert views.fingerprint(d, files, {"A": 1.0}) != views.fingerprint(d, files, {"A": None})


# ---------- the API ----------
def _no_value(client, *texts):
    for path in ("/api/agents", "/api/agents/scout", "/api/secrets", "/api/runners", "/api/inbox?status=all"):
        r = client.get(path)
        assert VALUE not in r.text, path
    with client.websocket_connect("/ws/client") as ws:
        assert VALUE not in json.dumps(ws.receive_json())
    for t in texts:
        assert VALUE not in t


def test_secrets_api(client, cfg):
    assert client.get("/api/secrets").json() == []
    r = client.put("/api/secrets/GITHUB_TOKEN", json={"value": VALUE})
    assert r.status_code == 200
    s = r.json()
    assert s == {"name": "GITHUB_TOKEN", "set": True, "updated_at": s["updated_at"], "used_by": [], "servers": []}
    assert client.get("/api/secrets").json() == [s]

    for name in ("github_token", "1X", "X-Y", "A" * 65):
        r = client.put(f"/api/secrets/{name}", json={"value": VALUE})
        assert r.status_code == 422 and "env var name" in r.json()["detail"], name
    for name in ("HOME", "PATH", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "DEEPSEEK_API_KEY", "DSH_HOME",
                 "DOCKER_HOST", "VALET_MASTER_KEY"):
        r = client.put(f"/api/secrets/{name}", json={"value": VALUE})
        assert r.status_code == 422 and r.json()["detail"] == f"{name}: reserved: Valet, a harness or the computer " \
                                                             "sets it", name
    for value, msg in [("", "empty"), ("a\0b", "NUL"), (VALUE * 1000, "longer than 32 KiB"), (123, "a string"),
                       ([VALUE], "a string")]:
        r = client.put("/api/secrets/OTHER", json={"value": value})
        assert r.status_code == 422 and msg in r.json()["detail"] and VALUE not in r.text, value
    r = client.put("/api/secrets/OTHER", content='{"value": "' + VALUE + '\\ud800"}',
                   headers={"content-type": "application/json"})
    assert r.status_code == 422 and r.json()["detail"] == "the value isn't valid text" and VALUE not in r.text
    r = client.put("/api/secrets/OTHER", content='{"value": "' + VALUE, headers={"content-type": "application/json"})
    assert r.status_code == 422 and VALUE not in r.text       # broken JSON isn't echoed either
    assert client.put("/api/secrets/OTHER", json={}).status_code == 422
    assert [x["name"] for x in client.get("/api/secrets").json()] == ["GITHUB_TOKEN"]

    # used_by and missing_secrets: by name, from the valid definitions that list them
    for name, secrets in [("scout", "GITHUB_TOKEN, NPM_TOKEN"), ("helper", "GITHUB_TOKEN")]:
        r = client.post("/api/agents", json={"name": name, "agent_yaml": YAML.format(name=name, secrets=secrets)})
        assert r.status_code == 201, r.text
    client.post("/api/agents", json={"name": "plain"})
    listed = client.get("/api/secrets").json()
    assert [(x["name"], x["set"], x["used_by"]) for x in listed] == [
        ("GITHUB_TOKEN", True, ["helper", "scout"]), ("NPM_TOKEN", False, ["scout"])]
    assert listed[1]["updated_at"] is None
    assert client.get("/api/agents/scout").json()["missing_secrets"] == ["NPM_TOKEN"]
    assert client.get("/api/agents/helper").json()["missing_secrets"] == []
    assert client.get("/api/agents/plain").json()["missing_secrets"] == []

    # the settings page's save refuses a reserved name, per field
    d = client.get("/api/agents/scout").json()["definition"]
    r = client.put("/api/agents/scout/definition", json={"definition": {**d, "secrets": ["HOME"]}})
    assert r.status_code == 422 and r.json()["errors"][0]["field"] == "secrets"
    assert "HOME: reserved" in r.json()["errors"][0]["msg"]

    assert client.delete("/api/secrets/GITHUB_TOKEN").json() == {"ok": True}
    assert client.delete("/api/secrets/GITHUB_TOKEN").status_code == 404
    assert client.get("/api/agents/helper").json()["missing_secrets"] == ["GITHUB_TOKEN"]
    _no_value(client)
    assert VALUE.encode() not in raw_files(cfg)


def _start(rpc, runner, client, name="scout"):
    r, cmd = rpc(runner, client.post, f"/api/agents/{name}/start", expect="agent.start")
    assert r.status_code == 200, r.text
    runner.status(name, "starting")
    runner.status(name, "running")
    _wait(lambda: client.get(f"/api/agents/{name}").json()["state"] == "running")
    return r, cmd


def _wait(predicate, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not reached")


def test_start_sends_the_values_and_restart_tracks_them(client, connect, rpc, cfg):
    client.post("/api/agents", json={"name": "scout", "agent_yaml": YAML.format(name="scout", secrets="GITHUB_TOKEN")})
    with connect() as runner:
        # Not set: the start is refused before anything changes, naming the secret.
        r = client.post("/api/agents/scout/start")
        assert r.status_code == 422
        assert r.json() == {"detail": "secret GITHUB_TOKEN isn't set: add it to Valet's secrets",
                            "missing_secrets": ["GITHUB_TOKEN"]}
        a = client.get("/api/agents/scout").json()
        assert (a["desired"], a["state"], a["runner"]) == ("stopped", "stopped", None)

        client.put("/api/secrets/GITHUB_TOKEN", json={"value": VALUE})
        _, cmd = _start(rpc, runner, client)        # the first command the runner gets: nothing was sent before
        assert cmd["secrets"] == {"GITHUB_TOKEN": VALUE}
        assert cmd["definition"]["secrets"] == ["GITHUB_TOKEN"]
        assert VALUE not in json.dumps({k: v for k, v in cmd.items() if k != "secrets"})
        assert client.get("/api/agents/scout").json()["restart_needed"] is False

        # A new value for a secret it runs with: restart needed, without the value going anywhere.
        time.sleep(0.01)
        client.put("/api/secrets/GITHUB_TOKEN", json={"value": "rotated " + VALUE})
        a = client.get("/api/agents/scout").json()
        assert a["restart_needed"] is True and a["missing_secrets"] == []
        _, cmd = _start(rpc, runner, client)
        assert cmd["secrets"] == {"GITHUB_TOKEN": "rotated " + VALUE}
        assert client.get("/api/agents/scout").json()["restart_needed"] is False

        # Deleted while it runs: it keeps going, says so, and can't be started again until it's set.
        client.delete("/api/secrets/GITHUB_TOKEN")
        a = client.get("/api/agents/scout").json()
        assert (a["state"], a["restart_needed"], a["missing_secrets"]) == ("running", True, ["GITHUB_TOKEN"])
        assert client.post("/api/agents/scout/start").status_code == 422
        assert client.get("/api/agents/scout").json()["state"] == "running"
        _no_value(client)
    assert VALUE.encode() not in raw_files(cfg)
    assert not client.app.state.control.db.query("SELECT 1 FROM events")


def test_a_missing_vault_entry_refuses_the_start(client, connect):
    yaml = "name: scout\ncomputer:\n  level: host\nsecrets:\n  - vault: aws/alice\n"
    assert client.post("/api/agents", json={"name": "scout", "agent_yaml": yaml}).status_code == 201
    with connect():
        r = client.post("/api/agents/scout/start")
    assert r.status_code == 422 and r.json()["detail"] == "vault entry aws/alice doesn't exist: add it to Valet's vault"
    assert r.json()["missing_secrets"] == ["vault:aws/alice"]
    assert client.get("/api/agents/scout").json()["missing_secrets"] == ["vault:aws/alice"]


def test_reconnect_start_fails_on_a_missing_secret(client, connect, rpc):
    """The runner restarted and the agent should run again, but its secret was deleted meanwhile: it fails,
    naming the secret, and nothing is sent to the runner."""
    client.post("/api/agents", json={"name": "scout", "agent_yaml": YAML.format(name="scout", secrets="GITHUB_TOKEN")})
    client.put("/api/secrets/GITHUB_TOKEN", json={"value": VALUE})
    with connect() as runner:
        _start(rpc, runner, client)
    client.delete("/api/secrets/GITHUB_TOKEN")
    with connect():
        _wait(lambda: client.get("/api/agents/scout").json()["state"] == "failed")
    a = client.get("/api/agents/scout").json()
    assert a["error"] == "secret GITHUB_TOKEN isn't set: add it to Valet's secrets" and a["desired"] == "running"
    client.put("/api/secrets/GITHUB_TOKEN", json={"value": VALUE})
    with connect() as runner:       # set again: the next hello starts it
        cmd = runner.recv("agent.start")
        runner.reply(cmd)
        assert cmd["secrets"] == {"GITHUB_TOKEN": VALUE}
