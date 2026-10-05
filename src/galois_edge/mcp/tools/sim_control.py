"""Sim-only MCP tools, bound to an extra backend's SimControl (contracts/edge-api.md §3).

Registered only when SIM_CONTROL_TOOLS=true (MCPServer) and some backend in
``instrument_manager.extra_backends`` exposes a non-None ``control``
(contracts/python/edgesim_edge_backend.py ``SimControl``).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, Optional

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from ..context import EdgeContext


def _find_control(instrument_manager: Any) -> Any:
    for backend in getattr(instrument_manager, "extra_backends", ()) or ():
        control = getattr(backend, "control", None)
        if control is not None:
            return control
    return None


def register_sim_control_tools(mcp: FastMCP, ctx: EdgeContext) -> bool:
    """Register sim_inject_fault/sim_advance/sim_reset; False (nothing registered) without a control."""
    control = _find_control(ctx.instrument_manager)
    if control is None:
        return False

    async def _call(fn, *args):
        # World access is synchronous and the backend holds its lock: keep it off the event loop.
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, lambda: fn(*args))
        except (KeyError, ValueError) as exc:
            raise ToolError(json.dumps({"error": str(exc)}))

    @mcp.tool(name="sim_inject_fault", description=(
        "SIMULATION ONLY: inject a fault (timeout, disconnect, garble, error, latency, busy, drift, trip) "
        "into a simulated instrument."))
    async def sim_inject_fault(instrument_id: str, kind: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        await _call(control.inject_fault, instrument_id, kind, dict(params or {}))
        return {"ok": True, "now_ns": control.now_ns()}

    @mcp.tool(name="sim_advance", description="SIMULATION ONLY: advance the bench's virtual clock by seconds.")
    async def sim_advance(seconds: float) -> Dict[str, Any]:
        return {"now_ns": await _call(control.advance, seconds)}

    @mcp.tool(name="sim_reset", description=(
        "SIMULATION ONLY: reset one simulated instrument (*RST semantics), or the whole bench when "
        "instrument_id is empty."))
    async def sim_reset(instrument_id: str = "") -> Dict[str, Any]:
        await _call(control.reset, instrument_id)
        return {"ok": True, "now_ns": control.now_ns()}

    return True
