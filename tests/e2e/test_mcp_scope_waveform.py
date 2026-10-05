"""M1 criterion 1, part 2, as processes: a scope waveform from a remote World, fetched through the edge daemon.

`edgesim world serve` serves the AWG -> RC low-pass -> 10x probe -> scope bench
(contracts/examples/benches/awg_rc_scope.bench.yaml). The daemon fetches the waveform over MCP
(`execute_command` on `waveform.data`): an IEEE 488.2 block that edge's decoder accepts. Its amplitude must
match the bench's physics.
"""
from __future__ import annotations

import base64
import math
import struct

import pytest

from tests.e2e.conftest import SCOPE_BENCH, assert_clean_daemon_log, mcp_agent

pytestmark = [pytest.mark.e2e, pytest.mark.slow]

AWG, SCOPE = "galois_sim-awg-1", "galois_sim-scope-1"

# The bench: AWG 2.0 Vpp sine at 1 kHz (galois_sim-awg-1 `amplitude` is Vpp) -> RC low-pass (1 kΩ, 100 nF;
# fc ≈ 1.59 kHz) -> 10x probe -> scope CH1. The probe divides what the scope port sees, and the scope has no
# probe compensation (semantics §5.7, §6.4), so CH1 reads the DUT output / 10.
RC_GAIN_1KHZ = 1 / math.sqrt(1 + (2 * math.pi * 1000.0 * 1000.0 * 1.0e-7) ** 2)
EXPECTED_CH1_VPP = 2.0 * RC_GAIN_1KHZ / 10.0          # ≈ 0.169 V


async def test_scope_waveform_through_the_daemon_matches_the_rc_response(e2e, tmp_path):
    world = e2e.world_serve(SCOPE_BENCH)
    edge = e2e.edge_daemon(remote_socket=world.socket, trace_dir=tmp_path / "edge-trace")

    async with mcp_agent(edge.mcp_url) as agent:
        listed = await agent.wait_for_instruments({AWG, SCOPE})
        awg, scope = listed[AWG]["id"], listed[SCOPE]["id"]
        await agent.execute(awg, "output_enable", {"state": "ON"})
        await agent.execute(scope, "digitize")
        out = await agent.execute(scope, "waveform.data", is_query=True)

    data = out["data"]
    assert (data["y_dtype"], data["y_length"], data["y_unit"]) == ("int16", 1000, "V")
    counts = struct.unpack(f"<{data['y_length']}h", base64.b64decode(data["y_data_base64"]))
    volts = [count * data["y_scale"] + data["y_offset"] for count in counts]
    period_samples = 1e-3 / data["x_increment"]
    assert len(volts) >= 2 * period_samples          # whole periods, so max - min is the peak-to-peak
    assert max(volts) - min(volts) == pytest.approx(EXPECTED_CH1_VPP, rel=0.05)

    rc, _ = edge.proc.close_stdin()
    assert rc == 0, edge.proc.output_tail()
    assert_clean_daemon_log(edge.proc)
    assert world.proc.terminate() == 0, world.proc.output_tail()
