"""Stream MCP tool placeholders.

Phase 1: register start_stream / stop_stream so the tool surface is
shaped correctly, but full progress-notification streaming lands in
Phase 3 (docs/mcp-integration.md section 4.3). The placeholders return
a stream_id and an explanatory note so an agent gets a helpful error
instead of a missing-tool failure.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, Optional

from mcp.server.fastmcp import FastMCP

from ..context import EdgeContext

logger = logging.getLogger(__name__)


def register_stream_tools(mcp: FastMCP, ctx: EdgeContext) -> None:
    """Register stream-tool placeholders for Phase 1."""

    _streams: Dict[str, Dict[str, Any]] = {}

    @mcp.tool(
        name="start_stream",
        description=(
            "Reserve a stream_id for a future streaming measurement. "
            "Phase 1 placeholder — real progress-notification streaming "
            "lands in Phase 3. The stream_id can be passed to "
            "stop_stream."
        ),
    )
    async def start_stream(
        instrument_id: str,
        command_name: str,
        interval_ms: int = 500,
        duration_ms: int = 30000,
        parameters: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        stream_id = f"mcp-{uuid.uuid4().hex[:8]}"
        _streams[stream_id] = {
            "instrument_id": instrument_id,
            "command_name": command_name,
            "status": "reserved",
        }
        return {
            "stream_id": stream_id,
            "status": "reserved",
            "note": (
                "Phase 1 placeholder; progress-notification streaming "
                "ships in Phase 3."
            ),
        }

    @mcp.tool(
        name="stop_stream",
        description=(
            "Cancel a stream_id reserved by start_stream. Phase 1 "
            "placeholder; real cancellation semantics arrive in Phase 3."
        ),
    )
    async def stop_stream(stream_id: str) -> Dict[str, Any]:
        record = _streams.pop(stream_id, None)
        if record is None:
            return {"stream_id": stream_id, "status": "not_found"}
        return {"stream_id": stream_id, "status": "stopped"}
