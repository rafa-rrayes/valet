"""A local stand-in for the owner's webhook (a Slack or Discord incoming webhook, say): an HTTP server in its own
thread that records every JSON body POSTed to it and answers `status`.

    hook = FakeWebhook().start()
    hook.url            # http://127.0.0.1:<port>/hook/T0KEN (the path stands for the credential such URLs carry)
    hook.bodies         # [dict]
    hook.stop()"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeWebhook:
    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.bodies: list[dict] = []
        self.paths: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(n)
                outer.paths.append(self.path)
                try:
                    outer.bodies.append(json.loads(raw))
                except ValueError:
                    outer.bodies.append({"raw": raw.decode(errors="replace")})
                self.send_response(outer.status)
                self.send_header("content-length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/hook/T0KEN-secret-part"

    def start(self) -> "FakeWebhook":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
