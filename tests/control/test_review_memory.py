"""Review of the agents' memory (m4): bugs shown by failing tests. Each test says what goes wrong."""
import pytest

from valet.control import memory
from valet.control.definitions import DefinitionsRepo


@pytest.fixture
def defs(tmp_path) -> DefinitionsRepo:
    repo = DefinitionsRepo(tmp_path / "defs")
    repo.init()
    repo.create("scout")
    return repo


def test_a_memory_file_name_has_no_newline(defs):
    """NAME_RE ends in `$`, which also matches before a final newline, and file_name strips the name before taking
    `.md` off, so "notes\\n.md" passes as "notes\\n": valet.memory's write makes agents/scout/memory/"notes\\n.md" in
    the definitions repo (a commit "Memory: scout wrote notes\\n.md"), listed beside notes.md under a name that breaks
    the memory layer's index and the owner's URLs. (The same `$` is in the skill, agent and profile name rules.)"""
    with pytest.raises(memory.MemoryError):
        memory.write(defs, "scout", "notes\n.md", "hello", "scout")
    assert [p.name for p in memory.directory(defs.root, "scout").glob("*")] == []
