"""Named secrets: values the owner sets once in Valet, which agents list by name in their definition (`secrets:
[GITHUB_TOKEN]`) and get as env vars of the same name. The control plane stores them encrypted and puts the values
an agent needs into its agent.start (hub.start_agent); no API returns a value.

An agent can also list a vault entry (`- vault: github/alice`, vault.py): it gets that entry's password as an env var,
named by the definition's `env` or derived from the entry's name (GITHUB_ALICE_PASSWORD).

Encryption (shared with the vault, vault.py):
AES-256-GCM from `cryptography`, a fresh 12-byte nonce per value, stored as nonce + ciphertext + tag. The associated
data binds a ciphertext to its name ("secret:<NAME>"), so a value can't be moved to another name in the database.
The key is VALET_MASTER_KEY, else the key file (ControlConfig.key_path, default <data>/master.key): 32 random bytes,
mode 0600, created on first use."""
import os
import time
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from valet.shared.config import ControlConfig
from valet.shared.definition import AgentDefinition, secret_name_error

from .db import Database
from .vault import Vault

KEY_BYTES = 32
NONCE_BYTES = 12
MAX_VALUE = 32 * 1024       # bytes (UTF-8): an env var, not a file


class SecretError(Exception):
    """A secret that can't be stored or handed out. str() is shown to the owner: it names secrets, never a value.
    `missing`: the definition's names that aren't set."""

    def __init__(self, message: str, missing: list[str] | None = None) -> None:
        super().__init__(message)
        self.missing = missing or []


def load_key(cfg: ControlConfig) -> bytes:
    """cfg.master_key, else the key file, created if it doesn't exist. SecretError if it can't be read or created,
    or isn't a key."""
    if cfg.master_key is not None:
        return cfg.master_key
    path = cfg.key_path
    try:
        if not path.exists():
            _create_key(path)
        key = path.read_bytes()
    except OSError as e:
        raise SecretError(f"can't read or create the master key file {path}: {e.strerror or e}") from None
    if len(key) != KEY_BYTES:
        raise SecretError(f"the master key file {path} isn't a {KEY_BYTES}-byte key")
    return key


def _create_key(path: Path) -> None:
    """A new random key, written whole before it appears under `path` (a temp file, then a hard link that fails if
    the file exists): a process starting at the same time (the CLI next to the server) reads the same key."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.urandom(4).hex()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, os.urandom(KEY_BYTES))
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.link(tmp, path)
    except FileExistsError:
        pass                    # someone else's key won: use theirs
    finally:
        tmp.unlink()


def _aad(name: str) -> bytes:
    return f"secret:{name}".encode()


def value_error(value: object) -> str | None:
    """Why `value` can't be stored, or None. The message never repeats the value."""
    if not isinstance(value, str):
        return "the value must be a string"
    if not value:
        return "the value is empty"
    if "\0" in value:
        return "the value can't contain a NUL character"
    try:
        size = len(value.encode())
    except UnicodeEncodeError:      # a lone surrogate ("\ud800" in the JSON): not text an env var can hold
        return "the value isn't valid text"
    if size > MAX_VALUE:
        return f"the value is longer than {MAX_VALUE // 1024} KiB"
    return None


class SecretStore:
    """The `secrets` table. Only resolve() (an agent's start) and get() (the MCP gateway connecting to a server whose
    mcp.yaml entry says ${secret:NAME}) decrypt; everything else deals in names and times."""

    def __init__(self, db: Database, cfg: ControlConfig) -> None:
        self.db = db
        self.cfg = cfg
        self._cipher: AESGCM | None = None
        self.vault = Vault(db, self._aead)      # the same master key

    def _aead(self) -> AESGCM:
        if self._cipher is None:
            self._cipher = AESGCM(load_key(self.cfg))
        return self._cipher

    def set(self, name: str, value: str) -> float:
        """Store or replace a secret; returns its new updated_at. ValueError (bad name or value), SecretError (key)."""
        if err := secret_name_error(name):
            raise ValueError(f"{name}: {err}")
        if err := value_error(value):
            raise ValueError(err)
        nonce = os.urandom(NONCE_BYTES)
        blob = nonce + self._aead().encrypt(nonce, value.encode(), _aad(name))
        now = time.time()
        self.db.execute("INSERT INTO secrets (name, value, created_at, updated_at) VALUES (?, ?, ?, ?) "
                        "ON CONFLICT (name) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                        (name, blob, now, now))
        return now

    def delete(self, name: str) -> bool:
        return bool(self.db.execute("DELETE FROM secrets WHERE name = ?", (name,)).rowcount)

    def listing(self) -> list[dict]:
        """[{"name", "created_at", "updated_at"}] by name. No values."""
        return self.db.query("SELECT name, created_at, updated_at FROM secrets ORDER BY name")

    def stamps(self, names: list[str]) -> dict[str, float | None]:
        """{name: updated_at, or None if it isn't set}, for each of `names`: what restart awareness compares."""
        if not names:
            return {}
        marks = ",".join("?" * len(names))
        rows = self.db.query(f"SELECT name, updated_at FROM secrets WHERE name IN ({marks})", tuple(names))
        have = {r["name"]: r["updated_at"] for r in rows}
        return {n: have.get(n) for n in names}

    def definition_stamps(self, d: AgentDefinition) -> dict[str, float | None]:
        """stamps() of the definition's named secrets, plus `vault:<entry>` -> when the entry's password last
        changed (None if there's no such entry, or no password) for each vault entry it lists: what its start is
        fingerprinted with."""
        entries = [v.vault for v in d.vault_secrets()]
        return {**self.stamps(d.secret_names()),
                **{f"vault:{n}": at for n, at in self.vault.stamps(entries).items()}}

    def resolve(self, d: AgentDefinition) -> tuple[dict[str, str], dict[str, float | None]]:
        """({env name: value}, stamps) for the definition's secrets, read together: the values go in agent.start,
        the stamps (as definition_stamps()) in the fingerprint of what it started with. A named secret's value
        goes under its name, a vault entry's password under its env name. SecretError naming every one that isn't
        set (`missing`: the names, and `vault:<entry>` for entries that don't exist or have no password), or one
        that doesn't decrypt with this key."""
        names = d.secret_names()
        vault = d.vault_secrets()
        rows = {}
        if names:
            marks = ",".join("?" * len(names))
            rows = {r["name"]: r for r in self.db.query(
                f"SELECT name, value, updated_at FROM secrets WHERE name IN ({marks})", tuple(names))}
        missing = [n for n in names if n not in rows]
        problems = []
        if len(missing) == 1:
            problems.append(f"secret {missing[0]} isn't set: add it to Valet's secrets")
        elif missing:
            problems.append(f"secrets {', '.join(missing)} aren't set: add them to Valet's secrets")
        passwords, vault_problems = self.vault.passwords([v.vault for v in vault])
        problems += vault_problems
        missing += [f"vault:{v.vault}" for v in vault if v.vault not in passwords]
        if problems:
            raise SecretError("; ".join(problems), missing)
        values = {n: self._open(n, rows[n]["value"]) for n in names}
        values.update({v.env_name: passwords[v.vault] for v in vault})
        stamps = {n: rows[n]["updated_at"] for n in names}
        if vault:
            stamps.update({f"vault:{n}": at for n, at in self.vault.stamps([v.vault for v in vault]).items()})
        return values, stamps

    def get(self, name: str) -> str | None:
        """One secret's value, or None if it isn't set. SecretError if it doesn't decrypt with this key."""
        row = self.db.one("SELECT value FROM secrets WHERE name = ?", (name,))
        return self._open(name, row["value"]) if row else None

    def _open(self, name: str, blob: bytes) -> str:
        try:
            return self._aead().decrypt(blob[:NONCE_BYTES], blob[NONCE_BYTES:], _aad(name)).decode()
        except InvalidTag:
            raise SecretError(f"secret {name} doesn't decrypt with this master key: set it again") from None
