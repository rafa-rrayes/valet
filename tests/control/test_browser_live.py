"""The owner's live view, /ws/browser/<profile> (control/browser/live.py) on a FakeBackend: who may open it, the
frames and the JSON messages, the input it dispatches, the action lock it shares with the agents, and what happens
when tabs close or the browser stops."""
import json
import time

import pytest
from starlette.websockets import WebSocketDisconnect

from tests.browser_fake import FakeBackend, FakeWeb
from valet.control.browser import live

EVIL = "https://blog.example.com"


def control(client):
    return client.app.state.control


@pytest.fixture
def fake(client) -> FakeBackend:
    backend = FakeBackend(FakeWeb())
    control(client).browser.backend = backend
    r = client.put("/api/browser/yaml", json={"text": "profiles:\n  alice: {}\n  work: {}\n"})
    assert r.status_code == 200
    return backend


class View:
    """The test's end of a live-view socket: frames and messages, read in order."""

    def __init__(self, ws):
        self.ws = ws
        self.frames: list[bytes] = []
        self.messages: list[dict] = []

    def next(self) -> dict | bytes:
        msg = self.ws.receive()
        if msg["type"] == "websocket.close":
            raise WebSocketDisconnect(msg.get("code", 1000))
        if msg.get("bytes") is not None:
            self.frames.append(msg["bytes"])
            return msg["bytes"]
        m = json.loads(msg["text"])
        self.messages.append(m)
        return m

    def until(self, pred, limit: int = 400):
        """The first message (or frame) `pred` accepts."""
        for _ in range(limit):
            m = self.next()
            if pred(m):
                return m
        raise AssertionError(f"not reached; messages: {self.messages[-10:]}")

    def message(self, type_: str, **match) -> dict:
        return self.until(lambda m: isinstance(m, dict) and m["type"] == type_
                          and all(m.get(k) == v for k, v in match.items()))

    def frame_of(self, tab: str) -> bytes:
        return self.until(lambda m: isinstance(m, bytes) and f"{tab}:".encode() in m)

    def send(self, **m) -> None:
        self.ws.send_text(json.dumps(m))


def ops(fake: FakeBackend, name: str) -> list[tuple]:
    return [op for op in fake.log if op[0] == name]


def eventually(fn, timeout: float = 5.0):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return
        time.sleep(0.02)
    raise AssertionError("condition not reached")


def test_who_may_open_it(client, fake):
    with client.websocket_connect("/ws/browser/alice", headers={"origin": EVIL}) as ws:
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_text()
        assert e.value.code == 4403
    for path in ("/ws/browser/ghost", "/ws/browser/Not_A_Name"):
        with client.websocket_connect(path) as ws:
            v = View(ws)
            with pytest.raises(WebSocketDisconnect) as e:
                while True:
                    v.next()
            assert e.value.code == 4404
            assert v.messages in ([], [{"type": "error", "error": "no browser profile named ghost"}])
    fake.fail = "Chrome isn't installed"
    with client.websocket_connect("/ws/browser/alice") as ws:
        v = View(ws)
        with pytest.raises(WebSocketDisconnect) as e:
            while True:
                v.next()
        assert e.value.code == 1011 and v.messages == [{"type": "error", "error": "Chrome isn't installed"}]
    fake.fail = None
    token = client.post("/api/login", json={"password": "correct horse battery", "client": "native"}).json()["token"]
    client.cookies.clear()
    with client.websocket_connect("/ws/browser/alice") as ws:
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_text()
        assert e.value.code == 4401
    # A native app's bearer token (or ?token= from a browser page) is the owner's.
    with client.websocket_connect(f"/ws/browser/alice?token={token}") as ws:
        assert View(ws).message("tabs")["tab"] == "t00001"
    with client.websocket_connect("/ws/browser/alice", headers={"Authorization": f"Bearer {token}"}) as ws:
        assert View(ws).message("tabs")["tab"] == "t00001"


def test_frames_messages_and_input(client, fake):
    with client.websocket_connect("/ws/browser/alice") as ws:
        v = View(ws)
        # No tab yet: it opens a blank one and streams it.
        assert v.message("tabs") == {"type": "tabs", "tabs": [{"tab": "t00001", "url": "about:blank", "title": ""}],
                                     "tab": "t00001"}
        v.message("page", tab="t00001", url="about:blank")
        assert v.message("busy") == {"type": "busy", "busy": False}
        v.frame_of("t00001")
        assert [b["viewers"] for b in client.get("/api/browser/running").json()] == [1]

        v.send(type="nav", url="github.com/alice")
        v.message("page", tab="t00001", url="https://github.com/alice", title="github.com")
        assert ("goto", "t00001", "https://github.com/alice") in fake.log
        v.send(type="tap", x=0.25, y=2)                 # clamped to the page
        v.send(type="mouse", action="move", x=0.5, y=0.5)
        v.send(type="mouse", action="drag", x=0.5, y=0.5)     # not an action: ignored
        v.send(type="wheel", x=0.5, y=0.5, dy=99999)
        v.send(type="text", text="hello")
        v.send(type="key", key="Enter")
        v.send(type="key", key="F12")                   # not one of the keys: ignored
        v.send(type="back")
        v.send(type="reload")
        v.send(type="whatever")
        ws.send_text("not json")
        v.send(type="open", url="https://example.com/")
        v.message("tabs", tab="t00002")
        v.frame_of("t00002")
        assert ops(fake, "mouse") == [("mouse", "t00001", "down", 0.25, 1.0), ("mouse", "t00001", "up", 0.25, 1.0),
                                      ("mouse", "t00001", "move", 0.5, 0.5)]
        assert ops(fake, "wheel") == [("wheel", "t00001", 5000.0)]
        assert ops(fake, "text") == [("text", "t00001", "hello")]
        assert ops(fake, "key") == [("key", "t00001", "Enter")]
        assert ops(fake, "back") == [("back", "t00001")] and ops(fake, "reload") == [("reload", "t00001")]
        assert ("goto", "t00002", "https://example.com/") in fake.log

        # Choosing a tab: its frames instead.
        v.send(type="tab", tab="t00001")
        v.message("tabs", tab="t00001")
        v.frame_of("t00001")
        v.send(type="tab", tab="t09999")
        assert v.message("error")["error"].startswith("no tab 't09999'") and v.messages[-1]["fatal"] is False

        # The tab it shows closes (an agent's close_tab): it moves to the newest one left.
        client.portal.call(fake.sessions["alice"].tab("t00001").close)
        v.message("tabs", tab="t00002")
    eventually(lambda: [b["viewers"] for b in client.get("/api/browser/running").json()] == [0])
    owner = [(a["agent"], a["action"], a["target"]) for a in client.get("/api/browser/audit").json()["actions"]]
    assert owner == [(None, "live_open", "https://example.com/"), (None, "live_navigate", "https://github.com/alice"),
                     (None, "live_view", "about:blank")]


def test_input_waits_for_an_agents_action(client, fake, monkeypatch):
    monkeypatch.setattr(live, "LOCK_WAIT", 0.3)
    with client.websocket_connect("/ws/browser/alice") as ws:
        v = View(ws)
        v.message("busy", busy=False)
        lock = fake.sessions["alice"].lock
        client.portal.call(lock.acquire)            # an agent's action in progress
        v.message("busy", busy=True)
        v.send(type="text", text="typed while busy")
        assert v.message("busy", dropped="text") == {"type": "busy", "busy": True, "dropped": "text"}
        assert ops(fake, "text") == []
        v.send(type="tab", tab="t00001")            # choosing what to watch needs no lock
        v.message("tabs", tab="t00001")
        client.portal.call(lock.release)
        v.message("busy", busy=False)
        v.send(type="text", text="now")
        eventually(lambda: ops(fake, "text") == [("text", "t00001", "now")])


def test_a_stopped_browser_ends_the_view(client, fake):
    with client.websocket_connect("/ws/browser/alice?tab=t00001") as ws:
        v = View(ws)
        v.message("tabs", tab="t00001")
        assert client.post("/api/browser/profiles/alice/stop").json()["was_running"] is True
        with pytest.raises(WebSocketDisconnect):
            v.message("nothing")
        assert {"type": "error", "error": "the browser stopped"} in v.messages
    # Opening it again starts the browser again; ?tab= picks a tab, an unknown one falls back to the newest.
    client.post("/api/browser/profiles/alice/tabs", json={"url": "https://a.example/"})
    client.post("/api/browser/profiles/alice/tabs", json={"url": "https://b.example/"})
    tabs = [t["tab"] for t in client.get("/api/browser/profiles/alice/tabs").json()]
    with client.websocket_connect(f"/ws/browser/alice?tab={tabs[1]}") as ws:
        assert View(ws).message("tabs")["tab"] == tabs[1]
    with client.websocket_connect("/ws/browser/alice?tab=nope") as ws:
        assert View(ws).message("tabs")["tab"] == tabs[0]


def test_the_viewer_says_its_size(client, fake):
    """A viewport message lays the streamed tab out at the viewer's size (clamped), at once and again for each tab it
    streams next, at the viewer's scale if it gives one; no lock needed (an agent's action doesn't hold it up); the
    same size again does nothing."""
    with client.websocket_connect("/ws/browser/alice") as ws:
        v = View(ws)
        v.message("busy", busy=False)
        v.send(type="viewport", width=393.4, height=652)
        eventually(lambda: ops(fake, "viewport") == [("viewport", "t00001", 393, 652)])
        v.send(type="viewport", width=393, height=652)               # the same: nothing
        v.send(type="viewport", width="wide", height=10)             # not numbers: ignored
        ws.send_text('{"type": "viewport", "width": 1e999, "height": 5}')     # infinite: ignored
        lock = fake.sessions["alice"].lock
        client.portal.call(lock.acquire)                             # an agent acting: a resize doesn't wait
        v.message("busy", busy=True)
        v.send(type="viewport", width=20, height=99999)              # clamped
        eventually(lambda: ops(fake, "viewport")[-1] == ("viewport", "t00001", 100, 4000))
        client.portal.call(lock.release)
        v.message("busy", busy=False)
        v.send(type="open", url="https://example.com/")              # a new tab: laid out at that size too
        v.message("tabs", tab="t00002")
        eventually(lambda: ("viewport", "t00002", 100, 4000) in ops(fake, "viewport"))
        assert len(ops(fake, "viewport")) == 3
        # The viewer's screen scale, for a sharp picture: clamped; not a number is Chrome's own.
        v.send(type="viewport", width=393, height=652, scale=2)
        eventually(lambda: ops(fake, "viewport")[-1] == ("viewport", "t00002", 393, 652, 2.0))
        v.send(type="viewport", width=393, height=652, scale=9)
        eventually(lambda: ops(fake, "viewport")[-1] == ("viewport", "t00002", 393, 652, 3.0))
        v.send(type="viewport", width=393, height=652, scale="retina")
        eventually(lambda: ops(fake, "viewport")[-1] == ("viewport", "t00002", 393, 652))
        ws.send_text('{"type": "viewport", "width": 393, "height": 652, "scale": NaN}')     # the same: nothing
        v.send(type="viewport", width=393, height=650, scale=0.5)
        eventually(lambda: ops(fake, "viewport")[-1] == ("viewport", "t00002", 393, 650, 1.0))
        assert len(ops(fake, "viewport")) == 7
