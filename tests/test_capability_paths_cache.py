from __future__ import annotations

import time
from pathlib import Path

import pytest

from galois_edge.capability_manager import CapabilityManager
from galois_edge.profile_schema import CommandConfig, InstrumentMetadata, InstrumentProfile, _gp_api, profile_from_dict
from galois_edge.tracing import CommandEvent, TraceWriter

pytestmark = pytest.mark.critical
EX = Path(__file__).resolve().parents[1] / "third_party/edgesim/contracts/examples/profiles"
ADDR = "TCPIP0::127.0.0.1::5025::SOCKET"


def _psu_manager():
    cm = CapabilityManager()
    cm.register_instrument(ADDR, ADDR, "GALOIS,SIM-PSU-2", profile_from_dict(
        _gp_api("load_yaml")((EX / "galois_sim-psu-2.yaml").read_text())))
    return cm


def test_alias_and_path_lookup_and_toggles():
    caps = _psu_manager().get_instrument_caps(ADDR)
    assert caps.get_command("set_voltage") is caps.get_command("source.voltage") is not None
    assert caps.command_path("set_voltage") == "source.voltage"
    assert caps.disable_command("set_voltage") is True
    assert "source.voltage" not in caps.enabled_commands and caps.get_command("source.voltage") is None
    assert caps.enable_command("source.voltage") is True and caps.get_command("set_voltage") is not None


def test_enabled_commands_is_cached_for_huge_profiles():
    profile = InstrumentProfile(instrument=InstrumentMetadata(manufacturer="X", model="Y"),
                                commands={f"c{i}": CommandConfig(scpi=f":C{i}?") for i in range(10_000)})
    cm = CapabilityManager()
    caps = cm.register_instrument("A", "A", "", profile)
    start = time.perf_counter()
    for i in range(2000):
        assert caps.get_command(f"c{i}") is not None
    assert time.perf_counter() - start < 0.5
    profile.commands["new"] = CommandConfig(scpi=":NEW?")     # adding a command invalidates by size
    assert caps.get_command("new") is not None


def test_alias_resolution_reaches_the_trace_delta(tmp_path):
    cm = _psu_manager()
    wire = cm.resolve_command(ADDR, "set_voltage", {"voltage": "5"}, is_query=False)
    assert wire == ":SOURce1:VOLTage 5" and wire.context.path == "source.voltage"
    writer = TraceWriter(tmp_path, run_id="r")
    writer.start()
    writer.observe(CommandEvent(ADDR, str(wire), wire.context, False, True, None, None, "", time.time_ns(), 1))
    writer.close()
    import json
    rec = json.loads(writer.path.read_text().splitlines()[1])
    assert rec["action"]["path"] == "source.voltage" and rec["delta"] == {"output.voltage_setpoint[1]": [None, 5.0]}
