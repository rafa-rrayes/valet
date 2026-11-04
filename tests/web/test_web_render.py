"""The web UI's markdown and session log, rendered by its own JS under node with a stand-in DOM (tests/web/render.js).
Agent output is untrusted: the markdown must never produce markup the text didn't earn. Skipped without node."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).parent
STATIC = HERE.parent.parent / "valet" / "control" / "static"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node isn't installed")

SAFE_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "strong", "em", "del", "code", "pre", "ul", "ol", "li",
             "blockquote", "hr", "br", "a", "table", "thead", "tbody", "tr", "th", "td", "div"}
LINK_ATTRS = {"href", "target", "rel"}


def render(*jobs: dict) -> list[dict]:
    out = subprocess.run(["node", str(HERE / "render.js"), str(STATIC)], input=json.dumps(jobs), capture_output=True,
                         text=True, timeout=60, check=False)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def md(text: str) -> str:
    """markdown(text) as HTML, without the wrapping div."""
    return render({"md": text})[0]["html"][len("<div>"):-len("</div>")]


def assert_safe(result: dict) -> None:
    """Only the tags and attributes markdown.js makes, links only to http(s), and no markup in the serialized
    HTML that isn't one of those tags (text is escaped, so injected tags show up as &lt;...)."""
    for el in result["elements"]:
        assert el["tag"] in SAFE_TAGS, el
        allowed = LINK_ATTRS if el["tag"] == "a" else {"start"} if el["tag"] == "ol" else set()
        assert set(el["attrs"]) <= allowed, el
        assert el["className"] in ("", "table"), el
        if el["tag"] == "a":
            assert re.match(r"https?://", el["attrs"]["href"]), el
            assert el["attrs"]["rel"] == "noopener noreferrer"
        if el["tag"] == "ol" and "start" in el["attrs"]:
            assert el["attrs"]["start"].isdigit()
    for tag in re.findall(r"<\/?([a-zA-Z0-9]+)", result["html"]):
        assert tag in SAFE_TAGS, tag


# ---------- markdown ----------

@pytest.mark.parametrize("text, expected", [
    ("# Title", "<h1>Title</h1>"),
    ("### Three ###", "<h3>Three</h3>"),
    ("#hashtag", "<p>#hashtag</p>"),
    ("one\ntwo\n\nthree", "<p>one<br>two</p><p>three</p>"),
    ("**bold**, *it*, __b__, _i_, ~~gone~~",
     "<p><strong>bold</strong>, <em>it</em>, <strong>b</strong>, <em>i</em>, <del>gone</del></p>"),
    ("***both***", "<p><strong><em>both</em></strong></p>"),
    ("snake_case_name and 2 * 3 * 4", "<p>snake_case_name and 2 * 3 * 4</p>"),
    ("`a *b* <c>` and ``x ` y``", "<p><code>a *b* &lt;c&gt;</code> and <code>x ` y</code></p>"),
    (r"\*not\* \`code\`", "<p>*not* `code`</p>"),
    ("```console\n$ ls\n<a> **b**\n```\nafter", "<pre><code>$ ls\n&lt;a&gt; **b**</code></pre><p>after</p>"),
    ("~~~\ncode\n~~~", "<pre><code>code</code></pre>"),
    ("- a\n- b\n  - c\n- d", "<ul><li>a</li><li>b<ul><li>c</li></ul></li><li>d</li></ul>"),
    ("Steps:\n1. one\n   - sub\n2. two", "<p>Steps:</p><ol><li>one<ul><li>sub</li></ul></li><li>two</li></ol>"),
    ("3. three\n4. four", '<ol start="3"><li>three</li><li>four</li></ol>'),
    ("- a\n\n- b", "<ul><li>a</li><li>b</li></ul>"),
    ("- item\n\n  more of it", "<ul><li><p>item</p><p>more of it</p></li></ul>"),
    ("> quoted\n> **text**\n\nout", "<blockquote><p>quoted<br><strong>text</strong></p></blockquote><p>out</p>"),
    ("---", "<hr>"),
    ("| a | b |\n|:--|--:|\n| `1` | 2 |", ('<div class="table"><table><thead><tr><th style="text-align: left">a</th>'
                                        '<th style="text-align: right">b</th></tr></thead><tbody><tr>'
                                        '<td style="text-align: left"><code>1</code></td>'
                                        '<td style="text-align: right">2</td></tr></tbody></table></div>')),
    ("[site](https://example.com/a?b=1)",
     '<p><a href="https://example.com/a?b=1" target="_blank" rel="noopener noreferrer">site</a></p>'),
    ("see https://example.com/x.",
     '<p>see <a href="https://example.com/x" target="_blank" rel="noopener noreferrer">https://example.com/x</a>.</p>'),
    ("<https://example.com>",
     '<p><a href="https://example.com/" target="_blank" rel="noopener noreferrer">https://example.com</a></p>'),
])
def test_markdown(text, expected):
    assert md(text) == expected


def test_unclosed_fence_is_code_to_the_end():
    """What a code block still streaming in looks like."""
    assert md("Run:\n```sh\nnpm i\nnpm t") == "<p>Run:</p><pre><code>npm i\nnpm t</code></pre>"


@pytest.mark.parametrize("text", [
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    '<a href="javascript:alert(1)">x</a>',
    "[x](javascript:alert(1))",
    "[x](JaVaScRiPt:alert(1))",
    "[x](\tjavascript:alert(1))",
    "[x](java\\script:alert(1))",
    "[x](data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==)",
    "[x](vbscript:msgbox(1))",
    "[x](//evil.example/path)",
    "[x](/relative)",
    "![x](javascript:alert(1))",
    "<javascript:alert(1)>",
    "[x](https://ok.example/\"onmouseover=\"alert(1))",
    '[x](https://ok.example/ "title" onclick="alert(1)")',
    "[<img src=x onerror=alert(1)>](https://ok.example)",
    "# <svg onload=alert(1)>",
    "| <b>a</b> |\n|---|\n| <i onclick=x>b</i> |",
    "```\"><script>alert(1)</script>\n<script>alert(2)</script>\n```",
    "`<script>alert(1)</script>`",
    "> <iframe src=javascript:alert(1)>",
    "- <style>body{display:none}</style>",
    "**<b onmouseover=alert(1)>x</b>**",
    "javascript:alert(1) and https://ok.example/<script>",
])
def test_markdown_is_xss_safe(text):
    (result,) = render({"md": text})
    assert_safe(result)
    assert "javascript:" not in " ".join(e["attrs"].get("href", "") for e in result["elements"]).lower()


def test_safe_link_text_and_hrefs():
    (result,) = render({"md": "[x](javascript:alert(1)) [y](https://ok.example/\"onmouseover=\"alert(1))"})
    links = [e for e in result["elements"] if e["tag"] == "a"]
    assert [a["attrs"]["href"] for a in links] == ["https://ok.example/%22onmouseover=%22alert(1)"]
    assert result["html"].startswith("<div><p>x <a ")  # the unsafe link keeps its text, without a link


def test_hostile_input_is_bounded():
    """Deep nesting and long runs of unmatched delimiters render quickly, as text."""
    jobs = [{"md": ">" * 5000 + " deep"}, {"md": "- " * 3000 + "deep"}, {"md": "*a " * 20000},
            {"md": "_a " * 20000}, {"md": "[" * 20000}, {"md": "`" * 5000 + "x"}, {"md": "**" * 20000},
            {"md": "[a](" + "(" * 20000}, {"md": "x" * 200000}, {"md": "https://" + "a" * 100000}]
    for job, result in zip(jobs, render(*jobs)):
        assert_safe(result)
        assert result["ms"] < 1500, (job["md"][:20], result["ms"])


# ---------- the session log ----------

def ev(id_, kind, data, turn="T1"):
    return {"id": id_, "agent": "scout", "session_id": "S1", "turn_id": turn, "ts": 0, "kind": kind, "data": data}


def chunks(text, kind="agent_message", start=1, size=7):
    return [ev(start + i, kind, {"content": {"type": "text", "text": text[k:k + size]}})
            for i, k in enumerate(range(0, len(text), size))]


STORY = ("# The Keeper\n\nElias kept the **light** at `Hollow Point`.\n\n- oil\n- wicks\n\n```console\n$ date\n"
         "Fri Oct 30\n```\n\n> The sea *never* sleeps.\n\n| a | b |\n|---|---|\n| 1 | 2 |\n")


def test_streamed_message_renders_like_the_whole_text():
    """Chunks of one message are joined and rendered as one markdown text, whether drawn per chunk or per batch."""
    events = chunks(STORY)
    each, end, whole = render({"events": events, "flush": "each"}, {"events": events, "flush": "end"},
                              {"md": STORY})
    inner = whole["html"][len("<div>"):-len("</div>")]
    expected = f'<div><div class="msg agent md">{inner}</div></div>'
    assert each["html"] == end["html"] == expected


def test_thoughts_are_markdown_and_user_messages_are_not():
    events = [ev(1, "user_message", {"content": {"type": "text", "text": "**hi** <b>"}}),
              *chunks("I'll *check* it.", "agent_thought", start=2)]
    (result,) = render({"events": events})
    assert result["html"] == ('<div><div class="msg user">**hi** &lt;b&gt;</div><details class="thought">'
                              '<summary>Thinking</summary><div class="md"><p>I&#39;ll <em>check</em> it.</p>'
                              '</div></details></div>').replace("&#39;", "'")


# Shaped like Claude's real events (claude-agent-acp 0.99.1) from the M0 test pass.
WRITE = [
    ev(1, "tool_call", {"toolCallId": "w1", "title": "Preparing file…", "kind": "edit", "status": "pending",
                        "content": [], "locations": [], "rawInput": {}}),
    ev(2, "tool_call_update", {"toolCallId": "w1", "title": "Write hello.txt", "locations": [{"path": "/work/hello.txt"}],
                               "rawInput": {"file_path": "/work/hello.txt"}}),
    ev(3, "tool_call_update", {"toolCallId": "w1", "content": [{"path": "/work/hello.txt", "newText": "hi from valet",
                                                                 "type": "diff"}],
                               "rawInput": {"file_path": "/work/hello.txt", "content": "hi from valet"}}),
    ev(4, "permission_request", {"requestId": "R1", "toolCall": {
        "toolCallId": "w1", "kind": "edit", "status": "pending", "title": "Write hello.txt",
        "content": [{"path": "/work/hello.txt", "newText": "hi from valet", "type": "diff"}],
        "locations": [{"path": "/work/hello.txt"}]},
        "options": [{"optionId": "allow-once", "name": "Yes", "kind": "allow_once"},
                    {"optionId": "allow-with-updates", "name": "Yes, allow all edits", "kind": "allow_always"},
                    {"optionId": "reject", "name": "No", "kind": "reject_once"}]}),
]
WRITE_DONE = [
    ev(5, "permission_answer", {"requestId": "R1", "outcome": {"outcome": "selected", "optionId": "allow-once"},
                                "status": "answered"}),
    ev(6, "tool_call_update", {"toolCallId": "w1", "status": "completed",
                               "rawOutput": "File created successfully at: /work/hello.txt"}),
]
PENDING = [{"id": "R1", "status": "pending", "answer": None}]


def tool_cards(result):
    return re.findall(r'<details class="tool[^"]*"[^>]*>.*?</details>', result["html"], re.DOTALL)


def test_permission_shows_on_its_tool_card():
    asking, done = render({"events": WRITE, "inbox": PENDING}, {"events": WRITE + WRITE_DONE})
    (card,) = tool_cards(asking)
    assert card.startswith('<details class="tool asking" open><summary><span class="name">Write hello.txt</span>')
    assert '<span class="badge waiting">needs you</span>' in card
    assert '<pre class="diff"><span class="add">+ hi from valet\n</span></pre>' in card
    assert 'Allow this?' in card and "<button" in card
    assert re.findall(r"<button[^>]*>([^<]*)</button>", card) == ["Yes", "No"]
    assert 'class="perm' not in asking["html"]          # no separate permission card

    (card,) = tool_cards(done)
    assert card.startswith('<details class="tool"><summary>')    # collapsed once done
    assert '<span class="badge allowed">Allowed</span>' in card and '<span class="badge completed">done</span>' in card
    assert "<button" not in card and 'class="perm' not in done["html"]
    assert "File created" not in card                     # the diff says it; raw output only without content


def test_rejected_bash_shows_command_output_and_answer():
    events = [
        ev(1, "tool_call", {"toolCallId": "b1", "title": "Terminal", "kind": "execute", "status": "pending",
                            "content": [], "rawInput": {}}),
        ev(2, "tool_call_update", {"toolCallId": "b1", "title": "rm /work/hello.txt",
                                   "rawInput": {"command": "rm /work/hello.txt"}}),
        ev(3, "permission_request", {"requestId": "R2", "toolCall": {"toolCallId": "b1", "title": "rm /work/hello.txt"},
                                     "options": [{"optionId": "allow-once", "name": "Yes", "kind": "allow_once"},
                                                 {"optionId": "reject", "name": "No", "kind": "reject_once"}]}),
        ev(4, "permission_answer", {"requestId": "R2", "outcome": {"outcome": "selected", "optionId": "reject"},
                                    "status": "answered"}),
        ev(5, "tool_call_update", {"toolCallId": "b1", "status": "failed", "content": [
            {"type": "content", "content": {"type": "text", "text": "```\nUser refused permission to run tool\n```"}}],
            "rawOutput": "User refused permission to run tool"}),
        ev(6, "tool_call", {"toolCallId": "b2", "title": "date", "kind": "execute", "status": "pending",
                            "rawInput": {"command": "date"}}),
        ev(7, "tool_call_update", {"toolCallId": "b2", "status": "completed", "content": [
            {"type": "content", "content": {"type": "text", "text": "```console\nFri Oct 30 15:39:13 UTC 2026\n```"}}]}),
    ]
    (result,) = render({"events": events})
    rejected, ok = tool_cards(result)
    assert '<span class="badge rejected">Rejected</span>' in rejected and "failed</span>" in rejected
    assert '<pre class="cmd">$ rm /work/hello.txt</pre>' in rejected
    assert "<pre><code>User refused permission to run tool</code></pre>" in rejected
    assert '<pre class="cmd">$ date</pre>' in ok
    assert '<div class="md out"><pre><code>Fri Oct 30 15:39:13 UTC 2026</code></pre></div>' in ok
    assert "badge allowed" not in ok and "badge rejected" not in ok


# Shaped like DeepSeek Harness's real events (dsh 0.2.1-rc.5) from the M1 test pass: the title is the tool's name,
# the command and a description are in rawInput, and the permission request names only the tool call.
DSH = [
    ev(1, "tool_call", {"toolCallId": "d1", "title": "bash", "kind": "other", "status": "in_progress",
                        "rawInput": {"description": "Append a line", "command": "echo 'second line' >> /work/hello.txt"}}),
    ev(2, "permission_request", {"requestId": "R3", "toolCall": {"toolCallId": "d1"},
                                 "options": [{"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
                                             {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"}]}),
]
DSH_DONE = [
    ev(3, "permission_answer", {"requestId": "R3", "outcome": {"outcome": "selected", "optionId": "allow-once"},
                                "status": "answered"}),
    ev(4, "tool_call_update", {"toolCallId": "d1", "status": "completed",
                               "content": [{"type": "content", "content": {"type": "text", "text": "ok"}}]}),
]


def test_deepseek_bash_card_asks_with_its_command():
    asking, done = render({"events": DSH, "inbox": [{"id": "R3", "status": "pending", "answer": None}]},
                          {"events": DSH + DSH_DONE})
    (card,) = tool_cards(asking)
    assert card.startswith('<details class="tool asking" open><summary><span class="name">bash · Append a line</span>'
                           '<span class="badge waiting">needs you</span>')        # no "other" kind
    assert '<pre class="cmd">$ echo &#39;second line&#39; &gt;&gt; /work/hello.txt</pre>'.replace("&#39;", "'") in card
    assert re.findall(r"<button[^>]*>([^<]*)</button>", card) == ["Allow once", "Reject"]
    (card,) = tool_cards(done)
    assert '<span class="badge allowed">Allowed</span>' in card and '<div class="md out"><p>ok</p></div>' in card


def test_permission_without_a_tool_card_stands_alone():
    events = [ev(1, "permission_request", {"requestId": "R9", "toolCall": {"toolCallId": "nope", "title": "Do it",
                                                                           "rawInput": {"x": 1}},
                                           "options": [{"optionId": "a", "name": "Allow", "kind": "allow_once"}]})]
    asking, done = render({"events": events, "inbox": [{"id": "R9", "status": "pending", "answer": None}]},
                          {"events": events + [ev(2, "permission_answer", {"requestId": "R9", "status": "answered",
                                                                           "outcome": {"outcome": "selected",
                                                                                       "optionId": "a"}})]})
    assert '<div class="perm">' in asking["html"] and "<button" in asking["html"] and "&quot;x&quot;: 1" in asking["html"]
    assert '<div class="perm closed">' in done["html"] and '<div class="result">Allowed</div>' in done["html"]


def test_tools_of_an_ended_turn_collapse():
    events = [ev(1, "tool_call", {"toolCallId": "t1", "title": "Long", "kind": "execute", "status": "in_progress",
                                  "rawInput": {"command": "sleep 99"}})]
    running, ended = render({"events": events},
                            {"events": events + [ev(2, "turn_end", {"stopReason": "cancelled", "error": None})]})
    assert tool_cards(running)[0].startswith('<details class="tool" open>')
    assert tool_cards(ended)[0].startswith('<details class="tool">')


def test_edit_diff_is_a_line_diff():
    events = [ev(1, "tool_call", {"toolCallId": "e1", "title": "Edit a.py", "kind": "edit", "status": "completed",
                                  "content": [{"type": "diff", "path": "/w/a.py",
                                               "oldText": "a\nb\nc\nd\ne\nold\nf",
                                               "newText": "a\nb\nc\nd\ne\nnew\nnewer\nf"}]})]
    (result,) = render({"events": events})
    diff = re.search(r'<pre class="diff">(.*?)</pre>', result["html"], re.DOTALL).group(1)
    lines = re.findall(r'<span class="(\w+)">([^<]*)</span>', diff)
    assert lines == [("ctx", "…\n"), ("ctx", "  c\n"), ("ctx", "  d\n"), ("ctx", "  e\n"), ("del", "- old\n"),
                     ("add", "+ new\n"), ("add", "+ newer\n"), ("ctx", "  f\n")]


# A gateway ask in the conversation (hub.ask_tool): a permission_request whose toolCallId is "gateway-<item id>",
# with no tool card of its own, carrying the tool's details. An event recorded before it carried them shows its
# title as it is.
GATEWAY_TOOL = {"server": "github", "name": "create_issue", "title": "Create issue", "description": "Open a new issue.",
                "arguments": {"title": "Bug <b>"}, "annotations": None}
GATEWAY = [ev(1, "permission_request", {"requestId": "g1", "toolCall": {
    "toolCallId": "gateway-g1", "title": "github: Create issue", "kind": "other", "rawInput": {"title": "Bug <b>"}},
    "options": [{"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"},
                {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"}], "tool": GATEWAY_TOOL})]
ALLOWED = ev(2, "permission_answer", {"requestId": "g1", "status": "answered",
                                      "outcome": {"outcome": "selected", "optionId": "allow_once"}})


def test_gateway_ask_shows_the_server_and_tool():
    asking, done = render({"events": GATEWAY, "inbox": [{"id": "g1", "kind": "tool", "status": "pending",
                                                         "answer": None, "tool": GATEWAY_TOOL}]},
                          {"events": GATEWAY + [ALLOWED]})
    assert asking["html"].startswith('<div><div class="perm toolask"><div class="title">scout wants to use '
                                     '<span class="tool-name">github › Create issue</span></div>'
                                     '<div class="sub desc">Open a new issue.</div>'
                                     '<details class="args"><summary>Arguments</summary>'
                                     '<pre>{\n  &quot;title&quot;: &quot;Bug &lt;b&gt;&quot;\n}</pre></details>')
    assert re.findall(r"<button[^>]*>([^<]*)</button>", asking["html"]) == ["Allow once", "Reject"]
    # answered, from the history alone (no inbox item): the event has what it needs
    assert '<div class="perm closed toolask">' in done["html"]
    assert '<span class="tool-name">github › Create issue</span>' in done["html"] and "Open a new issue." in done["html"]
    assert '<div class="result">Allowed</div>' in done["html"] and "<button" not in done["html"]


def test_older_gateway_ask_shows_its_title():
    old = [{**GATEWAY[0], "data": {k: v for k, v in GATEWAY[0]["data"].items() if k != "tool"}}, ALLOWED]
    (r,) = render({"events": old})
    assert '<span class="tool-name">github: Create issue</span>' in r["html"]
    assert "&quot;title&quot;: &quot;Bug &lt;b&gt;&quot;" in r["html"]
