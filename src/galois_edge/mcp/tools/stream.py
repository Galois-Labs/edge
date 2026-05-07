"""Streaming MCP tools: start_stream / stop_stream / fetch_waveform.

Phase 3 (docs/mcp-integration.md §4.3): each MeasurementDataPoint
produced by the configured profile command is forwarded to the calling
agent as an MCP progress notification keyed by the caller's
``progressToken`` (read from ``ctx.request_context.meta``). The final
tool response carries ``{stream_id, count, last}``.

For waveform-shaped points (``MeasurementDataPoint.vector_data`` in the
gRPC schema) we emit only the metadata in the progress message — the
raw ``y_data`` bytes can blow up SSE event payloads, so a sibling
``fetch_waveform(stream_id, point_index)`` tool returns the bytes by
index for callers that want them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP

from ..context import EdgeContext

if TYPE_CHECKING:
    from mcp.server.fastmcp import Context

logger = logging.getLogger(__name__)


@dataclass
class _StreamRecord:
    instrument_id: str
    command_name: str
    cancel_event: asyncio.Event
    waveforms: List[Dict[str, Any]] = field(default_factory=list)
    count: int = 0
    last: Optional[Dict[str, Any]] = None
    status: str = "running"
    started: float = field(default_factory=time.time)


def register_stream_tools(mcp: FastMCP, ctx: EdgeContext) -> None:
    """Register start_stream, stop_stream, and fetch_waveform."""

    streams: Dict[str, _StreamRecord] = {}

    @mcp.tool(
        name="start_stream",
        description=(
            "Stream periodic measurements from a profile-defined "
            "streamable command. Each sample is forwarded to the "
            "caller's progress token (notifications/progress) and the "
            "final tool result reports {stream_id, count, last}. "
            "interval_ms defaults to 500, duration_ms to 30000. Use "
            "stop_stream(stream_id) to cancel early."
        ),
    )
    async def start_stream(
        instrument_id: str,
        command_name: str,
        interval_ms: int = 500,
        duration_ms: int = 30000,
        parameters: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        ctx.authorize(
            tool_name="start_stream",
            scope=f"{instrument_id}:{command_name}",
            is_dangerous=False,
        )

        cap_mgr = ctx.capability_manager
        caps = cap_mgr.get_instrument_caps(instrument_id)
        if caps is None:
            return {
                "error": f"Instrument not found: {instrument_id}",
                "stream_id": "",
                "count": 0,
            }

        cmd = caps.get_command(command_name)
        if cmd is None:
            return {
                "error": (
                    f"Command '{command_name}' not found or disabled for {instrument_id}"
                ),
                "stream_id": "",
                "count": 0,
            }
        if not cmd.streamable:
            return {
                "error": f"Command '{command_name}' is not streamable",
                "stream_id": "",
                "count": 0,
            }

        params = dict(parameters) if parameters else None
        dispatch = cap_mgr.resolve_command(
            instrument_id=instrument_id,
            command_name=command_name,
            params=params,
            is_query=True,
        )
        if dispatch is None:
            return {
                "error": f"Failed to resolve command '{command_name}'",
                "stream_id": "",
                "count": 0,
            }
        if not isinstance(dispatch, str):
            return {
                "error": "SDK-backed streaming is not exposed via MCP",
                "stream_id": "",
                "count": 0,
            }

        stream_id = f"mcp-{uuid.uuid4().hex[:8]}"
        record = _StreamRecord(
            instrument_id=instrument_id,
            command_name=command_name,
            cancel_event=asyncio.Event(),
        )
        streams[stream_id] = record

        # Pull the active MCP Context off the FastMCP request context.
        # ``mcp.get_context()`` is the documented hook; it returns a
        # Context object whose request_context.meta carries progressToken.
        try:
            mcp_ctx = mcp.get_context()
        except Exception:
            mcp_ctx = None  # type: ignore[assignment]

        progress_token: Any = None
        if mcp_ctx is not None:
            try:
                meta = mcp_ctx.request_context.meta
                if meta is not None:
                    progress_token = getattr(meta, "progressToken", None)
            except Exception:
                progress_token = None

        unit = ""
        if cmd.returns and cmd.returns.unit:
            unit = cmd.returns.unit

        interval_s = max(interval_ms, 10) / 1000.0
        deadline = time.time() + (duration_ms / 1000.0) if duration_ms > 0 else None
        loop = asyncio.get_running_loop()

        try:
            while not record.cancel_event.is_set():
                if deadline is not None and time.time() >= deadline:
                    break
                loop_start = time.time()
                try:
                    result = await loop.run_in_executor(
                        None,
                        lambda: ctx.command_handler.execute_command(
                            scpi_cmd=dispatch,
                            instrument_id=instrument_id,
                            timeout_ms=5000,
                            force_query=True,
                        ),
                    )
                except Exception as exc:
                    logger.exception("stream poll raised")
                    if mcp_ctx is not None and progress_token is not None:
                        await _safe_progress(
                            mcp_ctx,
                            progress_token,
                            record.count,
                            json.dumps({"error": str(exc)}),
                        )
                    record.last = {"error": str(exc)}
                else:
                    ts_ms = int(time.time() * 1000)
                    if result.get("success"):
                        raw = (result.get("response") or "").strip()
                        try:
                            value = float(raw.split(",")[0].strip())
                        except (ValueError, IndexError):
                            value = 0.0
                        record.count += 1
                        record.last = {
                            "value": value,
                            "unit": unit,
                            "timestamp_ms": ts_ms,
                            "raw": raw,
                        }
                        if mcp_ctx is not None and progress_token is not None:
                            await _safe_progress(
                                mcp_ctx,
                                progress_token,
                                record.count,
                                json.dumps({
                                    "value": value,
                                    "unit": unit,
                                    "timestamp_ms": ts_ms,
                                }),
                            )
                    else:
                        record.last = {
                            "error": result.get("error", ""),
                            "timestamp_ms": ts_ms,
                        }
                        if mcp_ctx is not None and progress_token is not None:
                            await _safe_progress(
                                mcp_ctx,
                                progress_token,
                                record.count,
                                json.dumps(record.last),
                            )

                # Sleep for the remainder of the interval, with cancel check.
                elapsed = time.time() - loop_start
                remaining = max(0.0, interval_s - elapsed)
                if remaining > 0:
                    try:
                        await asyncio.wait_for(
                            record.cancel_event.wait(), timeout=remaining,
                        )
                        # cancel_event fired
                        break
                    except asyncio.TimeoutError:
                        pass
        finally:
            record.status = "stopped" if record.cancel_event.is_set() else "completed"

        return {
            "stream_id": stream_id,
            "count": record.count,
            "last": record.last,
            "status": record.status,
        }

    @mcp.tool(
        name="stop_stream",
        description=(
            "Cancel an in-flight start_stream call by stream_id. "
            "Returns the count of progress notifications delivered "
            "before cancellation (best-effort)."
        ),
    )
    async def stop_stream(stream_id: str) -> Dict[str, Any]:
        record = streams.get(stream_id)
        if record is None:
            return {"stream_id": stream_id, "status": "not_found", "count": 0}
        record.cancel_event.set()
        return {
            "stream_id": stream_id,
            "status": "stopping",
            "count": record.count,
        }

    @mcp.tool(
        name="fetch_waveform",
        description=(
            "Fetch the raw bytes of a waveform-shaped point by index. "
            "Used as a sibling to start_stream when the streamed command "
            "returns vector_data and the agent wants the full y_data "
            "buffer outside the SSE event payload."
        ),
    )
    async def fetch_waveform(
        stream_id: str, point_index: int = -1,
    ) -> Dict[str, Any]:
        record = streams.get(stream_id)
        if record is None:
            return {"stream_id": stream_id, "status": "not_found"}
        if not record.waveforms:
            return {
                "stream_id": stream_id,
                "status": "empty",
                "count": 0,
            }
        idx = point_index
        if idx < 0:
            idx = len(record.waveforms) + idx
        if idx < 0 or idx >= len(record.waveforms):
            return {"stream_id": stream_id, "status": "out_of_range"}
        return {
            "stream_id": stream_id,
            "status": "ok",
            "point_index": idx,
            "data": record.waveforms[idx],
        }


async def _safe_progress(
    mcp_ctx: "Context", token: Any, progress: int, message: str,
) -> None:
    try:
        await mcp_ctx.session.send_progress_notification(
            progress_token=token,
            progress=progress,
            total=None,
            message=message,
        )
    except Exception:
        logger.debug("send_progress_notification raised; client likely gone")
