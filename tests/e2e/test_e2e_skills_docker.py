"""Skills with the real thing: a Claude Code (or DeepSeek Harness) agent in a container (the agent-base image) gets a
shared skill it picks and one of its own, made through the API, and the model uses both: each holds a code word it
can't know otherwise. A third shared skill it doesn't pick stays out. The skills are in the read-only config dir:
the agent can't write there. Opt-in: VALET_TEST_DOCKER=1, the image built, and the harness's key in the
environment (CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY; DEEPSEEK_API_KEY). One prompt per harness."""
import asyncio
import os
import uuid
from dataclasses import replace

import pytest

from tests.e2e.stack import ControlServer, Stack, eventually, kinds, texts
from valet.runner.harness import claude, deepseek
from valet.runner.launch import container_name
from valet.shared.config import PASSTHROUGH_ENV

pytestmark = pytest.mark.docker
SLOW = 180.0
AGENT = """\
name: {name}
harness: {harness}
computer:
  level: container
permissions: ask
tools:
  skills: [shared-word]
"""


async def _docker(*args: str) -> tuple[int, str]:
    p = await asyncio.create_subprocess_exec("docker", *args, stdout=asyncio.subprocess.PIPE,
                                             stderr=asyncio.subprocess.STDOUT)
    out, _ = await p.communicate()
    return p.returncode, out.decode(errors="replace")


@pytest.mark.parametrize("harness", [claude, deepseek], ids=["claude", "deepseek"])
async def test_the_model_uses_a_shared_skill_and_its_own(tmp_path, harness):
    if not any(os.environ.get(k) for k in harness.KEYS):
        pytest.skip(f"needs one of {', '.join(harness.KEYS)}")
    name = "skl-" + uuid.uuid4().hex[:6]
    shared_word, own_word, unused_word = (f"{w}-{uuid.uuid4().hex[:4].upper()}" for w in ("PELICAN", "HERON", "OSPREY"))
    stack = Stack(tmp_path, "load")
    stack.ccfg = replace(stack.ccfg, rpc_timeout=SLOW)
    stack.control = ControlServer(stack.ccfg)
    stack.rcfg = replace(stack.rcfg, harness_commands={},
                         image=os.environ.get("VALET_AGENT_IMAGE", "valet/agent-base"),
                         passthrough={k: os.environ[k] for k in PASSTHROUGH_ENV if os.environ.get(k)})
    await stack.start()
    try:
        for skill, what, word in (("shared-word", "the shared code word", shared_word),
                                  ("unused-word", "the unused code word", unused_word)):
            r = await stack.http.post("/api/skills", json={
                "name": skill, "description": f"Use when asked for {what}.",
                "instructions": f"{what.capitalize()} is {word}. Answer with it exactly."})
            assert r.status_code == 201, r.text
        r = await stack.http.post("/api/agents", json={"name": name, "agent_yaml": AGENT.format(
            name=name, harness="claude" if harness is claude else "deepseek")})
        assert r.status_code == 201, r.text
        r = await stack.http.post(f"/api/agents/{name}/skills", json={
            "name": "own-word", "description": "Use when asked for your own code word.",
            "instructions": f"Your own code word is {own_word}. Answer with it exactly."})
        assert r.status_code == 201, r.text
        r = await stack.http.get(f"/api/agents/{name}/skills")
        assert r.json()["skills"] == [{"name": "own-word", "from": "agent"}, {"name": "shared-word", "from": "shared"}]

        r = await stack.http.post(f"/api/agents/{name}/start")
        assert r.status_code == 200, r.text

        async def running():
            a = await stack.agent(name)
            assert a["state"] != "failed", a["error"]
            return a["state"] == "running"
        await eventually(running, timeout=SLOW, what="the container agent running")

        # The skills are in the config dir, read-only inside the computer (keyless: no prompt needed).
        where = "/valet/claude-plugin/skills" if harness is claude else "/valet/skills"
        rc, out = await _docker("exec", container_name(name), "sh", "-c", f"ls {where}; touch {where}/x")
        assert rc != 0 and "Read-only file system" in out, out
        assert out.split()[:2] == ["own-word", "shared-word"], out

        r = await stack.http.post(f"/api/agents/{name}/sessions", timeout=SLOW)
        assert r.status_code == 201, r.text
        sid = r.json()["id"]
        turn = await stack.prompt(name, sid, "Using your skills, tell me the shared code word and your own code "
                                             "word. Reply with only the two code words.")

        async def ended():
            evs = [e for e in await stack.events(name, sid) if e["turn_id"] == turn]
            return evs if "turn_end" in kinds(evs) else None
        evs = await eventually(ended, timeout=SLOW, what="the turn to end")
        end = next(e["data"] for e in evs if e["kind"] == "turn_end")
        assert end["error"] is None, end
        said = texts(evs)
        assert shared_word in said and own_word in said, said
        assert unused_word not in said
        assert "permission_request" not in kinds(evs)       # loading a skill never asks
        assert await stack.pending() == []
    finally:
        await stack.close()
        await _docker("rm", "-f", container_name(name))
        for vol in (f"{container_name(name)}-home", f"{container_name(name)}-work"):
            await _docker("volume", "rm", "-f", vol)
