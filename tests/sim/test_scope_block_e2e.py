# tests/sim/test_scope_block_e2e.py
"""M1 criterion 1, part 2 (edge side): a simulated scope waveform is a real IEEE 488.2 block edge decodes."""
from __future__ import annotations

import base64
import math
import struct

import pytest

from galois_edge.waveform_assembly import decode_ieee_block
from tests.mcp._m1_profiles import parse
from tests.sim.conftest import SCOPE_BENCH

pytestmark = pytest.mark.critical

# awg_rc_scope bench: AWG 2.0 Vpp sine at 1 kHz (galois_sim-awg-1 `amplitude` is Vpp) -> RC low-pass
# (1 kΩ, 100 nF) -> 10x probe -> scope CH1. probe.atten divides what the scope port sees
# (semantics.md §5.7, §6.4) and the scope has no probe compensation, so CH1 reads the DUT output / 10.
_RC_GAIN_1KHZ = 1 / math.sqrt(1 + (2 * math.pi * 1000.0 * 1000.0 * 1.0e-7) ** 2)
EXPECTED_CH1_VPP = 2.0 * _RC_GAIN_1KHZ / 10.0                  # ≈ 0.169 V


async def test_waveform_data_is_an_ieee_block_edge_decodes(sim_stack):
    stack = sim_stack(SCOPE_BENCH)
    scope, awg = stack.address_of("galois_sim-scope-1"), stack.address_of("galois_sim-awg-1")
    call = stack.mcp.call_tool
    assert parse(await call("execute_command", {"instrument_id": awg, "command_name": "output_enable",
                                                "parameters": {"state": "ON"}}))["success"]
    assert parse(await call("execute_command", {"instrument_id": scope, "command_name": "digitize"}))["success"]
    out = parse(await call("execute_command", {"instrument_id": scope, "command_name": "waveform.data", "is_query": True}))
    assert out["success"], out
    data = out["data"]
    assert (data["y_dtype"], data["y_length"]) == ("int16", 1000)
    counts = struct.unpack(f"<{data['y_length']}h", base64.b64decode(data["y_data_base64"]))
    volts = [c * data["y_scale"] + data["y_offset"] for c in counts]
    assert max(volts) - min(volts) == pytest.approx(EXPECTED_CH1_VPP, rel=0.05)   # non-trivial, bounded


def test_raw_block_passes_edges_decoder(sim_stack):
    stack = sim_stack(SCOPE_BENCH)
    scope = stack.address_of("galois_sim-scope-1")
    mgr = stack.daemon._instrument_manager
    mgr.write(scope, ":DIGitize")
    payload = decode_ieee_block(mgr.query_raw(scope, ":WAVeform:DATA?"))
    assert len(payload) == 2000
