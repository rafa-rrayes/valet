"""The vault (control/vault.py): the encryption and what it's bound to, TOTP secrets, which entry covers a page,
what page text is masked with, and vault entries given to an agent as env vars (SecretStore.resolve, restart
awareness, missing_secrets). PASSWORD is a stand-in; the tests check it shows up nowhere it shouldn't."""
import json
import time

import pyotp
import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from valet.control.app import open_database
from valet.control.secrets import SecretError, SecretStore
from valet.control.vault import Vault, VaultError, clean_site, clean_totp, covers, totp_code
from valet.shared.definition import AgentDefinition

PASSWORD = "hunter2-correct-Horse"
TOTP = "JBSWY3DPEHPK3PXP"


@pytest.fixture
def store(cfg):
    return SecretStore(open_database(cfg), cfg)


@pytest.fixture
def vault(store):
    return store.vault


def raw_files(cfg) -> bytes:
    return b"".join(p.read_bytes() for p in cfg.data.glob("valet.db*"))


def add(vault, name="github/alice", profile="alice", site="github.com", **kw):
    return vault.put(name, profile=profile, site=site, create=True, **{"username": "alice", "password": PASSWORD, **kw})


# ---------- the store ----------
def test_round_trip_without_plaintext_on_disk(vault, cfg):
    view = add(vault, totp=TOTP, label="Personal")
    assert view == {"name": "github/alice", "profile": "alice", "site": "github.com", "label": "Personal",
                    "username": "alice", "has_password": True, "has_totp": True, "created_at": view["created_at"],
                    "updated_at": view["updated_at"], "used_at": None}
    assert PASSWORD not in json.dumps(vault.listing()) and TOTP not in json.dumps(vault.listing())
    assert PASSWORD.encode() not in raw_files(cfg) and TOTP.encode() not in raw_files(cfg)
    secret = vault.reveal(vault.row("github/alice"))
    assert secret == {"username": "alice", "password": PASSWORD, "totp": TOTP}
    assert vault.row("github/alice")["used_at"] is not None          # reveal marks it used


def test_a_ciphertext_is_bound_to_its_entry_and_key(vault, store, cfg):
    add(vault)
    add(vault, name="other", password="another-password")
    db = vault.db
    blob = db.one("SELECT secret FROM vault WHERE name = 'github/alice'")["secret"]
    # Moved to another entry: it doesn't open.
    db.execute("UPDATE vault SET secret = ? WHERE name = 'other'", (blob,))
    with pytest.raises(VaultError, match="vault entry other doesn't decrypt"):
        vault.open(vault.row("other"))
    # Moved into the secrets table under the same name: it doesn't open either (different associated data).
    db.execute("INSERT INTO secrets (name, value, created_at, updated_at) VALUES ('GITHUB_ALICE', ?, 0, 0)", (blob,))
    with pytest.raises(SecretError):
        store.get("GITHUB_ALICE")
    # Another master key: nothing opens, and secrets() skips what doesn't.
    wrong = Vault(db, lambda: AESGCM(b"\x01" * 32))
    with pytest.raises(VaultError) as e:
        wrong.open(wrong.row("github/alice"))
    assert PASSWORD not in str(e.value)
    assert wrong.secrets() == []
    assert wrong.passwords(["github/alice"])[1] == ["vault entry github/alice doesn't decrypt with this master key: "
                                                   "set it again"]


def test_create_update_delete(vault):
    with pytest.raises(VaultError, match="needs a profile and a site"):
        vault.put("x", profile="alice", create=True)
    with pytest.raises(VaultError, match="name:"):
        vault.put("Not/OK", profile="alice", site="a.com", create=True)
    with pytest.raises(VaultError, match="profile:"):
        vault.put("x", profile="Alice", site="a.com", create=True)
    with pytest.raises(VaultError, match="a site like"):
        vault.put("x", profile="alice", site="not a site", create=True)
    with pytest.raises(VaultError, match="one line"):
        vault.put("x", profile="alice", site="a.com", username="a\nb", create=True)
    with pytest.raises(VaultError, match="NUL"):
        vault.put("x", profile="alice", site="a.com", password="a\0b", create=True)
    with pytest.raises(VaultError, match="longer than 4096"):
        vault.put("x", profile="alice", site="a.com", password="x" * 4097, create=True)
    with pytest.raises(VaultError, match="2FA secret"):
        vault.put("x", profile="alice", site="a.com", totp="not base32!", create=True)
    assert vault.listing() == []

    first = add(vault, site="https://www.GitHub.com/login/")
    assert first["site"] == "github.com/login"                  # www. dropped, like a password manager
    with pytest.raises(FileExistsError):
        add(vault)
    with pytest.raises(FileNotFoundError):
        vault.put("nope", label="x", create=False)
    time.sleep(0.01)
    # A field left out stays; "" clears.
    v = vault.put("github/alice", label="Work", totp=TOTP, create=False)
    assert (v["label"], v["username"], v["has_password"], v["has_totp"]) == ("Work", "alice", True, True)
    assert v["created_at"] == first["created_at"] and v["updated_at"] > first["updated_at"]
    v = vault.put("github/alice", password="", label="", create=False)
    assert (v["label"], v["has_password"], v["has_totp"]) == (None, False, True)
    assert vault.reveal(vault.row("github/alice"))["totp"] == TOTP
    assert vault.count("alice") == 1 and vault.count("work") == 0
    assert vault.delete("github/alice") and not vault.delete("github/alice")


# ---------- TOTP ----------
def test_totp_secrets():
    assert clean_totp("jbsw y3dp-ehpk 3pxp") == TOTP
    uri = f"otpauth://totp/GitHub:alice?secret={TOTP}&issuer=GitHub"
    assert clean_totp(uri) == TOTP
    assert clean_totp("") is None and clean_totp("   ") is None and clean_totp(None) is None
    for bad in ("not base32!", "otpauth://totp/x?issuer=nosecret", "1890"):
        with pytest.raises(VaultError):
            clean_totp(bad)
    assert totp_code(TOTP) == pyotp.TOTP(TOTP).now() and len(totp_code(TOTP)) == 6


# ---------- which entry ----------
@pytest.mark.parametrize("site,url,hit", [
    ("instagram.com", "https://www.instagram.com/accounts/login/", True),     # a bare domain covers subdomains
    ("instagram.com", "https://instagram.com/", True),
    ("instagram.com", "https://evilinstagram.com/", False),
    ("instagram.com", "https://instagram.com.evil.net/", False),
    ("accounts.google.com", "https://accounts.google.com/signin", True),
    ("accounts.google.com", "https://mail.google.com/", False),
    ("github.com/login", "https://github.com/login/oauth", True),            # a path prefix matches as written
    ("github.com/login", "https://github.com/settings", False),
    ("*.example.com", "https://a.example.com/", True),
])
def test_covers(site, url, hit):
    assert covers(clean_site(site), url) is hit


def test_find_picks_the_most_specific_entry_in_the_profile(vault):
    add(vault, name="google/alice", site="google.com")
    add(vault, name="google/accounts", site="accounts.google.com")
    add(vault, name="google/work", profile="work", site="accounts.google.com")
    add(vault, name="google/second", site="google.com", username="other")
    url = "https://accounts.google.com/signin"
    assert vault.find("alice", url)["name"] == "google/accounts"
    assert vault.find("work", url)["name"] == "google/work"
    assert vault.find("alice", "https://mail.google.com/")["name"] == "google/alice"     # ties: the first by name
    assert vault.find("alice", url, "google/second")["name"] == "google/second"
    assert vault.find("alice", url, "google/work") is None           # another profile's entry
    assert vault.find("alice", "https://github.com/") is None
    assert vault.find("alice", "https://github.com/", "google/alice") is None            # doesn't cover the page
    assert vault.sites("alice") == ["accounts.google.com", "google.com"]


def test_secrets_are_what_page_text_is_masked_with(vault):
    add(vault, totp=TOTP)
    add(vault, name="short", password="abc")                    # too short to mask ordinary text with
    add(vault, name="longer", password=PASSWORD + "-and-more")
    assert vault.secrets() == [PASSWORD + "-and-more", PASSWORD, TOTP]


# ---------- vault entries as env vars ----------
def agent(*secrets) -> AgentDefinition:
    return AgentDefinition.model_validate({"name": "scout", "secrets": list(secrets)})


def test_resolve_gives_passwords_under_their_env_names(store, vault):
    store.set("GITHUB_TOKEN", "ghp_x")
    add(vault, name="aws/alice", password="aws-" + PASSWORD)
    add(vault, totp=TOTP)
    values, stamps = store.resolve(agent("GITHUB_TOKEN", {"vault": "github/alice"}, {"vault": "aws/alice", "env": "AWS_PW"}))
    assert values == {"GITHUB_TOKEN": "ghp_x", "GITHUB_ALICE_PASSWORD": PASSWORD, "AWS_PW": "aws-" + PASSWORD}
    assert set(stamps) == {"GITHUB_TOKEN", "vault:github/alice", "vault:aws/alice"}
    assert stamps["vault:aws/alice"] == vault.row("aws/alice")["password_at"]
    assert stamps == store.definition_stamps(agent("GITHUB_TOKEN", {"vault": "github/alice"},
                                                   {"vault": "aws/alice", "env": "AWS_PW"}))
    # The TOTP secret never leaves the vault this way.
    assert TOTP not in json.dumps(values)


def test_resolve_names_missing_entries(store, vault):
    add(vault, name="nopass", password="")
    d = agent("GITHUB_TOKEN", {"vault": "aws/alice"}, {"vault": "nopass"})
    with pytest.raises(SecretError) as e:
        store.resolve(d)
    assert str(e.value) == ("secret GITHUB_TOKEN isn't set: add it to Valet's secrets; vault entry aws/alice doesn't "
                            "exist: add it to Valet's vault; vault entry nopass has no password")
    assert e.value.missing == ["GITHUB_TOKEN", "vault:aws/alice", "vault:nopass"]
    stamps = store.definition_stamps(d)
    assert [n for n, at in stamps.items() if at is None] == e.value.missing


YAML = "name: scout\ncomputer:\n  level: host\nsecrets:\n  - vault: github/alice\n"


def test_start_with_a_vault_entry_and_restart_awareness(client, connect, rpc, cfg):
    from tests.control.test_ctl_secrets import _start, _wait
    assert client.post("/api/agents", json={"name": "scout", "agent_yaml": YAML}).status_code == 201
    client.put("/api/browser/yaml", json={"text": "profiles:\n  alice: {}\n"})
    with connect() as runner:
        r = client.post("/api/agents/scout/start")
        assert r.status_code == 422 and r.json()["missing_secrets"] == ["vault:github/alice"]
        r = client.post("/api/vault", json={"name": "github/alice", "profile": "alice", "site": "github.com",
                                            "username": "alice", "password": PASSWORD})
        assert r.status_code == 201, r.text
        _, cmd = _start(rpc, runner, client)
        assert cmd["secrets"] == {"GITHUB_ALICE_PASSWORD": PASSWORD}
        assert PASSWORD not in json.dumps({k: v for k, v in cmd.items() if k != "secrets"})
        a = client.get("/api/agents/scout").json()
        assert (a["restart_needed"], a["missing_secrets"]) == (False, [])

        # The agent got the password: a new label, username or 2FA secret (or the same password again) doesn't
        # need a restart, a new password does.
        time.sleep(0.01)
        client.put("/api/vault/github/alice", json={"label": "Personal", "username": "rrayes", "totp": TOTP,
                                                   "password": PASSWORD})
        assert client.get("/api/agents/scout").json()["restart_needed"] is False
        client.put("/api/vault/github/alice", json={"password": "rotated-" + PASSWORD})
        assert client.get("/api/agents/scout").json()["restart_needed"] is True
        _, cmd = _start(rpc, runner, client)
        assert cmd["secrets"] == {"GITHUB_ALICE_PASSWORD": "rotated-" + PASSWORD}
        assert client.get("/api/agents/scout").json()["restart_needed"] is False

        client.delete("/api/vault/github/alice")
        a = client.get("/api/agents/scout").json()
        assert (a["state"], a["restart_needed"], a["missing_secrets"]) == ("running", True, ["vault:github/alice"])
        assert client.post("/api/agents/scout/start").status_code == 422
        _wait(lambda: client.get("/api/agents/scout").json()["state"] == "running")
    assert PASSWORD.encode() not in raw_files(cfg)
