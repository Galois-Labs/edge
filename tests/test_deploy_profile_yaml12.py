"""CI-17: DeployProfile parses instrument profiles with galois-profiles' YAML 1.2 floats (edge-api.md §6)."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from galois_edge.capability_manager import CapabilityManager
from galois_edge.grpc_server import EdgeDaemonServicer
from galois_edge.profile_loader import ProfileLoader

pytestmark = pytest.mark.critical
ADDR = "TCPIP0::127.0.0.1::5025::SOCKET"
# YAML 1.1 (yaml.safe_load) reads `1e7` as the string "1e7"; YAML 1.2 reads it as a float.
PROFILE = (
    "instrument: {manufacturer: ACME, model: DEPLOYED}\n"
    "identity: {patterns: ['ACME,DEPLOYED']}\n"
    "commands:\n"
    "  freq: {scpi: ':FREQ {f}', type: write, params: {f: {type: float, max: 1e7}}}\n"
)


def _servicer(tmp_path):
    loader = ProfileLoader(str(tmp_path / "bundled"), dynamic_dir=str(tmp_path / "dynamic"))
    servicer = EdgeDaemonServicer(instrument_manager=MagicMock(), command_handler=MagicMock(), edge_id="t",
                                  capability_manager=CapabilityManager(), io_executor=MagicMock())
    servicer.set_profile_loader(loader)
    return servicer, loader


def test_deploy_reads_yaml12_floats(tmp_path):
    servicer, loader = _servicer(tmp_path)
    assert loader.load_all() == 0          # loaded before the deploy: it registers in memory, no reload
    resp = servicer._write_instrument_profile("acme_deployed", PROFILE)
    assert resp.success, resp.error_message
    assert loader.get_profile("acme_deployed").commands["freq"].params["f"].max == 1e7


async def test_bind_fallback_reads_yaml12_floats(tmp_path):
    servicer, loader = _servicer(tmp_path)
    (tmp_path / "dynamic").mkdir()
    (tmp_path / "dynamic" / "acme_deployed.yaml").write_text(PROFILE)   # deployed, not yet loaded
    resp = await servicer._bind_instrument_profile("acme_deployed", ADDR)
    assert resp.success, resp.error_message
    caps = servicer._capability_manager.get_instrument_caps(ADDR)
    assert caps.get_command("freq").params["f"].max == 1e7
