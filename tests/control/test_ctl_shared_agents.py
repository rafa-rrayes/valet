"""One shared space per agent (control/space.py): an agent reaches only its own items, by listing, by id with every
tool and through transfer tickets; the owner's API per agent (lists, notes and uploads in one agent's space, unseen
counts per agent, the unassigned items, moving one); an old database's items going to their agents at the first
start; forget_agent."""
import time

import pytest
from fastapi.testclient import TestClient

from tests.control.conftest import PASSWORD
from tests.control.test_ctl_shared import NONE, call, data, error, make_agent, start, upload
from valet.control.__main__ import set_password
from valet.control.app import create_app, open_database
from valet.control.db import SHARED_ASSIGN
from valet.control.definitions import DefinitionsRepo
from valet.control.gateway.builtin_shared import INLINE_TEXT
from valet.control.space import NotFound, SharedError

BASE = "http://127.0.0.1:8780"
BIG = b"x" * (INLINE_TEXT + 1)          # too big to come inline: shared_get hands out a download URL


def unassigned(client, title="Old note", by="gone") -> int:
    """An item from when the space was everyone's, whose agent couldn't be told."""
    now = time.time()
    return client.app.state.control.db.execute(
        "INSERT INTO shared (agent, kind, title, text, added_by, updated_by, created_at, updated_at) "
        "VALUES (NULL, 'note', ?, 'from before', ?, ?, ?, ?)", (title, by, by, now, now)).lastrowid


def listed(client, token, agent, **args) -> list[int]:
    return [i["id"] for i in data(call(client, token, "shared_list", args, agent=agent))["items"]]


# ---------- what an agent reaches ----------

def test_an_agent_reaches_only_its_own_items(client, connect, rpc):
    with connect() as runner:
        ada = start(client, rpc, runner, name="ada", level="unrestricted")
        bob = start(client, rpc, runner, name="bob", level="unrestricted")
        mine = data(call(client, ada, "shared_add_note", {"text": "ada's plan"}, agent="ada"))
        for_ada = client.post("/api/agents/ada/shared/notes", json={"text": "the owner to ada"}).json()
        [bobs_file] = upload(client, "bob.txt", BIG, agent="bob").json()
        bobs_note = data(call(client, bob, "shared_add_note", {"text": "bob's plan"}, agent="bob"))
        old = unassigned(client)

        # Listing and searching: its own and nothing else.
        assert listed(client, ada, "ada") == [for_ada["id"], mine["id"]]
        assert listed(client, ada, "ada", query="plan") == [mine["id"]]
        assert listed(client, ada, "ada", by="owner") == [for_ada["id"]]
        assert listed(client, ada, "ada", by="me") == [mine["id"]]
        assert "by is owner or me" in error(call(client, ada, "shared_list", {"by": "bob"}, agent="ada"))
        assert listed(client, bob, "bob") == [bobs_note["id"], bobs_file["id"]]

        # By id, with every tool: another agent's item, or an unassigned one, isn't there.
        for target in (bobs_file["id"], bobs_note["id"], old):
            for tool, args in (("shared_get", {}), ("shared_get", {"download": True}),
                               ("shared_update", {"text": "mine now"}), ("shared_update", {"content": "x"}),
                               ("shared_update", {"upload": True}), ("shared_delete", {})):
                msg = error(call(client, ada, tool, {"id": target, **args}, agent="ada"))
                assert f"there's no shared item {target}" in msg, (tool, args, msg)
        assert client.get(f"/api/shared/{bobs_note['id']}").json()["text"] == "bob's plan"
        assert client.get(f"/api/shared/{bobs_file['id']}/file").content == BIG
        assert client.get(f"/api/shared/{old}").json()["text"] == "from before"

        # Tickets: issued for its own item, they stop working once the item is another agent's.
        [own] = upload(client, "ada.txt", BIG, agent="ada").json()
        down = data(call(client, ada, "shared_get", {"id": own["id"]}, agent="ada"))["download_url"]
        up = data(call(client, ada, "shared_update", {"id": own["id"], "upload": True}, agent="ada"))["upload_url"]
        anon = client.__class__(client.app)
        assert anon.get(down.removeprefix(BASE)).content == BIG
        assert client.post(f"/api/shared/{own['id']}/move", json={"agent": "bob"}).json()["agent"] == "bob"
        assert anon.get(down.removeprefix(BASE)).status_code == 404
        assert anon.put(up.removeprefix(BASE), content=b"ada's bytes").status_code == 404
        assert client.get(f"/api/shared/{own['id']}/file").content == BIG
        assert own["id"] in listed(client, bob, "bob") and own["id"] not in listed(client, ada, "ada")
        # Another agent's gateway path with this agent's ticket: nothing either.
        theirs = data(call(client, bob, "shared_get", {"id": own["id"]}, agent="bob"))["download_url"]
        assert anon.get(theirs.removeprefix(BASE).replace("/bob/", "/ada/")).status_code == 404

        # What an agent adds is in its own space, the frame says whose.
        with client.websocket_connect("/ws/client") as ws:
            ws.receive_json()
            data(call(client, bob, "shared_add_note", {"text": "new"}, agent="bob"))
            frame = ws.receive_json()
            assert frame["item"]["agent"] == "bob" and frame["agents"]["bob"] >= 1


def test_an_agent_adds_only_to_its_own_space(client):
    make_agent(client, "ada")
    with pytest.raises(SharedError, match="its own shared space"):
        client.portal.call(client.app.state.control.shared.add_note, "ada", "bob", "", "x")


# ---------- the owner's API ----------

def test_the_owner_api_per_agent(client):
    make_agent(client, "ada")
    make_agent(client, "bob")
    shared = client.app.state.control.shared
    a = client.post("/api/agents/ada/shared/notes", json={"text": "for ada"})
    assert a.status_code == 201 and a.json()["agent"] == "ada"
    [b] = upload(client, "b.txt", b"b", agent="bob").json()
    r = client.post("/api/agents/bob/shared/files", files=[("file", ("c.txt", b"c", "text/plain"))])
    assert r.status_code == 201 and r.json()[0]["agent"] == "bob"
    for path in ("/api/agents/nobody/shared", "/api/agents/Bad!/shared"):
        assert client.get(path).status_code == 404
    assert client.post("/api/agents/nobody/shared/notes", json={"text": "x"}).status_code == 404
    assert upload(client, "x.txt", b"x", agent="nobody").status_code == 404

    def titles(path, **params):
        return [i["title"] for i in client.get(path, params=params).json()["items"]]
    assert titles("/api/agents/ada/shared") == ["for ada"]
    assert titles("/api/agents/bob/shared") == ["c.txt", "b.txt"]
    assert titles("/api/agents/bob/shared", q="b.t") == ["b.txt"]
    assert titles("/api/shared") == ["c.txt", "b.txt", "for ada"]

    # Unseen, per agent and in all.
    client.portal.call(shared.add_note, "ada", "ada", "", "from ada")
    client.portal.call(shared.add_note, "bob", "bob", "", "from bob")
    client.portal.call(shared.add_note, "bob", "bob", "", "from bob again")
    assert client.get("/api/shared/unseen").json() == {"unseen": 3, "agents": {"ada": 1, "bob": 2}, "unassigned": 0}
    assert client.get("/api/agents/bob/shared").json()["unseen"] == 2
    assert client.get("/api/shared").json()["unseen"] == 3

    # The unassigned: only the owner's to see, open, move or delete.
    old, older = unassigned(client, "Old note"), unassigned(client, "Older note", by="owner")
    assert titles("/api/shared/unassigned") == ["Older note", "Old note"]
    assert client.get("/api/shared/unassigned").json()["unseen"] == 1          # an agent changed it last
    assert client.get("/api/shared/unseen").json() == {"unseen": 4, "agents": {"ada": 1, "bob": 2}, "unassigned": 2}
    assert client.get(f"/api/shared/{old}").json()["agent"] is None
    moved = client.post(f"/api/shared/{old}/move", json={"agent": "ada"})
    assert moved.status_code == 200 and moved.json()["agent"] == "ada"
    assert "Old note" in titles("/api/agents/ada/shared") and titles("/api/shared/unassigned") == ["Older note"]
    assert client.get("/api/shared/unseen").json() == {"unseen": 4, "agents": {"ada": 2, "bob": 2}, "unassigned": 1}
    assert client.post(f"/api/shared/{older}/move", json={"agent": "nobody"}).status_code == 404
    assert client.post("/api/shared/999/move", json={"agent": "ada"}).status_code == 404
    assert client.delete(f"/api/shared/{older}").json() == {"ok": True}
    assert titles("/api/shared/unassigned") == []

    assert client.post("/api/shared/seen", json={}).json() == NONE


def test_forget_agent(client, cfg):
    make_agent(client, "ada")
    make_agent(client, "bob")
    [f1, f2] = client.post("/api/agents/ada/shared/files", files=[("file", ("1.txt", b"1", "text/plain")),
                                                                  ("file", ("2.txt", b"2", "text/plain"))]).json()
    client.post("/api/agents/ada/shared/notes", json={"text": "n"})
    [keep] = upload(client, "keep.txt", b"k", agent="bob").json()
    old = unassigned(client)
    assert client.app.state.control.shared.forget_agent("ada") == 3
    assert client.get("/api/agents/ada/shared").json()["items"] == []
    assert not (cfg.shared_dir / str(f1["id"])).exists() and not (cfg.shared_dir / str(f2["id"])).exists()
    assert client.get(f"/api/shared/{keep['id']}/file").content == b"k"
    assert client.get(f"/api/shared/{old}").status_code == 200
    assert client.app.state.control.shared.forget_agent("ada") == 0


def test_an_upload_still_arriving_when_its_agent_is_deleted_goes_nowhere(client, connect, rpc, cfg, monkeypatch):
    """The agent's PUT to its upload ticket (or the owner's upload) passed its checks before the delete, and its last bytes
    come after: no item for a deleted agent, which the next agent made with its name would get."""
    c = client.app.state.control
    with connect() as runner:
        ada = start(client, rpc, runner, name="ada", level="unrestricted")
        url = data(call(client, ada, "shared_add_file", {"name": "late.txt", "upload": True}, agent="ada"))["upload_url"]
        server = c.gateway.builtins["valet.shared"]
        receive = server._receive

        async def deleted_meanwhile(scope, recv):
            src = await receive(scope, recv)
            c.defs.delete("ada")                # the delete, as far as its definition and forget_agent
            c.shared.forget_agent("ada")
            return src
        monkeypatch.setattr(server, "_receive", deleted_meanwhile)
        r = client.__class__(client.app).put(url.removeprefix(BASE), content=b"late bytes")
        assert r.status_code == 404 and "no agent ada" in r.json()["detail"]
    assert c.db.one("SELECT COUNT(*) AS n FROM shared")["n"] == 0
    assert [p.name for p in cfg.shared_dir.iterdir() if p.name != ".incoming"] == []
    assert list((cfg.shared_dir / ".incoming").iterdir()) == []

    # The owner's own upload to it, the same.
    with pytest.raises(NotFound, match="no agent ada"):
        client.portal.call(c.shared.add_note, "ada", "owner", "", "for nobody")
    src = c.shared.incoming()
    src.write_bytes(b"x")
    with pytest.raises(NotFound, match="no agent ada"):
        client.portal.call(c.shared.add_file, "ada", "owner", "x.txt", src)
    assert not src.exists() and c.db.one("SELECT COUNT(*) AS n FROM shared")["n"] == 0


# ---------- an old database ----------

def test_an_old_database_gets_one_space_per_agent(cfg):
    """Before, one list was everyone's: each item goes to the agent that added it, else the one that changed it last,
    when that agent has a definitions folder; the rest stay unassigned. Once: an agent made later doesn't get them."""
    db = open_database(cfg)
    db.execute("DROP INDEX shared_agent")
    db.execute("ALTER TABLE shared DROP COLUMN agent")          # the table as the last Valet left it
    rows = [("ada", "ada"), ("owner", "ada"), ("bob", "ada"), ("gone", "bob"), ("owner", "owner"), ("gone", "gone")]
    for i, (added, updated) in enumerate(rows):
        db.execute("INSERT INTO shared (kind, title, text, added_by, updated_by, created_at, updated_at) "
                   "VALUES ('note', ?, 'x', ?, ?, ?, ?)", (f"n{i}", added, updated, i, i))
    db.close()
    repo = DefinitionsRepo(cfg.definitions)
    repo.init()
    repo.create("ada")
    repo.create("bob")
    set_password(cfg, PASSWORD)
    with TestClient(create_app(cfg)) as c:
        db = c.app.state.control.db
        assert [r["agent"] for r in db.query("SELECT agent FROM shared ORDER BY id")] == [
            "ada", "ada", "bob", "bob", None, None]
        assert db.get_setting(SHARED_ASSIGN) is None
        c.post("/api/login", json={"password": PASSWORD})
        assert len(c.get("/api/shared/unassigned").json()["items"]) == 2
    repo.create("gone")
    with TestClient(create_app(cfg)) as c:
        db = c.app.state.control.db
        assert [r["agent"] for r in db.query("SELECT agent FROM shared ORDER BY id")][-2:] == [None, None]
