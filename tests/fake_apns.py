"""A stand-in for Apple's push service (APNs) for tests: HTTP/2 only, over cleartext with prior knowledge (the `h2`
library's server side), so a client that falls back to HTTP/1.1 fails here as it would at Apple. It checks the
provider token the way Apple does (ES256 over header.claims with the team's public key) and records every push.

    apns = FakeApns(public_key, team_id="TEAM123456", key_id="KEY1234567").start()   # its own thread and loop
    apns.url            # http://127.0.0.1:<port>, for VALET_APNS_URL
    apns.answers[token] = (410, {"reason": "Unregistered"})
    apns.pushes         # [{"token", "headers", "payload", "claims"}] that it accepted or refused
    apns.stop()

Device tokens with no answer set get 200; a bad provider token gets 403 InvalidProviderToken."""
import asyncio
import base64
import json
import threading
import uuid

import h2.config
import h2.connection
import h2.events
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def check_jwt(token: str, public_key: ec.EllipticCurvePublicKey, team_id: str, key_id: str) -> dict | None:
    """The claims of a valid APNs provider token, else None."""
    try:
        head, claims, sig = token.split(".")
        header = json.loads(_unb64(head))
        raw = _unb64(sig)
        if header != {"alg": "ES256", "kid": key_id} or len(raw) != 64:
            return None
        der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
        public_key.verify(der, f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))
        body = json.loads(_unb64(claims))
        return body if body.get("iss") == team_id and isinstance(body.get("iat"), int) else None
    except (ValueError, InvalidSignature):
        return None


class FakeApns:
    def __init__(self, public_key: ec.EllipticCurvePublicKey, team_id: str, key_id: str) -> None:
        self.public_key, self.team_id, self.key_id = public_key, team_id, key_id
        self.answers: dict[str, tuple[int, dict | None]] = {}
        self.pushes: list[dict] = []
        self.connections = 0
        self.port = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "FakeApns":
        ready = threading.Event()

        def run():
            self._loop = asyncio.new_event_loop()

            async def main():
                self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
                self.port = self._server.sockets[0].getsockname()[1]
                ready.set()
                async with self._server:
                    await self._server.serve_forever()
            try:
                self._loop.run_until_complete(main())
            except asyncio.CancelledError:
                pass
            finally:
                self._loop.close()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        assert ready.wait(5), "fake APNs didn't start"
        return self

    def stop(self) -> None:
        if self._loop and self._server:
            self._loop.call_soon_threadsafe(self._server.close)
            for task in asyncio.all_tasks(self._loop):
                self._loop.call_soon_threadsafe(task.cancel)
        if self._thread:
            self._thread.join(5)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        conn = h2.connection.H2Connection(h2.config.H2Configuration(client_side=False, header_encoding="utf-8"))
        conn.initiate_connection()
        writer.write(conn.data_to_send())
        streams: dict[int, dict] = {}
        try:
            while data := await reader.read(65536):
                for ev in conn.receive_data(data):
                    if isinstance(ev, h2.events.RequestReceived):
                        streams[ev.stream_id] = {"headers": dict(ev.headers), "body": b""}
                    elif isinstance(ev, h2.events.DataReceived):
                        streams[ev.stream_id]["body"] += ev.data
                        conn.acknowledge_received_data(ev.flow_controlled_length, ev.stream_id)
                    elif isinstance(ev, h2.events.StreamEnded):
                        self._answer(conn, ev.stream_id, streams.pop(ev.stream_id))
                writer.write(conn.data_to_send())
                await writer.drain()
        except Exception:
            pass        # a client that isn't speaking HTTP/2, or went away
        finally:
            writer.close()

    def _answer(self, conn: h2.connection.H2Connection, stream: int, req: dict) -> None:
        headers = req["headers"]
        token = headers.get(":path", "").rsplit("/", 1)[-1]
        auth = headers.get("authorization", "")
        claims = check_jwt(auth.removeprefix("bearer "), self.public_key, self.team_id, self.key_id)
        try:
            payload = json.loads(req["body"] or b"null")
        except ValueError:
            payload = None
        self.pushes.append({"token": token, "headers": headers, "payload": payload, "claims": claims})
        if headers.get(":method") != "POST" or not headers.get(":path", "").startswith("/3/device/"):
            status, body = 404, {"reason": "BadPath"}
        elif claims is None:
            status, body = 403, {"reason": "InvalidProviderToken"}
        else:
            status, body = self.answers.get(token, (200, None))
        data = json.dumps(body).encode() if body is not None else b""
        out = [(":status", str(status)), ("apns-id", str(uuid.uuid4()))]
        if data:
            out += [("content-type", "application/json"), ("content-length", str(len(data)))]
        conn.send_headers(stream, out, end_stream=not data)
        if data:
            conn.send_data(stream, data, end_stream=True)
