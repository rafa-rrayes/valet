"""The web UI's skills: the settings page's Skills section, the library (#/skills) and the skill editor (a shared
skill, or an agent's own). Run by their own JS under node with a stand-in DOM and a fake API (tests/web/settings.js).
Skipped without node."""
import copy

from tests.web.test_web_settings import CATALOG, DEFINITION, agent, detail, pytestmark, run  # noqa: F401

LIBRARY = [
    {"name": "broken", "description": None, "files": ["SKILL.md"], "size": 14, "used_by": [],
     "error": "SKILL.md must start with YAML frontmatter"},
    {"name": "code-review", "description": "Use when reviewing a pull request.\n\nMore detail here.",
     "files": ["SKILL.md", "checklist.md"], "size": 900, "error": None, "used_by": ["coder", "scout"]},
    {"name": "release-notes", "description": "Use when writing release notes.", "files": ["SKILL.md"], "size": 200,
     "error": None, "used_by": []},
]

OWN = [
    {"name": "house-style", "description": "Use when writing anything for Alice.", "files": ["SKILL.md"], "size": 100,
     "error": None, "used_by": ["scout"], "replaces_shared": False},
    {"name": "release-notes", "description": "Use when writing Valet's release notes.", "files": ["SKILL.md"],
     "size": 100, "error": None, "used_by": ["scout"], "replaces_shared": True},
]

MINE = {"agent": "scout", "all": True, "own": OWN,
        "skills": [{"name": "code-review", "from": "shared"}, {"name": "house-style", "from": "agent"},
                   {"name": "release-notes", "from": "agent"}],
        "missing": [], "problems": ["broken: SKILL.md must start with YAML frontmatter"]}

SKILL = {"name": "code-review", "description": "Use when reviewing a pull request.", "files": ["SKILL.md", "checklist.md"],
         "size": 900, "error": None, "used_by": ["coder", "scout"], "agent": None,
         "instructions": "1. Read the diff.\n2. Check the tests.", "extra": {"license": "MIT"},
         "content": {"SKILL.md": "---\nname: code-review\n---\n", "checklist.md": "- tests\n- docs\n"},
         "commit": "0123456789abcdef"}

AGENTS = [agent(), agent(name="coder", display_name="Coder", emoji=None)]


def definition(skills) -> dict:
    d = copy.deepcopy(DEFINITION)
    d["tools"]["skills"] = skills
    return d


def page(actions=(), d=None, routes=None, mine=MINE, library=LIBRARY) -> dict:
    """scout's settings page, with the skills routes answered."""
    r = {"GET /api/agents/scout": {"status": 200, "body": d or detail()},
         "GET /api/catalog": {"status": 200, "body": CATALOG},
         "GET /api/agents/scout/history": {"status": 200, "body": []},
         "GET /api/skills": {"status": 200, "body": library},
         "GET /api/agents/scout/skills": {"status": 200, "body": mine}}
    r.update(routes or {})
    (result,) = run({"view": "settings", "name": "scout", "routes": r, "actions": list(actions), "agents": AGENTS})
    return result


def library_page(actions=(), routes=None) -> dict:
    r = {"GET /api/skills": {"status": 200, "body": LIBRARY}}
    r.update(routes or {})
    (result,) = run({"view": "skills", "routes": r, "actions": list(actions), "agents": AGENTS})
    return result


def editor(actions=(), skill=SKILL, agent_name=None, routes=None, agents=AGENTS) -> dict:
    path = f"/api/agents/{agent_name}/skills/{skill['name']}" if agent_name else f"/api/skills/{skill['name']}"
    r = {f"GET {path}": {"status": 200, "body": skill}}
    r.update(routes or {})
    (result,) = run({"view": "skill", "name": skill["name"], "agent": agent_name, "routes": r,
                     "actions": list(actions), "agents": agents})
    return result


def sent(result, method) -> list[tuple[str, object]]:
    return [(q["path"], q["body"]) for q in result["requests"] if q["method"] == method]


# ---------- getting there ----------

def test_the_agent_list_links_to_the_library():
    (r,) = run({"view": "agents", "agents": AGENTS})
    assert 'href="#/skills"' in r["html"] and "Instructions your agents load" in r["text"]


def test_routes():
    jobs = [{"view": "route", "hash": h, "routes": {}, "agents": AGENTS, "actions": []}
            for h in ("#/skills", "#/skills/code-review", "#/a/scout/skills/house-style")]
    lib, shared, own = run(*jobs)
    assert lib["viewClass"] == "skills" and ("GET", "/api/skills") in [(q["method"], q["path"]) for q in lib["requests"]]
    assert shared["viewClass"] == "skill-view" and shared["requests"][0]["path"] == "/api/skills/code-review"
    assert "No skill named code-review." in shared["text"]                      # the fake API had none
    assert own["requests"][0]["path"] == "/api/agents/scout/skills/house-style"
    assert 'href="#/a/scout/settings"' in own["html"] and "Scout has no skill named house-style." in own["text"]


# ---------- the library ----------

def test_the_library_lists_each_skill():
    r = library_page()
    review = r["rows"]["code-review"]
    assert review.startswith('<a class="skill row" href="#/skills/code-review"')
    assert "Use when reviewing a pull request." in review and "More detail" not in review
    assert "Used by Coder and Scout · 1 more file" in review
    assert "No agent uses it" in r["rows"]["release-notes"]
    assert "It has a problem, so no agent gets it: SKILL.md must start" in r["rows"]["broken"]


def test_an_empty_library():
    assert "No skills yet." in library_page(routes={"GET /api/skills": {"status": 200, "body": []}})["text"]


def test_a_new_shared_skill():
    made = {**SKILL, "name": "pdf-forms", "used_by": []}
    r = library_page([{"button": "New skill"}, {"input": "skill-new-name", "value": " PDF forms "},
                      {"input": "skill-new-description", "value": "  Use when filling a PDF form.\n"},
                      {"input": "skill-new-instructions", "value": "Use pdftk."}, {"button": "Create"}],
                     routes={"GET /api/skills": [{"status": 200, "body": LIBRARY},
                                                 {"status": 200, "body": [*LIBRARY, {**LIBRARY[2], "name": "pdf-forms"}]}],
                             "POST /api/skills": {"status": 201, "body": made}})
    assert sent(r, "POST") == [("/api/skills", {"name": "pdf-forms", "description": "Use when filling a PDF form.",
                                                "instructions": "Use pdftk."})]
    assert "pdf-forms" in r["rows"] and "skill-new-name" not in r["controls"]     # listed, and the form closed
    assert [q["path"] for q in r["requests"] if q["method"] == "GET"] == ["/api/skills", "/api/skills"]


def test_a_new_skill_is_checked_before_it_is_sent():
    base = [{"button": "New skill"}]
    jobs = [
        base + [{"button": "Create"}],
        base + [{"input": "skill-new-name", "value": "pdf!"}, {"button": "Create"}],
        base + [{"input": "skill-new-name", "value": "a--b"}, {"button": "Create"}],
        base + [{"input": "skill-new-name", "value": "pdf"}, {"input": "skill-new-description", "value": " "},
                {"button": "Create"}],
    ]
    out = run(*({"view": "skills", "routes": {"GET /api/skills": {"status": 200, "body": LIBRARY}}, "actions": a,
                 "agents": AGENTS} for a in jobs))
    assert [r["errors"]["skill-new"] for r in out] == [
        "Give it a name, like code-review.",
        "Use lowercase letters and digits, with single dashes between words, like code-review.",
        "Use lowercase letters and digits, with single dashes between words, like code-review.",
        "Say when to use it: that's what the agent goes by."]
    assert not any(sent(r, "POST") for r in out)


def test_a_refused_new_skill_says_why_and_stays_open():
    r = library_page([{"button": "New skill"}, {"input": "skill-new-name", "value": "code-review"},
                      {"input": "skill-new-description", "value": "Again."}, {"button": "Create"}],
                     routes={"POST /api/skills": {"status": 409,
                                                  "body": {"detail": "there's already a skill named code-review"}}})
    assert r["errors"]["skill-new"] == "there's already a skill named code-review"
    assert r["controls"]["skill-new-name"]["value"] == "code-review"


# ---------- the settings page ----------

def test_the_skills_section_with_all_shared_skills():
    r = page()
    c = r["controls"]
    assert c["skills:*"]["checked"] is True
    for n in ("broken", "code-review", "release-notes"):              # each on, and locked on
        assert c[f"skill:{n}"]["checked"] is True and c[f"skill:{n}"]["disabled"] is True
    assert "Its own skill of this name is used instead." in r["rows"]["skill:release-notes"]
    assert "It has a problem, so it's left out: SKILL.md must start" in r["rows"]["skill:broken"]
    assert "Use when reviewing a pull request." in r["rows"]["skill:code-review"]
    assert 'href="#/a/scout/skills/house-style"' in r["rows"]["own:house-style"]
    assert "Used instead of the shared skill of this name." in r["rows"]["own:release-notes"]
    assert "Left out, until fixed: broken: SKILL.md must start" in r["text"]
    assert 'href="#/skills"' in r["html"] and "Manage the skill library" in r["text"]


def test_all_off_keeps_each_skill_it_had_then_one_goes():
    saved = definition(["code-review", "release-notes"])
    r = page([{"check": "skills:*", "checked": False}],)
    assert r["controls"]["skill:code-review"] == {"value": "", "checked": True, "disabled": False}
    r = page([{"check": "skills:*", "checked": False}, {"check": "skill:broken", "checked": False}, {"button": "Save"}],
             routes={"PUT /api/agents/scout/definition": {"status": 200, "body": detail(saved)}})
    assert [b["definition"]["tools"]["skills"] for _, b in sent(r, "PUT")] == [["code-review", "release-notes"]]


def test_picking_skills_by_name():
    d = detail(definition(["release-notes", "gone"]))
    r = page(d=d)
    c = r["controls"]
    assert c["skills:*"]["checked"] is False and c["skill:release-notes"]["checked"] is True
    assert c["skill:code-review"]["checked"] is False and c["skill:code-review"]["disabled"] is False
    assert c["skill:gone"]["checked"] is True
    assert "The library has no skill by this name." in r["rows"]["skill:gone"]
    put = {"PUT /api/agents/scout/definition": {"status": 200, "body": d}}
    r = page([{"check": "skill:code-review", "checked": True}, {"check": "skill:gone", "checked": False},
              {"check": "skill:release-notes", "checked": False}, {"check": "skill:release-notes", "checked": True},
              {"button": "Save"}], d=d, routes=put)
    assert [b["definition"]["tools"]["skills"] for _, b in sent(r, "PUT")] == [["release-notes", "code-review"]]
    # All on, then off again: back to the saved picks, so nothing to save.
    r = page([{"check": "skills:*", "checked": True}], d=d)
    assert r["controls"]["skill:code-review"]["disabled"] is True and "Unsaved changes" in r["text"]
    r = page([{"check": "skills:*", "checked": True}, {"check": "skills:*", "checked": False}], d=d)
    assert "Unsaved changes" not in r["text"] and r["controls"]["skill:code-review"]["checked"] is False


def test_a_new_skill_of_its_own():
    made = {**SKILL, "name": "notes", "agent": "scout", "used_by": ["scout"]}
    more = {**MINE, "own": [*OWN, {**OWN[0], "name": "notes"}]}
    r = page([{"button": "New skill"}, {"input": "skill-new-name", "value": "notes"},
              {"input": "skill-new-description", "value": "Use when taking notes."}, {"button": "Create"}],
             routes={"POST /api/agents/scout/skills": {"status": 201, "body": made},
                     "GET /api/agents/scout/skills": [{"status": 200, "body": MINE}, {"status": 200, "body": more}]})
    assert sent(r, "POST") == [("/api/agents/scout/skills", {"name": "notes", "description": "Use when taking notes.",
                                                             "instructions": ""})]
    assert "own:notes" in r["rows"] and "skill-new-name" not in r["controls"]


def test_a_skills_error_from_saving_shows_in_the_section():
    errors = [{"field": "tools.skills.0", "msg": "'Bad Name': a shared skill's name"}]
    r = page([{"check": "skills:*", "checked": False}, {"button": "Save"}],
             routes={"PUT /api/agents/scout/definition": {"status": 422, "body": {"detail": "x", "errors": errors}}})
    assert r["errors"]["tools.skills"] == "'Bad Name': a shared skill's name"


def test_the_section_says_when_the_library_cant_load():
    r = page(routes={"GET /api/skills": {"status": 500, "body": {"detail": "boom"}}})
    assert "boom" in r["text"] and "skill:code-review" not in r["controls"]
    assert "own:house-style" in r["rows"]                      # its own still listed


# ---------- the editor ----------

def test_the_editor_shows_a_shared_skill():
    r = editor()
    c = r["controls"]
    assert c["description"]["value"] == "Use when reviewing a pull request."
    assert c["instructions"]["value"] == "1. Read the diff.\n2. Check the tests."
    assert "Shared skill · used by Coder and Scout" in r["text"]
    assert "Its SKILL.md also sets license: kept as they are." in r["text"]
    assert "checklist.md" in r["rows"]["file:checklist.md"] and "2 lines" in r["rows"]["file:checklist.md"]
    assert "file:SKILL.md" not in r["rows"] and "file:checklist.md" not in c          # closed until Edit
    assert 'href="#/skills"' in r["html"]


def test_the_editor_saves_only_what_changed():
    after = {**SKILL, "description": "Use when reviewing code."}
    r = editor([{"input": "description", "value": "Use when reviewing code.\n"},
                {"button": "Edit", "row": "file:checklist.md"}, {"input": "file:checklist.md", "value": "- tests\n"},
                {"input": "file-new", "value": "scripts/check.sh"}, {"button": "Add"},
                {"input": "file:scripts/check.sh", "value": "echo ok\n"}, {"button": "Save"}],
               routes={"PUT /api/skills/code-review": {"status": 200, "body": after}})
    assert sent(r, "PUT") == [("/api/skills/code-review", {
        "description": "Use when reviewing code.",
        "files": {"checklist.md": "- tests\n", "scripts/check.sh": "echo ok\n"}})]
    assert "Saved." in r["text"] and r["controls"]["description"]["value"] == "Use when reviewing code."


def test_removing_a_file():
    r = editor([{"button": "Remove", "row": "file:checklist.md"}, {"button": "Save"}],
               routes={"PUT /api/skills/code-review": {"status": 200, "body": {**SKILL, "content": {}}}})
    assert sent(r, "PUT") == [("/api/skills/code-review", {"files": {"checklist.md": None}})]


def test_discard_puts_it_back():
    r = editor([{"input": "instructions", "value": "Changed."}, {"button": "Discard"}])
    assert r["controls"]["instructions"]["value"] == SKILL["instructions"]
    assert "Unsaved changes" not in r["text"] and not sent(r, "PUT")


def test_a_refused_save_says_why():
    r = editor([{"input": "file-new", "value": "notes.md"}, {"button": "Add"}, {"button": "Save"}],
               routes={"PUT /api/skills/code-review": {"status": 422, "body": {"detail": "notes.md: too big"}}})
    assert r["errors"]["bar"] == "notes.md: too big" and "Unsaved changes" in r["text"]


def test_a_new_files_path_is_checked():
    jobs = [[{"input": "file-new", "value": p}, {"button": "Add"}] for p in ("", "SKILL.md", "checklist.md", "../x", ".env",
                                                                             "a/.git/x", "/abs")]
    out = run(*({"view": "skill", "name": "code-review", "agents": AGENTS, "actions": a,
                 "routes": {"GET /api/skills/code-review": {"status": 200, "body": SKILL}}} for a in jobs))
    rule = "Use a relative path like reference.md or scripts/check.sh: letters, digits, . _ - and /."
    assert [r["errors"]["file-new"] for r in out] == [
        "Type its path, like reference.md.", "SKILL.md is the description and instructions above.",
        "It already has checklist.md.", rule, rule, rule, rule]


def test_a_broken_skill_md_is_edited_as_a_file():
    broken = {**SKILL, "description": None, "instructions": "", "extra": {}, "error": "SKILL.md must start with YAML",
              "content": {"SKILL.md": "no frontmatter\n", "checklist.md": "x\n"}}
    r = editor([{"input": "file:SKILL.md", "value": "---\nname: code-review\ndescription: Fixed.\n---\n"},
                {"button": "Save"}], skill=broken,
               routes={"PUT /api/skills/code-review": {"status": 200, "body": SKILL}})
    assert sent(r, "PUT") == [("/api/skills/code-review", {
        "files": {"SKILL.md": "---\nname: code-review\ndescription: Fixed.\n---\n"}})]
    r = editor(skill=broken)
    assert "description" not in r["controls"] and r["controls"]["file:SKILL.md"]["value"] == "no frontmatter\n"
    assert "It has a problem, so no agent gets it: SKILL.md must start with YAML" in r["text"]
    assert ">Remove<" not in r["rows"]["file:SKILL.md"] and ">Remove<" in r["rows"]["file:checklist.md"]


def test_deleting_a_shared_skill_names_who_uses_it():
    r = editor([{"button": "Delete skill"}])
    assert "Coder and Scout use it. They keep it until they restart. Delete it?" in r["text"]
    assert not sent(r, "DELETE")
    r = editor([{"button": "Delete skill"}, {"button": "Delete", "within": "confirm"}],
               routes={"DELETE /api/skills/code-review": {"status": 200, "body": {"ok": True}}})
    assert sent(r, "DELETE") == [("/api/skills/code-review", None)] and r["hash"] == "#/skills"


def test_an_agents_own_skill():
    own = {**SKILL, "name": "house-style", "agent": "scout", "used_by": ["scout"], "extra": {}}
    r = editor(skill=own, agent_name="scout", agents=[agent(restart_needed=True)])
    assert "Only for Scout" in r["text"] and 'href="#/a/scout/settings"' in r["html"]
    assert "Saved changes apply after a restart." in r["text"]
    r = editor([{"button": "Delete skill"}, {"button": "Delete", "within": "confirm"}], skill=own, agent_name="scout",
               routes={"DELETE /api/agents/scout/skills/house-style": {"status": 200, "body": {"ok": True}}})
    assert "Delete Scout's skill house-style? It keeps it until it restarts." in r["text"]
    assert sent(r, "DELETE") == [("/api/agents/scout/skills/house-style", None)] and r["hash"] == "#/a/scout/settings"


def test_leaving_the_editor_with_unsaved_edits_asks_first():
    routes = {"GET /api/skills/code-review": {"status": 200, "body": SKILL},
              "GET /api/skills": {"status": 200, "body": LIBRARY}}
    base = [{"input": "description", "value": "Edited."}, {"go": "#/skills"}]
    asked, left = run(*({"view": "route", "hash": "#/skills/code-review", "routes": routes, "agents": AGENTS,
                         "actions": a} for a in (base, base + [{"button": "Discard & leave"}])))
    assert asked["hash"] == "#/skills/code-review" and asked["viewClass"] == "skill-view"
    assert "You have unsaved changes" in asked["text"] and asked["replaced"] == ["/#/skills/code-review"]
    assert left["hash"] == "#/skills" and left["viewClass"] == "skills" and not sent(left, "PUT")
