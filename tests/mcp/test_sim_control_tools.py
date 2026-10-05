from __future__ import annotations

from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from tests.mcp._m1_profiles import parse, tool_error

pytestmark = pytest.mark.critical
CONTRACTS = Path(__file__).resolve().parents[2] / "third_party/edgesim/contracts/python"


class FakeControl:
    def __init__(self):
        self.calls, self.t = [], 0

    def inject_fault(self, instrument_id, kind, params=None, at_s=None):
        if instrument_id == "missing":
            raise KeyError(instrument_id)
        self.calls.append(("inject", instrument_id, kind, dict(params or {})))

    def advance(self, seconds):
        if seconds < 0:
            raise ValueError("seconds must be >= 0")
        self.t += int(seconds * 1e9)
        return self.t

    def reset(self, instrument_id=""):
        self.calls.append(("reset", instrument_id))

    def now_ns(self):
        return self.t


def test_fake_conforms_to_the_contract(monkeypatch):
    monkeypatch.syspath_prepend(str(CONTRACTS))
    from edgesim_edge_backend import SimControl
    assert isinstance(FakeControl(), SimControl)


def _with_backend(edge_context, control):
    edge_context.instrument_manager.extra_backends = (type("B", (), {"control": control})(),)


async def test_tools_bind_to_the_first_backend_with_control(edge_context):
    from galois_edge.mcp.tools import register_sim_control_tools

    control = FakeControl()
    _with_backend(edge_context, control)
    mcp = FastMCP(name="sim")
    assert register_sim_control_tools(mcp, edge_context) is True
    assert {"sim_inject_fault", "sim_advance", "sim_reset"} <= {t.name for t in await mcp.list_tools()}
    assert parse(await mcp.call_tool("sim_advance", {"seconds": 0.5})) == {"now_ns": 500_000_000}
    await mcp.call_tool("sim_inject_fault", {"instrument_id": "PSU", "kind": "trip", "params": {"ch": 1}})
    await mcp.call_tool("sim_reset", {})
    assert control.calls == [("inject", "PSU", "trip", {"ch": 1}), ("reset", "")]
    with pytest.raises(ToolError) as ei:
        await mcp.call_tool("sim_inject_fault", {"instrument_id": "missing", "kind": "trip"})
    assert "missing" in tool_error(ei.value)["error"]


async def test_absent_without_a_control(edge_context):
    from galois_edge.mcp.tools import register_sim_control_tools

    edge_context.instrument_manager.extra_backends = ()
    mcp = FastMCP(name="sim")
    assert register_sim_control_tools(mcp, edge_context) is False
    assert not any(t.name.startswith("sim_") for t in await mcp.list_tools())


async def test_server_registers_only_when_enabled(edge_context):
    from galois_edge.mcp.server import MCPServer

    _with_backend(edge_context, FakeControl())
    kw = dict(capability_manager=edge_context.capability_manager, command_handler=edge_context.command_handler,
              instrument_manager=edge_context.instrument_manager, port=0, dynamic_tools_enabled=False)
    off = {t.name for t in await MCPServer(**kw, sim_control_tools=False).app.list_tools()}
    on = {t.name for t in await MCPServer(**kw, sim_control_tools=True).app.list_tools()}
    assert "sim_advance" not in off and "sim_advance" in on


async def test_skips_backends_without_control_and_reads_the_env(edge_context, monkeypatch):
    from galois_edge.mcp.server import MCPServer

    first, second = FakeControl(), FakeControl()
    second.t = 7
    edge_context.instrument_manager.extra_backends = (
        type("Real", (), {"control": None})(),
        type("Sim1", (), {"control": first})(),
        type("Sim2", (), {"control": second})(),
    )
    kw = dict(capability_manager=edge_context.capability_manager, command_handler=edge_context.command_handler,
              instrument_manager=edge_context.instrument_manager, port=0, dynamic_tools_enabled=False)
    assert "sim_advance" not in {t.name for t in await MCPServer(**kw).app.list_tools()}   # default off
    monkeypatch.setenv("SIM_CONTROL_TOOLS", "true")
    app = MCPServer(**kw).app
    assert parse(await app.call_tool("sim_reset", {"instrument_id": "DMM"})) == {"ok": True, "now_ns": 0}
    assert (first.calls, second.calls) == ([("reset", "DMM")], [])
