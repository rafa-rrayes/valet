"""Control-plane fixtures: an app on tmp dirs, a signed-in TestClient, and a fake runner driven by hand over
/ws/runner (the test plays the runner's side of docs/PROTOCOL.md)."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from valet.control.__main__ import add_runner, set_password
from valet.control.app import create_app
from valet.shared.config import ControlConfig

PASSWORD = "correct horse battery"


@pytest.fixture
def cfg(tmp_path: Path) -> ControlConfig:
    return ControlConfig(data=tmp_path / "data", definitions=tmp_path / "data" / "definitions", rpc_timeout=5.0)


@pytest.fixture
def client(cfg):
    set_password(cfg, PASSWORD)
    with TestClient(create_app(cfg)) as c:
        assert c.post("/api/login", json={"password": PASSWORD}).status_code == 200
        yield c


@pytest.fixture
def pool():
    with ThreadPoolExecutor(4) as ex:
        yield ex


class FakeRunner:
    """The test's end of a runner socket."""

    def __init__(self, ws):
        self.ws = ws

    def hello(self, agents=(), turns=(), pending=(), boot="", seq=0, last_seq=None, harnesses=None, features=None):
        """pending: PermissionRequest dicts. last_seq: what the welcome must say, if given. harnesses, features: what
        it says it can run and do; None leaves the field out, as a runner from before it existed does."""
        hello = {"type": "hello", "protocol": 1, "version": "test", "boot": boot, "seq": seq, "agents": list(agents),
                 "turns": list(turns), "pending_permissions": list(pending)}
        if harnesses is not None:
            hello["harnesses"] = list(harnesses)
        if features is not None:
            hello["features"] = list(features)
        self.ws.send_json(hello)
        welcome = self.ws.receive_json()
        assert welcome["type"] == "welcome"
        if last_seq is not None:
            assert welcome["last_seq"] == last_seq, welcome
        return welcome

    def recv(self, type_: str | None = None) -> dict:
        msg = self.ws.receive_json()
        if type_:
            assert msg["type"] == type_, msg
        return msg

    def reply(self, cmd: dict, result: dict | None = None, error: str | None = None):
        self.ws.send_json({"type": "reply", "id": cmd["id"], "ok": error is None,
                           "result": result if error is None else None, "error": error})

    def send(self, **msg):
        self.ws.send_json(msg)

    def status(self, agent, state, **kw):
        self.send(type="agent.status", agent=agent, state=state, **kw)


@pytest.fixture
def runner_token(cfg, client):
    return add_runner(cfg, "rr")


@pytest.fixture
def connect(client, runner_token):
    """connect() -> context manager yielding a FakeRunner that has already said hello."""
    def _connect(token=None, **hello):
        ws_cm = client.websocket_connect("/ws/runner", headers={"Authorization": f"Bearer {token or runner_token}"})

        class _CM:
            def __enter__(self):
                self.ws = ws_cm.__enter__()
                r = FakeRunner(self.ws)
                r.hello(**hello)
                return r

            def __exit__(self, *exc):
                return ws_cm.__exit__(*exc)
        return _CM()
    return _connect


@pytest.fixture
def rpc(pool):
    """rpc(runner, fn, *args, expect=type, result=, error=) -> (response, command): make an HTTP call that waits on
    the runner, answer the command it sends, return both."""
    def _rpc(runner: FakeRunner, fn, *args, expect: str, result=None, error=None, **kwargs):
        fut = pool.submit(fn, *args, **kwargs)
        cmd = runner.recv(expect)
        runner.reply(cmd, result or {}, error)
        return fut.result(timeout=10), cmd
    return _rpc
