"""The browser's and the vault's REST routes (api_browser.py): profiles and browser.yaml as commits, agents that
name profiles, the vault without values, the running browsers and their tabs (a FakeBackend instead of Chrome),
grants, the audit log and its screenshots."""
import json

import pytest

from tests.browser_fake import JPEG, FakeBackend, FakeWeb

PASSWORD = "hunter2-correct-Horse"
AGENT = "name: {name}\nharness: claude\ncomputer:\n  level: host\nbrowser:\n  profiles: [{profiles}]\n"


def control(client):
    return client.app.state.control


def last_commit(client) -> str:
    return control(client).defs._git("log", "-1", "--format=%s")


@pytest.fixture
def fake(client) -> FakeBackend:
    backend = FakeBackend(FakeWeb())
    control(client).browser.backend = backend
    return backend


def profile(client, name="alice", **body):
    r = client.post("/api/browser/profiles", json={"name": name, **body})
    assert r.status_code == 201, r.text
    return r.json()


def test_owner_only(cfg):
    from fastapi.testclient import TestClient

    from valet.control.app import create_app
    with TestClient(create_app(cfg)) as anon:
        for method, path in [("GET", "/api/browser/profiles"), ("GET", "/api/browser/yaml"), ("GET", "/api/vault"),
                             ("POST", "/api/vault"), ("GET", "/api/browser/audit"), ("GET", "/api/browser/grants"),
                             ("GET", "/api/browser/running"), ("GET", "/api/browser/shots/0123456789abcdef.jpg"),
                             ("GET", "/api/vault/github/alice")]:
            assert anon.request(method, path).status_code == 401, path


def test_profiles_are_commits_to_browser_yaml(client):
    assert client.get("/api/browser/profiles").json() == {"profiles": [], "error": None}
    p = profile(client, title="Alice's logins", note="Personal accounts.",
                sites={"https://GitHub.com/": {"commit": "allow"}, "*.google.com": {"login": "ask"}})
    assert p == {"name": "alice", "title": "Alice's logins", "note": "Personal accounts.",
                 "sites": {"github.com": {"commit": "allow"}, "*.google.com": {"login": "ask"}}, "used_by": [],
                 "vault": 0, "running": False}
    assert last_commit(client) == "Add browser profile alice"
    text = client.get("/api/browser/yaml").json()["text"]
    assert text.startswith("profiles:\n  alice:\n") and "github.com:" in text
    assert client.post("/api/browser/profiles", json={"name": "alice"}).status_code == 409
    r = client.post("/api/browser/profiles", json={"name": "Alice"})
    assert r.status_code == 422 and r.json()["detail"].startswith("name: lowercase letters")
    r = client.post("/api/browser/profiles", json={"name": "work", "sites": {"github.com": {"commit": "maybe"}}})
    assert r.status_code == 422 and r.json()["errors"][0]["field"].startswith("sites")
    r = client.post("/api/browser/profiles", json={"name": "work", "sites": {"a b": {"read": "allow"}}})
    assert r.status_code == 422 and "a site like" in r.json()["detail"]

    # PUT replaces the profile (a field left out is cleared).
    r = client.put("/api/browser/profiles/alice", json={"sites": {"github.com": {"commit": "deny"}}})
    assert r.status_code == 200 and r.json()["title"] is None and r.json()["sites"] == {"github.com": {"commit": "deny"}}
    assert last_commit(client) == "Edit browser profile alice"
    assert client.get("/api/browser/profiles/alice").json()["sites"] == {"github.com": {"commit": "deny"}}
    assert client.put("/api/browser/profiles/nope", json={}).status_code == 404
    assert client.get("/api/browser/profiles/nope").status_code == 404

    profile(client, "work")
    assert [p["name"] for p in client.get("/api/browser/profiles").json()["profiles"]] == ["alice", "work"]
    assert client.delete("/api/browser/profiles/work").json() == {"ok": True}
    assert last_commit(client) == "Delete browser profile work"
    assert client.delete("/api/browser/profiles/work").status_code == 404


def test_browser_yaml_as_text(client):
    text = "# Alice's browsers\nprofiles:\n  alice:\n    sites:\n      github.com: {commit: allow}  # pushing is fine\n"
    r = client.put("/api/browser/yaml", json={"text": text})
    assert r.status_code == 200 and r.json()["commit"] and r.json()["profiles"][0]["name"] == "alice"
    assert client.get("/api/browser/yaml").json()["text"] == text          # comments kept
    assert last_commit(client) == "Edit browser.yaml"
    head = client.get("/api/browser/yaml").json()["commit"]
    r = client.put("/api/browser/yaml", json={"text": "profiles:\n  Bad: {}\n"})
    assert r.status_code == 422 and r.json()["detail"].startswith("browser.yaml: profiles: 'Bad'")
    assert client.get("/api/browser/yaml").json() == {"text": text, "commit": head}       # nothing written

    # Broken behind Valet's back: the listing says so, changes through the profile routes wait for a fix.
    (control(client).cfg.definitions / "browser.yaml").write_text("profiles: [\n")
    out = client.get("/api/browser/profiles").json()
    assert out["profiles"] == [] and "not valid YAML" in out["error"]
    r = client.post("/api/browser/profiles", json={"name": "work"})
    assert r.status_code == 409 and "fix it first" in r.json()["detail"]
    assert client.put("/api/browser/yaml", json={"text": text}).status_code == 200
    assert client.put("/api/browser/yaml", json={"text": ""}).json()["profiles"] == []


def test_agents_name_profiles_that_exist(client, connect):
    yaml = AGENT.format(name="scout", profiles="alice")
    assert client.post("/api/agents", json={"name": "scout", "agent_yaml": yaml}).status_code == 201
    # A start with a profile browser.yaml doesn't have is refused.
    with connect():
        r = client.post("/api/agents/scout/start")
    assert r.status_code == 422
    assert r.json()["detail"] == ("browser profile alice: there's no such profile in browser.yaml; add it or take it out "
                                  "of the agent's browser profiles")
    # So is a settings save, per field.
    d = client.get("/api/agents/scout").json()["definition"]
    r = client.put("/api/agents/scout/definition", json={"definition": {**d, "browser": {"profiles": ["alice", "work"]}}})
    assert r.status_code == 422
    assert r.json()["errors"] == [{"field": "browser.profiles", "msg": "alice: there's no such browser profile"},
                                  {"field": "browser.profiles", "msg": "work: there's no such browser profile"}]
    r = client.post("/api/agents", json={"name": "other", "template": "blank",
                                         "definition": {"browser": {"profiles": ["ghost"]}}})
    assert r.status_code == 422 and r.json()["errors"] == [
        {"field": "browser.profiles", "msg": "ghost: there's no such browser profile"}]

    profile(client)
    r = client.put("/api/agents/scout/definition",
                   json={"definition": {**d, "browser": {"profiles": ["alice"], "rules": {"github.com": {"commit": "allow"}}}}})
    assert r.status_code == 200, r.text
    assert client.get("/api/browser/profiles/alice").json()["used_by"] == ["scout"]
    # Used: it can't be deleted.
    r = client.delete("/api/browser/profiles/alice")
    assert r.status_code == 409 and r.json()["detail"] == ("browser profile alice is used by scout: take it out of its "
                                                           "browser profiles first")


def test_vault_never_returns_a_value(client, cfg):
    profile(client)
    profile(client, "work")
    body = {"name": "github/alice", "profile": "alice", "site": "https://github.com/login", "username": "alice",
            "password": PASSWORD, "totp": "JBSWY3DPEHPK3PXP", "label": "Personal"}
    r = client.post("/api/vault", json=body)
    assert r.status_code == 201, r.text
    e = r.json()
    assert {k: e[k] for k in ("name", "profile", "site", "username", "label", "has_password", "has_totp", "used_by")} == {
        "name": "github/alice", "profile": "alice", "site": "github.com/login", "username": "alice", "label": "Personal",
        "has_password": True, "has_totp": True, "used_by": []}
    assert client.post("/api/vault", json=body).status_code == 409
    r = client.post("/api/vault", json={**body, "name": "x", "profile": "ghost"})
    assert r.status_code == 422 and r.json()["detail"] == "profile: there's no browser profile named ghost"
    r = client.post("/api/vault", json={**body, "name": "x", "password": 12345})
    assert r.status_code == 422 and r.json()["detail"] == "the password must be text" and "12345" not in r.text
    r = client.post("/api/vault", json={**body, "name": "x", "totp": "nope!"})
    assert r.status_code == 422 and "2FA secret" in r.json()["detail"]
    r = client.post("/api/vault", json={**body, "name": "Bad Name"})
    assert r.status_code == 422 and r.json()["detail"].startswith("name:")

    assert [x["name"] for x in client.get("/api/vault").json()] == ["github/alice"]
    assert client.get("/api/vault?profile=work").json() == []
    assert client.get("/api/vault/github/alice").json()["has_password"] is True
    assert client.get("/api/vault/nope").status_code == 404
    r = client.put("/api/vault/github/alice", json={"password": "", "profile": "work", "site": "github.com"})
    assert r.status_code == 200
    assert (r.json()["has_password"], r.json()["profile"], r.json()["site"], r.json()["username"]) == (
        False, "work", "github.com", "alice")
    assert client.put("/api/vault/github/alice", json={"profile": "ghost"}).status_code == 422
    assert client.put("/api/vault/nope", json={"label": "x"}).status_code == 404
    assert client.get("/api/browser/profiles/work").json()["vault"] == 1

    # A profile the vault has logins for can't be deleted.
    r = client.delete("/api/browser/profiles/work")
    assert r.status_code == 409 and r.json()["detail"] == "the vault has 1 login for work: delete it first"

    # used_by: agents that get its password as an env var.
    yaml = "name: scout\nharness: claude\nsecrets:\n  - vault: github/alice\n"
    client.post("/api/agents", json={"name": "scout", "agent_yaml": yaml})
    assert client.get("/api/vault/github/alice").json()["used_by"] == ["scout"]

    every = json.dumps([client.get("/api/vault").json(), client.get("/api/vault/github/alice").json(),
                        client.get("/api/browser/profiles").json()])
    assert PASSWORD not in every and "JBSWY3DPEHPK3PXP" not in every
    assert client.delete("/api/vault/github/alice").json() == {"ok": True}
    assert client.delete("/api/vault/github/alice").status_code == 404
    assert client.delete("/api/browser/profiles/work").status_code == 200
    assert PASSWORD.encode() not in b"".join(p.read_bytes() for p in cfg.data.glob("valet.db*"))


def test_running_browsers_and_tabs(client, fake):
    profile(client)
    assert client.get("/api/browser/running").json() == []
    assert client.get("/api/browser/profiles/alice/tabs").json() == []
    assert client.get("/api/browser/profiles/nope/tabs").status_code == 404
    r = client.post("/api/browser/profiles/alice/tabs", json={"url": "github.com/alice"})
    assert r.status_code == 201
    assert r.json() == {"tab": "t00001", "url": "https://github.com/alice", "title": "github.com"}
    client.post("/api/browser/profiles/alice/tabs", json={})
    tabs = client.get("/api/browser/profiles/alice/tabs").json()
    assert [t["tab"] for t in tabs] == ["t00002", "t00001"]                 # newest first
    running = client.get("/api/browser/running").json()
    assert [(b["profile"], b["tabs"], b["busy"], b["viewers"]) for b in running] == [("alice", 2, False, 0)]
    assert client.get("/api/browser/profiles/alice").json()["running"] is True
    # The owner's tab is in the audit log, as the owner's (agent null).
    rows = client.get("/api/browser/audit").json()["actions"]
    assert [(a["agent"], a["action"], a["host"], a["target"]) for a in rows][-1] == (
        None, "open_tab", "github.com", "https://github.com/alice")

    fake.fail = "Chrome isn't installed"
    assert client.post("/api/browser/profiles/alice/tabs", json={}).json() == {"detail": "Chrome isn't installed"}
    fake.fail = None
    assert client.post("/api/browser/profiles/alice/stop").json() == {"ok": True, "was_running": True}
    assert client.post("/api/browser/profiles/alice/stop").json() == {"ok": True, "was_running": False}
    assert client.get("/api/browser/running").json() == []

    # Deleting a profile stops its browser; ?wipe=1 also deletes its data.
    client.post("/api/browser/profiles/alice/tabs", json={})
    assert client.delete("/api/browser/profiles/alice?wipe=true").status_code == 200
    assert fake.wiped == ["alice"] and fake.running() == {}


def test_grants_audit_and_shots(client):
    svc = control(client).browser
    profile(client)
    g = svc.grant("scout", "alice", "github.com", "commit")
    svc.grant("scout", "alice", "old.example", "commit", seconds=-1)        # expired: not listed
    svc.grant("helper", "alice", "github.com", "interact")
    assert [(x["agent"], x["host"], x["kind"]) for x in client.get("/api/browser/grants").json()] == [
        ("helper", "github.com", "interact"), ("scout", "github.com", "commit")]
    assert [x["id"] for x in client.get("/api/browser/grants?agent=scout").json()] == [g]
    assert client.get("/api/browser/grants?profile=work").json() == []
    assert svc.live_grants("scout", "alice") == {("github.com", "commit")}
    assert client.delete(f"/api/browser/grants/{g}").json() == {"ok": True}
    assert client.delete(f"/api/browser/grants/{g}").status_code == 404
    assert svc.live_grants("scout", "alice") == set()

    shot = svc.save_shot(JPEG)
    for i in range(5):
        svc.audit("scout", "alice", "click", kind="commit", host="github.com", target=f"Save {i}", decision="ask",
                  outcome="ok", note="Alice allowed it once", shot=shot if i == 4 else None)
    svc.audit("helper", "work", "snapshot", kind="read", host="example.com", target="x" * 1000, decision="allow")
    page = client.get("/api/browser/audit?agent=scout&limit=2").json()
    assert page["more"] is True and [a["target"] for a in page["actions"]] == ["Save 4", "Save 3"]
    assert page["actions"][0]["shot"] == shot and page["actions"][0]["decision"] == "ask"
    rest = client.get(f"/api/browser/audit?agent=scout&before={page['actions'][-1]['id']}").json()
    assert rest["more"] is False and [a["target"] for a in rest["actions"]] == ["Save 2", "Save 1", "Save 0"]
    work = client.get("/api/browser/audit?profile=work").json()["actions"]
    assert len(work) == 1 and len(work[0]["target"]) == 300
    assert client.get("/api/browser/audit?limit=0").status_code == 422

    r = client.get(f"/api/browser/shots/{shot}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content == JPEG
    assert (svc.shots_dir / shot).stat().st_mode & 0o777 == 0o600
    for bad in ("nope.jpg", "../valet.db", "0123456789abcdef.jpg", shot.replace(".jpg", ".png")):
        assert client.get(f"/api/browser/shots/{bad}").status_code == 404, bad
