from __future__ import annotations

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from tests.mcp._m1_profiles import BIG_ADDR, PSU_ADDR, parse, register_m1_instruments

pytestmark = pytest.mark.critical


@pytest.fixture
def caps_mcp(edge_context):
    from galois_edge.mcp.tools import register_discovery_tools

    register_m1_instruments(edge_context.capability_manager)
    mcp = FastMCP(name="caps")
    register_discovery_tools(mcp, edge_context, dynamic_tools_max=200)
    return mcp


async def _caps(mcp, **kw):
    return parse(await mcp.call_tool("get_capabilities", kw))


async def test_default_is_full_for_small_profiles_and_keeps_v1_payload(caps_mcp):
    (smu,) = await _caps(caps_mcp, instrument_id="GPIB0::24::INSTR")
    assert (smu["detail"], smu["page"], smu["pages"]) == ("full", 0, 1)
    assert {"set_voltage", "ramp_voltage", "set_mode"} <= {c["name"] for c in smu["commands"]}
    value = next(p for c in smu["commands"] if c["name"] == "set_voltage" for p in c["parameters"])
    assert (value["min"], value["max"], value["unit"], value["map"]) == (-200.0, 200.0, "V", None)


async def test_default_is_summary_above_the_threshold(caps_mcp):
    (big,) = await _caps(caps_mcp, instrument_id=BIG_ADDR)
    assert big["detail"] == "summary" and big["command_count"] == 250 and "commands" not in big
    assert len(big["groups"]) == 25 and all(g["leaf_count"] == 10 for g in big["groups"])


async def test_group_and_full_pages(caps_mcp):
    (grp,) = await _caps(caps_mcp, instrument_id=BIG_ADDR, detail="group", path="g3")
    assert grp["path"] == "g3" and {c["name"] for c in grp["groups"]} == {f"c{i}" for i in range(10)}
    (p0,) = await _caps(caps_mcp, instrument_id=BIG_ADDR, detail="full", page=0)
    (p1,) = await _caps(caps_mcp, instrument_id=BIG_ADDR, detail="full", page=1)
    assert (len(p0["commands"]), len(p1["commands"]), p0["pages"]) == (200, 50, 2)
    with pytest.raises(ToolError):
        await _caps(caps_mcp, instrument_id=BIG_ADDR, detail="full", page=2)


async def test_v2_paths_are_command_names(caps_mcp):
    (psu,) = await _caps(caps_mcp, instrument_id=PSU_ADDR)
    assert "source.voltage" in {c["name"] for c in psu["commands"]}


async def test_server_threads_the_threshold_from_kwarg_or_env(edge_context, monkeypatch):
    from galois_edge.mcp.server import MCPServer

    register_m1_instruments(edge_context.capability_manager)
    kw = dict(capability_manager=edge_context.capability_manager, command_handler=edge_context.command_handler,
              instrument_manager=edge_context.instrument_manager, port=0, dynamic_tools_enabled=False)

    async def detail(server):
        (psu,) = parse(await server.app.call_tool("get_capabilities", {"instrument_id": PSU_ADDR}))
        return psu["detail"]

    assert await detail(MCPServer(**kw)) == "full"                       # default MCP_DYNAMIC_TOOLS_MAX=200
    assert await detail(MCPServer(**kw, dynamic_tools_max=5)) == "summary"  # PSU has 9 enabled commands
    monkeypatch.setenv("MCP_DYNAMIC_TOOLS_MAX", "5")
    assert await detail(MCPServer(**kw)) == "summary"
