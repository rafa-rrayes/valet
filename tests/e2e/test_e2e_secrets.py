"""A secret from end to end: set once through the API, listed by name in a definition, in the harness's env when
the agent starts; restart awareness when it changes; never in what the control plane stores or shows (until the
agent itself says it: the fake agent's `env` reply is the agent's own output, stored like any other)."""
from tests.e2e.stack import AGENT_YAML, texts

FIRST = "ghp_TEST_first-not-a-real-token_0123"
SECOND = "ghp_TEST_second-not-a-real-token_4567"


async def nowhere(stack, feed, value: str) -> None:
    """`value` isn't in any frame the client got, any listing, the control plane's database, or the config dir."""
    assert value not in str(feed.frames)
    for path in ("/api/agents", "/api/agents/scout", "/api/secrets", "/api/runners"):
        assert value not in (await stack.http.get(path)).text, path
    assert not [p for p in stack.ccfg.data.glob("valet.db*") if value.encode() in p.read_bytes()]
    assert not [p for p in stack.rcfg.data.rglob("*") if p.is_file() and value.encode() in p.read_bytes()]


async def test_secret_reaches_the_agent_and_nothing_else(stack):
    feed = await stack.feed()
    yaml = AGENT_YAML.format(name="scout") + "secrets: [GITHUB_TOKEN]\n"
    r = await stack.http.post("/api/agents", json={"name": "scout", "agent_yaml": yaml})
    assert r.status_code == 201, r.text
    r = await stack.http.post("/api/agents/scout/start")
    assert r.status_code == 422 and r.json()["missing_secrets"] == ["GITHUB_TOKEN"]

    r = await stack.http.put("/api/secrets/GITHUB_TOKEN", json={"value": FIRST})
    assert r.status_code == 200 and r.json()["used_by"] == ["scout"]
    await stack.start_agent("scout")
    sid = (await stack.new_session("scout"))["id"]
    await nowhere(stack, feed, FIRST)
    evs = await stack.run_turn("scout", sid, "env GITHUB_TOKEN")
    assert texts(evs) == '{"GITHUB_TOKEN": "' + FIRST + '"}'

    # A new value: the running agent says it needs a restart, and gets it when restarted.
    await stack.http.put("/api/secrets/GITHUB_TOKEN", json={"value": SECOND})
    assert (await stack.agent("scout"))["restart_needed"] is True
    await stack.restart_agent("scout")
    assert (await stack.agent("scout"))["restart_needed"] is False
    await nowhere(stack, feed, SECOND)
    evs = await stack.run_turn("scout", sid, "env GITHUB_TOKEN")
    assert texts(evs) == '{"GITHUB_TOKEN": "' + SECOND + '"}'
