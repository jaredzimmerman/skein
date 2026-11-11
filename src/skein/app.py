"""The skein HTTP app: one Starlette app serving MCP and REST for every member.

```
<base_path>/healthz   GET, no auth → "ok"
<base_path>/mcp       MCP streamable HTTP (stateless, JSON responses)
<base_path>/api/...   REST (see :mod:`skein.rest`)
```

Everything except healthz needs a member token (:class:`~skein.auth.AuthMiddleware`).
FastMCP's session manager runs inside this app's lifespan: requests to /mcp
fail until the lifespan has started.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from . import __version__
from .auth import Audit, AuthMiddleware, member_of
from .config import Config
from .mcp_tools import build_mcp
from .rest import ROUTES, error_response, routes as rest_routes
from .service import Skein

MCP_ACCEPT = b"application/json, text/event-stream"


class McpEndpoint:
    """ASGI endpoint in front of FastMCP's streamable HTTP session manager.

    Client quirks handled here:
    - GET asking for ``text/event-stream`` → 405 ``Allow: POST`` at once. This server is
      stateless and never pushes, and an idle stream made one client hang.
    - Any other GET → help JSON (what this is, how to authenticate, tools, REST routes).
    - POST → the Accept header is normalized to ``application/json, text/event-stream``.
    """

    def __init__(self, mcp: FastMCP, config: Config) -> None:
        self.mcp = mcp
        self.config = config

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        method = scope.get("method", "")
        if method in ("GET", "HEAD"):
            accept = Headers(scope=scope).get("accept", "")
            if "text/event-stream" in accept.lower():
                response: Response = PlainTextResponse(
                    "Method Not Allowed: this server does not stream; POST JSON-RPC requests.",
                    status_code=405,
                    headers={"Allow": "POST"},
                )
            else:
                response = JSONResponse(await self.help(Request(scope, receive)))
            await response(scope, receive, send)
            return
        if method == "POST":
            headers = [(k, v) for k, v in scope.get("headers", []) if k.lower() != b"accept"]
            scope = {**scope, "headers": [*headers, (b"accept", MCP_ACCEPT)]}
        await self.mcp.session_manager.handle_request(scope, receive, send)

    async def help(self, request: Request) -> dict[str, Any]:
        base = self.config.base_path
        auth = "Authorization: Bearer <token>, or X-API-Key: <token>"
        if self.config.url_tokens:
            auth += ", or ?token=<token> in the URL"
        return {
            "service": "skein",
            "version": __version__,
            "member": member_of(request),
            "about": "A shared task queue for AI assistants. One member is the point; "
            "the rest are members. Call skein_whoami first.",
            "mcp": {
                "endpoint": f"{base}/mcp",
                "transport": "streamable HTTP, stateless, JSON responses",
                "usage": "POST JSON-RPC 2.0 requests here; GET does not stream.",
            },
            "auth": auth,
            "tools": [t.name for t in await self.mcp.list_tools()],
            "rest": [
                {"route": f"{method} {base}/api{path}", "who": who} for method, path, who in ROUTES
            ],
        }


async def _healthz(request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


async def _http_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, HTTPException)
    if exc.status_code == 405:
        return error_response("invalid", "method not allowed", 405, exc.headers)
    return error_response("not_found", "no such route", 404, exc.headers)


def create_app(config: Config, skein: Skein | None = None) -> Starlette:
    """Build the app. ``skein`` defaults to one opened on ``config.db``."""
    if skein is None:
        skein = Skein(config.db)
    audit = Audit(config.audit_log)
    base = config.base_path
    mcp = build_mcp(skein, config, audit)
    mcp.streamable_http_app()  # creates mcp.session_manager; its routes are not used
    mcp_endpoint = McpEndpoint(mcp, config)

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with mcp.session_manager.run():
            yield

    app = Starlette(
        routes=[
            Route(f"{base}/healthz", _healthz, methods=["GET"]),
            Route(f"{base}/mcp", mcp_endpoint),
            Route(f"{base}/mcp/", mcp_endpoint),
            *rest_routes(skein, audit, f"{base}/api"),
        ],
        middleware=[
            Middleware(
                AuthMiddleware,
                authenticate=skein.authenticate,
                url_tokens=config.url_tokens,
                open_paths=frozenset({f"{base}/healthz"}),
                audit=audit,
            )
        ],
        exception_handlers={404: _http_error, 405: _http_error},
        lifespan=lifespan,
    )
    app.state.skein = skein
    app.state.config = config
    app.state.mcp = mcp
    return app
