"""semantics.md §3.3 decoding table (edge side of the parity rule; CI-4)."""
from __future__ import annotations

import pytest

from galois_edge.profile_schema import ParameterConfig as P
from galois_edge.validation import ParamValidationError, decode_float, decode_value

pytestmark = pytest.mark.critical

FV = P(type="float", unit="V", min=-10.0, max=10.0, default=1.0)
FA = P(type="float", unit="A", min=0.0, max=3.0)
FHZ = P(type="float", unit="Hz", min=0.0, max=1e10)
FRATE = P(type="float", unit="V/s")
FN = P(type="float")
I = P(type="int", min=1, max=4)
B = P(type="bool")
E = P(type="enum", options=["POSitive", "NEGative", "EITHer"])
EM = P(type="enum", options=["ON", "OFF"], map={"ON": 1, "OFF": 0})
S = P(type="string")

OK = [
    (FV, "5", 5.0), (FV, 5, 5.0), (FV, 2.5, 2.5), (FV, "1.2E-3", 1.2e-3), (FV, ".5", 0.5),
    (FV, "5 mV", 5e-3), (FV, "5mV", 5e-3), (FV, "5 V", 5.0), (FV, "-2.5v", -2.5),
    (FV, "MAX", 10.0), (FV, "min", -10.0), (FV, "DEF", 1.0), (FV, "maximum", 10.0),
    (FA, "250mA", 0.25), (FA, "2 A", 2.0), (FHZ, "1 MHz", 1e6), (FHZ, "10kHz", 1e4), (FHZ, "1 GHZ", 1e9),
    (FRATE, "0.5 V/s", 0.5), (FN, "3k", 3000.0), (FN, "2u", 2e-6),
    (I, "2", 2), (I, 3, 3), (I, "3.0", 3), (I, 4.0, 4), (I, "+1", 1),
    (B, "ON", True), (B, "off", False), (B, "1", True), (B, "0", False), (B, "true", True), (B, False, False), (B, 1, True),
    (E, "POSitive", "POSitive"), (E, "positive", "POSitive"), (E, "POS", "POSitive"), (E, "neg", "NEGative"), (E, "EITH", "EITHer"),
    (EM, "on", "ON"), (EM, "1", "ON"), (EM, 0, "OFF"), (EM, 1.0, "ON"),
    (S, "hello", "hello"), (S, '"quoted"', "quoted"), (S, "'single'", "single"), (S, 5, "5"),
]


@pytest.mark.parametrize("pc,raw,expected", OK)
def test_decodes(pc, raw, expected):
    got = decode_value("p", pc, raw)
    assert got == pytest.approx(expected) if isinstance(expected, float) else got == expected
    assert type(got) is type(expected)


ERR = [
    (FV, "11", -222, "p: 11.0 is out of range [-10.0, 10.0]"),
    (FV, "abc", -104, "p: expected float, got 'abc'"),
    (FV, "5 parsecs", -104, "p: expected float, got '5 parsecs'"),
    (FV, True, -104, "p: expected float, got True"),
    (FV, [1], -104, "p: expected float, got [1]"),
    (FN, "MAX", -224, "p: MAX is not defined for this parameter"),
    (I, "2.5", -104, "p: expected int, got '2.5'"),
    (I, "0", -222, "p: 0 is out of range [1, 4]"),
    (I, "1k", -104, "p: expected int, got '1k'"),
    (I, True, -104, "p: expected int, got True"),
    (B, "maybe", -224, "p: 'maybe' is not a boolean (ON|OFF|1|0|TRUE|FALSE)"),
    (B, 2, -224, "p: 2 is not a boolean (ON|OFF|1|0|TRUE|FALSE)"),
    (E, "SIDEways", -224, "p: 'SIDEways' is not one of ['POSitive', 'NEGative', 'EITHer']"),
    (EM, "2", -224, "p: '2' is not one of ['ON', 'OFF']"),
    (S, {"a": 1}, -104, "p: expected string, got {'a': 1}"),
]


@pytest.mark.parametrize("pc,raw,code,message", ERR)
def test_rejects(pc, raw, code, message):
    with pytest.raises(ParamValidationError) as ei:
        decode_value("p", pc, raw)
    assert (ei.value.field, ei.value.code, ei.value.message, str(ei.value)) == ("p", code, message, message)
    assert isinstance(ei.value, ValueError)


def test_decode_float_unit_rules():
    assert decode_float("1 MOHM", "ohm") == 1e6
    assert decode_float("5", None) == 5.0
    with pytest.raises(ValueError):
        decode_float("5 X", "V")


def test_codes_exist_in_the_contract_error_table():
    """edge-api.md §8: conformance reads third_party/edgesim/contracts/scpi_errors.json, never a copy."""
    import json
    from pathlib import Path

    from galois_edge import validation as v

    table = json.loads((Path(__file__).resolve().parents[1]
                        / "third_party/edgesim/contracts/scpi_errors.json").read_text())
    messages = {e["code"]: e["message"] for e in table["errors"]}
    assert {v.DATA_TYPE_ERROR: messages[-104], v.MISSING_PARAMETER: messages[-109],
            v.DATA_OUT_OF_RANGE: messages[-222], v.ILLEGAL_PARAMETER_VALUE: messages[-224]} == {
        -104: "Data type error", -109: "Missing parameter", -222: "Data out of range", -224: "Illegal parameter value"}
