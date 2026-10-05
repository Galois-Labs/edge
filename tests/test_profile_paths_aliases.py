from __future__ import annotations

from pathlib import Path

import pytest

from galois_edge.profile_schema import _gp_api, profile_from_dict
from galois_edge.tracing import WriteTarget

pytestmark = pytest.mark.critical
EX = Path(__file__).resolve().parents[1] / "third_party/edgesim/contracts/examples/profiles"


def _v2(name):
    return profile_from_dict(_gp_api("load_yaml")((EX / name).read_text()))


def test_path_first_then_alias():
    psu = _v2("galois_sim-psu-2.yaml")
    assert psu.path_of("source.voltage") == "source.voltage"
    assert psu.path_of("set_voltage") == "source.voltage"
    assert psu.resolve("set_voltage") is psu.commands["source.voltage"]
    assert psu.get_command("set_voltage") is psu.commands["source.voltage"]
    assert psu.get_command("SET_VOLTAGE") is None and psu.get_command("source") is None  # case-sensitive; no groups
    with pytest.raises(KeyError):
        psu.resolve("nope")


def test_v1_names_are_paths():
    from tests.mcp.conftest import _build_keithley_profile
    p = _build_keithley_profile()
    assert p.path_of("set_voltage") == "set_voltage" and p.get_command("set_voltage") is p.commands["set_voltage"]


def test_binary_refs_resolve_paths_and_aliases():
    scope = _v2("galois_sim-scope-1.yaml")
    assert scope.resolve_scpi_ref("waveform.preamble") == ":WAVeform:PREamble?"
    assert scope.resolve_scpi_ref("waveform_preamble") == ":WAVeform:PREamble?"
    assert scope.resolve_scpi_ref(":RAW:SCPI?") == ":RAW:SCPI?"


def test_writes_for_binds_state_index_and_type():
    psu = _v2("galois_sim-psu-2.yaml")
    assert psu.writes_for("set_voltage") == (WriteTarget("output.voltage_setpoint", "channel", 1, 2, "float", None),)
    enabled, tripped = psu.writes_for("output.state")
    assert (enabled.path, enabled.state_type, tripped.path) == ("output.enabled", "bool", "output.protection.tripped")
    assert psu.writes_for("measure.voltage") == () and psu.writes_for("nope") == ()
