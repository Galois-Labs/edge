"""Static MCP tool registrations for Phase 1.

Per docs/mcp-integration.md section 2.5: discovery, execute, sweep, and
stream tools are registered onto a FastMCP instance. Each module exposes
a `register_*_tools(mcp, ctx)` entry point.
"""

from .discovery import register_discovery_tools
from .execute import register_execute_tools
from .stream import register_stream_tools
from .sweep import register_sweep_tools

__all__ = [
    "register_discovery_tools",
    "register_execute_tools",
    "register_stream_tools",
    "register_sweep_tools",
]
