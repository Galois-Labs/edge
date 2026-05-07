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

from .context import EdgeContext
from .tools import (
    register_discovery_tools,
    register_execute_tools,
    register_stream_tools,
    register_sweep_tools,
)

if TYPE_CHECKING:
    from ..capability_manager import CapabilityManager
    from ..command_handler import CommandHandler

logger = logging.getLogger(__name__)


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
    ) -> None:
        self._port = port
        self._path = path
        self._host = host

        self._ctx = EdgeContext(
            capability_manager=capability_manager,
            command_handler=command_handler,
            instrument_manager=instrument_manager,
            edge_id=edge_id,
            edge_name=edge_name,
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
