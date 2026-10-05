# tests/sim/test_sim_trace.py
"""M1 criterion 4 (edge side): a SIM_MODE session traced to TRACE_DIR as schema-valid edgesim.trace/1."""
from __future__ import annotations

import json

import jsonschema
import pytest

from tests.mcp._m1_profiles import parse
from tests.sim.conftest import PSU_BENCH, SCHEMA, SCOPE_BENCH

pytestmark = pytest.mark.critical
VALIDATOR = jsonschema.Draft202012Validator(json.loads(SCHEMA.read_text()))


def _records(stack) -> list[dict]:
    return [json.loads(line) for line in stack.daemon._trace_writer.path.read_text().splitlines()]


async def test_trace_dir_records_sim_session(sim_stack, tmp_path):
    stack = sim_stack(PSU_BENCH, TRACE_DIR=str(tmp_path / "trace"))
    stack.daemon._start_tracing()
    psu = stack.address_of("galois_sim-psu-2")
    out = parse(await stack.mcp.call_tool("execute_command", {"instrument_id": psu, "command_name": "set_voltage",
                                                              "parameters": {"voltage": "5"}}))
    assert out["success"], out
    out = parse(await stack.mcp.call_tool("execute_command", {"instrument_id": psu, "command_name": "measure.voltage",
                                                              "is_query": True}))
    assert out["success"], out
    stack.daemon._stop_tracing()
    records = _records(stack)
    for rec in records:
        VALIDATOR.validate(rec)
    assert [r["kind"] for r in records] == ["run_start", "transition", "transition", "run_end"]
    transitions = [r for r in records if r["kind"] == "transition"]
    assert all(r["provenance"] == "sim" for r in transitions)                # CI-8: simulated backend ⇒ "sim"
    setter = next(r for r in transitions if r["action"].get("path") == "source.voltage")
    assert setter["delta"] == {"output.voltage_setpoint[1]": [None, 5.0]}
    assert setter["instrument_id"] == psu and setter["fidelity"] == "exact"
    reading = next(r for r in transitions if r["action"].get("path") == "measure.voltage")
    assert reading["observation"]["typed"] == pytest.approx(0.0, abs=1e-3)  # output still off


async def test_binary_waveform_is_traced_as_a_blob(sim_stack, tmp_path):
    stack = sim_stack(SCOPE_BENCH, TRACE_DIR=str(tmp_path / "trace"))
    stack.daemon._start_tracing()
    scope = stack.address_of("galois_sim-scope-1")
    await stack.mcp.call_tool("execute_command", {"instrument_id": scope, "command_name": "digitize"})
    await stack.mcp.call_tool("execute_command", {"instrument_id": scope, "command_name": "waveform.data",
                                                  "is_query": True})
    stack.daemon._stop_tracing()
    records = _records(stack)
    for rec in records:
        VALIDATOR.validate(rec)
    rec = records[-2]
    assert rec["kind"] == "transition" and rec["provenance"] == "sim"
    ref = rec["observation"]["data_ref"]
    assert ref["kind"] == "blob" and (tmp_path / "trace" / ref["uri"]).stat().st_size == ref["nbytes"]
