"""Discovery tool integration against an in-process FastMCP server."""

from __future__ import annotations

import json
from typing import Any


def _parse_call(result: Any) -> Any:
    """Pull the structured payload out of FastMCP's call_tool return.

    FastMCP returns ``(content_blocks, structured_content)`` where the
    structured payload looks like ``{"result": ...}`` for non-dict
    return values and the raw dict otherwise.
    """
    if isinstance(result, tuple) and len(result) == 2:
        structured = result[1]
        if isinstance(structured, dict) and "result" in structured:
            return structured["result"]
        return structured
    # Fallback: parse the first text block as JSON.
    blocks = result if isinstance(result, list) else result[0]
    if not blocks:
        return None
    text = getattr(blocks[0], "text", None)
    if text is None:
        return None
    return json.loads(text)


async def test_list_instruments_returns_two_synthetic(fastmcp_with_tools):
    res = await fastmcp_with_tools.call_tool("list_instruments", {})
    data = _parse_call(res)
    assert isinstance(data, list)
    assert len(data) == 2
    ids = {item["id"] for item in data}
    assert ids == {"GPIB0::24::INSTR", "USB::34461A::INSTR"}
    smu = next(it for it in data if it["id"] == "GPIB0::24::INSTR")
    assert smu["manufacturer"] == "Keithley"
    assert smu["model"] == "2400"
    assert smu["instrument_class"] == "smu"
    assert smu["profile_name"] == "keithley_2400"


async def test_list_instruments_filter(fastmcp_with_tools):
    res = await fastmcp_with_tools.call_tool(
        "list_instruments",
        {"filter": "keysight"},
    )
    data = _parse_call(res)
    assert len(data) == 1
    assert data[0]["model"] == "34461A"


async def test_get_capabilities_by_id(fastmcp_with_tools):
    res = await fastmcp_with_tools.call_tool(
        "get_capabilities",
        {"instrument_id": "GPIB0::24::INSTR"},
    )
    data = _parse_call(res)
    assert len(data) == 1
    cap = data[0]
    cmd_names = {c["name"] for c in cap["commands"]}
    assert "set_voltage" in cmd_names
    assert "ramp_voltage" in cmd_names
    assert "set_mode" in cmd_names


async def test_get_capabilities_by_class(fastmcp_with_tools):
    res = await fastmcp_with_tools.call_tool(
        "get_capabilities",
        {"instrument_class": "dmm"},
    )
    data = _parse_call(res)
    assert len(data) == 1
    assert data[0]["model"] == "34461A"


async def test_list_profiles_counts_matches(fastmcp_with_tools):
    res = await fastmcp_with_tools.call_tool("list_profiles", {})
    data = _parse_call(res)
    keys = {entry["profile_key"] for entry in data}
    assert "keithley_2400" in keys
    assert "keysight_34461a" in keys
    for entry in data:
        assert entry["matched_instruments"] == 1


async def test_get_status_shape(fastmcp_with_tools):
    res = await fastmcp_with_tools.call_tool("get_status", {})
    data = _parse_call(res)
    assert data["edge_id"] == "test-edge-1"
    assert data["edge_name"] == "test-host"
    assert data["instrument_count"] == 2
    assert data["profiled_count"] == 2
    assert "uptime_seconds" in data
    assert "os_info" in data
