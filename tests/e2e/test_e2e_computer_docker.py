"""The agent's computer with the real thing: a container agent (the agent-base image, the Claude adapter, no key: it
starts, and nothing here prompts it). Its terminal is `docker exec` into its own container (its user, /work, resize),
and closing it ends what it ran in there; its files are the container's, confined to its folders; its volumes are
listed, kept while it runs, and deleted once it's stopped. Opt-in: VALET_TEST_DOCKER=1 and the image built. Names
start with `m4c-`; the containers and volumes made here are removed at the end. With a Claude key in the
environment (CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY), one more: a real Claude Code is sent an image from the
composer's path and says what it shows (one short turn: it spends real credit)."""
import asyncio
import contextlib
import json
import os
import subprocess
import uuid
from dataclasses import replace

import pytest
from websockets.asyncio.client import connect

from tests.e2e.stack import Stack, eventually, kinds, texts
from tests.e2e.test_e2e_computer import png
from valet.runner.harness import claude
from valet.shared.config import PASSTHROUGH_ENV

pytestmark = pytest.mark.docker
SLOW = 180.0
AGENT = """\
name: {name}
harness: claude
computer:
  level: container
permissions: ask
"""


def docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=60)


@contextlib.asynccontextmanager
async def container_agent(tmp_path, keys: bool = False):
    name = "m4c-" + uuid.uuid4().hex[:6]
    stack = Stack(tmp_path, "load")
    passthrough = {k: os.environ[k] for k in PASSTHROUGH_ENV if keys and os.environ.get(k)}
    stack.rcfg = replace(stack.rcfg, harness_commands={}, passthrough=passthrough,
                         image=os.environ.get("VALET_AGENT_IMAGE", "valet/agent-base"))
    stack.ccfg = replace(stack.ccfg, rpc_timeout=SLOW)
    stack.control.cfg = stack.ccfg
    await stack.start()
    try:
        r = await stack.http.post("/api/agents", json={"name": name, "agent_yaml": AGENT.format(name=name),
                                                       "persona": "A test agent.\n"})
        assert r.status_code == 201, r.text
        r = await stack.http.post(f"/api/agents/{name}/start", timeout=SLOW)
        assert r.status_code == 200, r.text

        async def running():
            a = await stack.agent(name)
            assert a["state"] != "failed", a["error"]
            return a["state"] == "running"
        await eventually(running, timeout=SLOW, what="the container agent running")
        yield stack, name
    finally:
        await stack.close()
        docker("rm", "-f", f"valet-agent-{name}")
        docker("volume", "rm", "-f", f"valet-agent-{name}-home", f"valet-agent-{name}-work")


async def recv_until(ws, out: dict, pred, timeout: float = 20.0) -> None:
    async def loop():
        while not pred():
            m = await ws.recv()
            if isinstance(m, bytes):
                out["bytes"] += m
            else:
                out["texts"].append(json.loads(m))
    await asyncio.wait_for(loop(), timeout)


def marked(name: str, terminal: str) -> list[str]:
    """The container's processes whose environment carries the terminal's mark."""
    r = docker("exec", f"valet-agent-{name}", "sh", "-c",
               f"grep -l 'VALET_TERMINAL={terminal}' /proc/[0-9]*/environ 2>/dev/null || true")
    return [x for x in r.stdout.split() if x and "/self/" not in x]


async def test_container_terminal(tmp_path):
    async with container_agent(tmp_path) as (stack, name):
        url = f"ws://127.0.0.1:{stack.ccfg.port}/ws/terminal/{name}?cols=100&rows=30&token={stack.owner_token}"
        async with connect(url) as ws:
            got = {"bytes": b"", "texts": []}
            await recv_until(ws, got, lambda: got["texts"])
            assert got["texts"][0]["type"] == "ready", got
            terminal = got["texts"][0]["terminal"]
            await ws.send(b"echo who=$(id -un) at=$(pwd); stty size; sleep 300 &\n")
            await recv_until(ws, got, lambda: b"30 100" in got["bytes"])
            assert b"who=agent at=/work" in got["bytes"]
            await ws.send(json.dumps({"type": "resize", "cols": 70, "rows": 15}))
            for _ in range(20):     # the docker CLI passes the new size on by itself, a moment later
                await ws.send(b"stty size\n")
                with contextlib.suppress(TimeoutError):
                    await recv_until(ws, got, lambda: b"15 70" in got["bytes"], timeout=0.5)
                if b"15 70" in got["bytes"]:
                    break
            assert b"15 70" in got["bytes"], got["bytes"][-300:]
            assert marked(name, terminal), "the shell and its sleep carry the mark"
        # Leaving ends the shell and what it started, inside the container too.
        await eventually(lambda: not marked(name, terminal), timeout=20, what="the terminal's processes gone")
        assert (await stack.agent(name))["state"] == "running"


async def test_container_files(tmp_path):
    async with container_agent(tmp_path) as (stack, name):
        c = f"valet-agent-{name}"
        assert docker("exec", c, "sh", "-c", "mkdir -p /work/src && echo hello > /work/src/a.txt "
                                             "&& ln -s /etc /work/etc-link && ln -s /home/agent /work/home-link"
                      ).returncode == 0
        base = f"/api/agents/{name}/computer/files"
        r = await stack.http.get(base)
        assert r.status_code == 200, r.text
        out = r.json()
        assert [x["path"] for x in out["roots"]] == ["/work", "/home/agent"] and out["path"] == "/work"
        entries = {e["name"]: (e["type"], e["link"]) for e in out["entries"]}
        assert entries["src"] == ("dir", False) and entries["etc-link"] == ("other", True)
        assert entries["home-link"] == ("dir", True)
        assert (await stack.http.get(base + "/text", params={"path": "/work/src/a.txt"})).json()["text"] == "hello\n"
        for path, text in [("/work/etc-link", "leads outside"), ("/etc", "outside its folders"),
                           ("/work/../etc", ". or ..")]:
            r = await stack.http.get(base, params={"path": path})
            assert r.status_code == 422 and text in r.json()["detail"], r.text
        data = os.urandom(3 * 1024 * 1024)
        r = await stack.http.post(base + "/upload", params={"dir": "/work/src", "name": "b.bin"}, content=data,
                                  timeout=SLOW)
        assert r.status_code == 201 and r.json() == {"path": "/work/src/b.bin", "size": len(data)}, r.text
        r = await stack.http.get(base + "/download", params={"path": "/work/src/b.bin"}, timeout=SLOW)
        assert r.content == data
        owner = docker("exec", c, "stat", "-c", "%U", "/work/src/b.bin").stdout.strip()
        assert owner == "agent"
        r = await stack.http.post(base + "/folder", json={"path": "/home/agent/notes/today"})
        assert r.status_code == 201 and docker("exec", c, "test", "-d", "/home/agent/notes/today").returncode == 0
        r = await stack.http.post(base + "/upload", params={"dir": "/work/etc-link", "name": "x"}, content=b"x")
        assert r.status_code == 422 and docker("exec", c, "test", "-e", "/etc/x").returncode != 0


async def test_container_volumes(tmp_path):
    async with container_agent(tmp_path) as (stack, name):
        url = f"/api/agents/{name}/computer/volumes"
        r = await stack.http.get(url)
        assert r.status_code == 200, r.text
        assert r.json()["running"] is True and sorted(v["name"] for v in r.json()["volumes"]) == [
            f"valet-agent-{name}-home", f"valet-agent-{name}-work"]
        r = await stack.http.delete(url)
        assert r.status_code == 409
        assert (await stack.http.post(f"/api/agents/{name}/stop", timeout=SLOW)).status_code == 200
        await stack.wait_state(name, "stopped")
        await eventually(lambda: docker("container", "inspect", f"valet-agent-{name}").returncode != 0,
                         timeout=30, what="the container gone")
        r = await stack.http.delete(url, timeout=SLOW)
        assert r.status_code == 200, r.text
        assert sorted(r.json()["removed"]) == [f"valet-agent-{name}-home", f"valet-agent-{name}-work"]
        assert docker("volume", "inspect", f"valet-agent-{name}-home").returncode != 0
        assert (await stack.http.get(url)).json()["volumes"] == []
        entries = (await stack.http.get(f"/api/agents/{name}/computer/audit")).json()["entries"]
        assert entries[0]["action"] == "volumes_delete" and entries[0]["outcome"] == "ok"


@pytest.mark.skipif(not any(os.environ.get(k) for k in claude.KEYS), reason="needs a Claude key")
async def test_claude_sees_an_image(tmp_path):
    async with container_agent(tmp_path, keys=True) as (stack, name):
        a = (await stack.agent(name))["capabilities"]
        assert a["promptCapabilities"]["image"] is True, a
        r = await stack.http.post(f"/api/agents/{name}/attachments", params={"name": "square.png"},
                                  content=png(48, 48))
        assert r.status_code == 201 and r.json()["image"] is True, r.text
        att = r.json()["id"]
        r = await stack.http.post(f"/api/agents/{name}/sessions", timeout=SLOW)
        assert r.status_code == 201, r.text
        sid = r.json()["id"]
        r = await stack.http.post(f"/api/agents/{name}/sessions/{sid}/prompt", timeout=SLOW, json={
            "text": "What single colour fills the attached image? Answer with just the colour's name, nothing else.",
            "attachments": [att]})
        assert r.status_code == 202, r.text
        turn = r.json()["turn_id"]

        async def ended():
            evs = [e for e in await stack.events(name, sid) if e["turn_id"] == turn]
            return evs if "turn_end" in kinds(evs) else None
        evs = await eventually(ended, timeout=SLOW, what="the turn to end")
        end = next(e["data"] for e in evs if e["kind"] == "turn_end")
        assert end["error"] is None, end
        assert "red" in texts(evs).lower(), texts(evs)
