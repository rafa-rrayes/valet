"""SQLite state: small synchronous helpers behind a lock (writes are tiny and rare next to network waits), and
transaction() for the few changes that must happen together (a queued prompt becoming a turn).
The definitions themselves live in the git repo, not here; the agents table only holds runtime state."""
import contextlib
import os
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    -- owner_password_hash (argon2), session_secret (signs cookies and bearer tokens), webhook_url (notify.py)
CREATE TABLE IF NOT EXISTS runners (
    name        TEXT PRIMARY KEY,
    token_hash  TEXT NOT NULL UNIQUE,       -- sha256 hex of the full token (vrun_...)
    created_at  REAL NOT NULL,
    last_seen   REAL,
    version     TEXT,                       -- from its last hello
    revoked     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS agents (             -- runtime state; a row appears the first time an agent is touched
    name        TEXT PRIMARY KEY,
    runner      TEXT,                       -- where it runs now (definition.runner, or the runner it was placed on)
    desired     TEXT NOT NULL DEFAULT 'stopped',    -- running | stopped: what the owner asked for
    state       TEXT NOT NULL DEFAULT 'stopped',    -- starting | running | stopped | failed: what the runner reports
    error       TEXT,
    capabilities TEXT,                      -- JSON: ACP agentCapabilities
    agent_info  TEXT,                       -- JSON: ACP agentInfo
    started     TEXT,                       -- fingerprint of the definition it was last started with
    token_hash  TEXT,                       -- sha256 hex of its MCP gateway token (vagt_...), while it has one
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    agent       TEXT NOT NULL,
    id          TEXT NOT NULL,              -- the harness's ACP session id
    title       TEXT,                       -- the harness's (session_info), else the first prompt's 80 chars
    state       TEXT NOT NULL DEFAULT 'idle',       -- idle | working
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    PRIMARY KEY (agent, id)
);
CREATE TABLE IF NOT EXISTS turns (
    id          TEXT PRIMARY KEY,           -- uuid4 hex, control-generated
    agent       TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'running',    -- running | done | cancelled | failed | interrupted
    stop_reason TEXT,
    error       TEXT,
    started_at  REAL NOT NULL,
    ended_at    REAL
);
CREATE INDEX IF NOT EXISTS turns_session ON turns (agent, session_id, started_at);
CREATE TABLE IF NOT EXISTS events (             -- valet.shared.events.Event, one row each
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent       TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    turn_id     TEXT,
    kind        TEXT NOT NULL,
    data        TEXT NOT NULL,              -- JSON
    ts          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS events_session ON events (agent, session_id, id);
CREATE TABLE IF NOT EXISTS inbox (
    id          TEXT PRIMARY KEY,           -- the runner's request_id, or the gateway's (uuid4 hex)
    kind        TEXT NOT NULL,              -- 'permission' (a harness asks), 'tool' (the MCP gateway asks)
    agent       TEXT NOT NULL,
    session_id  TEXT,                       -- a tool item has one only if its agent had exactly one turn running
    turn_id     TEXT,
    runner      TEXT,                       -- the runner holding the request open; NULL: the gateway holds it
    status      TEXT NOT NULL DEFAULT 'pending',    -- pending | answered | expired
    title       TEXT NOT NULL,
    payload     TEXT NOT NULL,              -- JSON: {"tool_call": {...}, "options": [...], "tool"?: {...}}
    answer      TEXT,                       -- JSON: the ACP outcome sent to the harness (or the gateway)
    created_at  REAL NOT NULL,
    answered_at REAL
);
CREATE INDEX IF NOT EXISTS inbox_status ON inbox (status, created_at);
CREATE TABLE IF NOT EXISTS tool_calls (         -- the MCP gateway's audit log: every call an agent made through it
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent       TEXT NOT NULL,
    server      TEXT NOT NULL,              -- e.g. valet.agents, github ('' for a tool name no server has)
    tool        TEXT NOT NULL,              -- the server's own tool name (or the name the agent asked for)
    arguments   TEXT,                       -- JSON, cut at 2000 characters
    decision    TEXT,                       -- allow | ask | deny; NULL when it never got that far
    outcome     TEXT NOT NULL,              -- pending | ok | error | denied | rejected | expired | failed | unknown
                                            -- | cancelled | interrupted
    error       TEXT,
    started_at  REAL NOT NULL,
    ended_at    REAL,
    duration    REAL                        -- seconds, waiting for the owner included
);
CREATE INDEX IF NOT EXISTS tool_calls_agent ON tool_calls (agent, id);
CREATE TABLE IF NOT EXISTS runner_seq (         -- the last notification applied from each runner (protocol.Hello)
    runner      TEXT PRIMARY KEY,
    boot        TEXT NOT NULL,              -- the runner process it came from
    seq         INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS secrets (            -- named secrets agents get as env vars (control/secrets.py)
    name        TEXT PRIMARY KEY,           -- the env var name, e.g. GITHUB_TOKEN
    value       BLOB NOT NULL,              -- encrypted: 12-byte nonce + AES-256-GCM ciphertext and tag
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL               -- restart awareness compares this, never the value
);
CREATE TABLE IF NOT EXISTS tool_grants (        -- "Allow for an hour" on a gateway ask: that tool runs until expires_at
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent       TEXT NOT NULL,
    server      TEXT NOT NULL,              -- e.g. github, valet.agents
    tool        TEXT NOT NULL,              -- the server's own tool name
    expires_at  REAL NOT NULL,
    created_at  REAL NOT NULL,
    UNIQUE (agent, server, tool)            -- a new grant for the same tool renews it
);
CREATE TABLE IF NOT EXISTS devices (            -- the native apps' APNs device tokens, for push (control/notify.py)
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    token       TEXT NOT NULL UNIQUE,       -- hex, lowercase
    platform    TEXT NOT NULL,              -- ios | macos
    name        TEXT NOT NULL,              -- what the device called itself ("My iPhone")
    created_at  REAL NOT NULL,
    last_push   REAL                        -- when APNs last accepted a push for it
);
CREATE TABLE IF NOT EXISTS vault (              -- saved logins (control/vault.py): login() fills them, agents never see them
    name        TEXT PRIMARY KEY,           -- unique, e.g. github/alice
    profile     TEXT NOT NULL,              -- the browser profile it signs in (browser.yaml)
    site        TEXT NOT NULL,              -- a site pattern (shared/browser.py); a bare domain covers its subdomains
    label       TEXT,
    username    TEXT,                       -- shown to the owner, filled by login(); never the agent's to read
    secret      BLOB NOT NULL,              -- 12-byte nonce + AES-256-GCM of {"password", "totp"} JSON, AAD vault:<name>
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    password_at REAL NOT NULL,              -- the password's last change: restart awareness of agents given it as an env var
    used_at     REAL                        -- the last login() that filled it
);
CREATE TABLE IF NOT EXISTS browser_grants (     -- "allow for an hour" answers to browser asks
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent       TEXT NOT NULL,
    profile     TEXT NOT NULL,
    host        TEXT NOT NULL,
    kind        TEXT NOT NULL,              -- read | interact | commit | login
    expires_at  REAL NOT NULL,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS browser_audit (      -- every browser action, by an agent or (agent NULL) the owner
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    agent       TEXT,
    profile     TEXT NOT NULL,
    action      TEXT NOT NULL,              -- open_tab, click, type_text, login, request_human, live_view, ...
    kind        TEXT,                       -- read | interact | commit | login | human
    host        TEXT,
    target      TEXT,                       -- what it acted on (an element's name, a URL), never a typed value
    decision    TEXT,                       -- allow | ask | deny: what the rules said
    outcome     TEXT NOT NULL,              -- ok | denied | rejected | expired | failed | cancelled
    note        TEXT,
    shot        TEXT                        -- a screenshot's file name in <data>/browser/shots
);
CREATE INDEX IF NOT EXISTS browser_audit_profile ON browser_audit (profile, id);
CREATE TABLE IF NOT EXISTS shared (             -- the shared space (control/space.py): notes and files
    id          INTEGER PRIMARY KEY AUTOINCREMENT,  -- never reused: a transfer ticket or an ask names an item by id
    agent       TEXT,                       -- the agent whose space it's in (it and the owner only); NULL: unassigned
    kind        TEXT NOT NULL,              -- 'note' | 'file'
    title       TEXT NOT NULL,              -- a note's title, a file's name
    text        TEXT NOT NULL DEFAULT '',   -- a note's Markdown, a file's description
    mime        TEXT,                       -- files
    size        INTEGER,                    -- files: bytes
    path        TEXT,                       -- files: <id>/<name>, under <data>/shared
    added_by    TEXT NOT NULL,              -- 'owner' or the agent's name
    updated_by  TEXT NOT NULL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    seen_at     REAL                        -- when the owner last saw it (unseen: an agent changed it since)
);
CREATE INDEX IF NOT EXISTS shared_updated ON shared (updated_at);
CREATE TABLE IF NOT EXISTS computer_audit (     -- the owner in an agent's computer (control/computer.py)
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    agent       TEXT NOT NULL,
    action      TEXT NOT NULL,              -- terminal_open | terminal_close | file_upload | file_download
                                            -- | volumes_delete | attachment
    target      TEXT,                       -- a path, a terminal's id, the volumes' names
    outcome     TEXT NOT NULL,              -- ok | failed
    note        TEXT                        -- a size, how long a terminal was open and how it ended, an error
);
CREATE INDEX IF NOT EXISTS computer_audit_agent ON computer_audit (agent, id);
CREATE TABLE IF NOT EXISTS attachments (        -- files the owner attached to a message (control/attachments.py)
    id          TEXT PRIMARY KEY,           -- uuid4 hex
    agent       TEXT NOT NULL,
    session_id  TEXT,                       -- the session whose message carried it (started or queued); NULL
                                            -- until then
    kind        TEXT NOT NULL,              -- 'image' (bytes in <data>/attachments/<id>) | 'file' (in its computer)
    name        TEXT NOT NULL,
    mime        TEXT NOT NULL,
    size        INTEGER NOT NULL,
    path        TEXT,                       -- a file's path in the agent's computer
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS prompt_queue (       -- prompts waiting for their session's turn (control/prompts.py)
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent       TEXT NOT NULL,
    session_id  TEXT,                       -- NULL: the agent's main session, made when the agent next runs
    pos         REAL NOT NULL,              -- the order within a session (enqueue time; a prompt moved back keeps
                                            -- its place)
    text        TEXT NOT NULL,              -- the prompt as the agent gets it (source line included)
    source      TEXT NOT NULL,              -- owner | schedule | webhook:<name> | agent:<name> | wakeup | answer
    label       TEXT,                       -- how the apps name the source ("Every day at 08:00")
    retry_of    TEXT,                       -- the turn it repeats (the owner's Retry)
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS prompt_queue_session ON prompt_queue (agent, session_id, pos, id);
CREATE TABLE IF NOT EXISTS trigger_runs (       -- every time a trigger fired, or couldn't (control/scheduler.py)
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent       TEXT NOT NULL,
    trigger     TEXT NOT NULL,              -- Trigger.key: schedule:<expression> | webhook:<name>; wakeup:<id>
    source      TEXT NOT NULL,              -- schedule | webhook:<name> | wakeup
    label       TEXT,
    scheduled_for REAL,                     -- the run it was (schedules and wake-ups)
    fired_at    REAL NOT NULL,              -- when Valet handled it (the latest, for a folded row)
    outcome     TEXT NOT NULL,              -- firing (being delivered) | started | queued | skipped | missed | failed
    session_id  TEXT,
    turn_id     TEXT,
    queue_id    INTEGER,
    error       TEXT,                       -- why it was skipped, missed or failed
    count       INTEGER NOT NULL DEFAULT 1  -- consecutive identical skips fold into one row
);
CREATE INDEX IF NOT EXISTS trigger_runs_agent ON trigger_runs (agent, id);
CREATE TABLE IF NOT EXISTS trigger_state (      -- per schedule: the last run handled, so a restart neither repeats
    agent       TEXT NOT NULL,              -- nor forgets one
    trigger     TEXT NOT NULL,
    last        REAL NOT NULL,              -- the scheduled time of the last run handled (or when it was first seen)
    PRIMARY KEY (agent, trigger)
);
CREATE TABLE IF NOT EXISTS webhook_keys (       -- the key each webhook trigger is called with (control/webhooks.py)
    agent       TEXT NOT NULL,
    name        TEXT NOT NULL,
    key         BLOB NOT NULL,              -- encrypted like secrets (the master key), so its HMAC can be checked
    created_at  REAL NOT NULL,
    PRIMARY KEY (agent, name)
);
CREATE TABLE IF NOT EXISTS wakeups (            -- wake-ups an agent scheduled for itself (valet.owner)
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent       TEXT NOT NULL,
    session_id  TEXT,                       -- where it fires: the session that asked, or NULL for the main session
    prompt      TEXT NOT NULL,
    due_at      REAL NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',    -- pending | firing | fired | cancelled | skipped
    created_at  REAL NOT NULL,
    done_at     REAL,
    run_id      INTEGER                     -- its trigger_runs row once handled
);
CREATE INDEX IF NOT EXISTS wakeups_due ON wakeups (status, due_at);
"""

MIGRATIONS = [("agents", "started", "TEXT"), ("agents", "token_hash", "TEXT"),
              ("agents", "main_session", "TEXT"),                  # its main session (control/prompts.py)
              ("turns", "source", "TEXT NOT NULL DEFAULT 'owner'"),  # who prompted it (prompt_queue.source)
              ("turns", "source_label", "TEXT"),
              ("turns", "prompt", "TEXT"),                         # the text it was sent (for Retry)
              ("turns", "retry_of", "TEXT"),
              ("turns", "attachments", "TEXT"),                    # JSON: the attachments' ids it carried (Retry)
              ("prompt_queue", "attachments", "TEXT")]             # JSON: the queued prompt's attachments' ids
SHARED_ASSIGN = "shared.assign_pending"     # settings: an old database's shared items await an agent (space.start)


def _statements(script: str) -> list[str]:
    """SQL text -> its statements, for execute() (one statement each) inside _migrate's transaction."""
    out, buf = [], ""
    for line in script.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            out.append(buf.strip())
            buf = ""
    return out


class Database:
    """One connection (check_same_thread=False, WAL, row_factory=Row), an RLock around every call. Autocommit:
    every statement stands alone, except inside transaction()."""

    def __init__(self, path: Path) -> None:
        """Open (creating the file mode 0600 and running SCHEMA)."""
        path = Path(path)
        os.close(os.open(path, os.O_CREAT | os.O_RDWR, 0o600))
        os.chmod(path, 0o600)
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA busy_timeout=5000")      # the CLI may write while the server runs
            # Switching to WAL fails at once ("database is locked", no busy wait) while another connection writes,
            # e.g. migrating the same file (_migrate), so only a file that isn't WAL yet (a new one) is switched.
            if self._conn.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        """Bring a database an older Valet made up to SCHEMA (CREATE TABLE IF NOT EXISTS leaves old tables as they
        are): the columns added since (MIGRATIONS), shared.agent (and a flag to assign the items, SHARED_ASSIGN),
        and inbox.runner, NOT NULL before the gateway's items, whose
        constraint SQLite can't drop, so the table is copied. All of it under one write lock, in one transaction:
        the CLI may be opening the same old file at the same moment (a second ALTER of the same column would
        fail), and a migration that fails leaves the file as it was."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            for table, column, decl in MIGRATIONS:
                cols = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}
                if column not in cols:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            if "agent" not in {r[1] for r in self._conn.execute("PRAGMA table_info(shared)")}:
                # One space per agent: the items of the space everyone shared get their agent at the next start,
                # when the definitions are there to say which agents exist (SharedSpace.start).
                self._conn.execute("ALTER TABLE shared ADD COLUMN agent TEXT")
                self._conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, '1')", (SHARED_ASSIGN,))
            self._conn.execute("CREATE INDEX IF NOT EXISTS shared_agent ON shared (agent, updated_at)")
            notnull = {r[1]: r[3] for r in self._conn.execute("PRAGMA table_info(inbox)")}
            if notnull.get("runner"):
                cols = ", ".join(notnull)
                self._conn.execute("ALTER TABLE inbox RENAME TO inbox_old")
                self._conn.execute("DROP INDEX IF EXISTS inbox_status")
                for statement in _statements(SCHEMA[SCHEMA.index("CREATE TABLE IF NOT EXISTS inbox"):
                                                    SCHEMA.index("CREATE TABLE IF NOT EXISTS tool_calls")]):
                    self._conn.execute(statement)
                self._conn.execute(f"INSERT INTO inbox ({cols}) SELECT {cols} FROM inbox_old")
                self._conn.execute("DROP TABLE inbox_old")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    @contextlib.contextmanager
    def transaction(self) -> Iterator["Database"]:
        """Several statements as one (BEGIN IMMEDIATE ... COMMIT, ROLLBACK on an exception), holding the lock
        throughout: other threads wait, and the database never shows half of it. No awaits inside."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple = ()) -> dict | None:
        with self._lock:
            r = self._conn.execute(sql, params).fetchone()
        return dict(r) if r else None

    def get_setting(self, key: str) -> str | None:
        r = self.one("SELECT value FROM settings WHERE key = ?", (key,))
        return r["value"] if r else None

    def set_setting(self, key: str, value: str) -> None:
        self.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                     "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value))

    def close(self) -> None:
        with self._lock:
            self._conn.close()
