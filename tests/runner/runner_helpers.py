"""Runner test helpers (imported as runner_helpers): configs pointing at tests/runner/fake_acp.py, a collecting `send`, and waiting."""
import asyncio
import sys
from pathlib import Path

from valet.shared.config import RunnerConfig
from valet.shared.definition import AgentDefinition

FAKE = str(Path(__file__).with_name("fake_acp.py").resolve())


def fake_cmd(*extra: str) -> list[str]:
    return [sys.executable, FAKE, *extra]


def runner_cfg(tmp_path: Path, *extra: str, **kw) -> RunnerConfig:
    kw = {"control_url": "ws://127.0.0.1:9/ws/runner", "token": "vrun_test", "data": tmp_path / "runner",
          "harness_commands": {"claude": fake_cmd(*extra)}, **kw}
    return RunnerConfig(**kw)


def host_agent(name: str = "scout", **kw) -> AgentDefinition:
    return AgentDefinition.model_validate({"name": name, "computer": {"level": "host"}, **kw})


class Sent(list):
    """A `send` that records messages; `wait_for` polls until a matching one has arrived."""

    async def __call__(self, msg: dict) -> None:
        self.append(msg)

    def of(self, type_: str, **match) -> list[dict]:
        return [m for m in self if m["type"] == type_ and all(m.get(k) == v for k, v in match.items())]

    async def wait_for(self, type_: str, timeout: float = 10.0, **match) -> dict:
        async with asyncio.timeout(timeout):
            while True:
                if found := self.of(type_, **match):
                    return found[-1]
                await asyncio.sleep(0.01)

    def texts(self, session_id: str) -> list[str]:
        return [m["update"]["content"]["text"] for m in self.of("session.update", session_id=session_id)
                if m["update"]["sessionUpdate"] == "agent_message_chunk" and not m["replay"]]

