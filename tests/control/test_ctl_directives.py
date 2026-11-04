"""Directives on the control plane: the authority presets in the definitions repo, the owner profile and presets
over the API, the generated environment layer, the preview, agent.start carrying every layer, restart awareness
when a layer changes, and a missing preset refusing the start. The test plays the runner."""
import asyncio
import subprocess
import time

import pytest

from valet.control import directives
from valet.control.definitions import DefinitionsRepo
from valet.control.gateway import Gateway
from valet.shared.definition import AgentDefinition
from valet.shared.owner import say

FULL = "[owner, authority/full, persona.md, environment]"
YAML = "name: {name}\nharness: {harness}\ncomputer:\n  level: host\npermissions: {level}\ndirectives: {directives}\n"
OWNER = "# About the owner\n\nHe lives in São Paulo. His cat is called Bartolomeu.\n"


def agent(**kw) -> AgentDefinition:
    return AgentDefinition.model_validate({"name": "scout", **kw})


def create(client, name="scout", harness="claude", level="trusted", directives=FULL, persona="You are Scout.\n"):
    r = client.post("/api/agents", json={"name": name, "persona": persona, "agent_yaml": YAML.format(
        name=name, harness=harness, level=level, directives=directives)})
    assert r.status_code == 201, r.text
    return r.json()


def _wait(predicate, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not reached")


def start(rpc, runner, client, name="scout") -> dict:
    r, cmd = rpc(runner, client.post, f"/api/agents/{name}/start", expect="agent.start")
    assert r.status_code == 200, r.text
    runner.status(name, "running")
    _wait(lambda: client.get(f"/api/agents/{name}").json()["state"] == "running")
    return cmd


def git(root, *args) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout.strip()


# ---------- the presets in the repo ----------
def test_a_new_repo_starts_with_valets_presets(tmp_path):
    repo = DefinitionsRepo(tmp_path / "defs")
    repo.init()
    assert repo.presets() == ["full", "standard", "strict"]
    assert repo.preset_text("full") == (directives.SHIPPED / "full.md").read_text()
    assert git(repo.root, "log", "--format=%s") == "Definitions repo"       # in the first commit
    assert git(repo.root, "status", "--porcelain") == ""
    repo.init()
    assert git(repo.root, "rev-list", "--count", "HEAD") == "1"


def test_a_repo_from_before_gets_them_once(tmp_path):
    root = tmp_path / "defs"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "-c", "user.name=x", "-c", "user.email=x@x", "commit", "-q", "--allow-empty", "-m", "Definitions repo")
    repo = DefinitionsRepo(root)
    repo.init()
    assert repo.presets() == ["full", "standard", "strict"]
    assert git(root, "log", "-1", "--format=%s") == "Add Valet's authority presets"
    # The owner's own changes stay: a preset he deleted isn't put back, as long as authority/ is there.
    repo.delete_preset("strict")
    repo.init()
    assert repo.presets() == ["full", "standard"]
    assert git(root, "log", "-1", "--format=%s") == "Delete authority/strict"


def test_shipped_presets_read_as_described():
    shipped = directives.shipped_presets()
    assert set(shipped) == {"strict", "standard", "full"}
    assert "Ask {owner} first before you spend money" in shipped["full"]
    assert "act, and let Valet decide" in shipped["full"]


# ---------- the environment layer ----------
def test_environment_of_a_full_trust_container_agent():
    d = agent(computer={"level": "container", "image": "valet/agent-full",
                        "mounts": [{"host": "/srv/notes", "path": "/work/notes", "readonly": True}]},
              permissions="trusted", tools={"mcp": ["valet.agents", "github", "notes"]},
              secrets=["GITHUB_TOKEN", "NPM_TOKEN"], directives=["environment"])
    servers = [
        {"server": "valet.agents", "description": "Valet's agents.", "ok": True, "error": None,
         "tools": [{"name": "list_agents", "decision": "allow"}, {"name": "agent_status", "decision": "allow"}]},
        {"server": "github", "description": None, "ok": True, "error": None,
         "tools": [{"name": "get_issue", "decision": "allow"}, {"name": "create_issue", "decision": "allow"},
                   {"name": "delete_repo", "decision": "ask"}]},
        {"server": "notes", "description": None, "ok": False, "error": "notes: can't connect", "tools": []},
    ]
    text = directives.environment_text(d, servers)
    assert text.startswith("# Your environment\n\nYou are `scout`, one of the owner's agents in Valet")
    assert "you run in Claude Code" in text
    assert "Your own Linux container (image valet/agent-full)" in text
    assert "Folders shared from the owner's machine: /work/notes (read-only)." in text
    assert "Internet: open." in text
    assert "## Permission level: trusted\n\nInside your container nothing waits for approval" in text
    assert "destructive ones (sending, deleting, publishing, paying...), which wait for the owner's approval" in text
    assert "- valet.agents: Valet's agents. Runs: list_agents, agent_status.\n" in text
    assert "- github: Runs: get_issue, create_issue. Waits for the owner's approval: delete_repo.\n" in text
    assert "- notes: Unavailable right now.\n" in text and "can't connect" not in text
    assert "(mcp__valet__valet_agents__list_agents)" in text
    assert "Set as environment variables in your computer: GITHUB_TOKEN, NPM_TOKEN." in text
    assert "act and let Valet decide" in text
    assert text.endswith("for what only the owner can decide.\n")


def test_environment_of_a_deepseek_agent_on_the_host_at_ask():
    text = directives.environment_text(agent(harness="deepseek", computer={"level": "host"}, permissions="ask"), [])
    assert "you run in DeepSeek Harness" in text
    assert "directly on the owner's machine, as the runner's user, not in a container" in text
    assert "Reading, searching and keeping track of your work never wait" in text
    assert "Valet gives you no MCP tools." in text and "Valet gives you no secrets." in text
    assert "/valet" not in text and "Internet" not in text


@pytest.mark.parametrize("harness, host, level, says", [
    ("claude", False, "locked", "Read, Glob, Grep, WebSearch, TodoWrite"),
    ("deepseek", False, "locked", "Valet refuses every other tool"),
    ("claude", False, "ask", "Every edit, command or other change waits for the owner's approval"),
    ("claude", True, "trusted", "File edits go through without approval. Each command waits"),
    ("deepseek", True, "trusted", "A command that reaches outside it waits"),
    ("deepseek", False, "unrestricted", "Nothing waits for approval, wherever you run."),
])
def test_what_the_level_means_for_the_harness(harness, host, level, says):
    d = agent(harness=harness, computer={"level": "host" if host else "container"}, permissions=level)
    assert says in directives.environment_text(d)


def test_environment_names_secrets_only_and_a_locked_gateway():
    d = agent(permissions="locked", computer={"level": "container", "network": "none"}, tools={"mcp": ["github"]},
              secrets=["GITHUB_TOKEN", {"vault": "aws/alice", "env": "AWS_KEY"}])
    text = directives.environment_text(d)                   # no listing: servers by name
    assert "Read-only tools run; the others are blocked, and not listed to you." in text
    assert "\n- github\n" in text                    # the gateway is always reachable: no "can't reach them"
    assert "Internet: none. Your network reaches only your model and Valet's gateway" in text
    assert "Read, Glob, Grep, TodoWrite)" in text and "search the web" not in text     # web search is off
    assert "GITHUB_TOKEN, AWS_KEY (the password of vault entry aws/alice)." in text


async def test_a_slow_server_doesnt_hold_up_the_listing(client):
    gw: Gateway = client.app.state.control.gateway
    finished = asyncio.Event()

    async def server_tools(d, server, fresh=True, **kw):
        if server == "slow":
            await asyncio.sleep(0.3)
            finished.set()
        return []
    gw._server_tools = server_tools
    out = await gw.tools(agent(tools={"mcp": ["fast", "slow"]}), timeout=0.05)
    assert [(s["server"], s["ok"], s["error"]) for s in out] == [("fast", True, None),
                                                                ("slow", False, "slow: no answer within 0s")]
    await asyncio.wait_for(finished.wait(), 2)          # it carried on in the background
    await asyncio.sleep(0)
    assert not gw._listing


# ---------- the API ----------
def test_owner_profile_api(client, cfg):
    assert client.get("/api/owner").json()["text"] == ""
    create(client)
    create(client, "plain", directives="[persona.md]")
    r = client.put("/api/owner", json={"text": OWNER})
    assert r.status_code == 200
    body = r.json()
    assert (body["text"], body["used_by"]) == (OWNER, ["scout"])
    assert body["commit"] == git(cfg.definitions, "rev-parse", "HEAD")
    assert git(cfg.definitions, "log", "-1", "--format=%s") == "Edit the owner profile"
    assert (cfg.definitions / "owner.md").read_text() == OWNER
    assert client.put("/api/owner", json={"text": OWNER}).json()["commit"] == body["commit"]    # no change, no commit
    client.cookies.clear()
    assert client.get("/api/owner").status_code == 401


def test_authority_api(client, cfg):
    presets = client.get("/api/authority").json()
    assert [p["name"] for p in presets] == ["full", "standard", "strict"]
    assert presets[0]["text"].startswith("# Standing authority: full trust") and presets[0]["used_by"] == []
    create(client)
    assert client.get("/api/authority/full").json()["used_by"] == ["scout"]
    assert client.get("/api/authority/ghost").status_code == 404
    assert client.get("/api/authority/..%2Fowner").status_code == 404

    r = client.put("/api/authority/github-only", json={"text": "Manage GitHub issues freely.\n"})
    assert r.status_code == 200 and r.json() == {"name": "github-only", "text": "Manage GitHub issues freely.\n",
                                                 "used_by": []}
    assert git(cfg.definitions, "log", "-1", "--format=%s") == "Edit authority/github-only"
    for bad in ("Full", "a_b", "-x", "x" * 32):
        assert client.put(f"/api/authority/{bad}", json={"text": "x"}).status_code == 422, bad

    r = client.delete("/api/authority/full")
    assert r.status_code == 409 and r.json()["detail"] == ("authority/full is used by scout: give it another one "
                                                           "first")
    assert client.delete("/api/authority/github-only").json() == {"ok": True}
    assert client.delete("/api/authority/github-only").status_code == 404
    assert [p["name"] for p in client.get("/api/authority").json()] == ["full", "standard", "strict"]


def test_catalog_suggests_an_authority_per_level(client):
    assert client.get("/api/catalog").json()["authority"] == {"locked": "strict", "ask": "standard",
                                                              "trusted": "full", "unrestricted": "full"}


def test_template_agents_get_the_fuller_list_and_others_keep_theirs(client):
    def made(**body):
        r = client.post("/api/agents", json=body)
        assert r.status_code == 201, r.text
        return r.json()["definition"]["directives"]
    assert made(name="coder", template="coder") == ["owner", "authority/full", "persona.md", "environment"]
    assert made(name="helper", template="assistant") == ["owner", "authority/standard", "persona.md", "environment"]
    assert made(name="locked", template="blank", definition={"permissions": "locked"}) == [
        "owner", "authority/strict", "persona.md", "environment"]
    assert made(name="mine", template="coder", definition={"directives": ["persona.md"]}) == ["persona.md"]
    assert made(name="plain") == ["persona.md"]                                  # the API's plain default
    assert made(name="raw", agent_yaml="name: raw\n") == ["persona.md"]
    assert made(name="copy", copy_from="coder") == ["owner", "authority/full", "persona.md", "environment"]
    # A preset the owner deleted isn't suggested: the template leaves standing authority out.
    client.put("/api/agents/helper/definition", json={"definition": {"directives": ["persona.md"]}})
    assert client.delete("/api/authority/standard").status_code == 200
    assert made(name="next", template="assistant") == ["owner", "persona.md", "environment"]
    r = client.post("/api/agents", json={"name": "bad", "template": "blank",
                                         "definition": {"directives": ["authority/ghost"]}})
    assert r.status_code == 422 and r.json()["errors"] == [
        {"field": "directives", "msg": "authority/ghost: there's no such authority preset"}]


def test_save_refuses_a_preset_that_doesnt_exist(client):
    a = create(client)
    d = {**a["definition"], "directives": ["owner", "authority/ghost"]}
    r = client.put("/api/agents/scout/definition", json={"definition": d})
    assert r.status_code == 422
    assert r.json()["errors"] == [{"field": "directives", "msg": "authority/ghost: there's no such authority preset"}]
    d["directives"] = ["owner", "authority/strict", "environment"]
    assert client.put("/api/agents/scout/definition", json={"definition": d}).status_code == 200


# ---------- the preview ----------
def test_preview_of_the_saved_definition(client):
    create(client, persona="You are Scout.\n")
    client.put("/api/owner", json={"text": OWNER})
    p = client.get("/api/agents/scout/directives").json()
    assert (p["agent"], p["harness"], p["error"]) == ("scout", "claude", None)
    assert [(x["name"], x["file"], x["state"]) for x in p["layers"]] == [
        ("owner", "owner.md", "ok"), ("authority/full", "authority/full.md", "ok"),
        ("persona.md", "persona.md", "ok"), ("environment", "environment.md", "ok")]
    full = say(client.get("/api/authority/full").json()["text"])
    env = p["layers"][3]["text"]
    assert p["text"] == "\n\n".join(t.strip() for t in (OWNER, full, "You are Scout.", env))
    assert env.startswith("# Your environment") and "## Permission level: trusted" in env
    assert "File edits go through without approval" in env          # Claude, on the host, trusted


def test_preview_of_unsaved_settings(client):
    create(client)
    r = client.post("/api/agents/scout/directives/preview", json={
        "definition": {"harness": "deepseek", "permissions": "ask", "computer": {"level": "host"},
                       "directives": ["authority/strict", "persona.md", "memory", "owner"]},
        "persona": "You are Scout, unsaved.\n"})
    p = r.json()
    assert [(x["name"], x["state"]) for x in p["layers"]] == [
        ("authority/strict", "ok"), ("persona.md", "ok"), ("memory", "off"), ("owner", "empty")]
    assert p["harness"] == "deepseek"
    assert p["text"].startswith("# Standing authority: strict") and p["text"].endswith("You are Scout, unsaved.")
    # Nothing was saved.
    assert client.get("/api/agents/scout").json()["definition"]["directives"] == FULL[1:-1].split(", ")

    # A preset that isn't there: shown missing, and the start it would refuse.
    p = client.post("/api/agents/scout/directives/preview",
                    json={"definition": {"directives": ["authority/ghost", "persona.md"]}}).json()
    assert p["layers"][0]["state"] == "missing" and p["error"].startswith("authority/ghost: there's no such")
    assert p["text"] == "You are Scout."

    r = client.post("/api/agents/scout/directives/preview", json={"definition": {"permissions": "sometimes"}})
    assert r.status_code == 422 and r.json()["errors"][0]["field"] == "permissions"
    r = client.post("/api/agents/scout/directives/preview", json={"definition": {"name": "other"}})
    assert r.status_code == 422
    assert client.post("/api/agents/ghost/directives/preview", json={}).status_code == 404
    assert client.post("/api/agents/scout/directives/preview", json={}).json()["layers"][0]["name"] == "owner"


def test_preview_lists_the_gateways_tools(client):
    client.post("/api/agents", json={"name": "scout", "agent_yaml": YAML.format(
        name="scout", harness="claude", level="ask", directives="[environment]") + "tools:\n  mcp: [valet.agents]\n"})
    env = client.get("/api/agents/scout/directives").json()["text"]
    assert ("- valet.agents: Valet's agents, their status, and messages to them. Runs: list_agents, agent_status. "
            "Waits for the owner's approval: send_message.") in env
    assert "Read-only tools run; any other tool waits for the owner's approval." in env


# ---------- starting with them ----------
def test_start_sends_every_layer_and_restart_follows_them(client, connect, rpc):
    create(client)
    create(client, "plain", directives="[persona.md]", persona="You are Plain.\n")
    client.put("/api/owner", json={"text": OWNER})
    with connect() as runner:
        cmd = start(rpc, runner, client)
        files = cmd["files"]
        assert set(files) == {"owner.md", "authority/full.md", "persona.md", "environment.md"}
        assert files["owner.md"] == OWNER and files["persona.md"] == "You are Scout.\n"
        assert files["environment.md"].startswith("# Your environment\n\nYou are `scout`")
        plain = start(rpc, runner, client, "plain")
        assert plain["files"] == {"persona.md": "You are Plain.\n"}         # as before M2a
        restart = lambda n: client.get(f"/api/agents/{n}").json()["restart_needed"]    # noqa: E731
        assert not restart("scout") and not restart("plain")

        # The owner profile changed: the agents that read it need a restart, the others don't.
        client.put("/api/owner", json={"text": OWNER + "He prefers short answers.\n"})
        assert restart("scout") and not restart("plain")
        cmd = start(rpc, runner, client)
        assert "short answers" in cmd["files"]["owner.md"] and not restart("scout")

        # Its authority preset changed: likewise.
        client.put("/api/authority/full", json={"text": "Do anything.\n"})
        assert restart("scout")
        assert start(rpc, runner, client)["files"]["authority/full.md"] == "Do anything.\n"
        client.put("/api/authority/strict", json={"text": "Changed, but nobody uses it.\n"})
        assert not restart("scout")

        # Switching the environment off is a definition change; its text alone (tool lists) is not.
        d = client.get("/api/agents/scout").json()["definition"]
        client.put("/api/agents/scout/definition", json={"definition": {**d, "directives": ["owner", "persona.md"]}})
        assert restart("scout")
        assert set(start(rpc, runner, client)["files"]) == {"owner.md", "persona.md"}


def test_a_missing_preset_refuses_the_start(client, connect, rpc, cfg):
    create(client)
    (cfg.definitions / "authority" / "full.md").unlink()       # gone behind Valet's back (a hand edit)
    with connect():
        r = client.post("/api/agents/scout/start")
    assert r.status_code == 422 and r.json()["detail"].startswith("authority/full: there's no such authority preset")
    a = client.get("/api/agents/scout").json()
    assert (a["desired"], a["state"]) == ("stopped", "stopped")


def test_a_missing_preset_fails_a_reconnect_start(client, connect, rpc, cfg):
    create(client)
    with connect() as runner:
        start(rpc, runner, client)
    (cfg.definitions / "authority" / "full.md").unlink()
    with connect():
        _wait(lambda: client.get("/api/agents/scout").json()["state"] == "failed")
    a = client.get("/api/agents/scout").json()
    assert a["error"].startswith("authority/full: there's no such authority preset") and a["desired"] == "running"


def test_environment_of_an_allowlist_and_of_no_computer():
    d = agent(computer={"level": "container", "network": "allowlist", "allowlist": ["github.com", "pypi.org"]})
    text = directives.environment_text(d)
    assert "Internet: only these sites and their subdomains, through Valet's proxy: github.com, pypi.org." in text
    assert "web search is off" in text
    bare = directives.environment_text(agent(harness="deepseek", computer={"level": "none"}, permissions="trusted",
                                             tools={"mcp": ["github"]}))
    assert "No computer to work in: you have no file, shell or web tools and no workspace." in bare
    assert "your level only decides Valet's tools" in bare and "Inside your container" not in bare
    assert "/valet" not in bare and "Internet:" not in bare
    assert not any(w in bare.split() for w in ("he", "him", "his"))
