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


async def test_execute_command_ieee_block_uses_raw_path(
    fastmcp_with_tools, edge_context, synthetic_instrument_manager
):
    """returns.type==binary commands go through query_raw, never the
    text path (doc section 2.1); the preamble scaling is composed and
    y_scale is never 0."""
    import base64
    import struct

    from galois_edge.profile_schema import profile_from_dict

    scope_id = "TCPIP0::1.2.3.4::INSTR"
    profile = profile_from_dict(
        {
            "instrument": {
                "manufacturer": "Keysight",
                "model": "DSOX-TEST",
                "class": "oscilloscope",
            },
            "identity": {"pattern": "KEYSIGHT.*DSOX"},
            "commands": {
                "waveform_preamble": {
                    "scpi": ":WAVeform:PREamble?",
                    "type": "query",
                },
                "waveform_data": {
                    "scpi": ":WAVeform:DATA?",
                    "type": "query",
                    "returns": {
                        "type": "binary",
                        "format": "ieee_block",
                        "unit": "V",
                        "binary": {
                            "dtype": "int16",
                            "byte_order": "little",
                            "preamble_command": "waveform_preamble",
                            "preamble_map": {
                                "x_increment": 4,
                                "x_start": 5,
                                "x_reference": 6,
                                "y_scale": 7,
                                "y_offset": 8,
                                "y_reference": 9,
                            },
                        },
                    },
                },
            },
        }
    )
    edge_context.capability_manager.register_instrument(
        instrument_id=scope_id,
        visa_address=scope_id,
        idn_response="KEYSIGHT TECHNOLOGIES,DSOX-TEST,SN,1.0",
        profile=profile,
    )
    synthetic_instrument_manager.connect(scope_id)
    synthetic_instrument_manager.set_query_response(
        scope_id,
        ":WAVeform:PREamble?",
        "+1,+0,+4,+1,+2.0E-06,-1.0E-03,+0,+4.0E-03,+0.0E+00,+0",
    )
    payload = struct.pack("<4h", -100, 0, 100, 200)
    synthetic_instrument_manager.set_raw_response(
        scope_id, ":WAVeform:DATA?", b"#18" + payload + b"\n"
    )

    res = await fastmcp_with_tools.call_tool(
        "execute_command",
        {"instrument_id": scope_id, "command_name": "waveform_data"},
    )
    data = _parse_call(res)

    assert data["success"] is True, data
    block = data["data"]
    assert block["y_dtype"] == "int16"
    assert block["y_length"] == 4
    assert block["y_scale"] != 0
    assert block["x_increment"] != 0
    assert base64.b64decode(block["y_data_base64"]) == payload


async def test_execute_command_ieee_block_malformed_is_error(
    fastmcp_with_tools, edge_context, synthetic_instrument_manager
):
    from galois_edge.profile_schema import profile_from_dict

    scope_id = "TCPIP0::5.6.7.8::INSTR"
    profile = profile_from_dict(
        {
            "instrument": {
                "manufacturer": "Keysight",
                "model": "DSOX-TEST2",
                "class": "oscilloscope",
            },
            "identity": {"pattern": "KEYSIGHT.*DSOX"},
            "commands": {
                "waveform_data": {
                    "scpi": ":WAVeform:DATA?",
                    "type": "query",
                    "returns": {"type": "binary", "format": "ieee_block"},
                },
            },
        }
    )
    edge_context.capability_manager.register_instrument(
        instrument_id=scope_id,
        visa_address=scope_id,
        idn_response="KEYSIGHT TECHNOLOGIES,DSOX-TEST2,SN,1.0",
        profile=profile,
    )
    synthetic_instrument_manager.connect(scope_id)
    # Truncated block: declares 9 bytes, carries 3.
    synthetic_instrument_manager.set_raw_response(
        scope_id, ":WAVeform:DATA?", b"#19abc"
    )

    res = await fastmcp_with_tools.call_tool(
        "execute_command",
        {"instrument_id": scope_id, "command_name": "waveform_data"},
    )
    data = _parse_call(res)

    assert data["success"] is False
    assert "Malformed binary block" in data["error"]
