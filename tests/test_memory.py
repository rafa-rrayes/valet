"""An agent's memory (control/memory.py) on a real definitions repo: names, limits, the changes and their commits,
search, history, the memory layer; and which servers a definition reaches through the gateway."""
import os
import subprocess

import pytest

from valet.control import memory
from valet.control.definitions import DefinitionsRepo
from valet.shared.definition import AgentDefinition, DefinitionError
from valet.shared.owner import say


@pytest.fixture
def defs(tmp_path) -> DefinitionsRepo:
    repo = DefinitionsRepo(tmp_path / "defs")
    repo.init()
    repo.create("scout")
    return repo


def log(defs: DefinitionsRepo, *paths: str) -> list[str]:
    out = subprocess.run(["git", "-C", str(defs.root), "log", "--format=%s", "--", *paths],
                         capture_output=True, text=True, check=True).stdout
    return out.splitlines()


def test_names():
    assert memory.file_name("notes") == "notes" == memory.file_name("notes.md") == memory.file_name(" notes.md ")
    assert memory.file_name("project-x2") == "project-x2"
    for bad in ("", "Notes", "my notes", "a--b", "-a", "a-", "../x", "a/b", "x.txt", "a" * 65, None, 3):
        with pytest.raises(memory.MemoryError):
            memory.file_name(bad)


def test_title_of():
    assert memory.title_of("# Project X\n\nmore") == "Project X"
    assert memory.title_of("intro line\n\n## Later heading ##\n") == "Later heading"
    assert memory.title_of("\n\n- a list item\n") == "a list item"
    assert memory.title_of("") == ""
    assert len(memory.title_of("# " + "x" * 200)) == memory.TITLE_MAX


def test_write_read_append_delete_each_a_commit(defs):
    f = memory.write(defs, "scout", "notes", "# Notes\n\nAlice likes tea.", "scout")
    assert (f.name, f.title, f.size) == ("notes", "Notes", len("# Notes\n\nAlice likes tea.\n"))
    p = defs.root / "agents/scout/memory/notes.md"
    assert p.read_text() == "# Notes\n\nAlice likes tea.\n"       # a final newline, always
    f2, text = memory.read(defs.root, "scout", "notes.md")
    assert text == p.read_text() and f2 == f

    memory.append(defs, "scout", "notes", "He takes it without sugar.", "scout")
    assert p.read_text() == "# Notes\n\nAlice likes tea.\nHe takes it without sugar.\n"
    memory.append(defs, "scout", "log", "- started", "scout")      # append creates
    memory.write(defs, "scout", "notes", "# Notes\n\nCoffee now.\n", memory.OWNER)
    memory.delete(defs, "scout", "log", "scout")
    assert not (defs.root / "agents/scout/memory/log.md").exists()
    assert log(defs, "agents/scout/memory")[:5] == [
        "Memory: scout deleted log.md", "Memory: owner wrote scout's notes.md", "Memory: scout appended to log.md",
        "Memory: scout appended to notes.md", "Memory: scout wrote notes.md"]
    # Writing the same text again changes nothing and makes no commit.
    head = defs.head()
    memory.write(defs, "scout", "notes", "# Notes\n\nCoffee now.\n", "scout")
    assert defs.head() == head

    with pytest.raises(FileNotFoundError):
        memory.read(defs.root, "scout", "log")
    with pytest.raises(FileNotFoundError):
        memory.delete(defs, "scout", "log", "scout")
    with pytest.raises(memory.MemoryError):
        memory.write(defs, "scout", "notes", "   \n", "scout")         # empty: delete instead
    with pytest.raises(memory.MemoryError):
        memory.append(defs, "scout", "notes", "", "scout")
    with pytest.raises(memory.MemoryError):
        memory.write(defs, "scout", "notes", "nul \0 here", "scout")
    with pytest.raises(memory.MemoryError):
        memory.write(defs, "scout", "notes", 42, "scout")


def test_limits(defs):
    with pytest.raises(memory.MemoryError, match="at most 64 KiB"):
        memory.write(defs, "scout", "big", "x" * (memory.FILE_MAX + 1), "scout")
    memory.write(defs, "scout", "full", "x" * (memory.FILE_MAX - 1), "scout")    # + the newline: exactly the max
    with pytest.raises(memory.MemoryError, match="at most 64 KiB"):
        memory.append(defs, "scout", "full", "more", "scout")
    assert not (defs.root / "agents/scout/memory/big.md").exists()


def test_agent_limits(defs, monkeypatch):
    monkeypatch.setattr(memory, "FILES_MAX", 3)
    monkeypatch.setattr(memory, "AGENT_MAX", 100)
    for n in ("a", "b", "c"):
        memory.write(defs, "scout", n, "x" * 20, "scout")
    with pytest.raises(memory.MemoryError, match="at most 3 files"):
        memory.write(defs, "scout", "d", "x", "scout")
    memory.write(defs, "scout", "a", "y" * 20, "scout")              # replacing one is fine
    with pytest.raises(memory.MemoryError, match="in all"):
        memory.write(defs, "scout", "a", "y" * 70, "scout")         # 70 + 2 x 21 > 100
    memory.delete(defs, "scout", "c", "scout")
    memory.write(defs, "scout", "d", "x", "scout")


def test_files_and_search(defs):
    memory.write(defs, "scout", "project-x", "# Project X\n\nDeadline: Friday.\nOwner: Alice", "scout")
    memory.write(defs, "scout", "people", "# People\n\nAlice's cat is Bartolomeu.", "scout")
    d = defs.root / "agents/scout/memory"
    (d / "Bad Name.md").write_text("skipped")
    (d / "binary.md").write_bytes(b"\xff\xfe")
    (d / "notes.txt").write_text("not memory")
    os.symlink(d / "people.md", d / "link.md")
    assert [f.name for f in memory.files(defs.root, "scout")] == ["people", "project-x"]
    assert memory.search(defs.root, "scout", "ALICE") == [
        {"name": "people", "line": 3, "text": "Alice's cat is Bartolomeu."},
        {"name": "project-x", "line": 4, "text": "Owner: Alice"}]
    assert memory.search(defs.root, "scout", "project")[0] == {"name": "project-x", "line": 0, "text": "Project X"}
    with pytest.raises(memory.MemoryError):
        memory.search(defs.root, "scout", " ")
    assert memory.files(defs.root, "nobody") == []


def test_history_and_older_versions(defs):
    memory.write(defs, "scout", "notes", "one", "scout")
    memory.write(defs, "scout", "notes", "two", "scout")
    memory.delete(defs, "scout", "notes", memory.OWNER)
    h = memory.history(defs, "scout", "notes")
    assert [e["message"] for e in h] == ["Memory: owner deleted scout's notes.md", "Memory: scout wrote notes.md",
                                         "Memory: scout wrote notes.md"]
    assert memory.at(defs, "scout", "notes", h[1]["commit"]) == "two\n"
    assert memory.at(defs, "scout", "notes", h[2]["commit"]) == "one\n"
    assert memory.at(defs, "scout", "notes", h[0]["commit"]) is None
    with pytest.raises(ValueError):
        memory.at(defs, "scout", "notes", "HEAD")
    # The agent's settings history leaves memory out.
    assert not any(e["message"].startswith("Memory:") for e in defs.history("scout"))


def test_layer(defs, monkeypatch):
    empty = memory.layer(defs.root, "scout")
    assert empty.startswith("# Your memory\n\n" + say(memory.PREAMBLE)) and "You have no memory files yet." in empty

    memory.write(defs, "scout", "old", "# Old stuff\n\n" + "o" * 300, "scout")
    memory.write(defs, "scout", "new", "# New stuff\n\nfresh", "scout")
    os.utime(defs.root / "agents/scout/memory/old.md", (1_700_000_000, 1_700_000_000))
    text = memory.layer(defs.root, "scout")
    assert "Files: 2," in text and "- new: New stuff (" in text and "- old: Old stuff (" in text
    assert "updated 2023-11-14" in text                       # the old file's date
    assert text.index("## Memory file: new") < text.index("## Memory file: old")      # newest first
    assert "Not loaded here" not in text

    small = memory.layer(defs.root, "scout", budget=100)      # only the new one fits
    assert "## Memory file: new" in small and "## Memory file: old" not in small
    assert "Not loaded here, for room (0 KiB at most): old. Read them with valet.memory's read" in small


def test_gateway_servers():
    d = AgentDefinition.model_validate({"name": "scout", "tools": {"mcp": ["valet.agents", "github",
                                                                            "valet.agents"]}})
    assert d.gateway_servers() == ["valet.agents", "github"]
    assert d.model_copy(update={"memory": True}).gateway_servers() == ["valet.agents", "github", "valet.memory"]
    assert AgentDefinition.model_validate({"name": "scout", "memory": True}).gateway_servers() == ["valet.memory"]
    assert AgentDefinition.model_validate({"name": "scout"}).gateway_servers() == []
    with pytest.raises(ValueError, match="comes with `memory: true`"):
        AgentDefinition.model_validate({"name": "scout", "tools": {"mcp": ["valet.memory"]}})
    with pytest.raises(ValueError, match="stands for the owner"):
        AgentDefinition.model_validate({"name": "owner"})


def test_owner_cant_name_an_agent(defs):
    with pytest.raises(DefinitionError):
        defs.create("owner")
