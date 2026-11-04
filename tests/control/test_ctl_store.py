"""Database, auth primitives, definitions repo and the CLI helpers, without the app."""
import sqlite3
import stat
import subprocess

import pytest

from valet.control import auth
from valet.control.__main__ import add_runner, revoke_runner, set_password
from valet.control.app import open_database
from valet.control.db import Database
from valet.control.definitions import DefinitionsRepo
from valet.shared.definition import DefinitionError


def test_database(tmp_path):
    db = Database(tmp_path / "v.db")
    assert stat.S_IMODE((tmp_path / "v.db").stat().st_mode) == 0o600
    assert db.get_setting("x") is None
    db.set_setting("x", "1")
    db.set_setting("x", "2")
    assert db.get_setting("x") == "2"
    assert db.one("SELECT value FROM settings WHERE key = 'x'") == {"value": "2"}
    assert db.one("SELECT 1 FROM runners") is None
    assert {r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type = 'table'")} >= {
        "settings", "runners", "agents", "sessions", "turns", "events", "inbox"}
    db.close()
    Database(tmp_path / "v.db").close()     # reopening runs the schema again harmlessly


# The tables M1 (the last release before the settings page and M2) created that have changed since, as it wrote them.
M1_SCHEMA = """
CREATE TABLE agents (
    name        TEXT PRIMARY KEY,
    runner      TEXT,
    desired     TEXT NOT NULL DEFAULT 'stopped',
    state       TEXT NOT NULL DEFAULT 'stopped',
    error       TEXT,
    capabilities TEXT,
    agent_info  TEXT,
    updated_at  REAL NOT NULL
);
CREATE TABLE inbox (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    agent       TEXT NOT NULL,
    session_id  TEXT,
    turn_id     TEXT,
    runner      TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    title       TEXT NOT NULL,
    payload     TEXT NOT NULL,
    answer      TEXT,
    created_at  REAL NOT NULL,
    answered_at REAL
);
CREATE INDEX inbox_status ON inbox (status, created_at);
INSERT INTO agents (name, runner, state, updated_at) VALUES ('scout', 'rr', 'running', 1);
INSERT INTO inbox (id, kind, agent, session_id, turn_id, runner, title, payload, answer, created_at, answered_at)
    VALUES ('p1', 'permission', 'scout', 's1', 't1', 'rr', 'Write x', '{"options": []}', NULL, 2.0, NULL);
"""


def m1_database(path):
    old = sqlite3.connect(path)
    old.execute("PRAGMA journal_mode=WAL")      # as M1's Database left every file
    old.executescript(M1_SCHEMA)
    old.commit()
    old.close()


def test_database_migrates_an_m1_file(tmp_path):
    """A database M1 made: CREATE TABLE IF NOT EXISTS leaves its tables alone, so opening it adds agents.started
    and agents.token_hash, relaxes inbox.runner (NOT NULL then; the gateway's items have none) by copying the table,
    and creates the new tables, keeping every row; the next open finds it all there."""
    path = tmp_path / "v.db"
    m1_database(path)
    db = Database(path)
    assert db.one("SELECT * FROM agents") == {"name": "scout", "runner": "rr", "desired": "stopped",
                                              "state": "running", "error": None, "capabilities": None,
                                              "agent_info": None, "updated_at": 1.0, "started": None,
                                              "token_hash": None, "main_session": None}
    turns = {r["name"]: r["dflt_value"] for r in db.query("PRAGMA table_info(turns)")}
    assert turns["source"] == "'owner'" and {"source_label", "prompt", "retry_of"} <= set(turns)
    assert db.one("SELECT * FROM inbox") == {"id": "p1", "kind": "permission", "agent": "scout", "session_id": "s1",
                                             "turn_id": "t1", "runner": "rr", "status": "pending",
                                             "title": "Write x", "payload": '{"options": []}', "answer": None,
                                             "created_at": 2.0, "answered_at": None}
    notnull = {r["name"]: r["notnull"] for r in db.query("PRAGMA table_info(inbox)")}
    assert notnull["runner"] == 0 and notnull["kind"] == 1
    db.execute("INSERT INTO inbox (id, kind, agent, runner, title, payload, created_at) "
               "VALUES ('g1', 'tool', 'scout', NULL, 't', '{}', 3.0)")
    names = {r["name"] for r in db.query("SELECT name FROM sqlite_master")}
    assert {"secrets", "tool_calls", "inbox_status", "tool_calls_agent"} <= names and "inbox_old" not in names
    assert not db._conn.in_transaction           # the migration's transaction is over
    db.execute("UPDATE agents SET started = 'f00d', token_hash = 'abc'")
    db.close()
    db = Database(path)
    assert db.one("SELECT started, token_hash FROM agents") == {"started": "f00d", "token_hash": "abc"}
    assert db.one("SELECT count(*) AS n FROM inbox") == {"n": 2}
    db.close()


def test_two_processes_opening_an_m1_file_at_once(tmp_path):
    """The server and the CLI may open the same old file together: one migrates under the write lock, the other
    then finds nothing left to do (a second ALTER or table copy would fail)."""
    import threading
    path = tmp_path / "v.db"
    m1_database(path)
    errors, barrier = [], threading.Barrier(4)

    def open_it():
        barrier.wait()
        try:
            Database(path).close()
        except Exception as e:      # noqa: BLE001
            errors.append(e)
    threads = [threading.Thread(target=open_it) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    db = Database(path)
    assert db.one("SELECT runner, title FROM inbox") == {"runner": "rr", "title": "Write x"}
    db.close()


def test_a_failed_migration_leaves_the_file_as_it_was(tmp_path, monkeypatch):
    from valet.control import db as dbmod
    path = tmp_path / "v.db"
    m1_database(path)
    monkeypatch.setattr(dbmod, "MIGRATIONS", [*dbmod.MIGRATIONS, ("agents", "broken", "NOT A TYPE (")])
    with pytest.raises(sqlite3.OperationalError):
        Database(path)
    old = sqlite3.connect(path)
    assert [r[1] for r in old.execute("PRAGMA table_info(agents)")][-1] == "updated_at"     # nothing added
    assert {r[1]: r[3] for r in old.execute("PRAGMA table_info(inbox)")}["runner"] == 1     # not copied
    old.close()


def test_password_and_sessions(tmp_path):
    db = Database(tmp_path / "v.db")
    assert not auth.check_password(db, "anything at all")
    with pytest.raises(ValueError):
        auth.set_password(db, "short")
    auth.set_password(db, "a long enough password")
    assert auth.check_password(db, "a long enough password")
    assert not auth.check_password(db, "a long enough passwore")
    s = auth.issue_session(db)
    assert auth.is_owner_token(db, s)
    assert auth.is_owner_token(db, "vown_" + s)
    assert auth.is_owner_token(db, "Bearer vown_" + s)
    assert not auth.is_owner_token(db, s[:-2] + "xx")
    assert not auth.is_owner_token(db, None)
    auth.set_password(db, "another long password")      # rotates the secret: old sessions die
    assert not auth.is_owner_token(db, s)


def test_runner_tokens(tmp_path):
    db = Database(tmp_path / "v.db")
    token = auth.add_runner(db, "rr")
    assert token.startswith("vrun_")
    assert token not in str(db.query("SELECT * FROM runners"))     # only the hash is stored
    assert auth.runner_for_token(db, f"Bearer {token}") == "rr"
    assert auth.runner_for_token(db, "Bearer vrun_nope") is None
    assert auth.runner_for_token(db, None) is None
    new = auth.add_runner(db, "rr")         # re-key: the old token stops working
    assert auth.runner_for_token(db, f"Bearer {token}") is None
    assert auth.runner_for_token(db, f"Bearer {new}") == "rr"
    with pytest.raises(ValueError):
        auth.add_runner(db, "Bad Name")


def test_login_limiter():
    lim = auth.LoginLimiter()
    for _ in range(5):
        assert lim.begin("1.2.3.4") is not None
    assert lim.begin("1.2.3.4") is None
    assert lim.begin("5.6.7.8") is not None
    lim._fails["1.2.3.4"] = [t - lim.WINDOW for t in lim._fails["1.2.3.4"]]     # the window passes
    assert lim.begin("1.2.3.4") is not None
    t = lim.begin("9.9.9.9")
    lim.succeed("9.9.9.9", t)                     # a right password forgives
    assert "9.9.9.9" not in lim._fails and t not in lim._all


def test_login_limiter_parallel_attempts():
    """Attempts count when they start: 40 at once from one IP get 5 tries, not 40."""
    from concurrent.futures import ThreadPoolExecutor
    lim = auth.LoginLimiter()
    with ThreadPoolExecutor(40) as ex:
        results = list(ex.map(lambda _: lim.begin("1.2.3.4"), range(40)))
    assert sum(r is not None for r in results) == 5


def test_login_limiter_global_cap():
    """Rotating addresses doesn't buy unlimited guesses."""
    lim = auth.LoginLimiter()
    for i in range(lim.GLOBAL_LIMIT):
        assert lim.begin(f"10.0.{i // 256}.{i % 256}") is not None
    assert lim.begin("192.0.2.1") is None


def test_client_ip_trusts_cloudflare_only_when_told():
    from types import SimpleNamespace

    def req(peer, cf=None):
        return SimpleNamespace(client=SimpleNamespace(host=peer), headers={"cf-connecting-ip": cf} if cf else {})
    assert auth.client_ip(req("127.0.0.1", "203.0.113.9")) == "127.0.0.1"
    assert auth.client_ip(req("127.0.0.1", "203.0.113.9"), trust_cf=True) == "203.0.113.9"
    assert auth.client_ip(req("198.51.100.7", "203.0.113.9"), trust_cf=True) == "198.51.100.7"   # not the tunnel
    assert auth.client_ip(req("127.0.0.1"), trust_cf=True) == "127.0.0.1"


def test_cli_helpers(tmp_path):
    from valet.shared.config import ControlConfig
    cfg = ControlConfig(data=tmp_path / "data", definitions=tmp_path / "defs")
    set_password(cfg, "a long enough password")
    assert stat.S_IMODE(cfg.data.stat().st_mode) == 0o700
    token = add_runner(cfg, "rr")
    db = open_database(cfg)
    assert auth.check_password(db, "a long enough password")
    assert auth.runner_for_token(db, f"Bearer {token}") == "rr"
    revoke_runner(cfg, "rr")
    assert auth.runner_for_token(db, f"Bearer {token}") is None
    with pytest.raises(KeyError):
        revoke_runner(cfg, "ghost")
    with pytest.raises(ValueError):
        set_password(cfg, "short")


def _log(root):
    return subprocess.run(["git", "-C", str(root), "log", "--format=%an <%ae>|%s"], capture_output=True,
                          text=True, check=True).stdout.splitlines()


def test_definitions_repo(tmp_path):
    repo = DefinitionsRepo(tmp_path / "defs")
    repo.init()
    repo.init()                                         # idempotent
    first = repo.head()
    assert repo.names() == []
    c1 = repo.create("scout")
    assert c1 != first and repo.head() == c1
    assert repo.names() == ["scout"]
    d, files = repo.load("scout")
    assert d.name == "scout" and d.computer.level == "container"
    assert "scout" in files["persona.md"]
    with pytest.raises(FileExistsError):
        repo.create("scout")

    assert repo.write("scout", {"persona.md": repo.raw("scout")["persona.md"]}, "no-op") == c1
    c2 = repo.write("scout", {"persona.md": "Terse.\n"}, "Edit persona")
    assert c2 != c1 and repo.raw("scout")["persona.md"] == "Terse.\n"

    before = repo.raw("scout")["agent.yaml"]
    with pytest.raises(DefinitionError):
        repo.write("scout", {"agent.yaml": "name: other\n"}, "rename")
    with pytest.raises(DefinitionError):
        repo.write("scout", {"agent.yaml": "name: scout\nbogus: 1\n"}, "bad key")
    assert repo.raw("scout")["agent.yaml"] == before and repo.head() == c2
    with pytest.raises(ValueError):
        repo.write("scout", {"other.txt": "x"}, "nope")

    log = _log(repo.root)
    assert log == ["Valet <valet@localhost>|Edit persona", "Valet <valet@localhost>|Create agent scout",
                   "Valet <valet@localhost>|Definitions repo"]
    status = subprocess.run(["git", "-C", str(repo.root), "status", "--porcelain"], capture_output=True, text=True)
    assert status.stdout == ""


def test_definitions_create_invalid(tmp_path):
    repo = DefinitionsRepo(tmp_path / "defs")
    repo.init()
    with pytest.raises(DefinitionError):
        repo.create("scout", agent_yaml="name: scout\ncomputer: {level: moon}\n")
    assert repo.names() == []
    with pytest.raises(FileNotFoundError):
        repo.raw("scout")


def test_definitions_history_and_files_at_a_commit(tmp_path):
    repo = DefinitionsRepo(tmp_path / "defs")
    repo.init()
    first = repo.write("scout", {"agent.yaml": "name: scout\n"}, "first")
    repo.write("scout", {"persona.md": "Hi.\n\n"}, "second")
    repo.write("scout-2", {"agent.yaml": "name: scout-2\n"}, "other agent")
    assert [e["message"] for e in repo.history("scout")] == ["second", "first"]
    assert repo.history("scout", limit=1)[0]["message"] == "second"
    assert repo.history("ghost") == []
    assert repo.at("scout", first) == {"agent.yaml": "name: scout\n", "persona.md": ""}    # no persona then
    assert repo.at("scout", repo.head()[:7]) == {"agent.yaml": "name: scout\n", "persona.md": "Hi.\n\n"}  # exact
    with pytest.raises(ValueError, match="not a commit hash"):
        repo.at("scout", "HEAD~1")
    with pytest.raises(FileNotFoundError, match="scout has no definition at fffffff"):
        repo.at("scout", "f" * 40)


def test_logs_redact_ws_tokens():
    import logging

    from valet.control.__main__ import RedactTokens
    record = logging.LogRecord("uvicorn.error", logging.INFO, __file__, 1, '%s - "WebSocket %s" [accepted]',
                               ("127.0.0.1:1", "/ws/client?token=vown_secret.sig&x=1"), None)
    RedactTokens().filter(record)
    assert record.getMessage() == '127.0.0.1:1 - "WebSocket /ws/client?token=***&x=1" [accepted]'
