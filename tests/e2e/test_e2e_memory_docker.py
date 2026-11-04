"""Memory with the real thing: a Claude Code (or DeepSeek Harness) agent in a container (the agent-base image) saves
a fact with its valet.memory write tool, through the gateway; Valet commits it, and after a restart the agent's
instructions carry it (the `memory` layer), so a new session knows it without looking. Opt-in: VALET_TEST_DOCKER=1,
the image built, and the harness's key in the environment (CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY;
DEEPSEEK_API_KEY). Two short turns per harness."""
import asyncio
import os
import uuid
from dataclasses import replace

import pytest

from tests.e2e.stack import ControlServer, Stack, eventually, kinds, texts
from valet.runner.harness import claude, deepseek
from valet.shared.config import PASSTHROUGH_ENV

pytestmark = pytest.mark.docker
SLOW = 180.0
AGENT = """\
name: {name}
harness: {harness}
computer:
  level: container
permissions: trusted
memory: true
directives: [persona.md, environment, memory]
"""


@pytest.mark.parametrize("harness", [claude, deepseek], ids=["claude", "deepseek"])
async def test_a_container_agent_remembers_across_a_restart(tmp_path, harness):
    if not any(os.environ.get(k) for k in harness.KEYS):
        pytest.skip(f"needs one of {', '.join(harness.KEYS)}")
    name = "m4m-" + uuid.uuid4().hex[:6]
    word = "PITANGA" + uuid.uuid4().hex[:4].upper()
    stack = Stack(tmp_path, "load")
    url = os.environ.get("VALET_TEST_GATEWAY_URL") or f"http://host.docker.internal:{stack.ccfg.port}"
    stack.ccfg = replace(stack.ccfg, gateway_url=url, rpc_timeout=SLOW)
    stack.control = ControlServer(stack.ccfg)
    stack.rcfg = replace(stack.rcfg, harness_commands={},
                         image=os.environ.get("VALET_AGENT_IMAGE", "valet/agent-base"),
                         passthrough={k: os.environ[k] for k in PASSTHROUGH_ENV if os.environ.get(k)})
    await stack.start()
    try:
        r = await stack.http.post("/api/agents", json={
            "name": name, "persona": "You are a test agent. Keep replies short.\n",
            "agent_yaml": AGENT.format(name=name, harness="claude" if harness is claude else "deepseek")})
        assert r.status_code == 201, r.text
        feed = await stack.feed()
        r = await stack.http.post(f"/api/agents/{name}/start")
        assert r.status_code == 200, r.text

        def state_frames(s):
            return [i for i, f in enumerate(feed.frames)
                    if f["type"] == "agent" and f["agent"]["name"] == name and f["agent"]["state"] == s]

        async def running(after: int = -1):
            a = await stack.agent(name)
            assert a["state"] != "failed", a["error"]
            return any(i > after for i in state_frames("running"))
        await eventually(running, timeout=SLOW, what="the container agent running")
        assert not stack.rcfg.harness_commands      # the real adapter, in the container

        async def turn(sid: str, text: str) -> str:
            t = await stack.prompt(name, sid, text)

            async def ended():
                evs = [e for e in await stack.events(name, sid) if e["turn_id"] == t]
                return evs if "turn_end" in kinds(evs) else None
            evs = await eventually(ended, timeout=SLOW, what="the turn to end")
            end = next(e["data"] for e in evs if e["kind"] == "turn_end")
            assert end["error"] is None, end
            return texts(evs)

        r = await stack.http.post(f"/api/agents/{name}/sessions", timeout=SLOW)
        assert r.status_code == 201, r.text
        await turn(r.json()["id"], f"Save this in your Valet memory now, with your valet.memory write tool: a file "
                                   f"named `facts` whose text is \"The code word is {word}.\" Then reply with just "
                                   f"\"saved\".")
        mem = (await stack.http.get(f"/api/agents/{name}/memory")).json()
        assert mem["enabled"] is True and mem["files"], mem
        found = [(await stack.http.get(f"/api/agents/{name}/memory/{f['name']}")).json()["text"]
                 for f in mem["files"]]
        assert any(word in t for t in found), found
        told = (await stack.http.get(f"/api/agents/{name}/directives")).json()
        assert told["error"] is None and word in told["text"]

        # Restart: the new process starts with the memory layer, and a new session answers from it.
        mark = len(feed.frames)
        r = await stack.http.post(f"/api/agents/{name}/start")
        assert r.status_code == 200, r.text
        await eventually(lambda: any(i >= mark for i in state_frames("starting")), timeout=SLOW,
                         what="the agent restarting")
        await eventually(lambda: running(mark), timeout=SLOW, what="the agent running again")
        r = await stack.http.post(f"/api/agents/{name}/sessions", timeout=SLOW)
        assert r.status_code == 201, r.text
        reply = await turn(r.json()["id"], "Without using any tools, answer from your instructions: what is the "
                                           "code word in your memory? Reply with just the word.")
        assert word in reply, reply
    finally:
        await stack.close()
        for vol in (f"valet-agent-{name}-home", f"valet-agent-{name}-work"):
            p = await asyncio.create_subprocess_exec("docker", "volume", "rm", "-f", vol,
                                                     stdout=asyncio.subprocess.DEVNULL,
                                                     stderr=asyncio.subprocess.DEVNULL)
            await p.wait()
