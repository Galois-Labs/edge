from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from mcp.server.fastmcp import FastMCP

from galois_edge import edge_pb2
from galois_edge.grpc_server import EdgeDaemonServicer, _build_capabilities_proto
from tests.mcp._m1_profiles import parse

pytestmark = pytest.mark.critical
SMU, DMM = "GPIB0::24::INSTR", "USB::34461A::INSTR"


def _mark_smu_simulated(edge_context):
    edge_context.instrument_manager.backend_for = (
        lambda addr: SimpleNamespace(simulated=True) if addr == SMU else None)


async def _mcp(edge_context, mark):
    from galois_edge.mcp.tools import register_discovery_tools
    mcp = FastMCP(name="m")
    register_discovery_tools(mcp, edge_context, dynamic_tools_max=200, mark_simulated=mark)
    return mcp


async def test_marker_on(edge_context):
    _mark_smu_simulated(edge_context)
    mcp = await _mcp(edge_context, True)
    entries = {e["id"]: e for e in parse(await mcp.call_tool("list_instruments", {}))}
    assert (entries[SMU]["is_simulated"], entries[DMM]["is_simulated"]) == (True, False)
    assert parse(await mcp.call_tool("get_status", {}))["is_simulated"] is True


async def test_marker_off_means_indistinguishable(edge_context):
    _mark_smu_simulated(edge_context)
    mcp = await _mcp(edge_context, False)
    assert all("is_simulated" not in e for e in parse(await mcp.call_tool("list_instruments", {})))
    assert "is_simulated" not in parse(await mcp.call_tool("get_status", {}))


@pytest.mark.parametrize("env,expected", [("true", {"true"}), ("", set())])
async def test_grpc_capabilities_marker(edge_context, monkeypatch, env, expected):
    _mark_smu_simulated(edge_context)
    monkeypatch.setenv("SIM_MARK_INSTRUMENTS", env)
    servicer = EdgeDaemonServicer(instrument_manager=edge_context.instrument_manager,
                                  command_handler=edge_context.command_handler, edge_id="t",
                                  capability_manager=edge_context.capability_manager, max_workers=2)
    resp = await servicer.GetCapabilities(edge_pb2.GetCapabilitiesRequest(instrument_id=SMU), MagicMock())
    assert set(v for k, v in resp.capabilities[0].settings.items() if k == "simulated") == expected
    assert "simulated" not in _build_capabilities_proto(edge_context.capability_manager.get_instrument_caps(DMM)).settings


async def test_grpc_marker_on_class_and_all_listings(edge_context, monkeypatch):
    _mark_smu_simulated(edge_context)
    monkeypatch.setenv("SIM_MARK_INSTRUMENTS", "true")
    servicer = EdgeDaemonServicer(instrument_manager=edge_context.instrument_manager,
                                  command_handler=edge_context.command_handler, edge_id="t",
                                  capability_manager=edge_context.capability_manager, max_workers=2)
    every = await servicer.GetCapabilities(edge_pb2.GetCapabilitiesRequest(), MagicMock())
    marked = {c.instrument_id: c.settings.get("simulated") for c in every.capabilities}
    assert marked == {SMU: "true", DMM: None}
    smus = await servicer.GetCapabilities(edge_pb2.GetCapabilitiesRequest(instrument_class="smu"), MagicMock())
    assert [c.settings.get("simulated") for c in smus.capabilities] == ["true"]
