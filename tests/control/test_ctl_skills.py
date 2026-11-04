"""Skills in the control plane: the shared library and an agent's own in the definitions repo (a commit each), the
API, what agent.start carries, and restart awareness, with the test playing the runner."""
import subprocess

import pytest

from tests.control.test_ctl_secrets import _start
from valet.shared import skills

YAML = "name: {name}\ncomputer:\n  level: host\ntools:\n  skills: [{skills}]\n"


def new_agent(client, name="scout", picks=""):
    r = client.post("/api/agents", json={"name": name, "agent_yaml": YAML.format(name=name, skills=picks)})
    assert r.status_code == 201, r.text


def new_skill(client, name, description="Use when asked for the code word.", instructions="Say PELICAN.",
              agent=None):
    base = f"/api/agents/{agent}/skills" if agent else "/api/skills"
    r = client.post(base, json={"name": name, "description": description, "instructions": instructions})
    assert r.status_code == 201, r.text
    return r.json()


def git_log(cfg, *paths) -> list[str]:
    out = subprocess.run(["git", "-C", str(cfg.definitions), "log", "--format=%s", "--", *paths],
                         capture_output=True, text=True, check=True).stdout
    return out.splitlines()


# ---------- the shared library ----------

def test_create_get_list_edit_delete_a_shared_skill(client, cfg):
    s = new_skill(client, "code-word")
    assert (s["name"], s["description"], s["instructions"], s["files"], s["error"]) == (
        "code-word", "Use when asked for the code word.", "Say PELICAN.", ["SKILL.md"], None)
    assert s["content"]["SKILL.md"] == skills.render("code-word", "Use when asked for the code word.", "Say PELICAN.")
    assert (cfg.definitions / "skills" / "code-word" / "SKILL.md").read_text() == s["content"]["SKILL.md"]
    assert client.get("/api/skills").json() == [{k: s[k] for k in ("name", "description", "files", "size", "error",
                                                                    "used_by")}]

    # Edit the instructions only: the description and any other frontmatter stay; add a file alongside.
    path = cfg.definitions / "skills" / "code-word" / "SKILL.md"
    path.write_text(skills.render("code-word", "Old.", "Old body.", {"license": "MIT"}))
    r = client.put("/api/skills/code-word", json={"instructions": "Say HERON.", "files": {"refs/words.md": "HERON\n"}})
    assert r.status_code == 200, r.text
    s = r.json()
    assert (s["description"], s["instructions"], s["extra"]) == ("Old.", "Say HERON.", {"license": "MIT"})
    assert s["files"] == ["SKILL.md", "refs/words.md"] and s["content"]["refs/words.md"] == "HERON\n"

    # A file sent as null is removed; SKILL.md sent whole replaces it.
    raw = skills.render("code-word", "New.", "Raw body.")
    s = client.put("/api/skills/code-word", json={"files": {"refs/words.md": None, "SKILL.md": raw}}).json()
    assert s["files"] == ["SKILL.md"] and s["content"]["SKILL.md"] == raw
    assert not (cfg.definitions / "skills" / "code-word" / "refs").exists()

    assert client.delete("/api/skills/code-word").json() == {"ok": True}
    assert client.get("/api/skills").json() == []
    assert client.get("/api/skills/code-word").status_code == 404
    assert client.delete("/api/skills/code-word").status_code == 404
    assert git_log(cfg, "skills") == ["Delete skill code-word", "Edit skill code-word", "Edit skill code-word",
                                      "Create skill code-word"]


def test_an_edit_that_changes_nothing_makes_no_commit(client, cfg):
    new_skill(client, "code-word")
    client.put("/api/skills/code-word", json={"instructions": "Say PELICAN."})
    assert git_log(cfg, "skills") == ["Create skill code-word"]


@pytest.mark.parametrize("body,msg", [
    ({"name": "Code Word", "description": "d"}, "name: lowercase"),
    ({"name": "x" * 65, "description": "d"}, "name: lowercase"),
    ({"name": "code-word", "description": " "}, "description is required"),
    ({"name": "code-word", "description": "x" * 1025}, "at most 1024"),
])
def test_create_refusals(client, cfg, body, msg):
    r = client.post("/api/skills", json=body)
    assert r.status_code == 422 and msg in r.json()["detail"], r.text
    assert not (cfg.definitions / "skills").exists()


def test_create_conflict_and_edit_refusals(client, cfg):
    new_skill(client, "code-word")
    assert client.post("/api/skills", json={"name": "code-word", "description": "d"}).status_code == 409
    before = (cfg.definitions / "skills" / "code-word" / "SKILL.md").read_text()
    for body, msg in [({"files": {"../x": "y"}}, "a relative path"),
                      ({"files": {"a.bin": "x\u0000y"}}, "text files only"),
                      ({"files": {"SKILL.md": None}}, "needs a SKILL.md"),
                      ({"files": {"SKILL.md": "no frontmatter"}}, "frontmatter"),
                      ({"description": "", "instructions": "x"}, "description is required"),
                      ({"description": "d", "files": {"SKILL.md": "x"}}, "not both")]:
        r = client.put("/api/skills/code-word", json=body)
        assert r.status_code == 422 and msg in r.json()["detail"], (body, r.text)
    assert (cfg.definitions / "skills" / "code-word" / "SKILL.md").read_text() == before
    assert client.put("/api/skills/nope", json={"instructions": "x"}).status_code == 404


def test_a_broken_skill_in_the_repo_is_listed_with_its_error(client, cfg):
    d = cfg.definitions / "skills" / "hand-made"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("no frontmatter")
    (cfg.definitions / "skills" / "img").mkdir()
    (cfg.definitions / "skills" / "img" / "SKILL.md").write_bytes(b"\xff\xfe")
    by = {s["name"]: s for s in client.get("/api/skills").json()}
    assert by["hand-made"]["error"].startswith("SKILL.md must start with YAML frontmatter")
    assert by["hand-made"]["description"] is None and by["hand-made"]["files"] == ["SKILL.md"]
    assert by["img"]["error"] == "SKILL.md: skills are text files only (UTF-8)" and by["img"]["files"] == []
    # Fixing it through the API: the whole SKILL.md.
    r = client.put("/api/skills/hand-made", json={"files": {"SKILL.md": skills.render("hand-made", "d", "b")}})
    assert r.status_code == 200 and r.json()["error"] is None
    assert client.put("/api/skills/img", json={"instructions": "x"}).status_code == 422


# ---------- an agent's own, and what it gets ----------

def test_an_agents_own_skills_and_what_it_gets(client, cfg):
    new_agent(client, "scout", "alpha, beta, nope")
    new_agent(client, "coder", '"*"')
    new_agent(client, "plain")
    for n in ("alpha", "beta", "gamma"):
        new_skill(client, n)
    own = new_skill(client, "beta", description="Its own beta.", agent="scout")
    assert own["agent"] == "scout" and own["used_by"] == ["scout"]
    assert (cfg.definitions / "agents" / "scout" / "skills" / "beta" / "SKILL.md").is_file()
    new_skill(client, "mine", agent="scout")

    got = client.get("/api/agents/scout/skills").json()
    assert got["all"] is False and got["missing"] == ["nope"] and got["problems"] == []
    assert got["skills"] == [{"name": "alpha", "from": "shared"}, {"name": "beta", "from": "agent"},
                             {"name": "mine", "from": "agent"}]
    assert [(s["name"], s["replaces_shared"]) for s in got["own"]] == [("beta", True), ("mine", False)]
    assert client.get("/api/agents/coder/skills").json()["all"] is True

    used = {s["name"]: s["used_by"] for s in client.get("/api/skills").json()}
    assert used == {"alpha": ["coder", "scout"], "beta": ["coder"], "gamma": ["coder"]}   # scout has its own beta

    r = client.put("/api/agents/scout/skills/mine", json={"description": "Changed."})
    assert r.status_code == 200 and r.json()["description"] == "Changed."
    assert client.get("/api/agents/scout/skills/mine").json()["description"] == "Changed."
    assert client.get("/api/agents/plain/skills/mine").status_code == 404
    assert client.delete("/api/agents/scout/skills/mine").json() == {"ok": True}
    assert [s["name"] for s in client.get("/api/agents/scout/skills").json()["own"]] == ["beta"]
    assert client.get("/api/agents/nobody/skills").status_code == 404
    assert git_log(cfg, "agents/scout/skills") == ["Delete scout's skill mine", "Edit scout's skill mine",
                                                   "Create scout's skill mine", "Create scout's skill beta"]
    # The agent's history (its dir's commits) shows them too.
    assert "Create scout's skill beta" in [h["message"] for h in client.get("/api/agents/scout/history").json()]


def test_agent_skills_of_an_invalid_definition(client, cfg):
    new_agent(client)
    (cfg.definitions / "agents" / "scout" / "agent.yaml").write_text("name: scout\nbogus: 1\n")
    assert client.get("/api/agents/scout/skills").status_code == 422


def test_a_copy_gets_the_originals_own_skills(client, cfg):
    new_agent(client, "scout", "alpha")
    new_skill(client, "mine", agent="scout")
    r = client.post("/api/agents", json={"name": "scout-2", "copy_from": "scout"})
    assert r.status_code == 201, r.text
    got = client.get("/api/agents/scout-2/skills").json()
    assert [s["name"] for s in got["own"]] == ["mine"] and got["missing"] == ["alpha"]
    assert git_log(cfg, "agents/scout-2")[0] == "Copy scout's skills to scout-2"


# ---------- agent.start and restarts ----------

def test_start_carries_the_skills_and_restart_tracks_them(client, connect, rpc):
    new_agent(client, "scout", "alpha")
    new_agent(client, "coder", '"*"')
    new_skill(client, "alpha")
    new_skill(client, "mine", agent="scout")
    with connect() as runner:
        _, cmd = _start(rpc, runner, client)
        alpha = skills.render("alpha", "Use when asked for the code word.", "Say PELICAN.")
        assert {k: v for k, v in cmd["files"].items() if k.startswith("skills/")} == {
            "skills/alpha/SKILL.md": alpha,
            "skills/mine/SKILL.md": skills.render("mine", "Use when asked for the code word.", "Say PELICAN.")}
        assert "persona.md" in cmd["files"]
        _start(rpc, runner, client, "coder")
        assert client.get("/api/agents/scout").json()["restart_needed"] is False

        # A skill nobody running uses: no restart for anyone.
        client.post("/api/agents", json={"name": "other", "agent_yaml": YAML.format(name="other", skills="")})
        new_skill(client, "theirs", agent="other")
        assert client.get("/api/agents/scout").json()["restart_needed"] is False
        # A new shared skill: coder takes every one ("*"), scout picks by name.
        new_skill(client, "gamma")
        assert client.get("/api/agents/coder").json()["restart_needed"] is True
        assert client.get("/api/agents/scout").json()["restart_needed"] is False
        # An edit to one scout uses, and to its own.
        client.put("/api/skills/alpha", json={"instructions": "Say HERON."})
        assert client.get("/api/agents/scout").json()["restart_needed"] is True
        _, cmd = _start(rpc, runner, client)
        assert "Say HERON." in cmd["files"]["skills/alpha/SKILL.md"]
        assert client.get("/api/agents/scout").json()["restart_needed"] is False
        client.delete("/api/agents/scout/skills/mine")
        assert client.get("/api/agents/scout").json()["restart_needed"] is True
        _, cmd = _start(rpc, runner, client)
        assert "skills/mine/SKILL.md" not in cmd["files"]
        # Turning one off in the definition is a definition change: restart needed, and gone at the next start.
        client.put("/api/agents/scout/files/agent.yaml", json={"text": YAML.format(name="scout", skills="")})
        assert client.get("/api/agents/scout").json()["restart_needed"] is True
        _, cmd = _start(rpc, runner, client)
        assert not [k for k in cmd["files"] if k.startswith("skills/")]


def test_a_missing_or_broken_skill_doesnt_stop_the_start(client, connect, rpc, cfg):
    new_agent(client, "scout", "alpha, nope")
    new_skill(client, "alpha")
    (cfg.definitions / "skills" / "alpha" / "SKILL.md").write_text("broken")
    with connect() as runner:
        _, cmd = _start(rpc, runner, client)
    assert not [k for k in cmd["files"] if k.startswith("skills/")]
    got = client.get("/api/agents/scout/skills").json()
    assert got["missing"] == ["nope"] and got["problems"][0].startswith("alpha: SKILL.md must start")
