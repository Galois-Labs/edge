"""FastMCP server bound to a uvicorn ASGI app.

Per docs/mcp-integration.md section 2.6: Phase 1 runs uvicorn directly
(not through aiohttp) bound to 127.0.0.1:<port>. The static tool surface
is registered up front; per-instrument dynamic tools land in Phase 3.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Optional

from mcp.server.fastmcp import FastMCP

from .context import EdgeContext, reset_current_caller, set_current_caller
from .tools import (
    register_discovery_tools,
    register_execute_tools,
    register_stream_tools,
    register_sweep_tools,
)

if TYPE_CHECKING:
    from ..capability_manager import CapabilityManager
    from ..command_handler import CommandHandler
    from .auth import JWTValidator

logger = logging.getLogger(__name__)

# HTTP header the Go relay client sets when forwarding a relay-routed
# mcp_request to the local FastMCP. Phase 1 / tailnet-direct callers don't
# set this and skip JWT validation entirely.
CALLER_JWT_HEADER = "galois-caller-jwt"


class MCPServer:
    """Owns the FastMCP app and the uvicorn lifecycle."""

    def __init__(
        self,
        capability_manager: "CapabilityManager",
        command_handler: "CommandHandler",
        instrument_manager: Any,
        port: int = 8767,
        path: str = "/mcp",
        host: str = "127.0.0.1",
        edge_id: str = "",
        edge_name: str = "",
        jwt_validator: Optional["JWTValidator"] = None,
    ) -> None:
        self._port = port
        self._path = path
        self._host = host
        self._jwt_validator = jwt_validator

        self._ctx = EdgeContext(
            capability_manager=capability_manager,
            command_handler=command_handler,
            instrument_manager=instrument_manager,
            edge_id=edge_id,
            edge_name=edge_name,
            jwt_validator=jwt_validator,
        )

        self._mcp: FastMCP = FastMCP(
            name="galois-edge",
            instructions=(
                "Galois edge daemon MCP surface. Use list_instruments + "
                "get_capabilities to learn what is connected and what "
                "commands are available, then execute_command (or "
                "send_scpi for raw SCPI) to drive the instrument."
            ),
            host=host,
            port=port,
            streamable_http_path=path,
        )

        register_discovery_tools(self._mcp, self._ctx)
        register_execute_tools(self._mcp, self._ctx)
        register_sweep_tools(self._mcp, self._ctx)
        register_stream_tools(self._mcp, self._ctx)

        self._server: Optional[Any] = None
        self._task: Optional[asyncio.Task] = None

    @property
    def app(self) -> FastMCP:
        return self._mcp

    @property
    def context(self) -> EdgeContext:
        return self._ctx

    async def start(self) -> None:
        """Bring up the streamable-HTTP listener on a uvicorn server."""
        import uvicorn

        asgi_app = self._mcp.streamable_http_app()
        # Wrap with the caller-JWT middleware so each request's claims are
        # placed on a contextvar before any tool handler runs. The middleware
        # is a no-op when no validator is configured (Phase 1 deployments).
        if self._jwt_validator is not None:
            asgi_app = _CallerJWTMiddleware(asgi_app, self._jwt_validator)

        config = uvicorn.Config(
            app=asgi_app,
            host=self._host,
            port=self._port,
            log_level="warning",
            lifespan="on",
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())

        await _wait_until_started(self._server, timeout=5.0)
        logger.info(
            "MCP server listening on http://%s:%d%s",
            self._host,
            self._port,
            self._path,
        )

    async def stop(self) -> None:
        """Tear down the uvicorn server cleanly."""
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):
                    pass
        self._server = None
        self._task = None


async def _wait_until_started(server: Any, timeout: float) -> None:
    """Poll uvicorn's started flag with a short fixed budget."""
    elapsed = 0.0
    step = 0.05
    while elapsed < timeout:
        if getattr(server, "started", False):
            return
        await asyncio.sleep(step)
        elapsed += step
    logger.warning(
        "MCP server did not signal started within %.1fs", timeout
    )


class _CallerJWTMiddleware:
    """ASGI middleware that validates Galois-Caller-JWT and stuffs claims
    onto a contextvar visible to tool handlers via EdgeContext.authorize.

    Tailnet-direct callers (Phase 1 path) won't set this header; we let the
    request through without populating the contextvar, and authorize() is a
    no-op for them. Relay-routed calls always carry the header (set by the
    Go supervisor in `internal/relay/mcp.go`).
    """

    def __init__(self, app: Any, validator: "JWTValidator") -> None:
        self._app = app
        self._validator = validator

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        token: Optional[str] = None
        for name, value in scope.get("headers", []):
            if name.decode("latin-1").lower() == CALLER_JWT_HEADER:
                token = value.decode("latin-1")
                break

        if not token:
            await self._app(scope, receive, send)
            return

        try:
            claims = await self._validator.validate(token)
        except Exception as exc:  # pragma: no cover — exercised in tests
            await _send_jwt_error(send, str(exc))
            return

        ctx_token = set_current_caller(claims)
        try:
            await self._app(scope, receive, send)
        finally:
            reset_current_caller(ctx_token)


async def _send_jwt_error(send: Any, message: str) -> None:
    """Emit a 401 with a JSON body when JWT validation fails."""
    body = (
        b'{"jsonrpc":"2.0","id":null,"error":'
        b'{"code":-32001,"message":"caller_jwt invalid: '
        + message.encode("utf-8").replace(b'"', b'\\"')
        + b'"}}'
    )
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})
