from __future__ import annotations

import pytest

from galois_edge.profile_schema import CommandConfig, ParameterConfig as P, ReturnConfig, SDKCallConfig
from galois_edge.validation import (
    ParamValidationError, argument_placeholders, coerce_response, select_template,
    template_placeholders, validate_params, wire_params,
)

pytestmark = pytest.mark.critical

VOLT = CommandConfig(
    type="property", getter=":SOUR{channel}:VOLT?", setter=":SOUR{channel}:VOLT {value}",
    params={"channel": P(type="int", min=1, max=2, default=1),
            "value": P(type="float", unit="V", min=0.0, max=30.0, default=0.0)},
)
SCALE = CommandConfig(  # DSOX-like: header param without a default
    type="property", getter=":CHANnel{channel}:SCALe?", setter=":CHANnel{channel}:SCALe {scale}",
    params={"channel": P(type="int", min=1, max=4), "scale": P(type="float")},
)
DISPLAY = CommandConfig(  # DSOX channel_display shape (options ON/OFF/1/0, map ON->1)
    type="property", getter=":CHANnel{channel}:DISPlay?", setter=":CHANnel{channel}:DISPlay {state}",
    params={"channel": P(type="int", min=1, max=4, default=1),
            "state": P(type="enum", options=["ON", "OFF", "1", "0"], map={"ON": 1, "OFF": 0})},
)


def test_property_without_value_reads_even_with_default():  # Review Focus 1
    assert select_template(VOLT, {}, False, use_defaults=True) == (VOLT.getter, "getter")
    assert validate_params(VOLT, None, is_query=False) == {"channel": 1}  # value NOT filled


def test_property_with_value_and_defaulted_header_writes():
    assert select_template(VOLT, {"value": "5"}, False, use_defaults=True) == (VOLT.setter, "setter")
    assert validate_params(VOLT, {"value": "5"}, is_query=False) == {"value": 5.0, "channel": 1}


def test_legacy_fallback_without_defaults_matches_format_scpi():
    assert select_template(SCALE, {"channel": "1"}, False) == (SCALE.getter, "getter")
    assert SCALE.format_scpi({"channel": "1"}, is_query=False) == ":CHANnel1:SCALe?"


def test_missing_header_without_default_is_rejected():
    with pytest.raises(ParamValidationError) as ei:
        validate_params(SCALE, {"scale": "0.5"}, is_query=False)
    assert (ei.value.field, ei.value.code, ei.value.message) == ("channel", -109, "channel: missing required parameter")


def test_property_read_validates_only_the_getter_params():  # CI-1 ruling, edge-api §4
    # A setter-only value on a read is never sent, so it passes through untouched.
    assert validate_params(VOLT, {"value": "999"}, is_query=True) == {"value": "999", "channel": 1}
    assert validate_params(VOLT, {"value": ""}, is_query=True) == {"value": "", "channel": 1}
    # The getter's own params are still checked on a read ...
    with pytest.raises(ParamValidationError) as ei:
        validate_params(VOLT, {"channel": "3", "value": "1"}, is_query=True)
    assert (ei.value.field, ei.value.code) == ("channel", -222)
    # ... and the setter's value param is checked when the setter is sent.
    with pytest.raises(ParamValidationError) as ei:
        validate_params(VOLT, {"value": "999"}, is_query=False)
    assert (ei.value.field, ei.value.code) == ("value", -222)


def test_unknown_keys_pass_through():  # Review Focus 2
    out = validate_params(VOLT, {"value": "5", "channels": "CHAN1,CHAN2"}, is_query=False)
    assert out["channels"] == "CHAN1,CHAN2"


def test_query_command_defaults_and_required():
    q = CommandConfig(type="query", scpi=":MEAS{channel}:VOLT?", params={"channel": P(type="int", default=1)})
    assert validate_params(q, {}) == {"channel": 1}
    q2 = CommandConfig(type="query", scpi=":MEAS{channel}:VOLT?")
    with pytest.raises(ParamValidationError):
        validate_params(q2, {})


def test_non_property_form_follows_the_header():  # semantics.md §3.1/§3.2
    freq = CommandConfig(type="query", scpi=":MEASure:FREQuency? {source}", params={"source": P(type="string")})
    assert select_template(freq, {"source": "CHAN1"}, True) == (freq.scpi, "query")
    assert select_template(CommandConfig(scpi="MEAS:CURR:PHAS? {channel}"), {}, True)[1] == "query"
    assert select_template(CommandConfig(scpi=":MEMory:VALid? {type},{location}"), {}, True)[1] == "query"
    assert select_template(CommandConfig(scpi=":SOUR:VOLT {value}"), {}, False)[1] == "write"
    assert select_template(CommandConfig(scpi="*RST"), {}, False)[1] == "write"
    assert select_template(CommandConfig(type="query", scpi=":FETCh"), {}, True)[1] == "query"  # explicit type


def test_placeholders_ignore_optional_nodes_and_manual_suffixes():
    assert template_placeholders("[:SOURce[<n>]]:VOLTage {value}") == (("value",), ())
    assert template_placeholders("[:SOUR{ch}]:VOLT {v}") == (("v",), ("ch",))
    assert argument_placeholders(":SOUR{channel}:VOLT {value}") == ("value",)
    assert argument_placeholders("*RST") == ()


def test_validation_does_not_apply_map_but_format_does_once():  # Review Focus 3
    v = validate_params(DISPLAY, {"state": "on"}, is_query=False)
    assert v == {"state": "ON", "channel": 1}
    wire = wire_params(DISPLAY, {"state": "on"}, v)
    assert wire == {"state": "ON", "channel": 1}
    assert DISPLAY.format_scpi(wire, is_query=False) == ":CHANnel1:DISPlay 1"


def test_non_string_enum_options_keep_the_caller_spelling_on_the_wire():
    # YAML 1.1 loads `options: [ON, OFF]` as [True, False]; the wire must not become "True".
    outp = CommandConfig(type="property", getter=":OUTP?", setter=":OUTP {state}",
                         params={"state": P(type="enum", options=[True, False])})
    v = validate_params(outp, {"state": "ON"}, is_query=False)
    assert v == {"state": True}
    assert wire_params(outp, {"state": "ON"}, v) == {"state": "ON"}
    assert outp.format_scpi(wire_params(outp, {"state": "ON"}, v), is_query=False) == ":OUTP ON"


def test_wire_params_keep_caller_spelling():
    v = validate_params(VOLT, {"value": "5"}, is_query=False)
    assert wire_params(VOLT, {"value": "5"}, v) == {"value": "5", "channel": 1}
    assert VOLT.format_scpi(wire_params(VOLT, {"value": "5"}, v), is_query=False) == ":SOUR1:VOLT 5"


def test_sdk_commands_validate_supplied_params_only():
    sdk = CommandConfig(sdk_call=SDKCallConfig(method="m"), params={"x": P(type="float", min=0.0)})
    assert validate_params(sdk, {}) == {}
    with pytest.raises(ParamValidationError):
        validate_params(sdk, {"x": "-1"})


@pytest.mark.parametrize("rtype,extra,text,expected", [
    ("float", {}, "+1.234500E+00", 1.2345), ("int", {}, "42", 42), ("int", {}, "4.2", None),
    ("bool", {}, "1", True), ("bool", {}, "OFF", False), ("bool", {}, "maybe", None),
    ("array", {}, "1,2,3", [1.0, 2.0, 3.0]), ("array", {"element_type": "int", "separator": ";"}, "1;2", [1, 2]),
    ("string", {}, " x ", "x"), ("binary", {}, "#14abcd", None), ("float", {}, "garbage", None),
    ("array", {"element_type": "int"}, "1,inf", None), ("int", {}, "inf", None),
])
def test_coerce_response(rtype, extra, text, expected):
    got = coerce_response(ReturnConfig(type=rtype, **extra), text)
    assert got == (pytest.approx(expected) if isinstance(expected, float) else expected)


def test_coerce_response_without_returns_is_the_stripped_string():
    assert coerce_response(None, " ok\n") == "ok"
    assert coerce_response(None, None) is None


def test_is_query_is_an_additive_keyword_only_parameter():
    """edge-api.md §4 as amended for CI-1: ``validate_params(command, params, *, is_query=True)``."""
    import inspect

    param = inspect.signature(validate_params).parameters["is_query"]
    assert (param.kind, param.default) == (inspect.Parameter.KEYWORD_ONLY, True)
