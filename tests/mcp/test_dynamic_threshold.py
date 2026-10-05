from __future__ import annotations

import pytest
from mcp.server.fastmcp import FastMCP

from tests.mcp._m1_profiles import BIG_ADDR, PSU_ADDR, register_m1_instruments

pytestmark = pytest.mark.critical


async def _tool_names(edge_context, max_commands):
    from galois_edge.mcp.dynamic_tools import DynamicToolRegistry

    mcp = FastMCP(name="dyn")
    DynamicToolRegistry(mcp, edge_context, emit_list_changed=False, max_commands=max_commands)
    register_m1_instruments(edge_context.capability_manager)
    return {t.name for t in await mcp.list_tools()}


async def test_big_profiles_get_no_per_command_tools(edge_context):
    names = await _tool_names(edge_context, 200)
    assert not any(n.startswith("acme_big__") for n in names)
    assert "keithley_2400__set_voltage" in names           # v1 names unchanged


async def test_v2_tool_names_are_mcp_safe(edge_context):
    names = await _tool_names(edge_context, 200)
    assert "galois_sim-psu-2__source_voltage" in names
    assert all("." not in n for n in names)


async def test_threshold_zero_disables_command_tools(edge_context):
    names = await _tool_names(edge_context, 0)
    assert not any("__set_voltage" in n for n in names)


async def test_colliding_safe_names_keep_the_first_and_warn(edge_context, caplog):
    from galois_edge.mcp.dynamic_tools import DynamicToolRegistry
    from galois_edge.profile_schema import profile_from_dict

    profile = profile_from_dict({
        "schema_version": 2,
        "instrument": {"manufacturer": "ACME", "model": "CLASH", "class": "dmm"},
        "identity": {"patterns": ["ACME,CLASH"]},
        "commands": {"a": {"_doc": "group a", "b": {"scpi": ":A:B?", "type": "query"}},
                     "a_b": {"scpi": ":AB?", "type": "query"}},
    })
    mcp = FastMCP(name="dyn")
    registry = DynamicToolRegistry(mcp, edge_context, emit_list_changed=False, max_commands=200)
    edge_context.capability_manager.register_instrument("CLASH", "CLASH", "ACME,CLASH,0,1", profile)
    assert registry.registered_tools()["CLASH"] == ["acme_clash__a_b"]
    assert "dynamic tool name collision acme_clash__a_b" in caplog.text
