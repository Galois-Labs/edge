"""Sweep-tool integration tests."""

from __future__ import annotations

import asyncio
import json
from typing import Any


def _parse_call(result: Any) -> Any:
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


async def test_sweep_happy_path_completes_on_idle(fastmcp_with_tools):
    started = await fastmcp_with_tools.call_tool(
        "start_sweep",
        {
            "instrument_id": "GPIB0::24::INSTR",
            "command_name": "ramp_voltage",
            "target_value": 5.0,
            "sweep_rate": 0.5,
        },
    )
    sweep = _parse_call(started)
    assert sweep["status"] == "running"
    sweep_id = sweep["sweep_id"]

    # Allow the polling loop a few cycles to detect IDLE.
    for _ in range(20):
        status_res = await fastmcp_with_tools.call_tool(
            "get_sweep_status",
            {"sweep_id": sweep_id},
        )
        status = _parse_call(status_res)
        if status["status"] == "completed":
            break
        await asyncio.sleep(0.05)
    assert status["status"] == "completed"


async def test_get_sweep_status_unknown_sweep(fastmcp_with_tools):
    res = await fastmcp_with_tools.call_tool(
        "get_sweep_status",
        {"sweep_id": "no-such-sweep"},
    )
    data = _parse_call(res)
    assert data["status"] == "not_found"


async def test_stop_sweep_idempotent(fastmcp_with_tools):
    res1 = await fastmcp_with_tools.call_tool(
        "stop_sweep",
        {"sweep_id": "no-such-sweep"},
    )
    data1 = _parse_call(res1)
    assert data1["status"] == "not_found"

    # A real sweep, then stop twice — second stop should not raise.
    started = await fastmcp_with_tools.call_tool(
        "start_sweep",
        {
            "instrument_id": "GPIB0::24::INSTR",
            "command_name": "ramp_voltage",
            "target_value": 1.0,
            "sweep_rate": 0.1,
        },
    )
    sweep_id = _parse_call(started)["sweep_id"]
    first = _parse_call(
        await fastmcp_with_tools.call_tool(
            "stop_sweep",
            {"sweep_id": sweep_id},
        )
    )
    second = _parse_call(
        await fastmcp_with_tools.call_tool(
            "stop_sweep",
            {"sweep_id": sweep_id},
        )
    )
    assert first["sweep_id"] == sweep_id
    assert second["sweep_id"] == sweep_id
