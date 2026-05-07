"""Execute-tool integration tests."""

from __future__ import annotations

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


async def test_execute_command_happy_path(fastmcp_with_tools):
    res = await fastmcp_with_tools.call_tool(
        "execute_command",
        {
            "instrument_id": "GPIB0::24::INSTR",
            "command_name": "set_voltage",
            "parameters": {"value": "1.5"},
        },
    )
    data = _parse_call(res)
    assert data["success"] is True
    assert data["scpi_command"] == ":SOUR:VOLT 1.5"


async def test_execute_command_rejects_requires_sweep(fastmcp_with_tools):
    res = await fastmcp_with_tools.call_tool(
        "execute_command",
        {
            "instrument_id": "GPIB0::24::INSTR",
            "command_name": "ramp_voltage",
            "parameters": {"value": "5.0", "sweep_rate": "0.1"},
        },
    )
    data = _parse_call(res)
    assert data["success"] is False
    assert "requires sweep" in data["error"].lower()


async def test_execute_command_unknown_command(fastmcp_with_tools):
    res = await fastmcp_with_tools.call_tool(
        "execute_command",
        {
            "instrument_id": "GPIB0::24::INSTR",
            "command_name": "no_such_command",
        },
    )
    data = _parse_call(res)
    assert data["success"] is False
    assert "not found" in data["error"].lower()


async def test_send_scpi_falls_through_to_handler(fastmcp_with_tools):
    res = await fastmcp_with_tools.call_tool(
        "send_scpi",
        {
            "instrument_id": "GPIB0::24::INSTR",
            "scpi_command": "*IDN?",
        },
    )
    data = _parse_call(res)
    assert data["success"] is True
    assert "KEITHLEY" in data["response"]
