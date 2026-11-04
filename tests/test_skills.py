"""Skills (valet/shared/skills.py): the name and file rules, SKILL.md's frontmatter, reading a skill directory, and
which skills an agent gets."""
import os

import pytest

from valet.shared import skills
from valet.shared.definition import AgentDefinition, DefinitionError, parse_agent
from valet.shared.skills import SkillError


def md(name="code-word", description="Use when asked for the code word.", body="Say PELICAN.", **extra) -> str:
    return skills.render(name, description, body, extra)


def write(base, name, files):
    for path, text in files.items():
        p = base / name / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


# ---------- names, paths, descriptions ----------

@pytest.mark.parametrize("name", ["a", "code-word", "pdf2", "x" * 64, "a-b-c"])
def test_good_names(name):
    assert skills.name_error(name) is None


@pytest.mark.parametrize("name", ["", "Code", "a_b", "-a", "a-", "a--b", "a b", "x" * 65, "../x", "a.b", None, 3])
def test_bad_names(name):
    assert skills.name_error(name)


@pytest.mark.parametrize("path", ["SKILL.md", "reference.md", "scripts/run.py", "a/b/c/d/e.txt", "x_y-z.1"])
def test_good_paths(path):
    assert skills.path_error(path) is None


@pytest.mark.parametrize("path", ["", "/abs", "../x", "a/../b", ".hidden", "a/.git/config", "a//b", "a b",
                                  "a/b/c/d/e/f", "x" * 201, "a\\b", "a/"])
def test_bad_paths(path):
    assert skills.path_error(path)


def test_description_rule():
    assert skills.description_error("Two lines\nand a\ttab.") is None
    for bad in ("", "   ", None, 5, "x" * 1025, "a\x85b", "a\x00b", "a b"):
        assert skills.description_error(bad), bad


# ---------- SKILL.md ----------

def test_render_and_split_round_trip():
    text = md(description='Quotes "here": and a colon', body="\n\n# Steps\n\n1. Do it.\n\n", license="MIT",
              **{"allowed-tools": ["Read"]})
    assert text.startswith("---\nname: code-word\ndescription: ")
    meta, body = skills.split(text)
    assert meta == {"name": "code-word", "description": 'Quotes "here": and a colon', "license": "MIT",
                    "allowed-tools": ["Read"]}
    assert body == "# Steps\n\n1. Do it."
    assert skills.render("code-word", meta["description"], body, meta) == text
    assert md(body="").endswith("---\n")


def test_render_never_lets_extra_frontmatter_rename_it():
    assert skills.split(skills.render("a", "d", "", {"name": "b", "description": "x"}))[0] == {
        "name": "a", "description": "d"}


@pytest.mark.parametrize("text,msg", [
    ("no frontmatter", "must start with YAML frontmatter"),
    ("---\nname: x\n", "must start with YAML frontmatter"),
    ("---\n[unclosed\n---\n", "isn't valid YAML"),
    ("---\n- a list\n---\n", "must be a mapping"),
])
def test_split_refusals(text, msg):
    with pytest.raises(SkillError, match=msg):
        skills.split(text)


def test_split_takes_crlf_a_bom_and_an_empty_frontmatter():
    assert skills.split("﻿---\r\nname: a\r\n---\r\nbody\r\n") == ({"name": "a"}, "body")
    assert skills.split("---\n---\n") == ({}, "")


def test_check_a_whole_skill():
    meta = skills.check("code-word", {"SKILL.md": md(), "refs/words.md": "PELICAN\n"})
    assert meta["description"] == "Use when asked for the code word."


@pytest.mark.parametrize("files,msg", [
    ({"README.md": "x"}, "needs a SKILL.md"),
    ({"SKILL.md": md(name="other")}, "name must be code-word"),
    ({"SKILL.md": md(description=" ")}, "description is required"),
    ({"SKILL.md": md(), "../x": "y"}, "a relative path"),
    ({"SKILL.md": md(), "a.bin": "x\x00y"}, "text files only"),
    ({"SKILL.md": md(), "a.md": "\udc80"}, "not valid text"),
    ({"SKILL.md": md(), "a.md": 5}, "text files only"),
    ({"SKILL.md": md(), "a.md": "x" * (skills.FILE_MAX + 1)}, "256 KiB a file"),
    ({"SKILL.md": md(), "Notes.md": "a", "notes.md": "b"}, "other letter case"),
    ({"SKILL.md": md(), **{f"f{i}.md": "x" for i in range(skills.FILES_MAX)}}, "at most 50 files"),
    ({"SKILL.md": md(), **{f"f{i}.md": "x" * skills.FILE_MAX for i in range(4)}}, "1 MiB in a skill"),
])
def test_check_refusals(files, msg):
    with pytest.raises(SkillError, match=msg):
        skills.check("code-word", files)


def test_check_refuses_a_bad_name():
    with pytest.raises(SkillError, match="lowercase"):
        skills.check("Code", {"SKILL.md": md(name="Code")})


# ---------- on disk ----------

def test_read_and_names(tmp_path):
    write(tmp_path, "code-word", {"SKILL.md": md(), "refs/words.md": "PELICAN\n"})
    write(tmp_path, "Not-A-Name", {"SKILL.md": "x"})
    (tmp_path / "stray.md").write_text("not a skill")
    assert skills.names(tmp_path) == ["code-word"]
    assert skills.names(tmp_path / "nope") == []
    assert skills.load(tmp_path, "code-word") == {"SKILL.md": md(), "refs/words.md": "PELICAN\n"}
    with pytest.raises(FileNotFoundError):
        skills.read(tmp_path / "absent")


def test_read_refuses_binary_links_and_hidden_files(tmp_path):
    write(tmp_path, "bin", {"SKILL.md": md(name="bin")})
    (tmp_path / "bin" / "x.dat").write_bytes(b"\xff\xfe\x00")
    with pytest.raises(SkillError, match="text files only"):
        skills.read(tmp_path / "bin")
    write(tmp_path, "link", {"SKILL.md": md(name="link")})
    os.symlink("/etc/passwd", tmp_path / "link" / "passwd")
    with pytest.raises(SkillError, match="links aren't allowed"):
        skills.read(tmp_path / "link")
    write(tmp_path, "dot", {"SKILL.md": md(name="dot"), ".DS_Store": "x"})
    with pytest.raises(SkillError, match="not starting with"):
        skills.read(tmp_path / "dot")
    os.symlink(tmp_path / "dot", tmp_path / "linked")
    assert "linked" not in skills.names(tmp_path)


# ---------- what an agent gets ----------

@pytest.fixture
def libs(tmp_path):
    own, shared = tmp_path / "own", tmp_path / "shared"
    for n in ("alpha", "beta", "gamma"):
        write(shared, n, {"SKILL.md": md(name=n, body=f"shared {n}")})
    write(own, "beta", {"SKILL.md": md(name="beta", body="its own beta")})
    write(own, "mine", {"SKILL.md": md(name="mine")})
    return own, shared


def test_select_by_name(libs):
    sel = skills.select(*libs, ["alpha", "beta", "nope", "alpha"])
    assert sel.sources == {"beta": "agent", "mine": "agent", "alpha": "shared"}     # its own wins beta
    assert skills.split(sel.skills["beta"]["SKILL.md"])[1] == "its own beta"
    assert sel.missing == ["nope"] and sel.problems == []
    assert sorted(sel.files()) == ["skills/alpha/SKILL.md", "skills/beta/SKILL.md", "skills/mine/SKILL.md"]


def test_select_all(libs):
    sel = skills.select(*libs, ["*"])
    assert sel.sources == {"alpha": "shared", "beta": "agent", "gamma": "shared", "mine": "agent"}
    assert sel.missing == []


def test_select_none_gives_only_its_own(libs):
    assert skills.select(*libs, []).sources == {"beta": "agent", "mine": "agent"}
    assert skills.select(libs[0].parent / "nobody", libs[1], []).skills == {}


def test_select_leaves_out_a_broken_skill_and_says_why(libs):
    own, shared = libs
    (shared / "gamma" / "SKILL.md").write_text("no frontmatter")
    (own / "mine" / "SKILL.md").write_text(md(name="wrong"))
    sel = skills.select(own, shared, ["*"])
    assert set(sel.skills) == {"alpha", "beta"}
    assert sel.problems == ["mine (its own): SKILL.md: name must be mine, the skill's own name",
                            "gamma: SKILL.md must start with YAML frontmatter: a line ---, name: and description:, then ---"]


def test_select_caps_what_one_agent_carries(libs, monkeypatch):
    monkeypatch.setattr(skills, "AGENT_MAX", len(md(name="beta", body="its own beta").encode()) + 1)
    sel = skills.select(*libs, ["*"])
    assert list(sel.skills) == ["beta"]
    assert [p.split(":")[0] for p in sel.problems] == ["mine", "alpha", "gamma"]


def test_by_skill_is_the_runners_side_of_files(libs):
    files = {**skills.select(*libs, ["alpha"]).files(), "persona.md": "x", "skills/Bad/SKILL.md": "x",
             "skills/alpha/../x": "x", "skills/nofile": "x"}
    assert sorted(skills.by_skill(files)) == ["alpha", "beta", "mine"]
    assert list(skills.by_skill(files)["alpha"]) == ["SKILL.md"]


# ---------- the definition ----------

def test_definition_takes_skill_names_or_all():
    assert AgentDefinition.model_validate({"name": "a", "tools": {"skills": ["*"]}}).tools.skills == ["*"]
    assert parse_agent("name: a\ntools:\n  skills: [code-word, pdf]\n").tools.skills == ["code-word", "pdf"]
    with pytest.raises(DefinitionError, match="tools.skills: .*'Code Word': a shared skill's name"):
        parse_agent("name: a\ntools:\n  skills: [Code Word]\n")
