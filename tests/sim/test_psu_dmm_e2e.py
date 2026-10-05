# tests/sim/test_psu_dmm_e2e.py
"""M1 criterion 1 (edge side): an agent navigates, configures, and measures PSU -> 1 kΩ -> DMM over MCP."""
from __future__ import annotations

import pytest

from tests.mcp._m1_profiles import parse
from tests.sim.conftest import PSU_BENCH

pytestmark = pytest.mark.critical


def test_instruments_register_with_hinted_profiles(sim_stack):
    stack = sim_stack(PSU_BENCH)
    keys = {c.profile_key for c in stack.daemon._capability_manager.all_instruments.values()}
    assert {"galois_sim-psu-2", "galois_sim-dmm"} <= keys
    for address in stack.backend.list_resources():
        assert stack.daemon._instrument_manager.backend_for(address) is stack.backend


async def test_agent_navigates_configures_and_measures(sim_stack):
    stack = sim_stack(PSU_BENCH)
    psu, dmm = stack.address_of("galois_sim-psu-2"), stack.address_of("galois_sim-dmm")
    call = stack.mcp.call_tool

    listed = {i["id"]: i for i in parse(await call("list_instruments", {}))}
    assert listed[psu]["is_simulated"] is True and listed[dmm]["is_simulated"] is True
    groups = parse(await call("list_command_groups", {"instrument_id": psu}))
    assert {"source", "output", "measure"} <= {c["name"] for c in groups["children"]}
    hits = parse(await call("search_commands", {"instrument_id": psu, "query": "voltage"}))
    assert "source.voltage" in [h["path"] for h in hits["results"]]
    desc = parse(await call("describe_command", {"instrument_id": psu, "command": "source.voltage"}))
    assert desc["params"]["voltage"]["max"] == 30.0

    for command, params in (("source.voltage", {"voltage": "5"}),
                            ("set_current_limit", {"current": "0.1"}),       # alias
                            ("output.state", {"state": "ON"})):
        out = parse(await call("execute_command", {"instrument_id": psu, "command_name": command, "parameters": params}))
        assert out["success"], out

    volts = parse(await call("execute_command", {"instrument_id": dmm, "command_name": "measure_dc_voltage", "is_query": True}))
    amps = parse(await call("execute_command", {"instrument_id": psu, "command_name": "measure.current", "is_query": True}))
    assert volts["value_typed"] == pytest.approx(5.0, rel=0.01)
    assert amps["value_typed"] == pytest.approx(0.005, rel=0.02)


async def test_bad_params_are_rejected_before_reaching_the_sim(sim_stack):
    stack = sim_stack(PSU_BENCH)
    psu = stack.address_of("galois_sim-psu-2")
    out = parse(await stack.mcp.call_tool("execute_command",
                                          {"instrument_id": psu, "command_name": "source.voltage",
                                           "parameters": {"voltage": "31"}}))
    assert out["field"] == "voltage" and "out of range" in out["error"]
    assert not list(stack.backend.world.error_queue(stack.backend.world.resolve_address(psu)))
