"""agent.forget: a deleted agent stops, and what the runner kept of it goes (its container, its data dir, its volumes
when asked), except a host agent's own work: its working folder when that has files, and always a computer.workdir."""
import stat
import sys
from pathlib import Path

import pytest

from runner_helpers import FAKE, host_agent, runner_cfg
from valet.runner.supervisor import CommandError, Supervisor
from valet.shared.protocol import parse_control_message


def cmd(type_: str, **fields) -> object:
    return parse_control_message({"type": type_, "id": "c-" + type_, **fields})


def stand_in_docker(tmp_path: Path) -> tuple[Path, Path]:
    """A `docker` that logs each call: `run` is the fake ACP agent, `volume inspect` finds every volume, `ps` and
    `network ls` find one each (c1, n1), the rest succeeds."""
    log, script = tmp_path / "docker.log", tmp_path / "docker"
    script.write_text(f"""#!/bin/sh
echo "$@" >> {log}
if [ "$1" = run ]; then exec {sys.executable} {FAKE}; fi
if [ "$1 $2" = "volume inspect" ]; then for last; do :; done; echo "$last"; fi
if [ "$1" = ps ]; then echo c1; fi
if [ "$1 $2" = "network ls" ]; then echo n1; fi
exit 0
""")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script, log


@pytest.fixture
def docker(tmp_path):
    return stand_in_docker(tmp_path)


async def start(sup, sent, d) -> None:
    await sup.handle(cmd("agent.start", agent=d.name, definition=d.model_dump(mode="json"), files={}))
    await sent.wait_for("agent.status", agent=d.name, state="running")


async def test_a_host_agent_keeps_its_work_and_loses_the_rest(tmp_path, sent, docker):
    sup = Supervisor(runner_cfg(tmp_path, docker=str(docker[0])), sent)
    try:
        await start(sup, sent, host_agent("helper"))
        data = tmp_path / "runner" / "agents" / "helper"
        assert (data / "config").is_dir()
        (data / "work" / "notes.txt").write_text("mine")

        out = await sup.handle(cmd("agent.forget", agent="helper"))
        assert out == {"volumes": [], "kept": str(data / "work")}
        assert sent.of("agent.status", agent="helper")[-1]["state"] == "stopped"
        assert sup.agents() == []                               # forgotten: not in the next hello
        assert sorted(p.name for p in data.iterdir()) == ["work"]
        assert (data / "work" / "notes.txt").read_text() == "mine"
        assert "volume" not in docker[1].read_text()            # never its volumes unless asked
    finally:
        await sup.shutdown()


async def test_an_empty_working_folder_goes_and_a_workdir_is_never_touched(tmp_path, sent, docker):
    sup = Supervisor(runner_cfg(tmp_path, docker=str(docker[0])), sent)
    mine = tmp_path / "projects"
    mine.mkdir()
    (mine / "code.py").write_text("print(1)")
    try:
        await start(sup, sent, host_agent("plain"))
        await start(sup, sent, host_agent("pinned", computer={"level": "host", "workdir": str(mine)}))
        assert await sup.handle(cmd("agent.forget", agent="plain")) == {"volumes": [], "kept": None}
        assert await sup.handle(cmd("agent.forget", agent="pinned")) == {"volumes": [], "kept": None}
        assert not (tmp_path / "runner" / "agents" / "plain").exists()
        assert not (tmp_path / "runner" / "agents" / "pinned").exists()
        assert (mine / "code.py").read_text() == "print(1)"
    finally:
        await sup.shutdown()


async def test_a_container_agent_and_its_volumes(tmp_path, sent, docker):
    script, log = docker
    sup = Supervisor(runner_cfg(tmp_path, docker=str(script), harness_commands={}), sent)
    try:
        await start(sup, sent, host_agent("box", computer={"level": "container"}))
        log.write_text("")
        out = await sup.handle(cmd("agent.forget", agent="box"))
        assert out == {"volumes": [], "kept": None}
        calls = log.read_text().splitlines()
        assert "stop valet-agent-box" in calls
        # By their label, not their names: agent box's proxy (valet-agent-box-egress) would be agent box-egress's
        # container.
        assert calls[-4:] == ["ps -aq --filter label=valet.agent=box", "rm -f c1",
                              "network ls -q --filter label=valet.agent=box", "network rm n1"]
        assert not (tmp_path / "runner" / "agents" / "box").exists()

        # Asked to, its volumes go too (an agent that never ran since this runner started: still confirmed stopped).
        log.write_text("")
        out = await sup.handle(cmd("agent.forget", agent="box2", volumes=True))
        assert out == {"volumes": ["valet-agent-box2-home", "valet-agent-box2-work"], "kept": None}
        assert sent.of("agent.status", agent="box2")[-1]["state"] == "stopped"
        assert log.read_text().splitlines()[-1] == "volume rm valet-agent-box2-home valet-agent-box2-work"
    finally:
        await sup.shutdown()


async def test_without_docker_and_bad_names(tmp_path, sent):
    sup = Supervisor(runner_cfg(tmp_path, docker=str(tmp_path / "no-docker")), sent)
    (tmp_path / "runner" / "agents" / "gone" / "config").mkdir(parents=True)
    assert await sup.handle(cmd("agent.forget", agent="gone", volumes=True)) == {"volumes": [], "kept": None}
    assert not (tmp_path / "runner" / "agents" / "gone").exists()
    with pytest.raises(CommandError, match="not an agent name"):
        await sup.handle(cmd("agent.forget", agent="../x"))


async def test_only_what_the_runner_made_goes(tmp_path, sent, docker):
    """A computer.workdir inside the agent's data dir, or anything else put there, stays: only config/, egress/ and an
    empty work/ are the runner's."""
    sup = Supervisor(runner_cfg(tmp_path, docker=str(docker[0])), sent)
    data = tmp_path / "runner" / "agents" / "inside"
    mine = data / "projects"
    mine.mkdir(parents=True)
    (mine / "code.py").write_text("print(1)")
    try:
        await start(sup, sent, host_agent("inside", computer={"level": "host", "workdir": str(mine)}))
        (data / "egress").mkdir()
        (data / "work").mkdir()
        assert await sup.handle(cmd("agent.forget", agent="inside")) == {"volumes": [], "kept": str(data)}
        assert sorted(p.name for p in data.iterdir()) == ["projects"]
        assert (mine / "code.py").read_text() == "print(1)"
    finally:
        await sup.shutdown()
