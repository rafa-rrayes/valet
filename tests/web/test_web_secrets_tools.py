"""The web UI's secrets and tools: the settings page's Secrets and Tools sections, starts refused for secrets that
aren't set, the "Secrets & tools" screen, and gateway asks in the inbox. Run by their own JS under node with a
stand-in DOM and a fake API (tests/web/settings.js). Skipped without node."""
import copy
import re
import time

from tests.web.test_web_settings import CATALOG, DEFINITION, agent, detail, puts, pytestmark, run  # noqa: F401

NOW = time.time()   # times below are hours old, so "3h ago" holds however long the suite takes

SECRETS = [
    {"name": "GH_TOKEN", "set": True, "updated_at": NOW - 3 * 3600, "used_by": ["scout"], "servers": ["github"]},
    {"name": "NPM_TOKEN", "set": True, "updated_at": NOW - 2 * 3600, "used_by": [], "servers": []},
    {"name": "SLACK_TOKEN", "set": False, "updated_at": None, "used_by": ["coder", "scout"], "servers": []},
    {"name": "NOTES_KEY", "set": False, "updated_at": None, "used_by": [], "servers": ["notes"]},
]

SERVERS = {"servers": [
    {"name": "valet.agents", "kind": "builtin", "builtin": True,
     "description": "Valet's agents and their status (read-only).", "agents": [], "connected": True},
    {"name": "github", "kind": "http", "builtin": False, "description": "GitHub issues and pull requests",
     "url": "https://api.githubcopilot.com/mcp/", "headers": ["Authorization"], "command": None, "args": [],
     "env": [], "secrets": ["GH_TOKEN"], "agents": ["scout"], "connected": True},
    {"name": "notes", "kind": "stdio", "builtin": False, "description": None, "url": None, "headers": [],
     "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/srv/notes"],
     "env": ["LOG_LEVEL", "NOTES_KEY"], "secrets": ["NOTES_KEY"], "agents": [], "connected": False},
], "error": None}


def tool(name, title, decision, **ann):
    return {"name": f"github__{name}", "tool": name, "title": title, "description": f"{title}, on GitHub.\n\nMore.",
            "annotations": ann or None, "decision": decision}


TOOLS = {"agent": "scout", "permissions": "ask", "url": "http://127.0.0.1:8780/mcp/agents/scout", "servers": [
    {"server": "github", "ok": True, "error": None, "tools": [
        tool("get_issue", "Get issue", "allow", readOnlyHint=True),
        tool("create_issue", "Create issue", "ask", destructiveHint=False),
        tool("delete_repo", "Delete repository", "ask")]},
]}


def definition(**over) -> dict:
    d = copy.deepcopy(DEFINITION)
    d.update(over)
    return d


def page(actions=(), d=None, routes=None, agents=None, tools=TOOLS, secrets=SECRETS, servers=SERVERS) -> dict:
    """The settings page of scout, with GET /api/secrets, /api/gateway/servers and its tools answered."""
    d = d or detail()
    r = {"GET /api/agents/scout": {"status": 200, "body": d}, "GET /api/catalog": {"status": 200, "body": CATALOG},
         "GET /api/agents/scout/history": {"status": 200, "body": []},
         "GET /api/secrets": {"status": 200, "body": secrets},
         "GET /api/gateway/servers": {"status": 200, "body": servers},
         "GET /api/agents/scout/tools": {"status": 200, "body": tools}}
    r.update(routes or {})
    (result,) = run({"view": "settings", "name": "scout", "routes": r, "actions": list(actions),
                     "agents": agents if agents is not None else [agent()]})
    return result


def sent(result, method=None) -> list[tuple]:
    """(method, path, body) of every request but the GETs (or of `method` only)."""
    return [(q["method"], q["path"], q["body"]) for q in result["requests"]
            if (q["method"] == method if method else q["method"] != "GET")]


def html_of(result, key: str) -> str:
    """The element with data-row (or data-tool) `key`, as HTML."""
    return result["rows"][key]


# ---------- settings › secrets ----------

def test_secrets_section_shows_each_name_and_whether_it_is_set():
    r = page()
    gh = html_of(r, "GH_TOKEN")
    assert '<span class="badge idle">set</span>' in gh and "Set 3h ago · also used by the github server" in gh
    assert "Change value" in gh and "Remove" in gh
    vault = html_of(r, "vault:aws/alice")                                # its env var, the entry, no value field
    assert "AWS_KEY" in vault and "The password of aws/alice" in vault and "secret-value" not in vault
    assert "secret:NPM_TOKEN" in [c for c in re.findall(r'data-choice="([^"]+)"', r["html"])]   # set elsewhere
    assert 'data-choice="secret:SLACK_TOKEN"' not in r["html"]           # not set: type it to add it
    assert not any(k.startswith("secret-value:") for k in r["controls"])  # no value field until asked for


def test_status_falls_back_to_missing_secrets_without_the_secrets_list():
    a = agent(missing_secrets=["GH_TOKEN"])
    r = page(d=detail(missing_secrets=["GH_TOKEN"]), agents=[a],
             routes={"GET /api/secrets": {"status": 500, "body": {"detail": "boom"}}})
    gh = html_of(r, "GH_TOKEN")
    assert '<span class="badge waiting">not set</span>' in gh and "it can't start until it is" in gh
    assert ">Set value</button>" in gh


def test_add_and_remove_secrets_keeps_vault_entries():
    d = definition()
    assert d["secrets"] == ["GH_TOKEN", {"vault": "aws/alice", "env": "AWS_KEY"}]
    r = page([{"button": "Remove", "row": "GH_TOKEN"}, {"click": "secret:NPM_TOKEN"},
              {"input": "secret-new", "value": " slack-token "}, {"button": "Add"}, {"button": "Save"}],
             d=detail(d), routes={"PUT /api/agents/scout/definition": {"status": 500, "body": {"detail": "x"}}})
    ((_, _, body),) = sent(r, "PUT")
    assert body["definition"]["secrets"] == [{"vault": "aws/alice", "env": "AWS_KEY"}, "NPM_TOKEN", "SLACK_TOKEN"]
    assert {k: v for k, v in body["definition"].items() if k != "secrets"} == \
        {k: v for k, v in d.items() if k != "secrets"}                   # nothing else moved
    # SLACK_TOKEN isn't set, so its value field opened when it was added; it's empty
    assert r["controls"]["secret-value:SLACK_TOKEN"]["value"] == ""
    assert r["controls"]["secret-new"]["value"] == ""


def test_a_new_name_is_checked_here_and_reserved_ones_by_the_server():
    bad = page([{"input": "secret-new", "value": "9 lives"}, {"button": "Add"}])
    assert bad["errors"] == {"secret-new": "Use capital letters, digits and _, starting with a letter, like GITHUB_TOKEN."}
    twice = page([{"input": "secret-new", "value": "gh_token"}, {"enter": "secret-new"}])
    assert twice["errors"] == {"secret-new": "It already has GH_TOKEN."}
    assert sent(bad) == sent(twice) == []

    reserved = "VALET_X: reserved: Valet, a harness or the computer sets it"
    r = page([{"input": "secret-new", "value": "VALET_X"}, {"button": "Add"},
              {"input": "secret-value:VALET_X", "value": "v"}, {"button": "Save value", "row": "VALET_X"},
              {"button": "Save"}],
             routes={"PUT /api/secrets/VALET_X": {"status": 422, "body": {"detail": reserved}},
                     "PUT /api/agents/scout/definition": {"status": 422, "body": {
                         "detail": "invalid", "errors": [{"field": "secrets", "msg": reserved}]}}})
    assert r["errors"]["secret:VALET_X"] == reserved                     # under its value field
    assert r["errors"]["secrets"] == reserved                            # and under the list, from the save


def test_set_value_is_write_only_and_trimmed():
    r = page([{"button": "Change value", "row": "GH_TOKEN"}])
    assert r["controls"]["secret-value:GH_TOKEN"]["value"] == ""         # never prefilled
    empty = page([{"button": "Change value", "row": "GH_TOKEN"}, {"button": "Save value"}])
    assert empty["errors"] == {"secret:GH_TOKEN": "Paste the value first."} and sent(empty) == []

    saved = {**SECRETS[0], "updated_at": time.time()}
    r = page([{"button": "Change value", "row": "GH_TOKEN"}, {"input": "secret-value:GH_TOKEN", "value": "  ghp_abc\n"},
              {"enter": "secret-value:GH_TOKEN"}],
             routes={"PUT /api/secrets/GH_TOKEN": {"status": 200, "body": saved},
                     "GET /api/secrets": [{"status": 200, "body": SECRETS},       # the page's, then the refetch
                                          {"status": 200, "body": [saved, *SECRETS[1:]]}]})
    assert sent(r) == [("PUT", "/api/secrets/GH_TOKEN", {"value": "ghp_abc"})]
    assert "secret-value:GH_TOKEN" not in r["controls"]                 # closed, and the value gone with it
    assert "Set just now" in html_of(r, "GH_TOKEN")
    assert "Unsaved changes" not in r["text"]                            # a value isn't part of the definition


def test_secret_value_trims_a_single_line_only():
    for raw, expected in [("  tok \n", "tok"), ("tok", "tok"), ("  ", ""), ("-----BEGIN-----\nabc\n-----END-----\n",
                                                                               "-----BEGIN-----\nabc\n-----END-----\n")]:
        (r,) = run({"call": "secretValue", "args": [raw]})
        assert r["result"] == expected
    for typed, expected in [("github token", "GITHUB_TOKEN"), (" npm-token ", "NPM_TOKEN"), ("A", "A")]:
        (r,) = run({"call": "secretName", "args": [typed]})
        assert r["result"] == expected


# ---------- starts refused for secrets ----------

MISSING = {"status": 422, "body": {"detail": "secret GH_TOKEN isn't set: add it to Valet's secrets",
                                   "missing_secrets": ["GH_TOKEN"]}}


def test_save_and_restart_asks_for_a_missing_secret_then_restarts():
    edited = definition(permissions="trusted")
    routes = {"PUT /api/agents/scout/definition": {"status": 200, "body": detail(edited)},
              "POST /api/agents/scout/start": [MISSING, {"status": 200, "body": agent()}],
              "PUT /api/secrets/GH_TOKEN": {"status": 200, "body": SECRETS[0]}}
    asked = page([{"click": "permissions:trusted"}, {"button": "Save & restart"}], routes=copy.deepcopy(routes))
    assert "Saved. It can't start until this secret is set." in asked["text"]
    assert asked["controls"]["secret-value:GH_TOKEN"]["value"] == ""
    done = page([{"click": "permissions:trusted"}, {"button": "Save & restart"},
                 {"input": "secret-value:GH_TOKEN", "value": "ghp_1 "}, {"button": "Set & restart"}],
                routes=copy.deepcopy(routes))
    assert [(m, p) for m, p, _ in sent(done)] == [
        ("PUT", "/api/agents/scout/definition"), ("POST", "/api/agents/scout/start"),
        ("PUT", "/api/secrets/GH_TOKEN"), ("POST", "/api/agents/scout/start")]
    assert ("PUT", "/api/secrets/GH_TOKEN", {"value": "ghp_1"}) in sent(done)
    assert "Restarted." in done["text"] and "until this secret is set" not in done["text"]


def agents_list(actions, routes, agents) -> dict:
    (result,) = run({"view": "agents", "routes": routes, "actions": actions, "agents": agents})
    return result


STOPPED = agent(desired="stopped", state="stopped", status="stopped", missing_secrets=["GH_TOKEN"])


def test_start_from_the_list_asks_for_the_secret_and_survives_the_redraw():
    routes = {"POST /api/agents/scout/start": [MISSING, {"status": 200, "body": agent()}],
              "GET /api/agents/scout": {"status": 200, "body": STOPPED},
              "PUT /api/secrets/GH_TOKEN": {"status": 200, "body": SECRETS[0]}}
    asked = agents_list([{"button": "Start"}], copy.deepcopy(routes), [STOPPED])
    # the refused start refetches the agent, and its frame redraws every card: the fields must still be there
    assert ("GET", "/api/agents/scout") in [(q["method"], q["path"]) for q in asked["requests"]]
    assert "It can't start until this secret is set." in asked["text"] and "secret-value:GH_TOKEN" in asked["controls"]
    done = agents_list([{"button": "Start"}, {"input": "secret-value:GH_TOKEN", "value": "t"}, {"button": "Set & start"}],
                       copy.deepcopy(routes), [STOPPED])
    assert [(m, p) for m, p, _ in sent(done)] == [
        ("POST", "/api/agents/scout/start"), ("PUT", "/api/secrets/GH_TOKEN"), ("POST", "/api/agents/scout/start")]
    assert "secret-value:GH_TOKEN" not in done["controls"]


def test_needs_warning_sets_without_starting():
    r = agents_list([{"button": "Set"}, {"input": "secret-value:GH_TOKEN", "value": "t"}, {"button": "Save"}],
                    {"PUT /api/secrets/GH_TOKEN": {"status": 200, "body": SECRETS[0]}}, [STOPPED])
    assert "Needs GH_TOKEN" in r["text"]
    assert sent(r) == [("PUT", "/api/secrets/GH_TOKEN", {"value": "t"})]


def test_a_refusal_without_missing_secrets_is_just_its_message():
    vault = {"status": 422, "body": {"detail": "vault entries (aws/alice) arrive in M3: list secrets by name for now",
                                     "missing_secrets": []}}
    stopped = {**STOPPED, "missing_secrets": []}
    r = agents_list([{"button": "Start"}], {"POST /api/agents/scout/start": vault,
                                            "GET /api/agents/scout": {"status": 200, "body": stopped}}, [stopped])
    assert "vault entries (aws/alice) arrive in M3" in r["text"]
    assert not any(k.startswith("secret-value:") for k in r["controls"])


def test_agent_page_says_what_it_needs():
    a = agent(desired="stopped", state="stopped", status="stopped", missing_secrets=["GH_TOKEN", "NPM_TOKEN"])
    (r,) = run({"view": "agent", "name": "scout", "agents": [a], "actions": [], "routes": {
        "GET /api/agents/scout": {"status": 200, "body": detail(missing_secrets=["GH_TOKEN", "NPM_TOKEN"],
                                                                desired="stopped", state="stopped", status="stopped")},
        "GET /api/agents/scout/sessions": {"status": 200, "body": []}}})
    assert "Needs GH_TOKEN and NPM_TOKEN" in r["text"]


# ---------- settings › tools ----------

def test_tool_switches_edit_tools_mcp():
    r = page()
    assert r["controls"]["mcp:github"]["checked"] is True
    assert r["controls"]["mcp:valet.agents"]["checked"] is False and r["controls"]["mcp:notes"]["checked"] is False
    r = page([{"check": "mcp:valet.agents", "checked": True}, {"check": "mcp:github", "checked": False},
              {"button": "Save"}], routes={"PUT /api/agents/scout/definition": {"status": 500, "body": {"detail": "x"}}})
    ((_, _, body),) = sent(r, "PUT")
    assert body["definition"]["tools"] == {"mcp": ["valet.agents"], "skills": ["*"], "rules": {}}
    assert {k: v for k, v in body["definition"].items() if k != "tools"} == \
        {k: v for k, v in DEFINITION.items() if k != "tools"}


def test_off_and_on_again_is_no_change():
    d = definition(tools={"mcp": ["github", "valet.agents"], "skills": []})
    r = page([{"check": "mcp:github", "checked": False}, {"check": "mcp:github", "checked": True}], d=detail(d))
    assert "Unsaved changes" not in r["text"]


def test_each_tool_says_what_a_call_does():
    tools = copy.deepcopy(TOOLS)
    tools["servers"][0]["tools"][2]["decision"] = "deny"
    r = page(tools=tools)
    gh = html_of(r, "github")
    assert "3 tools: 1 run, 1 ask you, 1 blocked" in gh
    assert re.search(r'Get issue</span><span class="badge runs">runs</span>', html_of(r, "github__get_issue"))
    assert '<span class="badge asks">asks you</span>' in html_of(r, "github__create_issue")
    assert '<span class="badge blocked">blocked</span>' in html_of(r, "github__delete_repo")
    assert "Create issue, on GitHub." in gh and "More." not in gh      # the description's first paragraph
    assert sent(r) == []


def test_an_unreachable_server_says_so_and_a_new_one_waits_for_the_save():
    d = definition(tools={"mcp": ["github", "nope"], "skills": []})
    tools = {**TOOLS, "servers": [TOOLS["servers"][0], {"server": "nope", "ok": False, "tools": [],
                                                        "error": "no server named nope in mcp.yaml"}]}
    r = page([{"check": "mcp:notes", "checked": True}], d=detail(d), tools=tools)
    assert "Can't use it right now: no server named nope in mcp.yaml" in html_of(r, "nope")
    assert "Valet has no tool server by this name." in html_of(r, "nope")
    assert "Save to see its tools." in html_of(r, "notes")


TRUSTED = copy.deepcopy(TOOLS)
TRUSTED["permissions"] = "trusted"
TRUSTED["servers"][0]["tools"][1]["decision"] = "allow"     # create_issue: additive, so it runs when trusted


def test_an_unsaved_level_previews_its_decisions():
    preview = {"GET /api/agents/scout/tools?permissions=trusted": {"status": 200, "body": TRUSTED}}
    r = page([{"click": "permissions:trusted"}], routes=preview)
    assert "/api/agents/scout/tools?permissions=trusted" in [q["path"] for q in r["requests"]]
    assert "What each tool does below is for Trusted, which isn't saved yet." in r["text"]
    assert '<span class="badge runs">runs</span>' in html_of(r, "github__create_issue")
    assert "3 tools: 2 run, 1 ask you" in html_of(r, "github") and "toolset stale" not in r["html"]
    back = page([{"click": "permissions:trusted"}, {"click": "permissions:ask"}], routes=preview)
    assert [q["path"] for q in back["requests"]].count("/api/agents/scout/tools") == 2   # the saved level again
    assert "isn't saved yet" not in back["text"] and "asks you" in html_of(back, "github__create_issue")
    assert puts(r) == puts(back) == []


def test_decisions_dim_until_the_new_level_is_in():
    # the preview never answers here (no route: 404): the last answer stays, dimmed, and the error says why
    r = page([{"click": "permissions:trusted"}])
    assert "not found" in html_of(r, "github")


def test_tools_wait_for_their_listing():
    r = page(routes={"GET /api/agents/scout/tools": {"status": 502, "body": {"detail": "gateway: timed out"}}})
    assert "gateway: timed out" in html_of(r, "github")


# ---------- the secrets & tools screen ----------

CALLS = [
    {"id": 41, "agent": "scout", "server": "github", "tool": "create_issue", "arguments": {"title": "Bug"},
     "decision": "ask", "outcome": "rejected", "error": None, "started_at": NOW - 60, "ended_at": NOW - 50,
     "duration": 10.0},
    {"id": 40, "agent": "scout", "server": "github", "tool": "get_issue", "arguments": {"number": 7},
     "decision": "allow", "outcome": "ok", "error": None, "started_at": NOW - 120, "ended_at": NOW - 120,
     "duration": 0.25},
]
OLDER = [{"id": 12, "agent": "coder", "server": "notes", "tool": "write_file", "arguments": {},
          "decision": "deny", "outcome": "denied", "error": None, "started_at": NOW - 9000, "ended_at": NOW - 9000,
          "duration": 0.0},
         {"id": 11, "agent": "coder", "server": "", "tool": "made_up", "arguments": "{\"x\": 1",
          "decision": None, "outcome": "unknown", "error": "no such tool", "started_at": NOW - 9100,
          "ended_at": NOW - 9100, "duration": 0.0}]


def screen(actions=(), routes=None) -> dict:
    r = {"GET /api/secrets": {"status": 200, "body": SECRETS},
         "GET /api/gateway/servers": {"status": 200, "body": SERVERS},
         "GET /api/gateway/calls?limit=20": {"status": 200, "body": {"calls": CALLS, "more": True}},
         "GET /api/gateway/calls?limit=20&before=40": {"status": 200, "body": {"calls": OLDER, "more": False}},
         "GET /api/gateway/registry": {"status": 200, "body": {"text": "servers: {}\n", "commit": "abc"}}}
    r.update(routes or {})
    (result,) = run({"view": "secrets", "routes": r, "actions": list(actions),
                     "agents": [agent(), agent(name="coder", display_name="Coder", emoji=None)]})
    return result


def test_screen_lists_secrets_and_servers():
    r = screen()
    assert "Used by Scout and the github server · set 3h ago" in html_of(r, "GH_TOKEN")
    assert "Nothing uses it · set 2h ago" in html_of(r, "NPM_TOKEN")
    assert "Used by the notes server · it can't connect until it's set" in html_of(r, "NOTES_KEY")
    slack = html_of(r, "SLACK_TOKEN")
    assert "not set" in slack and "Used by Coder and Scout · they can't start until it's set" in slack
    assert ">Set value</button>" in slack and "Delete" not in slack     # nothing to delete
    gh = html_of(r, "github")
    assert '<span class="badge idle">connected</span>' in gh and "https://api.githubcopilot.com/mcp/" in gh
    assert "Headers: Authorization" in gh and "Secrets: GH_TOKEN<" in gh and "Remote (HTTP) · used by Scout" in gh
    notes = html_of(r, "notes")
    assert "not connected" in notes and "npx -y @modelcontextprotocol/server-filesystem /srv/notes" in notes
    assert "Environment: LOG_LEVEL, NOTES_KEY" in notes and "Secrets: NOTES_KEY (not set)" in notes
    assert "no agent uses it yet" in notes
    assert "built in" in html_of(r, "valet.agents")


def test_delete_warns_when_agents_use_it():
    r = screen([{"button": "Delete", "row": "GH_TOKEN"}])
    assert ("Scout uses GH_TOKEN. It keeps it until it stops, then can't start again until it's set. The github "
            "server stops working until it's set again. Delete it?" in r["text"])
    assert sent(r) == []
    r = screen([{"button": "Delete", "row": "GH_TOKEN"}, {"button": "Delete", "within": "confirm"}],
               {"DELETE /api/secrets/GH_TOKEN": {"status": 200, "body": {"ok": True}}})
    assert sent(r) == [("DELETE", "/api/secrets/GH_TOKEN", None)]
    assert [q["path"] for q in r["requests"]].count("/api/secrets") == 2   # listed again after
    unused = screen([{"button": "Delete", "row": "NPM_TOKEN"}])
    assert "Delete NPM_TOKEN? Nobody can read it back" in unused["text"]


def test_new_secret_and_change_value_on_the_screen():
    r = screen([{"button": "New secret"}, {"input": "secret-new", "value": "pypi token"},
                {"input": "secret-value:new", "value": " pypi-xyz "}, {"button": "Save", "within": "newsecret"}],
               {"PUT /api/secrets/PYPI_TOKEN": {"status": 200, "body": {}}})
    assert sent(r) == [("PUT", "/api/secrets/PYPI_TOKEN", {"value": "pypi-xyz"})]
    assert "secret-value:new" not in r["controls"]                      # the form closed (and emptied)
    taken = screen([{"button": "New secret"}, {"input": "secret-new", "value": "npm_token"},
                    {"input": "secret-value:new", "value": "x"}, {"button": "Save", "within": "newsecret"}])
    assert taken["errors"] == {"secret-new": "NPM_TOKEN is already set: use Change value on it below."}
    change = screen([{"button": "Set value", "row": "SLACK_TOKEN"}, {"input": "secret-value:SLACK_TOKEN", "value": "s"},
                     {"button": "Save value"}], {"PUT /api/secrets/SLACK_TOKEN": {"status": 200, "body": {}}})
    assert sent(change) == [("PUT", "/api/secrets/SLACK_TOKEN", {"value": "s"})]


def test_servers_file_editor_shows_the_reason_inline():
    reason = "mcp.yaml: servers.Bad: server name 'Bad': lowercase letters, digits, '-' and single '_'"
    r = screen([{"open": "advanced"}, {"input": "file:mcp.yaml", "value": "servers:\n  Bad: {url: https://x}\n"},
                {"button": "Save file"}], {"PUT /api/gateway/registry": {"status": 422, "body": {"detail": reason}}})
    assert r["errors"] == {"registry": reason}
    assert r["controls"]["file:mcp.yaml"]["value"] == "servers:\n  Bad: {url: https://x}\n"   # kept for fixing
    servers = {"servers": SERVERS["servers"][:1], "error": None}
    ok = screen([{"open": "advanced"}, {"input": "file:mcp.yaml", "value": "servers: {}\n"}, {"button": "Save file"}],
                {"PUT /api/gateway/registry": {"status": 200, "body": {"commit": "f00ba12345", **servers}}})
    assert sent(ok) == [("PUT", "/api/gateway/registry", {"text": "servers: {}\n"})]
    assert "Saved (f00ba12). It applies to the next tool call." in ok["text"]
    assert 'data-row="github"' not in ok["html"]                        # the list is the saved file's
    assert [q["path"] for q in ok["requests"]].count("/api/secrets") == 2   # the secrets it names, listed again


def test_servers_file_with_an_error_says_so():
    r = screen(routes={"GET /api/gateway/servers": {"status": 200, "body": {
        "servers": SERVERS["servers"][:1], "error": "mcp.yaml is not valid YAML: oops"}}})
    assert "mcp.yaml has an error, so its servers can't be used. mcp.yaml is not valid YAML: oops" in r["text"]


def test_recent_calls_and_more():
    r = screen()
    assert "<span>Scout · </span><span class=\"mono\">github › create_issue</span>" in html_of(r, "41")
    assert '<span class="badge rejected">rejected</span>' in html_of(r, "41")
    assert "asked you · 10.0 s" in html_of(r, "41") and "250 ms" in html_of(r, "40")
    assert '<details class="args"><summary>Arguments</summary>' in html_of(r, "41")
    assert ">More</button>" in r["html"]
    more = screen([{"button": "More"}])
    assert "/api/gateway/calls?limit=20&before=40" in [q["path"] for q in more["requests"]]
    assert '<span class="badge rejected">blocked</span>' in html_of(more, "12")
    assert "made_up" in html_of(more, "11") and "no such tool" in html_of(more, "11")
    assert ">More</button>" not in more["html"]


# ---------- gateway asks in the inbox ----------

def tool_item(**over) -> dict:
    item = {"id": "i1", "kind": "tool", "agent": "scout", "session_id": None, "turn_id": None, "status": "pending",
            "title": "github: Create issue", "created_at": NOW - 30, "answered_at": None, "answer": None,
            "tool_call": {"toolCallId": "gateway-i1", "title": "github: Create issue", "kind": "other",
                          "rawInput": {"title": "Bug", "body": "x" * 5000}},
            "options": [{"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"},
                        {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"}],
            "tool": {"server": "github", "name": "create_issue", "title": "Create issue",
                     "description": "Open a new issue.\n\nNeeds a title.", "arguments": {"title": "Bug", "body": "x" * 5000},
                     "annotations": {"destructiveHint": False}}}
    item.update(over)
    return item


def test_tool_item_in_the_inbox():
    answered = tool_item(status="answered", answer={"outcome": "selected", "optionId": "allow_once"})
    (r,) = run({"view": "inbox", "agents": [agent()], "inbox": [tool_item()], "actions": [{"button": "Allow once"}],
                "routes": {"GET /api/inbox": {"status": 200, "body": [tool_item()]},
                           "POST /api/inbox/i1/answer": {"status": 200, "body": answered}}})
    assert sent(r) == [("POST", "/api/inbox/i1/answer", {"option_id": "allow_once"})]
    (r,) = run({"view": "inbox", "agents": [agent()], "inbox": [tool_item()], "actions": [],
                "routes": {"GET /api/inbox": {"status": 200, "body": [tool_item()]}}})
    card = re.search(r'<div class="perm toolask">.*', r["html"], re.DOTALL).group(0)
    assert ('<div class="title">Scout wants to use <span class="tool-name">github › Create issue</span></div>'
            in card)
    assert "Open a new issue." in card and "Needs a title." not in card
    args = re.search(r'<details class="args"><summary>Arguments</summary><pre>(.*?)</pre></details>', card,
                     re.DOTALL).group(1)                                # folded: no `open`
    assert args.startswith("{\n  &quot;title&quot;: &quot;Bug&quot;") and args.endswith("\n…") and len(args) < 2200
    assert re.findall(r"<button[^>]*>([^<]*)</button>", card) == ["Allow once", "Reject"]
