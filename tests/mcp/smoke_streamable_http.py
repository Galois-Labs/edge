"""Manual smoke test against a running daemon.

Not collected by pytest — run directly:

    DEMO_MODE=1 MCP_ENABLED=true MCP_PORT=8767 \\
      .venv/bin/python -m galois_edge   # starts the daemon
    .venv/bin/python tests/mcp/smoke_streamable_http.py

This script connects to the live MCP server, lists tools, and dumps
the response.
"""

from __future__ import annotations

import asyncio
import os
import sys

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def main(url: str) -> int:
    print(f"Connecting to MCP server at {url} ...")
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            init_result = await session.initialize()
            print(f"Initialized. Server: {init_result.serverInfo}")

            tools = await session.list_tools()
            tool_names = sorted(t.name for t in tools.tools)
            print(f"Tools ({len(tool_names)}):")
            for name in tool_names:
                print(f"  - {name}")

            status = await session.call_tool("get_status", {})
            print(f"\nget_status -> isError={status.isError}")
            if status.structuredContent:
                print(f"  structured: {status.structuredContent}")

            instruments = await session.call_tool("list_instruments", {})
            print(f"\nlist_instruments -> isError={instruments.isError}")
            if instruments.structuredContent:
                print(f"  structured: {instruments.structuredContent}")
    return 0


if __name__ == "__main__":
    port = os.environ.get("MCP_PORT", "8767")
    path = os.environ.get("MCP_PATH", "/mcp")
    url = f"http://127.0.0.1:{port}{path}"
    sys.exit(asyncio.run(main(url)))
