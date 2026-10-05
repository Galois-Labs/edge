from __future__ import annotations

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from tests.mcp._m1_profiles import DP_ADDR, PSU_ADDR, parse, register_m1_instruments, tool_error

pytestmark = pytest.mark.critical


@pytest.fixture
def nav(edge_context):
    from galois_edge.mcp.tools import register_navigate_tools

    register_m1_instruments(edge_context.capability_manager)
    mcp = FastMCP(name="nav-test")
    register_navigate_tools(mcp, edge_context)
    return mcp


async def test_six_tools_are_registered(nav):
    names = {t.name for t in await nav.list_tools()}
    assert {"list_command_groups", "search_commands", "describe_command", "related_commands",
            "find_capability", "get_state_schema"} <= names


async def test_list_groups_v2(nav):
    out = parse(await nav.call_tool("list_command_groups", {"instrument_id": PSU_ADDR}))
    assert out["profile_key"] == "galois_sim-psu-2" and out["truncated"] is False
    assert {c["name"] for c in out["children"] if c["kind"] == "group"} >= {"source", "output", "measure"}


async def test_list_groups_v1_flat(nav):
    out = parse(await nav.call_tool("list_command_groups", {"instrument_id": DP_ADDR}))
    assert out["profile_key"] == "rigol_dp800" and out["children"]
    assert all(c["kind"] == "command" for c in out["children"])   # v1: every command is a root leaf
    assert out["truncated"] is True                                  # 106 commands > default limit 50


async def test_search_describe_related_state(nav):
    hits = parse(await nav.call_tool("search_commands", {"instrument_id": PSU_ADDR, "query": "voltage"}))
    assert "source.voltage" in [h["path"] for h in hits["results"]]
    desc = parse(await nav.call_tool("describe_command", {"instrument_id": PSU_ADDR, "command": "set_voltage"}))
    assert desc["path"] == "source.voltage" and desc["params"]["voltage"]["max"] == 30.0
    assert desc["params"]["channel"]["inherited_from"] == "source"
    rel = parse(await nav.call_tool("related_commands",
                                    {"instrument_id": PSU_ADDR, "command": "source.voltage", "relation": "writes"}))
    assert rel["relation"] == "writes" and isinstance(rel["related"], list)
    st = parse(await nav.call_tool("get_state_schema", {"instrument_id": PSU_ADDR}))
    assert any(g["path"] == "output" for g in st["groups"])


async def test_find_capability_across_instruments(nav):
    out = parse(await nav.call_tool("find_capability", {"capability": "psu.set_voltage"}))
    assert {"profile_key": "galois_sim-psu-2", "path": "source.voltage"} in out["matches"]


async def test_unknown_instrument_is_an_mcp_error(nav):
    with pytest.raises(ToolError) as ei:
        await nav.call_tool("list_command_groups", {"instrument_id": "NOPE"})
    assert tool_error(ei.value) == {"error": "Unknown instrument or no profile: NOPE", "suggestions": []}


async def test_unknown_command_has_bounded_suggestions(nav):
    with pytest.raises(ToolError) as ei:
        await nav.call_tool("describe_command", {"instrument_id": PSU_ADDR, "command": "voltage"})
    err = tool_error(ei.value)
    assert err["error"].startswith("Unknown command 'voltage'") and len(err["suggestions"]) <= 5
    assert all(isinstance(s, str) for s in err["suggestions"])


async def test_results_are_exactly_the_nav_dicts(nav, edge_context):
    from galois_profiles import nav as gp_nav

    gp = edge_context.capability_manager.get_instrument_caps(PSU_ADDR).profile.galois_profile
    calls = [
        ("list_command_groups", {"path": "output", "depth": 2}, gp_nav.list_groups(gp, path="output", depth=2)),
        ("search_commands", {"query": "voltage", "limit": 3}, gp_nav.search(gp, "voltage", limit=3)),
        ("describe_command", {"command": "set_voltage"}, gp_nav.describe(gp, "set_voltage")),
        ("related_commands", {"command": "source.voltage", "relation": "writes"},
         gp_nav.related(gp, "source.voltage", "writes")),
        ("get_state_schema", {"path": "output"}, gp_nav.state_schema(gp, path="output")),
    ]
    for tool, args, expected in calls:
        assert parse(await nav.call_tool(tool, {"instrument_id": PSU_ADDR, **args})) == expected, tool
    one = parse(await nav.call_tool("find_capability", {"capability": "psu.set_voltage", "instrument_id": PSU_ADDR}))
    assert one == gp_nav.find_capability({gp.key: gp}, "psu.set_voltage")
