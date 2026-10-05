"""tests/sim: edge in SIM_MODE against edgesim benches. Needs the `sim` extra (make test-prereqs).

`sim_stack` drives real edge code paths without opening any socket: `_build_instrument_manager`,
`_load_profiles` and `_try_match_profile` from main.py, and an `MCPServer` that is constructed but
never started, so its FastMCP app is called in-process. Stepped clock; seed derived from the test
node id (spec §10 rules 4-5); benches read in place from the submodule's contract examples.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

try:
    import edgesim.edge  # noqa: F401
except ImportError as exc:  # never skip silently (edge-api.md §7)
    raise pytest.UsageError(
        "tests/sim needs the sim extra: uv pip install -e '.[dev,test,sim]' with cargo + maturin on PATH"
    ) from exc

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "third_party/edgesim/contracts/python"
BENCHES = ROOT / "third_party/edgesim/contracts/examples/benches"
PSU_BENCH = BENCHES / "psu_resistor_dmm.bench.json"
SCOPE_BENCH = BENCHES / "awg_rc_scope.bench.yaml"
SCHEMA = ROOT / "third_party/edgesim/contracts/schemas/trace-v1.schema.json"


def node_seed(nodeid: str) -> int:
    return int.from_bytes(hashlib.blake2b(nodeid.encode(), digest_size=8).digest(), "little")


@dataclass
class SimStack:
    daemon: Any
    mcp: Any
    backend: Any

    def address_of(self, profile_key: str) -> str:
        for instrument_id, caps in self.daemon._capability_manager.all_instruments.items():
            if caps.profile_key == profile_key:
                return instrument_id
        raise KeyError(profile_key)


@pytest.fixture
def sim_stack(monkeypatch, request, tmp_path):
    from galois_edge.capability_manager import CapabilityManager
    from galois_edge.command_handler import CommandHandler
    from galois_edge.config import Config
    from galois_edge.main import EdgeDaemon
    from galois_edge.mcp.server import MCPServer

    # Hermetic (spec §10): no PyVISA or raw-USB enumeration; every bench address routes to the backend.
    monkeypatch.setattr("galois_edge.instrument_manager.PYVISA_AVAILABLE", False)
    monkeypatch.setattr("galois_edge.instrument_manager.USB_AVAILABLE", False)
    made = []

    def build(bench: Path, **env: str) -> SimStack:
        settings = {"SIM_MODE": "true", "SIM_BENCH": str(bench), "SIM_CLOCK": "stepped",
                    "SIM_SEED": str(node_seed(request.node.nodeid)), "SIM_MARK_INSTRUMENTS": "true",
                    "SIM_CONTROL_TOOLS": "true", **env}
        for key, value in settings.items():
            monkeypatch.setenv(key, value)
        daemon = EdgeDaemon(Config(gpib_enabled=False, usb_monitor_enabled=False, lan_instruments="",
                                   mcp_enabled=False, dynamic_profile_dir=str(tmp_path / "dynamic"),
                                   driver_profile_dir=str(tmp_path / "drivers")))
        made.append(daemon)
        daemon._instrument_manager = daemon._build_instrument_manager()
        daemon._capability_manager = CapabilityManager()
        daemon._command_handler = CommandHandler(daemon._instrument_manager)
        daemon._load_profiles()
        backend = daemon._instrument_manager.extra_backends[0]
        for address in backend.list_resources():
            daemon._try_match_profile(address)
        server = MCPServer(capability_manager=daemon._capability_manager, command_handler=daemon._command_handler,
                           instrument_manager=daemon._instrument_manager, port=0)
        return SimStack(daemon, server.app, backend)

    yield build
    for daemon in made:
        daemon._stop_tracing()
        daemon._instrument_manager.close_backends()
        daemon._io_executor.shutdown(wait=False)
