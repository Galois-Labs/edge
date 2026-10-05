from __future__ import annotations

from unittest.mock import MagicMock

import grpc
import pytest

from galois_edge import edge_pb2
from galois_edge.grpc_server import EdgeDaemonServicer
from tests.mcp._m1_profiles import PSU_ADDR, parse, register_m1_instruments

pytestmark = pytest.mark.critical
MSG = "value: 999.0 is out of range [-200.0, 200.0]"
SMU = "GPIB0::24::INSTR"


async def test_static_dynamic_and_grpc_reject_identically(fastmcp_with_tools, edge_context):
    from galois_edge.mcp.dynamic_tools import DynamicToolRegistry

    static = parse(await fastmcp_with_tools.call_tool(
        "execute_command", {"instrument_id": SMU, "command_name": "set_voltage", "parameters": {"value": "999"}}))
    from mcp.server.fastmcp import FastMCP
    dyn_mcp = FastMCP(name="dyn")
    DynamicToolRegistry(dyn_mcp, edge_context, emit_list_changed=False)
    dynamic = parse(await dyn_mcp.call_tool("keithley_2400__set_voltage", {"value": 999.0}))
    ctx = MagicMock()
    servicer = EdgeDaemonServicer(instrument_manager=edge_context.instrument_manager,
                                  command_handler=edge_context.command_handler, edge_id="t",
                                  capability_manager=edge_context.capability_manager, max_workers=2)
    resp = await servicer.ExecuteCommand(edge_pb2.ExecuteCommandRequest(
        command_id="c", instrument_id=SMU, command_name="set_voltage", parameters={"value": "999"}), ctx)
    assert (static["error"], static["field"], static["success"]) == (MSG, "value", False)
    assert (dynamic["error"], dynamic["field"]) == (MSG, "value")
    assert resp.error_message == MSG
    ctx.set_code.assert_called_once_with(grpc.StatusCode.INVALID_ARGUMENT)


async def test_execute_accepts_path_and_alias_and_types_queries(fastmcp_with_tools, edge_context):
    register_m1_instruments(edge_context.capability_manager)
    edge_context.instrument_manager.connect(PSU_ADDR)
    edge_context.instrument_manager.set_query_response(PSU_ADDR, ":SOURce1:VOLTage?", "+5.000000E+00")
    by_alias = parse(await fastmcp_with_tools.call_tool(
        "execute_command", {"instrument_id": PSU_ADDR, "command_name": "set_voltage", "is_query": True}))
    by_path = parse(await fastmcp_with_tools.call_tool(
        "execute_command", {"instrument_id": PSU_ADDR, "command_name": "source.voltage", "is_query": True}))
    assert by_alias["scpi_command"] == by_path["scpi_command"] == ":SOURce1:VOLTage?"
    assert by_alias["data"] == "+5.000000E+00" and by_alias["value_typed"] == 5.0


async def test_writes_have_no_value_typed(fastmcp_with_tools):
    out = parse(await fastmcp_with_tools.call_tool(
        "execute_command", {"instrument_id": SMU, "command_name": "set_voltage", "parameters": {"value": "1.5"}}))
    assert out["success"] is True and "value_typed" not in out


async def test_stream_maps_validation_errors(fastmcp_with_tools, edge_context):
    from galois_edge.profile_schema import CommandConfig, ParameterConfig as P
    edge_context.capability_manager.get_instrument_caps(SMU).profile.commands["meas_ch"] = CommandConfig(
        scpi=":MEAS{ch}?", type="query", streamable=True, params={"ch": P(type="int", min=1, max=2)})
    out = parse(await fastmcp_with_tools.call_tool(
        "start_stream", {"instrument_id": SMU, "command_name": "meas_ch", "parameters": {"ch": "7"}}))
    assert out == {"error": "ch: 7 is out of range [1, 2]", "field": "ch", "stream_id": "", "count": 0}


async def test_dynamic_tools_leave_bounds_and_enums_to_central_validation(fastmcp_with_tools, edge_context):
    from mcp.server.fastmcp import FastMCP

    from galois_edge.mcp.dynamic_tools import DynamicToolRegistry

    dyn_mcp = FastMCP(name="dyn")
    DynamicToolRegistry(dyn_mcp, edge_context, emit_list_changed=False)
    schema = {t.name: t for t in await dyn_mcp.list_tools()}["keithley_2400__set_mode"].inputSchema
    assert schema["properties"]["mode"]["enum"] == ["VOLT", "CURR"]          # still advertised
    ok = parse(await dyn_mcp.call_tool("keithley_2400__set_mode", {"mode": "volt"}))
    assert ok["success"] is True and ok["scpi_command"] == ":SOUR:FUNC VOLT"  # semantics §3.3 case folding
    bad = parse(await dyn_mcp.call_tool("keithley_2400__set_mode", {"mode": "bogus"}))
    static = parse(await fastmcp_with_tools.call_tool(
        "execute_command", {"instrument_id": SMU, "command_name": "set_mode", "parameters": {"mode": "bogus"}}))
    assert (bad["success"], bad["field"]) == (False, "mode") and bad["error"] == static["error"]
