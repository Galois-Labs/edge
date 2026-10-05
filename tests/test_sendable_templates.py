from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from galois_edge.profile_schema import CommandConfig, ParameterConfig as P, profile_from_dict

pytestmark = pytest.mark.critical
DP800 = Path(__file__).resolve().parents[1] / "src/galois_edge/profiles/scpi/rigol_dp800.yaml"


def _sample(pc):
    if pc.default is not None:
        return pc.default
    if pc.type == "enum":  # a bracket-free option, so only the template can leave manual notation behind
        return next((o for o in pc.options if not set(str(o)) & set("[]<>")), pc.options[0])
    if pc.type in ("int", "float"):
        return pc.min if pc.min is not None else 1
    return "ON" if pc.type == "bool" else "X"


def test_every_dp800_command_formats_without_manual_notation():
    profile = profile_from_dict(yaml.safe_load(DP800.read_text()))
    checked = 0
    for name, cmd in profile.commands.items():
        params = {n: _sample(pc) for n, pc in (cmd.params or {}).items()}
        for is_query in ([True, False] if cmd.type == "property" else [True]):
            if cmd.get_scpi_string(is_query) is None:
                continue
            wire = cmd.format_scpi(params, is_query)
            assert not set(wire) & set("[]<>"), (name, is_query, wire)
            checked += 1
    assert checked >= 100


def test_dp800_known_strings():
    p = profile_from_dict(yaml.safe_load(DP800.read_text()))
    assert p.commands["source_voltage"].format_scpi({"value": 5}, is_query=False) == ":VOLTage 5"
    assert p.commands["source_voltage"].format_scpi({}, is_query=True) == ":VOLTage?"
    assert p.commands["source_voltage"].format_scpi({}, is_query=False) == ":VOLTage?"   # read fallback kept
    assert p.commands["select_channel_query"].format_scpi() == ":INSTrument?"
    # "<V" is a DP800 option value, not manual notation: values are sent as given.
    assert (p.commands["monitor_voltage_condition"].format_scpi({"condition": "<V", "logic": "AND"}, is_query=False)
            == ":MONItor:VOLTage:CONDition <V,AND")


def test_plain_templates_unchanged():
    cmd = CommandConfig(type="write", scpi=":CHAN{ch}:DISP {state}",
                        params={"ch": P(type="int"), "state": P(type="enum", options=["ON", "OFF"], map={"ON": 1})})
    assert cmd.format_scpi({"ch": 2, "state": "ON"}, is_query=False) == ":CHAN2:DISP 1"
    assert CommandConfig(scpi="*RST").format_scpi() == "*RST"
    with pytest.raises(ValueError):
        CommandConfig(type="property", getter=":X?").format_scpi({}, is_query=False)
