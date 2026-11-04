"""The FastAPI app: REST under /api (docs/API.md), the client WebSocket /ws/client, the runner WebSocket
/ws/runner (docs/PROTOCOL.md), the MCP gateway at /mcp/agents/<name> (gateway/), the browser's routes and live view
(api_browser.py), and the web UI from ./static at / and /static/*."""
import contextlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from valet.shared import owner
from valet.shared.config import ControlConfig
from valet.shared.protocol import CLOSE_UNAUTHORIZED

from . import (
    api, api_browser, api_computer, api_grants, api_handoff, api_memory, api_notify, api_shared, api_triggers, auth,
)
from .attachments import Attachments
from .browser import BrowserService
from .clock import Clock
from .computer import Computer
from .db import Database
from .definitions import DefinitionsRepo
from .gateway import Gateway
from .hub import Hub
from .notify import Notifier
from .prompts import Prompts
from .scheduler import Scheduler
from .secrets import SecretStore
from .space import SharedSpace
from .webhooks import Webhooks

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"

CLOSE_FORBIDDEN = 4403     # /ws/client from a page that isn't Valet's own

CSP = ("default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
       "connect-src 'self' ws: wss:")


@dataclass
class Control:
    cfg: ControlConfig
    defs: DefinitionsRepo
    db: Database | None = None
    hub: Hub | None = None
    secrets: SecretStore | None = None
    gateway: Gateway | None = None
    notifier: Notifier | None = None
    browser: BrowserService | None = None
    shared: SharedSpace | None = None
    computer: Computer | None = None
    attachments: Attachments | None = None
    logins: auth.LoginLimiter = field(default_factory=auth.LoginLimiter)
    clock: Clock = field(default_factory=Clock)
    prompts: Prompts | None = None          # every prompt into a session, and the queues (prompts.py)
    scheduler: Scheduler | None = None      # schedules, wake-ups, trigger runs (scheduler.py)
    webhooks: Webhooks | None = None


def open_database(cfg: ControlConfig) -> Database:
    """cfg.data holds the database and, by default, the definitions: owner-only."""
    cfg.data.mkdir(parents=True, exist_ok=True)
    cfg.data.chmod(0o700)
    return Database(cfg.db_path)


class GatewayRoute:
    """Plain ASGI: /mcp/* goes to the MCP gateway (agent-token auth, its own handling), the rest to the app."""

    def __init__(self, app, control: Control) -> None:
        self.app = app
        self.control = control

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/mcp/"):
            return await self.control.gateway.asgi(scope, receive, send)
        await self.app(scope, receive, send)


class SecurityHeaders:
    """Plain ASGI middleware (BaseHTTPMiddleware buffers streaming responses): headers on every HTTP response."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        api_path = scope["path"].startswith("/api/")

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", [])]
                headers += [(b"content-security-policy", CSP.encode()), (b"x-frame-options", b"DENY"),
                            (b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"no-referrer")]
                if api_path:
                    headers.append((b"cache-control", b"no-store"))     # live data: never cached
                elif not any(k.lower() == b"cache-control" for k, _ in headers):
                    # The UI: revalidate every load (cheap with ETags), or an update leaves browsers on stale JS.
                    headers.append((b"cache-control", b"no-cache"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


def create_app(cfg: ControlConfig, clock: Clock | None = None) -> FastAPI:
    """Build the app. Its lifespan opens the database (creating cfg.data, mode 0700), initializes the
    definitions repo (git init + first commit if missing), runs the scheduler, and closes runner/client sockets on
    shutdown. Everything the routes need hangs off app.state.control (a Control object: cfg, db, defs, hub,
    secrets, gateway, notifier, browser, shared, prompts, scheduler, webhooks, computer, attachments). `clock`: the
    time the always-on parts read (tests pass a ManualClock)."""
    owner.configure(cfg.owner_name)         # the name in Valet's texts (shared/owner.py): before anything renders
    control = Control(cfg=cfg, defs=DefinitionsRepo(cfg.definitions), clock=clock or Clock())
    control.hub = Hub(control)
    control.shared = SharedSpace(control)
    control.gateway = Gateway(control)
    control.notifier = Notifier(control)
    control.browser = BrowserService(control)
    control.prompts = Prompts(control)
    control.scheduler = Scheduler(control)
    control.webhooks = Webhooks(control)
    control.computer = Computer(control)
    control.attachments = Attachments(control)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        control.db = open_database(cfg)
        control.secrets = SecretStore(control.db, cfg)
        control.defs.init()
        control.shared.start()
        control.attachments.start()
        try:
            async with control.gateway.run():
                await control.scheduler.start()
                try:
                    yield
                finally:
                    await control.scheduler.stop()
        finally:
            await control.hub.close()
            await control.computer.close()
            await control.notifier.close()
            await control.browser.close()
            control.db.close()

    app = FastAPI(title="Valet", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.control = control
    app.include_router(api.router)
    app.include_router(api_grants.router)
    app.include_router(api_notify.router)
    app.include_router(api_browser.router)
    app.include_router(api_handoff.router)
    app.include_router(api_memory.router)
    app.include_router(api_shared.router)
    app.include_router(api_triggers.router)
    app.include_router(api_triggers.router_hooks)
    app.include_router(api_computer.router)
    app.add_middleware(GatewayRoute, control=control)
    app.add_middleware(SecurityHeaders)

    @app.websocket("/ws/runner")
    async def runner_socket(ws: WebSocket):
        runner = auth.runner_for_token(control.db, ws.headers.get("authorization"))
        await ws.accept()
        if not runner:
            await ws.close(CLOSE_UNAUTHORIZED)
            return
        await control.hub.serve_runner(ws, runner)

    @app.websocket("/ws/client")
    async def client_socket(ws: WebSocket):
        await ws.accept()
        if not auth.origin_ok(ws.headers.get("origin"), cfg.public_url):
            log.warning("client socket from origin %s refused (VALET_PUBLIC_URL is %s)",
                        ws.headers.get("origin"), cfg.public_url)
            await ws.close(CLOSE_FORBIDDEN)
            return
        if not (token := auth.websocket_is_owner(ws)):
            await ws.close(CLOSE_UNAUTHORIZED)
            return
        await control.hub.serve_client(ws, token)

    @app.get("/", include_in_schema=False)
    def index():
        page = STATIC / "index.html"
        if not page.is_file():
            raise HTTPException(404, "the web UI isn't installed")
        return FileResponse(page)

    app.mount("/static", StaticFiles(directory=STATIC, check_dir=False), name="static")
    return app
