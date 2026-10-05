"""M1 criterion 1 (and 4), as processes: an agent drives PSU -> 1 kΩ -> DMM through a real edge daemon.

`edgesim world serve` serves the cloud-format bench (contracts/examples/benches/psu_resistor_dmm.bench.json).
The edge daemon (`python -m galois_edge`, SIM_MODE, SIM_REMOTE_SOCKET) serves it over MCP, and an MCP
streamable-HTTP client does what an agent would. Both processes trace (edgesim.trace/1). Closing the
daemon's stdin, as the Go supervisor does, stops it.
"""
from __future__ import annotations

import pytest

from tests.e2e.conftest import (
    PSU_BENCH, SHUTDOWN_GRACE_S, assert_clean_daemon_log, mcp_agent, trace_records, trace_validator,
)

pytestmark = [pytest.mark.e2e, pytest.mark.slow]

PSU, DMM = "galois_sim-psu-2", "galois_sim-dmm"


async def test_agent_navigates_configures_and_measures_through_the_daemon(e2e, tmp_path):
    world = e2e.world_serve(PSU_BENCH, trace_dir=tmp_path / "world-trace")
    edge = e2e.edge_daemon(remote_socket=world.socket, trace_dir=tmp_path / "edge-trace")

    async with mcp_agent(edge.mcp_url) as agent:
        listed = await agent.wait_for_instruments({PSU, DMM})
        assert listed[PSU]["is_simulated"] is True and listed[DMM]["is_simulated"] is True
        psu, dmm = listed[PSU]["id"], listed[DMM]["id"]

        # Navigate: find the voltage setpoint without loading the whole command tree.
        groups = await agent.call("list_command_groups", instrument_id=psu)
        assert {"source", "output", "measure"} <= {group["name"] for group in groups["children"]}
        hits = await agent.call("search_commands", instrument_id=psu, query="voltage")
        assert "source.voltage" in [hit["path"] for hit in hits["results"]]
        described = await agent.call("describe_command", instrument_id=psu, command="source.voltage")
        assert described["params"]["voltage"]["max"] == 30.0

        # Configure: 5 V by path; current limit and output by alias (v1-style names).
        await agent.execute(psu, "source.voltage", {"voltage": "5"})
        await agent.execute(psu, "set_current_limit", {"current": "0.1"})
        await agent.execute(psu, "output_enable", {"state": "ON"})
        assert (await agent.execute(psu, "set_voltage", is_query=True))["value_typed"] == 5.0
        assert (await agent.execute(psu, "output.state", is_query=True))["value_typed"] is True

        # Measure across the bench.
        volts = await agent.execute(dmm, "measure_dc_voltage", is_query=True)
        amps = await agent.execute(psu, "measure.current", is_query=True)
        assert volts["value_typed"] == pytest.approx(5.0, rel=0.01)
        assert amps["value_typed"] == pytest.approx(0.005, rel=0.02)

    rc, seconds = edge.proc.close_stdin()
    assert rc == 0, edge.proc.output_tail()
    assert seconds < SHUTDOWN_GRACE_S
    log = edge.proc.output()
    assert "Stdin closed (EOF) -- initiating shutdown" in log and "Edge daemon stopped." in log
    assert_clean_daemon_log(edge.proc)
    assert world.proc.terminate() == 0, world.proc.output_tail()

    validator = trace_validator()

    # edge's TRACE_DIR: every transition of a simulated instrument is labelled "sim" (CI-8).
    records = trace_records(tmp_path / "edge-trace")
    for record in records:
        validator.validate(record)
    assert records[0]["kind"] == "run_start"
    assert (records[-1]["kind"], records[-1]["status"]) == ("run_end", "ok")
    transitions = [r for r in records if r["kind"] == "transition"]
    assert transitions and all(r["provenance"] == "sim" for r in transitions)
    setter = next(r for r in transitions if r["action"].get("path") == "source.voltage" and r.get("delta"))
    assert setter["instrument_id"] == psu and setter["delta"] == {"output.voltage_setpoint[1]": [None, 5.0]}
    enable = next(r for r in transitions if r["action"].get("path") == "output.state" and r.get("delta"))
    assert enable["delta"] == {"output.enabled[1]": [None, True]}            # the alias, traced by its path
    reading = next(r for r in transitions                                     # the alias, traced by its path
                   if r["instrument_id"] == dmm and r["action"].get("path") == "measure.voltage.dc")
    assert reading["observation"]["typed"] == pytest.approx(5.0, rel=0.01)

    # The World's own trace of the same session.
    records = trace_records(tmp_path / "world-trace")
    for record in records:
        validator.validate(record)
    start = records[0]
    assert start["kind"] == "run_start" and start["provenance"] == "sim"
    assert (start["bench_id"], start["seed"]) == ("psu-resistor-dmm", e2e.seed)
    transitions = [r for r in records if r["kind"] == "transition"]
    setter = next(r for r in transitions if r["action"].get("path") == "source.voltage" and r.get("delta"))
    assert setter["delta"] == {"output.voltage_setpoint[1]": [0.0, 5.0]}
    enable = next(r for r in transitions if r["action"].get("path") == "output.state" and r.get("delta"))
    assert enable["delta"]["output.enabled[1]"] == [False, True]
    assert enable["delta"]["output.measured_voltage[1]"][1] == pytest.approx(5.0, rel=0.01)
    reading = next(r for r in transitions if r["action"].get("path") == "measure.voltage.dc")
    assert reading["observation"]["typed"] == pytest.approx(5.0, rel=0.01)
