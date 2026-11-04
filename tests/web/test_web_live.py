"""The web UI's live view (browser.js's LiveView) over /ws/browser/{profile}: frames drawn, the owner's input as the
socket's messages (docs/API.md, "The live view"), by mouse and by touch, tabs, the busy state, errors and
reconnecting, and the asks waiting on the profile. Run by its own JS under node with a stand-in DOM and a fake socket
(tests/web/live.js). Skipped without node."""
import json
import subprocess

from tests.web.test_web_browser import ASK, HUMAN, browser_item
from tests.web.test_web_settings import HERE, STATIC, agent, pytestmark  # noqa: F401

TABS = {"type": "tabs", "tabs": [{"tab": "t1", "url": "https://github.com/", "title": "GitHub"},
                                 {"tab": "t2", "url": "about:blank", "title": ""}], "tab": "t1"}
PAGE = {"type": "page", "tab": "t1", "url": "https://github.com/", "title": "GitHub"}
UP = [{"open": True}, {"msg": TABS}, {"msg": PAGE}, {"frame": [1280, 900]}]


def live(*jobs: dict) -> list[dict]:
    jobs = [{"profile": "work", "agents": [agent()], **j} for j in jobs]
    out = subprocess.run(["node", str(HERE / "live.js"), str(STATIC)], input=json.dumps(jobs), capture_output=True,
                         text=True, timeout=60, check=False)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def one(steps, **job) -> dict:
    (r,) = live({"steps": steps, **job})
    return r


def sent(r) -> list[dict]:
    return [m for s in r["sockets"] for m in s["sent"]]


def test_it_opens_the_profile_socket_and_draws_frames():
    r = one(UP + [{"frame": [1280, 900]}], tab="t1")
    (sock,) = r["sockets"]
    assert sock["url"] == "ws://valet.test/ws/browser/work?tab=t1"
    assert r["drawn"] == 2 and r["canvas"]["width"] == 1280 and r["canvas"]["height"] == 900
    assert r["canvas"]["hidden"] is False and "Starting the browser" not in r["seen"]
    assert r["canvas"]["style"]["width"] == "640px"                           # Fit: 640 of 644 wide, by 450 high
    assert r["tabs"] == [["t1", "GitHub"], ["t2", "New tab"]] and r["tab"] == "t1" and not r["tabsDisabled"]
    assert r["status"] == "GitHub"
    assert one([])["sockets"][0]["url"] == "ws://valet.test/ws/browser/work"           # no tab: its newest


def test_the_tab_it_shows_goes_in_the_address():
    r = one([{"open": True}, {"msg": {**TABS, "tab": "t2"}}], tab="t1")
    assert r["replaced"] == ["#/browser/work/live/t2"]


def test_a_mouse_clicks_drags_and_double_clicks_at_fractions_of_the_frame():
    r = one(UP + [{"pointer": "down", "x": 320, "y": 225, "t": 1000}, {"pointer": "up", "x": 320, "y": 225, "t": 1080},
                  {"pointer": "down", "x": 321, "y": 225, "t": 1200}, {"pointer": "up", "x": 321, "y": 225, "t": 1260},
                  {"pointer": "move", "x": 64, "y": 45}, {"wait": 150}])
    assert sent(r) == [
        {"type": "mouse", "action": "down", "x": 0.5, "y": 0.5, "clicks": 1},
        {"type": "mouse", "action": "up", "x": 0.5, "y": 0.5, "clicks": 1},
        {"type": "mouse", "action": "down", "x": 321 / 640, "y": 0.5, "clicks": 2},
        {"type": "mouse", "action": "up", "x": 321 / 640, "y": 0.5, "clicks": 2},
        {"type": "mouse", "action": "move", "x": 0.1, "y": 0.1}]


def test_a_touch_tap_is_a_tap_and_a_swipe_scrolls_the_page():
    tap = one(UP + [{"pointer": "down", "type": "touch", "x": 64, "y": 90},
                    {"pointer": "up", "type": "touch", "x": 66, "y": 92}])
    assert sent(tap) == [{"type": "tap", "x": 0.1, "y": 0.2}]
    swipe = one(UP + [{"pointer": "down", "type": "touch", "x": 320, "y": 300},
                      {"pointer": "move", "type": "touch", "x": 320, "y": 250},
                      {"pointer": "move", "type": "touch", "x": 320, "y": 200},
                      {"pointer": "up", "type": "touch", "x": 320, "y": 200}, {"wait": 80}])
    (wheel,) = sent(swipe)                                       # batched: one message
    assert wheel["type"] == "wheel" and wheel["dy"] == 200       # 100 px up on a frame shown at half size
    # A pan the browser took over (zoomed in): no tap.
    cancelled = one(UP + [{"pointer": "down", "type": "touch", "x": 64, "y": 90}, {"pointer": "cancel"},
                          {"pointer": "up", "type": "touch", "x": 64, "y": 90}])
    assert sent(cancelled) == []


def test_the_wheel_scrolls_and_the_scroll_buttons_too():
    r = one(UP + [{"wheel": 100, "x": 320, "y": 225}, {"wheel": 60, "x": 320, "y": 225}, {"wait": 80},
                  {"wheel": 3, "mode": 1, "x": 0, "y": 0}, {"wait": 80}, {"button": "Scroll down"}])
    assert sent(r) == [{"type": "wheel", "x": 0.5, "y": 0.5, "dy": 160}, {"type": "wheel", "x": 0, "y": 0, "dy": 120},
                       {"type": "wheel", "x": 0.5, "y": 0.5, "dy": 630}]


def test_keys_typed_on_the_frame_go_as_text_and_keys_in_order():
    r = one(UP + [{"key": "h"}, {"key": "i"}, {"key": "Enter"}, {"key": "v", "meta": True}, {"paste": "pasted"},
                  {"key": "x"}, {"wait": 100}])
    assert sent(r) == [{"type": "text", "text": "hi"}, {"type": "key", "key": "Enter"},
                       {"type": "text", "text": "pasted"}, {"type": "text", "text": "x"}]


def test_the_bar_navigates_types_and_switches_tabs():
    r = one(UP + [{"input": "live-address", "value": "news.ycombinator.com"}, {"submit": "live-address"},
                  {"button": "Back"}, {"button": "Reload"}, {"change": "live-tab", "value": "t2"},
                  {"button": "New tab"}, {"input": "live-text", "value": "hello"}, {"submit": "live-text"},
                  {"button": "Enter key"}, {"button": "Escape key"}])
    assert sent(r) == [{"type": "nav", "url": "news.ycombinator.com"}, {"type": "back"}, {"type": "reload"},
                       {"type": "tab", "tab": "t2"}, {"type": "open"}, {"type": "text", "text": "hello"},
                       {"type": "key", "key": "Enter"}, {"type": "key", "key": "Escape"}]


def test_zoom_steps_through_fit_two_and_three_times():
    r = one(UP + [{"button": "Zoom in"}, {"button": "Zoom in"}, {"button": "Zoom in"}])
    assert r["zoom"] == "3×" and r["canvas"]["style"]["width"] == "1920px"
    r = one(UP + [{"button": "Zoom in"}, {"button": "Zoom out"}, {"button": "Zoom out"}])
    assert r["zoom"] == "Fit" and r["canvas"]["style"]["width"] == "640px"


def test_busy_and_a_dropped_input_say_so():
    r = one(UP + [{"msg": {"type": "busy", "busy": True}}])
    assert r["status"].startswith("An agent is using the browser")
    r = one(UP + [{"msg": {"type": "busy", "busy": False, "dropped": "tap"}}])
    assert "Your tap was dropped" in r["status"]


def test_errors_and_closes():
    stopped = one(UP + [{"msg": {"type": "error", "error": "the browser stopped"}}, {"close": 1011}])
    assert "The browser stopped." in stopped["status"] and "Start it again" in stopped["status"]
    assert len(stopped["sockets"]) == 1                                      # no retry on its own
    again = one(UP + [{"msg": {"type": "error", "error": "the browser stopped"}}, {"close": 1011},
                      {"button": "Start it again"}])
    assert len(again["sockets"]) == 2
    gone = one([{"msg": {"type": "error", "error": "no browser profile named work"}}, {"close": 4404}])
    assert "no browser profile named work" in gone["status"]
    origin = one([{"close": 4403}])
    assert "VALET_PUBLIC_URL" in origin["status"]
    soft = one(UP + [{"msg": {"type": "error", "error": "that address didn't load", "fatal": False}}])
    assert "that address didn't load" in soft["status"] and len(soft["sockets"]) == 1
    dropped = one(UP + [{"close": 1006}, {"wait": 1100}])
    assert len(dropped["sockets"]) == 2 and dropped["sockets"][0]["closed"] is False
    signed_out = one([{"close": 4401}], routes={"GET /api/me": {"status": 200, "body": {"owner": False}}})
    assert signed_out["signedOut"] is True


def test_input_before_the_socket_is_up_says_so():
    r = one([{"button": "Back"}])
    assert sent(r) == [] and "Not connected yet" in r["status"]


def test_hidden_closes_the_socket_and_visible_dials_again():
    r = one(UP + [{"hidden": True}, {"hidden": False}])
    assert [s["closed"] for s in r["sockets"]] == [True, False]
    r = one(UP + [{"close_view": True}])
    assert r["sockets"][0]["closed"] is True


def test_asks_on_this_profile_wait_in_a_bar_that_unfolds_to_their_answers():
    human = {**ASK, "kind": "human", "tab": "t2", "summary": "take over", "detail": {"reason": "the captcha"}}
    other = browser_item({**ASK, "profile": "home"}, id="b2")
    folded = one(UP, inbox=[browser_item(human, HUMAN), other])
    assert "Scout: take over" in folded["seen"] and "I can't" not in folded["seen"]      # the page keeps the room
    r = one(UP + [{"button": "Show"}], inbox=[browser_item(human, HUMAN), other])
    assert "Scout needs you to take over" in r["seen"] and ": the captcha" in r["seen"]
    assert "I can't" in r["seen"] and "Open the live view" not in r["seen"]   # here already
    assert "/api/browser/shots/" not in r["html"] and "Show its tab" in r["seen"]
    assert "Merge pull request" not in r["seen"]                             # another profile's
    two = one(UP, inbox=[browser_item(human, HUMAN), browser_item(id="b3")])
    assert "2 asks wait on you" in two["seen"]
    switched = one(UP + [{"button": "Show"}, {"button": "Show its tab"}], inbox=[browser_item(human, HUMAN)])
    assert sent(switched) == [{"type": "tab", "tab": "t2"}]
    # One that comes in while watching unfolds the bar; answered, it goes.
    came = one(UP + [{"app": {"type": "inbox", "item": browser_item(human, HUMAN)}}])
    assert "Scout needs you to take over" in came["seen"]
    answered = one(UP + [{"app": {"type": "inbox", "item": browser_item(human, HUMAN, status="answered")}}],
                   inbox=[browser_item(human, HUMAN)])
    assert "take over" not in answered["seen"]
