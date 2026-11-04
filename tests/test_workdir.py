"""A host agent's working folder (computer.workdir): validated in the definition, its harness's cwd and its
sessions', its own folder under the runner's data by default, named in the environment layer, and left out of the
fingerprint while unset."""
import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from tests.runner.runner_helpers import Sent, host_agent, runner_cfg
from valet.control import directives, views
from valet.runner.agent import AgentProcess
from valet.runner.harness import Compiled
from valet.runner.launch import host_workdir, plan
from valet.shared.config import RunnerConfig
from valet.shared.definition import AgentDefinition, parse_agent, to_yaml


def computer(**kw) -> AgentDefinition:
    return AgentDefinition.model_validate({"name": "scout", "computer": {"level": "host", **kw}})


@pytest.mark.parametrize("given, kept", [("/Users/rafa/Code/valet", "/Users/rafa/Code/valet"),
                                         ("/Users/rafa/Code/valet/", "/Users/rafa/Code/valet"),
                                         ("//srv///work//", "/srv/work"), ("/", "/"),
                                         ("/My Projects/né", "/My Projects/né"), ("", None), (None, None)])
def test_a_workdir_is_an_absolute_path(given, kept):
    assert computer(workdir=given).computer.workdir == kept


@pytest.mark.parametrize("bad, says", [("Code/valet", "absolute"), ("~/Code", "absolute"), ("./x", "absolute"),
                                       ("/srv/../etc", ". or .."), ("/srv/./work", ". or .."), ("/srv/a\nb", "control"),
                                       ("/srv/\0", "control"), ("/" + "a" * 1024, "absolute")])
def test_a_bad_workdir_is_refused(bad, says):
    with pytest.raises(ValidationError) as e:
        computer(workdir=bad)
    assert says in str(e.value)
    assert e.value.errors()[0]["loc"] == ("computer", "workdir")


def test_it_round_trips_and_stays_out_of_the_file_when_unset():
    d = computer(workdir="/srv/work")
    assert "  workdir: /srv/work\n" in to_yaml(d) and parse_agent(to_yaml(d)) == d
    assert "workdir" not in to_yaml(computer())


def test_unset_it_leaves_the_fingerprint_as_it_was():
    d = computer()
    dump = views._running_part(d)                # COSMETIC and LIVE fields aside, as the fingerprint takes it
    del dump["computer"]["workdir"]
    before = hashlib.sha256(json.dumps({"definition": dump, "files": {}}, sort_keys=True).encode()).hexdigest()[:16]
    assert views.fingerprint(d, {}) == before
    assert views.fingerprint(computer(workdir="/srv/work"), {}) != before


def test_the_launch_runs_there():
    cfg = RunnerConfig(control_url="ws://x", token="t", data=Path("/srv/runner"))
    c = Compiled(argv=["claude-agent-acp"])
    default = plan(computer(), c, cfg)
    assert default.cwd == default.session_cwd == "/srv/runner/agents/scout/work" == host_workdir(computer(), cfg)
    own = plan(computer(workdir="/Users/rafa/Code/valet"), c, cfg)
    assert own.cwd == own.session_cwd == "/Users/rafa/Code/valet"
    # Anywhere but the host it's ignored: the workspace is /work.
    boxed = AgentDefinition.model_validate({"name": "scout", "computer": {"level": "container",
                                                                          "workdir": "/Users/rafa/Code/valet"}})
    assert plan(boxed, c, cfg).session_cwd == "/work" and "/Users/rafa/Code/valet" not in plan(boxed, c, cfg).args


def test_the_environment_layer_names_it():
    text = directives.environment_text(computer(workdir="/Users/rafa/Code/valet"))
    assert "Your working directory is /Users/rafa/Code/valet." in text
    assert "Your working directory is" not in directives.environment_text(computer())


async def say(p: AgentProcess, sent: Sent, sid: str, turn: str, text: str) -> str:
    await p.prompt(sid, turn, [{"type": "text", "text": text}])
    await sent.wait_for("turn.end", turn_id=turn)
    return sent.texts(sid)[-1]


async def test_the_harness_and_its_sessions_start_there(tmp_path):
    sent = Sent()
    folder = tmp_path / "project"
    folder.mkdir()
    p = AgentProcess(host_agent(computer={"level": "host", "workdir": str(folder)}), {}, runner_cfg(tmp_path), sent)
    await p.start()
    try:
        assert p.state == "running", p.error
        sid = (await p.new_session())["session_id"]
        assert Path(await say(p, sent, sid, "t1", "pwd")).resolve() == folder.resolve()
        assert json.loads(await say(p, sent, sid, "t2", "meta"))["cwd"] == str(folder)
    finally:
        await p.stop()
    assert not (tmp_path / "runner" / "agents" / "scout" / "work").exists()     # its own folder wasn't made


async def test_by_default_its_own_folder_is_made(tmp_path):
    sent = Sent()
    p = AgentProcess(host_agent(), {}, runner_cfg(tmp_path), sent)
    await p.start()
    try:
        own = tmp_path / "runner" / "agents" / "scout" / "work"
        assert p.state == "running" and own.is_dir()
        sid = (await p.new_session())["session_id"]
        assert Path(await say(p, sent, sid, "t1", "pwd")).resolve() == own.resolve()
    finally:
        await p.stop()


async def test_a_missing_folder_fails_the_start(tmp_path):
    sent = Sent()
    gone = tmp_path / "not-there"
    p = AgentProcess(host_agent(computer={"level": "host", "workdir": str(gone)}), {}, runner_cfg(tmp_path), sent)
    await p.start()
    assert p.state == "failed" and p.error == f"its working folder {gone} doesn't exist on this machine"
    assert not gone.exists()
    assert sent.of("agent.status")[-1]["error"] == p.error
