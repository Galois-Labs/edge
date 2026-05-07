"""MCP (Model Context Protocol) integration for galois-edge.

Phase 1 surface: a uvicorn-hosted FastMCP server exposing a static set of
tools (discovery, execute, sweep, stream placeholders) that dispatch
in-process to CapabilityManager / CommandHandler / InstrumentManager.
"""

from .server import MCPServer

__all__ = ["MCPServer"]
