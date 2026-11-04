"""The egress proxy itself (valet/runner/egress.mjs), run by node on 127.0.0.1 against a local target: what it lets
through, what it refuses and how. Matching is checked without any network: an allowed name that doesn't resolve is a
502 (it tried), a refused one a 403 (it didn't). Skipped without node."""
import asyncio
import json
import shutil
from pathlib import Path

import pytest

from valet.runner.egress import SCRIPT

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node isn't installed")


class Target:
    """A TCP server that answers one HTTP request with what it received, or echoes a tunnel's bytes."""

    def __init__(self) -> None:
        self.requests: list[bytes] = []

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def _serve(self, reader, writer) -> None:
        data = await reader.readuntil(b"\r\n\r\n")
        self.requests.append(data)
        if data.startswith(b"PING"):
            writer.write(b"PONG " + data)
        else:
            body = data
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(body) + body)
        await writer.drain()
        writer.close()


@pytest.fixture
async def proxy(tmp_path):
    """Start egress.mjs with rules -> its port. The process stops when the test ends (stdin closed)."""
    procs = []

    async def start(allow: list[dict]) -> int:
        rules = tmp_path / f"rules{len(procs)}.json"
        rules.write_text(json.dumps({"port": 0, "bind": "127.0.0.1", "allow": allow}))
        p = await asyncio.create_subprocess_exec("node", str(SCRIPT), str(rules), stdin=asyncio.subprocess.PIPE,
                                                 stdout=asyncio.subprocess.PIPE)
        procs.append(p)
        ready = json.loads(await asyncio.wait_for(p.stdout.readline(), 10))
        assert ready["event"] == "ready"
        return ready["port"]

    start.procs = procs
    yield start
    for p in procs:
        if p.returncode is None:
            p.stdin.close()
            await asyncio.wait_for(p.wait(), 10)


async def exchange(port: int, data: bytes, then: bytes = b"") -> tuple[bytes, bytes]:
    """Send `data`, read the proxy's response head (and body, until close or `then`'s answer) -> (head, rest)."""
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(data)
    await w.drain()
    head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 10)
    if then:
        w.write(then)
        await w.drain()
    rest = await asyncio.wait_for(r.read(), 10)
    w.close()
    return head, rest


async def decisions(proxy_start, n: int) -> list[dict]:
    p = proxy_start.procs[-1]
    return [json.loads(await asyncio.wait_for(p.stdout.readline(), 10)) for _ in range(n)]


async def test_connect_to_an_allowed_host_tunnels(proxy):
    target = Target()
    tport = await target.start()
    port = await proxy([{"host": "127.0.0.1", "ports": [tport]}])
    head, rest = await exchange(port, f"CONNECT 127.0.0.1:{tport} HTTP/1.1\r\nHost: x\r\n\r\n".encode(),
                                then=b"PING hello\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 200 ")
    assert rest == b"PONG PING hello\r\n\r\n"
    assert await decisions(proxy, 1) == [{"event": "allow", "method": "CONNECT", "host": "127.0.0.1", "port": tport}]


async def test_bytes_sent_with_the_connect_reach_the_target(proxy):
    """A client may send its first bytes (a TLS ClientHello) right behind the CONNECT, before the 200."""
    target = Target()
    tport = await target.start()
    port = await proxy([{"host": "127.0.0.1", "ports": [tport]}])
    head, rest = await exchange(port, f"CONNECT 127.0.0.1:{tport} HTTP/1.1\r\n\r\nPING early\r\n\r\n".encode())
    assert head.startswith(b"HTTP/1.1 200 ") and rest == b"PONG PING early\r\n\r\n"


async def test_plain_http_is_forwarded_in_origin_form_one_request_per_connection(proxy):
    target = Target()
    tport = await target.start()
    port = await proxy([{"host": "127.0.0.1", "ports": [tport]}])
    head, body = await exchange(port, (f"GET http://127.0.0.1:{tport}/mcp/agents/x?a=1 HTTP/1.1\r\n"
                                       f"Host: 127.0.0.1:{tport}\r\nProxy-Authorization: Basic eDp5\r\n"
                                       "Proxy-Connection: keep-alive\r\nConnection: keep-alive\r\n"
                                       "Authorization: Bearer vagt_x\r\n\r\n").encode())
    assert head.startswith(b"HTTP/1.1 200 OK")
    lines = body.decode().split("\r\n")
    assert lines[0] == "GET /mcp/agents/x?a=1 HTTP/1.1"
    assert "Authorization: Bearer vagt_x" in lines and "Connection: close" in lines
    assert not any(l.lower().startswith(("proxy-", "connection: keep-alive")) for l in lines)


@pytest.mark.parametrize("request_line,where", [
    ("CONNECT github.com:443", "github.com:443"),                   # not listed
    ("CONNECT api.anthropic.com:80", "api.anthropic.com:80"),       # listed, other port
    ("CONNECT evil-anthropic.com:443", "evil-anthropic.com:443"),
    ("CONNECT api.anthropic.com.evil.com:443", "api.anthropic.com.evil.com:443"),
    ("CONNECT sub.api.anthropic.com:443", "sub.api.anthropic.com:443"),   # a host rule is that host only
    ("CONNECT notexample.org:443", "notexample.org:443"),           # a domain is matched on a dot boundary
    ("CONNECT 127.0.0.1:443", "127.0.0.1:443"),
    ("GET http://github.com/ HTTP/1.1", "github.com:80"),
])
async def test_anything_else_is_refused_with_a_403_saying_why(proxy, request_line, where):
    port = await proxy([{"host": "api.anthropic.com", "ports": [443]}, {"domain": "example.org", "ports": [80, 443]}])
    line = request_line if request_line.endswith("HTTP/1.1") else request_line + " HTTP/1.1"
    head, body = await exchange(port, f"{line}\r\nHost: x\r\n\r\n".encode())
    assert head.startswith(b"HTTP/1.1 403 Forbidden")
    assert body.decode() == f"Valet: {where} is not on this agent's network allow list.\n"
    [d] = await decisions(proxy, 1)
    assert d["event"] == "deny" and f"{d['host']}:{d['port']}" == where


@pytest.mark.parametrize("request_line", [
    "CONNECT model.valet.invalid:443", "CONNECT MODEL.Valet.INVALID.:443",       # case, a trailing dot
    "CONNECT site.invalid:443", "CONNECT www.site.invalid:443", "CONNECT a.b.site.invalid:80",   # subdomains
    "GET http://www.site.invalid/x HTTP/1.1",
])
async def test_allowed_names_are_tried(proxy, request_line):
    """Allowed: the proxy connects, which fails here (.invalid never resolves): a 502 that says so, not a 403."""
    port = await proxy([{"host": "model.valet.invalid", "ports": [443]}, {"domain": "site.invalid", "ports": [80, 443]}])
    line = request_line if request_line.endswith("HTTP/1.1") else request_line + " HTTP/1.1"
    head, body = await exchange(port, f"{line}\r\n\r\n".encode())
    assert head.startswith(b"HTTP/1.1 502 Bad Gateway"), head
    assert body.startswith(b"Valet: can't reach ")


@pytest.mark.parametrize("data", [b"GET / HTTP/1.1\r\nHost: x\r\n\r\n",            # origin-form: not a proxy request
                                  b"GET https://x.org/ HTTP/1.1\r\n\r\n",            # https absolute-form
                                  b"CONNECT example.org HTTP/1.1\r\n\r\n",           # no port
                                  b"hello there\r\n\r\n"])
async def test_requests_that_arent_proxy_requests_are_a_400(proxy, data):
    port = await proxy([{"domain": "example.org", "ports": [80, 443]}])
    head, _ = await exchange(port, data)
    assert head.startswith(b"HTTP/1.1 400 ")


async def test_nothing_allowed_refuses_everything(proxy):
    port = await proxy([])
    head, _ = await exchange(port, b"CONNECT api.anthropic.com:443 HTTP/1.1\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 403")


async def test_it_exits_when_its_stdin_closes(proxy):
    await proxy([])
    p = proxy.procs[-1]
    p.stdin.close()
    assert await asyncio.wait_for(p.wait(), 10) == 0


def test_the_script_is_what_the_proxy_container_runs():
    assert SCRIPT == Path(__file__).parents[2] / "valet" / "runner" / "egress.mjs"
