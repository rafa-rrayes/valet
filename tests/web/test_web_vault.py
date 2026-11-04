"""The web UI's vault (vault.js): saved logins by profile, a new one, editing one with write-only password and 2FA
fields (never filled, emptied once saved, Clear), deleting one; and vault entries as an agent's secrets (Settings ›
Secrets, a start refused for one). Run by their own JS under node with a stand-in DOM and a fake API
(tests/web/settings.js). Skipped without node."""
import copy
import re
from pathlib import Path

from tests.web.test_web_browser import HOME, LOGIN, WORK, at, ok
from tests.web.test_web_secrets_tools import agents_list, html_of, page, sent
from tests.web.test_web_settings import agent, detail, pytestmark  # noqa: F401
from valet.shared.definition import RESERVED_PREFIXES

PLAIN = {**LOGIN, "name": "news", "profile": "home", "site": "news.ycombinator.com", "label": None, "username": None,
         "has_password": False, "has_totp": False}
STRAY = {**LOGIN, "name": "old/one", "profile": "gone"}
ENTRY = {**LOGIN, "has_totp": True, "used_by": ["scout"], "used_at": None}


def vault(actions=(), **routes) -> dict:
    r = {"GET /api/vault": ok([LOGIN, PLAIN, STRAY])}
    r.update(routes)
    return at("#/vault", actions, **r)


def entry(actions=(), e=ENTRY, **routes) -> dict:
    r = {"GET /api/vault/github/alice": ok(e)}
    r.update(routes)
    return at("#/vault/github/alice", actions, **r)


def test_the_vault_lists_saved_logins_by_profile():
    r = vault()
    work, home = html_of(r, "vault-profile:work"), html_of(r, "vault-profile:home")
    assert "Work logins" in work and 'href="#/vault/github/alice"' in work and "Work · alice@x.com · github.com" in work
    assert 'href="#/browser/work"' in work
    assert "news.ycombinator.com" in home and '<span class="badge pending">no password</span>' in home
    assert "gone (no such profile)" in html_of(r, "vault-profile:gone")
    # Profiles in browser.yaml's order (home, then work), then the strays.
    assert r["html"].index("vault-profile:home") < r["html"].index("vault-profile:work") < r["html"].index(
        "vault-profile:gone")
    assert r["hash"] == "#/vault" and sent(r) == []


def test_a_new_login_is_named_from_its_site_and_username():
    made = {**LOGIN, "label": None}
    r = vault([{"button": "New login"}, {"change": "vault-profile", "value": "work"},
               {"input": "vault-site", "value": "https://www.github.com/login"},
               {"input": "vault-username", "value": "alice@x.com"},
               {"input": "vault-password", "value": "hunter2-and-more"},
               {"button": "Save"}], **{"POST /api/vault": ok(made, 201)})
    assert sent(r) == [("POST", "/api/vault", {"name": "github/alice", "profile": "work",
                                               "site": "https://www.github.com/login", "username": "alice@x.com",
                                               "password": "hunter2-and-more"})]
    assert "Saved github/alice." in r["seen"]
    assert "hunter2-and-more" not in r["html"]                       # gone from the page once sent
    r = vault([{"button": "New login"}, {"change": "vault-profile", "value": "home"},
               {"input": "vault-site", "value": "example.com"}, {"input": "vault-name", "value": "Not A Name"},
               {"button": "Save"}])
    assert sent(r) == [] and "Lowercase letters, digits" in r["errors"]["vault-new"]


def test_a_refused_login_says_why_and_keeps_the_form():
    r = vault([{"button": "New login"}, {"change": "vault-profile", "value": "work"},
               {"input": "vault-site", "value": "github.com"}, {"input": "vault-totp", "value": "not base32!"},
               {"button": "Save"}],
              **{"POST /api/vault": ok({"detail": "totp: a base32 secret or an otpauth:// link"}, 422)})
    assert r["errors"]["vault-new"] == "totp: a base32 secret or an otpauth:// link"
    assert r["controls"]["vault-site"]["value"] == "github.com"
    assert sent(r)[0][2]["totp"] == "not base32!" and sent(r)[0][2]["name"] == "github"


def test_a_profile_new_login_starts_with_that_profile():
    r = at("#/browser/work", [{"button": "New login"}], **{"GET /api/vault": ok([])})
    assert r["hash"] == "#/vault" and r["controls"]["vault-profile"]["value"] == "work"
    assert "vault-site" in r["controls"]                                    # the form is open


def test_an_entry_shows_its_fields_but_never_a_secret():
    r = entry()
    c = r["controls"]
    assert c["vault-site"]["value"] == "github.com" and c["vault-username"]["value"] == "alice@x.com"
    assert c["vault-label"]["value"] == "Work" and c["vault-profile"]["value"] == "work"
    assert c["vault-password"]["value"] == "" and c["vault-totp"]["value"] == ""
    pw, totp = html_of(r, "secret:password"), html_of(r, "secret:totp")
    assert "Saved" in pw and 'type="password"' in pw and 'autocomplete="new-password"' in pw
    assert ">Clear</button>" in pw and ">Clear</button>" in totp
    assert "Scout gets its password as an env var" in r["text"] and "Never used" in r["text"]
    assert sent(r) == []
    off = entry(e={**ENTRY, "has_password": False, "has_totp": False})
    assert "Not set" in html_of(off, "secret:password") and "Clear" not in off["seen"]


def test_saving_sends_only_what_changed_and_empties_the_secret_fields():
    after = {**ENTRY, "username": "alice"}
    r = entry([{"input": "vault-username", "value": "alice"}, {"input": "vault-password", "value": "n3w-pass"},
               {"button": "Save"}], **{"PUT /api/vault/github/alice": ok(after)})
    assert sent(r) == [("PUT", "/api/vault/github/alice", {"username": "alice", "password": "n3w-pass"})]
    assert r["controls"]["vault-password"]["value"] == "" and "n3w-pass" not in r["html"]
    assert "Saved. Scout gets the new password at the next restart." in r["seen"]


def test_clear_takes_a_secret_away_at_the_save():
    r = entry([{"button": "Clear", "row": "secret:totp"}, {"button": "Save"}],
              **{"PUT /api/vault/github/alice": ok({**ENTRY, "has_totp": False})})
    assert sent(r) == [("PUT", "/api/vault/github/alice", {"totp": ""})]
    assert "Not set" in html_of(r, "secret:totp")
    kept = entry([{"button": "Clear", "row": "secret:password"}, {"button": "Keep it", "row": "secret:password"}])
    assert "Unsaved changes" not in kept["seen"]


def test_deleting_an_entry_says_who_loses_it():
    r = entry([{"button": "Delete login"}, {"button": "Delete"}],
              **{"DELETE /api/vault/github/alice": ok({"ok": True}), "GET /api/vault": ok([])})
    assert sent(r) == [("DELETE", "/api/vault/github/alice", None)] and r["hash"] == "#/vault"
    asked = entry([{"button": "Delete login"}])
    assert "Scout lists it under secrets and won't start without it" in asked["seen"]


def test_an_entry_that_is_not_there():
    r = entry(**{"GET /api/vault/github/alice": ok({"detail": "not found"}, 404)})
    assert "There's no saved login named github/alice." in r["text"] and "Delete login" not in r["text"]


def test_the_profile_list_offers_a_gone_profile_only_for_its_entry():
    r = entry(e={**ENTRY, "profile": "gone"})
    assert r["controls"]["vault-profile"]["value"] == "gone"
    assert "gone (no such profile)" in r["html"]
    assert [HOME["name"], WORK["name"]]                                      # both listed as options too
    assert ">home</option>" in r["html"] and ">Work logins (work)</option>" in r["html"]


# ---------- vault entries as an agent's secrets (Settings › Secrets, a refused start) ----------

AWS = {**LOGIN, "name": "aws/alice", "site": "aws.amazon.com", "label": None, "username": "alice", "used_by": ["scout"]}


def secrets_page(actions=(), vault_list=None, **routes) -> dict:
    r = {"GET /api/vault": ok(vault_list if vault_list is not None else [AWS, LOGIN, PLAIN])}
    r.update(routes)
    return page(actions, routes=r)


def test_a_vault_secret_shows_its_env_var_and_entry():
    r = secrets_page()
    row = html_of(r, "vault:aws/alice")
    assert "AWS_KEY" in row and "The password of aws/alice, as alice on aws.amazon.com" in row
    assert '<span class="badge idle">vault</span>' in row and 'href="#/vault/aws/alice"' in row
    assert "secret-value" not in row                                     # no field: its value is the vault's
    gone = secrets_page(vault_list=[LOGIN])
    assert "There's no vault entry aws/alice: add it in the vault" in html_of(gone, "vault:aws/alice")
    assert '<span class="badge waiting">not in the vault</span>' in html_of(gone, "vault:aws/alice")


def test_a_vault_password_is_picked_from_the_list():
    r = secrets_page([{"change": "secret-vault", "value": "github/alice"}])
    assert "GITHUB_ALICE_PASSWORD" in html_of(r, "vault:github/alice") and "Unsaved changes" in r["seen"]
    r = secrets_page([{"change": "secret-vault", "value": "github/alice"}, {"button": "Save"}],
                     **{"PUT /api/agents/scout/definition": ok(detail())})
    (put,) = [b for m, p, b in sent(r) if m == "PUT"]
    assert put["definition"]["secrets"][-1] == {"vault": "github/alice", "env": None}
    picker = re.search(r'<select class="from-vault".*?</select>', secrets_page()["html"]).group(0)
    options = re.findall(r"<option[^>]*>([^<]*)</option>", picker)
    assert options == ["A password from the vault…", "github/alice (github.com) as GITHUB_ALICE_PASSWORD"]


def test_an_entry_whose_name_makes_no_env_var_gets_one_with_vault_in_front():
    """An IP site's suggested name (0/alice) would give 0_ALICE_PASSWORD, which can't be an env var: the picker and the
    saved row say VAULT_0_ALICE_PASSWORD, as the definition derives it (vault_env), so the save goes through."""
    ip = {**LOGIN, "name": "0/alice", "site": "10.0.0.5"}
    picker = re.search(r'<select class="from-vault".*?</select>', secrets_page(vault_list=[ip])["html"]).group(0)
    assert "0/alice (10.0.0.5) as VAULT_0_ALICE_PASSWORD" in picker
    r = secrets_page([{"change": "secret-vault", "value": "0/alice"}], vault_list=[AWS, ip])
    assert "VAULT_0_ALICE_PASSWORD" in html_of(r, "vault:0/alice")
    app_js = (Path(__file__).parents[2] / "valet/control/static/app.js").read_text()
    mirrored = re.search(r"const RESERVED_PREFIXES = \[([^\]]*)\]", app_js).group(1)
    assert tuple(re.findall(r'"([^"]+)"', mirrored)) == RESERVED_PREFIXES


def test_a_start_refused_for_a_vault_password_takes_it_right_there():
    missing = {"status": 422, "body": {"detail": "vault entry aws/alice has no password",
                                       "missing_secrets": ["vault:aws/alice"]}}
    routes = {"POST /api/agents/scout/start": [missing, {"status": 200, "body": {}}],
              "PUT /api/vault/aws/alice": ok(AWS)}
    stopped = agent(desired="stopped", state="stopped", status="stopped", missing_secrets=["vault:aws/alice"])
    done = agents_list([{"button": "Start"}, {"input": "secret-value:vault:aws/alice", "value": "s3cret"},
                        {"button": "Set & start"}], copy.deepcopy(routes), [stopped])
    assert ("PUT", "/api/vault/aws/alice", {"password": "s3cret"}) in sent(done)
    assert "aws/alice's password" in agents_list([{"button": "Start"}], copy.deepcopy(routes), [stopped])["text"]
