"""Tests for the Phase 3 progress-notification streaming surface.

Covers §4.7 #4 (≥50 progress notifications in a 30-second 500 ms-interval
stream — exercised here via a faster cadence to keep tests under a few
seconds while still hitting the same code path).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest


def _parse_call(result: Any) -> Any:
    """FastMCP returns either a (content, structured) tuple or a list."""
    if isinstance(result, tuple) and len(result) == 2:
        structured = result[1]
        if isinstance(structured, dict) and "result" in structured:
            return structured["result"]
        return structured
    blocks = result if isinstance(result, list) else result[0]
    if not blocks:
        return None
    text = getattr(blocks[0], "text", None)
    if text is None:
        return None
    return json.loads(text)


@pytest.mark.asyncio
async def test_start_stream_happy_path_delivers_progress(
    edge_context: Any,
) -> None:
    """A short stream emits progress notifications and returns count + last."""
    from mcp.server.fastmcp import FastMCP
    from galois_edge.mcp.tools import register_stream_tools

    mcp = FastMCP(name="stream-test")
    register_stream_tools(mcp, edge_context)

    result = await mcp.call_tool(
        "start_stream",
        {
            "instrument_id": "GPIB0::24::INSTR",
            "command_name": "measure_voltage",
            "interval_ms": 50,
            "duration_ms": 600,
        },
    )
    data = _parse_call(result)
    assert data["count"] >= 5, data
    assert data["last"]["value"] == pytest.approx(1.5, abs=1e-3)
    assert data["status"] == "completed"


@pytest.mark.asyncio
async def test_start_stream_acceptance_50_notifications(
    edge_context: Any,
) -> None:
    """Acceptance §4.7 #4 — sustained interval delivers ≥ 50 samples.

    The spec asks for "30s @ 500ms = 60 ticks". The behaviour is the same
    polling loop at any cadence, so we exercise 50+ samples at a tight
    interval to keep tests fast while running the identical code path.
    """
    from mcp.server.fastmcp import FastMCP
    from galois_edge.mcp.tools import register_stream_tools

    mcp = FastMCP(name="stream-acceptance")
    register_stream_tools(mcp, edge_context)

    result = await mcp.call_tool(
        "start_stream",
        {
            "instrument_id": "GPIB0::24::INSTR",
            "command_name": "measure_voltage",
            "interval_ms": 30,
            "duration_ms": 1800,
        },
    )
    data = _parse_call(result)
    assert data["count"] >= 50, data


@pytest.mark.asyncio
async def test_stop_stream_cancels_in_flight(edge_context: Any) -> None:
    """stop_stream cancels an active stream before duration_ms elapses."""
    from mcp.server.fastmcp import FastMCP
    from galois_edge.mcp.tools import register_stream_tools

    mcp = FastMCP(name="stream-cancel")
    register_stream_tools(mcp, edge_context)

    async def _run_stream():
        return await mcp.call_tool(
            "start_stream",
            {
                "instrument_id": "GPIB0::24::INSTR",
                "command_name": "measure_voltage",
                "interval_ms": 25,
                "duration_ms": 30_000,
            },
        )

    stream_task = asyncio.create_task(_run_stream())
    await asyncio.sleep(0.15)

    # Reach into the stop_stream tool's closure to find the streams dict
    # (the only mutable state in the tool module).
    stop_handler = mcp._tool_manager.get_tool("stop_stream")
    streams_dict = None
    for cell in (stop_handler.fn.__closure__ or ()):
        contents = cell.cell_contents
        if isinstance(contents, dict) and contents:
            streams_dict = contents
            break
    assert streams_dict is not None and streams_dict, (
        "no live stream record found"
    )
    stream_id = next(iter(streams_dict.keys()))

    cancel = await mcp.call_tool("stop_stream", {"stream_id": stream_id})
    cancel_data = _parse_call(cancel)
    assert cancel_data["status"] == "stopping"

    final = await asyncio.wait_for(stream_task, timeout=5.0)
    final_data = _parse_call(final)
    assert final_data["status"] == "stopped"


@pytest.mark.asyncio
async def test_stop_stream_unknown_returns_not_found(
    edge_context: Any,
) -> None:
    """stop_stream on an unknown id returns status=not_found."""
    from mcp.server.fastmcp import FastMCP
    from galois_edge.mcp.tools import register_stream_tools

    mcp = FastMCP(name="stream-unknown")
    register_stream_tools(mcp, edge_context)

    result = await mcp.call_tool("stop_stream", {"stream_id": "no-such"})
    data = _parse_call(result)
    assert data["status"] == "not_found"


@pytest.mark.asyncio
async def test_start_stream_unknown_command_returns_error(
    edge_context: Any,
) -> None:
    """A non-streamable command produces a clean error result."""
    from mcp.server.fastmcp import FastMCP
    from galois_edge.mcp.tools import register_stream_tools

    mcp = FastMCP(name="stream-bad")
    register_stream_tools(mcp, edge_context)

    result = await mcp.call_tool(
        "start_stream",
        {
            "instrument_id": "GPIB0::24::INSTR",
            "command_name": "set_voltage",  # not streamable
            "interval_ms": 50,
            "duration_ms": 100,
        },
    )
    data = _parse_call(result)
    assert "not streamable" in (data.get("error") or ""), data


@pytest.mark.asyncio
async def test_fetch_waveform_empty(edge_context: Any) -> None:
    """fetch_waveform on a no-vector stream returns status=empty."""
    from mcp.server.fastmcp import FastMCP
    from galois_edge.mcp.tools import register_stream_tools

    mcp = FastMCP(name="stream-fetch")
    register_stream_tools(mcp, edge_context)

    # Run a tiny stream to populate the streams dict, then fetch.
    await mcp.call_tool(
        "start_stream",
        {
            "instrument_id": "GPIB0::24::INSTR",
            "command_name": "measure_voltage",
            "interval_ms": 30,
            "duration_ms": 100,
        },
    )
    fetch_handler = mcp._tool_manager.get_tool("fetch_waveform")
    streams_dict = None
    for cell in (fetch_handler.fn.__closure__ or ()):
        contents = cell.cell_contents
        if isinstance(contents, dict) and contents:
            streams_dict = contents
            break
    assert streams_dict, "expected at least one stream record"
    stream_id = next(iter(streams_dict.keys()))

    result = await mcp.call_tool(
        "fetch_waveform", {"stream_id": stream_id, "point_index": 0},
    )
    data = _parse_call(result)
    # measure_voltage returns scalar values, not vector_data, so the
    # waveforms list stays empty.
    assert data["status"] in ("empty", "out_of_range"), data
