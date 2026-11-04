"""The agent's files (runner/files.py): the same cases against HostFiles (Python, a host agent's working folder) and
ContainerFiles (computer.cjs, the helper a container runs under `docker exec ... node -e`; here on the runner's own
node, no prefix, against local folders). Confinement: absolute paths only, no `..`, links resolved and checked,
nothing written through a link."""
import os
import shutil
import uuid

import pytest

from runner_helpers import host_agent, runner_cfg
from valet.runner.files import (
    ContainerFiles, FilesError, HostFiles, Root, Uploads, check_path, roots_of,
)
from valet.shared.definition import AgentDefinition

KINDS = ["host", pytest.param("container", marks=pytest.mark.skipif(shutil.which("node") is None,
                                                                    reason="node isn't installed"))]


@pytest.fixture(params=KINDS)
def world(request, tmp_path):
    """(files, work, home, outside): two roots (one read-only), and a folder outside them."""
    work, home, ro, outside = (tmp_path / n for n in ("work", "home", "ro", "outside"))
    for d in (work, home, ro, outside):
        d.mkdir()
    (work / "src").mkdir()
    (work / "src" / "main.py").write_text("print('hi')\n")
    (work / "Notes.md").write_text("# notes\n")
    (work / "b.txt").write_bytes(b"0123456789" * 1000)
    (outside / "secret.txt").write_text("nope")
    (ro / "keep.txt").write_text("keep")
    os.symlink(outside, work / "escape")                  # a link out of the roots
    os.symlink(outside / "secret.txt", work / "leak.txt")
    os.symlink(home, work / "to-home")                    # a link to another root: fine
    os.symlink(work / "missing", work / "dangling")
    roots = [Root(str(work), "Workspace", True), Root(str(home), "Home", True), Root(str(ro), "Shared", False)]
    files = HostFiles(roots) if request.param == "host" else ContainerFiles(roots, [], env=dict(os.environ))
    return files, work, home, ro, outside


def up() -> str:
    return uuid.uuid4().hex


async def refused(coro, text: str) -> None:
    with pytest.raises(FilesError) as e:
        await coro
    assert text in str(e.value), str(e.value)


async def test_lists_a_folder_dirs_first(world):
    files, work, *_ = world
    out = await files.list(str(work))
    assert out["path"] == str(work) and out["type"] == "dir" and out["truncated"] is False
    names = [(e["name"], e["type"], e["link"]) for e in out["entries"]]
    assert names == [("src", "dir", False), ("to-home", "dir", True), ("b.txt", "file", False),
                     ("dangling", "other", True), ("escape", "other", True), ("leak.txt", "other", True),
                     ("Notes.md", "file", False)]
    b = next(e for e in out["entries"] if e["name"] == "b.txt")
    assert b["size"] == 10000 and b["mtime"] > 0
    # No path: the first root.
    assert (await files.list(None))["path"] == str(work)


async def test_a_file_path_says_what_it_is(world):
    files, work, *_ = world
    out = await files.list(str(work / "src" / "main.py"))
    assert out["type"] == "file" and out["size"] == 12 and out["entries"] == []


async def test_paths_must_be_absolute_plain_and_inside(world):
    files, work, home, ro, outside = world
    await refused(files.list("src"), "absolute")
    await refused(files.list(f"{work}/src/../../outside"), ". or ..")
    await refused(files.list(f"{work}/./src"), ". or ..")
    await refused(files.list(str(outside)), "outside its folders")
    await refused(files.read(str(outside / "secret.txt"), 0, 10), "outside its folders")
    # Inside as written, outside once resolved.
    await refused(files.list(str(work / "escape")), "leads outside")
    await refused(files.read(str(work / "leak.txt"), 0, 10), "leads outside")
    await refused(files.list(str(work / "nope")), "no such file or folder")
    # A link to another root is fine; doubled slashes are just written plainly.
    assert (await files.list(str(work / "to-home")))["type"] == "dir"
    assert (await files.list(str(work) + "//src/"))["path"] == str(work / "src")


async def test_reads_in_chunks(world):
    files, work, *_ = world
    meta, data = await files.read(str(work / "b.txt"), 0, 4096)
    assert meta == {"size": 10000, "eof": False} and data == (b"0123456789" * 1000)[:4096]
    meta, data = await files.read(str(work / "b.txt"), 8192, 4096)
    assert meta == {"size": 10000, "eof": True} and len(data) == 1808
    meta, data = await files.read(str(work / "b.txt"), 20000, 10)
    assert meta["eof"] is True and data == b""
    await refused(files.read(str(work / "src"), 0, 10), "not a file")


async def test_uploads_in_chunks_then_renames_into_place(world):
    files, work, *_ = world
    u = up()
    assert await files.write(u, str(work / "src"), "data.bin", 0, b"abc", False, "fail") == {"size": 3}
    assert (work / "src" / f".valet-upload-{u}").read_bytes() == b"abc"
    # The partial file isn't listed, and the name isn't there yet.
    assert [e["name"] for e in (await files.list(str(work / "src")))["entries"]] == ["main.py"]
    await refused(files.write(u, str(work / "src"), "data.bin", 10, b"x", False, "fail"), "out of step")
    out = await files.write(u, str(work / "src"), "data.bin", 3, b"def", True, "fail")
    assert out == {"path": str(work / "src" / "data.bin"), "size": 6}
    assert (work / "src" / "data.bin").read_bytes() == b"abcdef"
    assert not (work / "src" / f".valet-upload-{u}").exists()


async def test_exists_fail_replace_or_rename(world):
    files, work, *_ = world
    await refused(files.write(up(), str(work), "Notes.md", 0, b"new", True, "fail"), "exists")
    assert not [p for p in os.listdir(work) if p.startswith(".valet-upload-")]    # the partial file went
    out = await files.write(up(), str(work), "Notes.md", 0, b"new", True, "replace")
    assert out["path"] == str(work / "Notes.md") and (work / "Notes.md").read_bytes() == b"new"
    out = await files.write(up(), str(work), "Notes.md", 0, b"two", True, "rename")
    assert out["path"] == str(work / "Notes-2.md") and (work / "Notes-2.md").read_bytes() == b"two"
    out = await files.write(up(), str(work), "Notes.md", 0, b"three", True, "rename")
    assert out["path"] == str(work / "Notes-3.md")
    # Never replaces a folder or a link (so never writes through one).
    await refused(files.write(up(), str(work), "src", 0, b"x", True, "replace"), "folder or a link")
    await refused(files.write(up(), str(work), "leak.txt", 0, b"x", True, "replace"), "folder or a link")
    assert (world[4] / "secret.txt").read_text() == "nope"


async def test_upload_refusals(world):
    files, work, home, ro, outside = world
    await refused(files.write(up(), str(ro), "x.txt", 0, b"x", True, "fail"), "read-only")
    await refused(files.write(up(), str(work / "escape"), "x.txt", 0, b"x", True, "fail"), "leads outside")
    await refused(files.write(up(), str(outside), "x.txt", 0, b"x", True, "fail"), "outside its folders")
    await refused(files.write(up(), str(work / "b.txt"), "x.txt", 0, b"x", True, "fail"), "not a folder")
    for bad in ("a/b", "..", ".", ".valet-upload-x"):
        await refused(files.write(up(), str(work), bad, 0, b"x", True, "fail"), "name")
    await refused(files.write("not-hex", str(work), "x.txt", 0, b"x", True, "fail"), "upload id")
    assert not list(outside.iterdir())[1:] and sorted(os.listdir(outside)) == ["secret.txt"]


async def test_cancel_drops_the_partial_file(world):
    files, work, *_ = world
    u = up()
    await files.write(u, str(work), "big.bin", 0, b"abc", False, "fail")
    await files.cancel(u, str(work))
    assert not (work / f".valet-upload-{u}").exists()
    await files.cancel(u, str(work))        # again: nothing to do


async def test_truncates_huge_folders(world, monkeypatch):
    files, work, *_ = world
    if isinstance(files, ContainerFiles):
        pytest.skip("the helper's limit is its own constant")
    monkeypatch.setattr("valet.runner.files.MAX_ENTRIES", 3)
    out = await files.list(str(work))
    assert out["truncated"] is True and len(out["entries"]) == 3


def test_roots_follow_the_computer(tmp_path):
    cfg = runner_cfg(tmp_path)
    box = AgentDefinition.model_validate({"name": "box", "computer": {"level": "container", "mounts": [
        {"host": "/srv/notes", "path": "/work/notes/", "readonly": True}]}})
    assert [(r.path, r.writable) for r in roots_of(box, cfg)] == [
        ("/work", True), ("/home/agent", True), ("/work/notes", False)]
    assert roots_of(AgentDefinition.model_validate({"name": "n", "computer": {"level": "none"}}), cfg) == []
    [root] = roots_of(host_agent("h"), cfg)
    assert root.path == str((tmp_path / "runner" / "agents" / "h" / "work").absolute())
    assert roots_of(host_agent("h", computer={"level": "host", "workdir": "/srv/app"}), cfg)[0].path == "/srv/app"


def test_check_path_is_lexical():
    roots = [Root("/work", "W", True), Root("/home/agent", "H", True)]
    assert check_path("/work//a/b/", roots) == "/work/a/b"
    for bad in ("work/a", "/work/../etc", "/workshop", "/etc/passwd", "/work/a\0b", None):
        with pytest.raises(FilesError):
            check_path(bad, roots)


def test_uploads_go_stale(monkeypatch):
    u = Uploads()
    u.touch("scout", "abc12345", "/work")
    assert u.stale() == []
    monkeypatch.setattr("valet.runner.files.STALE_UPLOAD", -1)
    assert u.stale() == [("scout", "abc12345", "/work")]
    assert u.stale() == []


async def test_mkdir_makes_missing_folders_inside(world):
    files, work, home, ro, outside = world
    assert await files.mkdir(str(work / "attachments" / "a" / "b")) == {"path": str(work / "attachments" / "a" / "b")}
    assert (work / "attachments" / "a" / "b").is_dir()
    assert await files.mkdir(str(work / "attachments")) == {"path": str(work / "attachments")}     # there: fine
    await files.mkdir(str(work / "to-home" / "made"))           # through a link to another root
    assert (home / "made").is_dir()
    await refused(files.mkdir(str(work / "escape" / "x")), "leads outside")
    assert sorted(os.listdir(outside)) == ["secret.txt"]        # nothing made through the link
    await refused(files.mkdir(str(ro / "x")), "read-only")
    await refused(files.mkdir(str(work / "b.txt" / "x")), "not a folder")
    await refused(files.mkdir(str(outside / "x")), "outside its folders")
