"""F18: the shim keeps edge's 20 dataclasses and builds them from galois-profiles."""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
import yaml

import galois_edge.profile_schema as ps
from galois_edge.profile_schema import (
    _gp_api, _legacy_profile_from_dict, profile_from_dict, profile_from_galois, profile_to_v1_mapping,
)

pytestmark = pytest.mark.critical
ROOT = Path(__file__).resolve().parents[1]
BUNDLED = sorted((ROOT / "src/galois_edge/profiles/scpi").glob("*.yaml"))
EXAMPLES = ROOT / "third_party/edgesim/contracts/examples/profiles"

# Snapshot taken at edge 16ed85e: (field name, default) in declaration order.
F18 = {
    "ParameterConfig": [("type", "'string'"), ("unit", "None"), ("min", "None"), ("max", "None"), ("default", "None"), ("description", "None"), ("options", "None"), ("map", "None")],
    "PreambleMap": [("x_increment", "None"), ("x_start", "None"), ("y_scale", "None"), ("y_offset", "None"), ("x_reference", "None"), ("y_reference", "None")],
    "BinaryConfig": [("dtype", "'uint8'"), ("byte_order", "'little'"), ("preamble_command", "None"), ("preamble_map", "None"), ("source_command", "None")],
    "ReturnConfig": [("type", "'string'"), ("unit", "None"), ("element_type", "None"), ("separator", "None"), ("format", "None"), ("fields", "None"), ("parser", "None"), ("binary", "None"), ("x_name", "None"), ("x_unit", "None"), ("x_start_query", "None"), ("x_increment_query", "None")],
    "SDKCallConfig": [("method", "None"), ("getter", "None"), ("setter", "None"), ("args_map", "None"), ("result_field", "None"), ("is_property", "False")],
    "SweepConfig": [("rate_param", "'sweep_rate'"), ("command", "''"), ("check_command", "''"), ("check_idle_match", "''"), ("stop_command", "''"), ("poll_interval_ms", "1000")],
    "CANSignalConfig": [("start_bit", "0"), ("bit_length", "8"), ("byte_order", "'little_endian'"), ("signed", "False"), ("scale", "1.0"), ("offset", "0.0")],
    "CANCommandConfig": [("message_id", "0"), ("direction", "'rx'"), ("signals", "None"), ("response_id", "None"), ("payload", "None"), ("dlc", "8")],
    "CommandConfig": [("scpi", "None"), ("getter", "None"), ("setter", "None"), ("type", "None"), ("description", "None"), ("enabled", "True"), ("streamable", "False"), ("is_dangerous", "False"), ("params", "None"), ("returns", "None"), ("sdk_call", "None"), ("force_query", "False"), ("requires_sweep", "False"), ("sweep", "None"), ("waveform_assembly", "None"), ("can", "None")],
    "SequenceStepConfig": [("command", "None"), ("scpi", "None"), ("args", "None"), ("capture", "None")],
    "SequenceConfig": [("steps", "factory:list"), ("description", "None"), ("parameters", "None"), ("returns", "None"), ("enabled", "True")],
    "SettingsConfig": [("timeout_ms", "5000"), ("terminator", "'\\n'"), ("opc_query", "False"), ("init_commands", "None"), ("cleanup_commands", "None")],
    "IdentityConfig": [("query", "'*IDN?'"), ("pattern", "None"), ("patterns", "None")],
    "InterfaceConfig": [("type", "'gpib'"), ("port", "None"), ("default_address", "None"), ("baud_rate", "None"), ("parity", "None"), ("data_bits", "None"), ("stop_bits", "None"), ("usb_vid", "None"), ("usb_pid", "None"), ("bus", "None"), ("bitrate", "None"), ("can_protocol", "None")],
    "SDKConnectConfig": [("method", "None"), ("args", "None"), ("defaults", "None"), ("constructor_args", "None")],
    "SDKDisconnectConfig": [("method", "None")],
    "SDKIdentityConfig": [("method", "None"), ("property", "None"), ("pattern", "None")],
    "SDKConfig": [("package", "''"), ("import_path", "''"), ("class_name", "''"), ("is_async", "False"), ("connect", "factory:SDKConnectConfig"), ("disconnect", "factory:SDKDisconnectConfig"), ("identity", "None")],
    "InstrumentMetadata": [("manufacturer", "''"), ("model", "''"), ("instrument_class", "''"), ("description", "None")],
    "InstrumentProfile": [("instrument", "factory:InstrumentMetadata"), ("identity", "factory:IdentityConfig"), ("interfaces", "factory:list"), ("settings", "factory:SettingsConfig"), ("commands", "factory:dict"), ("sequences", "None"), ("sdk", "None")],
}
MEMBERS = {
    "ParameterConfig": ["validate"], "PreambleMap": ["to_index_dict", "validate"], "BinaryConfig": ["validate"],
    "ReturnConfig": ["effective_binary", "is_ieee_block", "parse_response", "validate"],
    "CANSignalConfig": ["validate"], "CANCommandConfig": ["validate"],
    "CommandConfig": ["format_scpi", "get_scpi_string", "is_can_command", "is_sdk_command", "validate"],
    "SequenceStepConfig": ["validate"], "SequenceConfig": ["validate"],
    "IdentityConfig": ["all_patterns", "matches", "validate"],
    "InstrumentProfile": ["enabled_commands", "enabled_sequences", "get_command", "get_sequence", "is_sdk_instrument",
                          "matches_idn", "profile_key", "resolve_scpi_ref", "resolve_source_ref",
                          "to_capability_dict", "validate"],
}


def _default(f):
    if f.default is not dataclasses.MISSING:
        return repr(f.default)
    return "factory:" + f.default_factory.__name__


@pytest.mark.parametrize("name", sorted(F18))
def test_twenty_plain_dataclasses_unchanged(name):
    cls = getattr(ps, name)
    assert dataclasses.is_dataclass(cls) and "pydantic" not in str(cls.__mro__)
    assert [(f.name, _default(f)) for f in dataclasses.fields(cls)] == F18[name]
    for member in MEMBERS.get(name, []):
        assert hasattr(cls, member), (name, member)


def test_constants_and_entry_points_kept():
    assert ps.ALLOWED_BYTE_ORDERS == ("little", "big") and ps.IEEE_BLOCK_FORMATS == ("ieee_block", "ieee_binary")
    assert {"int8", "int16", "uint8", "float32", "float64"} <= set(ps.ALLOWED_BINARY_DTYPES)
    assert callable(ps.profile_from_dict)


@pytest.mark.parametrize("path", BUNDLED, ids=lambda p: p.name)
def test_galois_path_equals_the_legacy_reader_for_bundled_v1(path):
    legacy = _legacy_profile_from_dict(yaml.safe_load(path.read_text()))
    via = profile_from_galois(_gp_api("load_profile")(path))
    assert via.commands == legacy.commands
    assert (via.instrument, via.settings, via.interfaces, via.sequences, via.sdk) == (
        legacy.instrument, legacy.settings, legacy.interfaces, legacy.sequences, legacy.sdk)
    assert (via.identity.query, via.identity.all_patterns) == (legacy.identity.query, legacy.identity.all_patterns)
    assert via.profile_key == legacy.profile_key


@pytest.mark.parametrize("path", BUNDLED, ids=lambda p: p.name)
def test_v1_profile_from_dict_is_byte_identical_to_legacy(path):
    data = yaml.safe_load(path.read_text())
    assert profile_from_dict(data) == _legacy_profile_from_dict(data)


def test_profile_to_v1_mapping_round_trips_fixtures():
    from tests.mcp.conftest import _build_dmm_profile, _build_keithley_profile

    for profile in [_build_keithley_profile(), _build_dmm_profile(),
                    *[_legacy_profile_from_dict(yaml.safe_load(p.read_text())) for p in BUNDLED]]:
        assert _legacy_profile_from_dict(profile_to_v1_mapping(profile)) == profile


def test_v2_is_strict_and_keyed_by_path():
    doc = _gp_api("load_yaml")((EXAMPLES / "galois_sim-psu-2.yaml").read_text())
    profile = profile_from_dict(doc)
    assert "source.voltage" in profile.commands and "set_voltage" not in profile.commands
    assert set(profile.commands["source.voltage"].params) == {"channel", "voltage"}  # inherited _params flattened
    bad = dict(doc, bogus_key=1)
    with pytest.raises(ValueError):
        profile_from_dict(bad)


def test_v1_lenient_documents_still_parse_then_fail_validate():  # CI-16, tests/test_dynamic_profile_dir.py:203
    profile = profile_from_dict({"commands": {"x": {"params": {"v": "float"}}}})
    with pytest.raises(ValueError):
        profile.validate()


def test_galois_profile_is_lazy_for_directly_built_dataclasses():
    profile = _legacy_profile_from_dict(yaml.safe_load(BUNDLED[0].read_text()))
    assert profile._gp is None
    gp = profile.galois_profile
    assert gp.key == profile.profile_key and profile.galois_profile is gp
