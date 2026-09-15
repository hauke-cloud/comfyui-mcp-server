"""ASGI application: MCP at /mcp, REST under /api, probes at the root."""

from __future__ import annotations

import hmac
import json

from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.types import ASGIApp, Receive, Scope, Send

from .api import create_routes
from .config import Settings
from .mcp_server import create_mcp_server
from .services import Services

PROTECTED_PREFIXES = ("/mcp", "/api/")


class BearerTokenMiddleware:
    """Require ``Authorization: Bearer <token>`` on MCP and API paths.

    Plain ASGI rather than BaseHTTPMiddleware, which buffers responses and
    would break the MCP event stream.
    """

    def __init__(self, app: ASGIApp, token: str):
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"].startswith(PROTECTED_PREFIXES):
            supplied = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(supplied, self.expected):
                body = json.dumps({"error": "unauthorized"}).encode()
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"www-authenticate", b"Bearer"),
                            (b"content-length", str(len(body)).encode()),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


def create_app(settings: Settings, services: Services | None = None) -> Starlette:
    services = services or Services.create(settings)
    server = create_mcp_server(services)
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=bool(settings.allowed_hosts),
        allowed_hosts=settings.allowed_hosts,
        allowed_origins=[f"{scheme}://{host}" for host in settings.allowed_hosts for scheme in ("http", "https")],
    )
    app = server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=settings.stateless_http,
        transport_security=security,
        host=settings.host,
        custom_starlette_routes=create_routes(services),
    )
    app.state.services = services
    if settings.api_token:
        app.add_middleware(BearerTokenMiddleware, token=settings.api_token)
    return app
