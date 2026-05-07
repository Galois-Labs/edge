"""End-to-end test: boot MCPServer on an ephemeral port and round-trip
initialize + tools/list + a tool call via the streamable-HTTP client.
"""

from __future__ import annotations

import asyncio
import socket
from contextlib import closing
from typing import Any

import pytest


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.asyncio
async def test_streamable_http_round_trip(
    synthetic_capability_manager: Any,
    synthetic_command_handler: Any,
    synthetic_instrument_manager: Any,
):
    from galois_edge.mcp.server import MCPServer
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    port = _free_port()
    server = MCPServer(
        capability_manager=synthetic_capability_manager,
        command_handler=synthetic_command_handler,
        instrument_manager=synthetic_instrument_manager,
        port=port,
        path="/mcp",
        edge_id="test-edge-9",
        edge_name="ephemeral",
    )

    await server.start()
    try:
        url = f"http://127.0.0.1:{port}/mcp"
        async with streamablehttp_client(url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                tool_names = {t.name for t in tools.tools}
                expected = {
                    "list_instruments",
                    "get_capabilities",
                    "scan_instruments",
                    "list_profiles",
                    "get_status",
                    "execute_command",
                    "execute_sequence",
                    "send_scpi",
                    "start_sweep",
                    "get_sweep_status",
                    "stop_sweep",
                    "start_stream",
                    "stop_stream",
                }
                assert expected.issubset(tool_names)

                # Sanity-call one tool over the wire.
                result = await session.call_tool(
                    "get_status",
                    {},
                )
                assert result.isError is False
                payload = result.structuredContent or {}
                # FastMCP wraps non-dict returns under "result"; dict
                # returns are passed through. Either path must surface
                # the edge_id.
                if "result" in payload:
                    payload = payload["result"]
                assert payload.get("edge_id") == "test-edge-9"
    finally:
        await server.stop()
        # Give the bound socket time to release.
        await asyncio.sleep(0.05)
