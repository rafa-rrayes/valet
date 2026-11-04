"""The real thing: the agent-base image and the Claude adapter in a container. Needs Docker and the image
(docker build -t valet/agent-base docker/agent-base); a prompt also needs ANTHROPIC_API_KEY."""
import os
import subprocess

import pytest

from runner_helpers import host_agent, runner_cfg
from valet.runner.agent import AgentProcess
from valet.shared.config import PASSTHROUGH_ENV

pytestmark = [pytest.mark.docker,
              pytest.mark.skipif(os.environ.get("VALET_TEST_DOCKER") != "1", reason="VALET_TEST_DOCKER=1 not set")]


async def test_claude_container(tmp_path, sent):
    passthrough = {k: os.environ[k] for k in PASSTHROUGH_ENV if os.environ.get(k)}
    cfg = runner_cfg(tmp_path, harness_commands={}, passthrough=passthrough)
    p = AgentProcess(host_agent("valet-test", computer={"level": "container"}), {"persona.md": "Be brief."}, cfg, sent)
    await p.start()
    try:
        assert p.state == "running", p.error
        assert p.agent_info and "claude" in p.agent_info["name"]
        if not passthrough:
            return
        sid = (await p.new_session())["session_id"]
        await p.prompt(sid, "t1", [{"type": "text", "text": "Reply with exactly the word: pong"}])
        end = await sent.wait_for("turn.end", turn_id="t1", timeout=120)
        assert end["stop_reason"] == "end_turn", end
        assert "pong" in "".join(sent.texts(sid)).lower()
    finally:
        await p.stop()
        # Volumes outlive the agent by design; the test removes its own.
        for vol in ("valet-agent-valet-test-home", "valet-agent-valet-test-work"):
            subprocess.run(["docker", "volume", "rm", "-f", vol], capture_output=True)
