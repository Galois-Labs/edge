from __future__ import annotations

import pytest

from galois_edge.capability_manager import CapabilityManager, SDKCommandRequest
from galois_edge.profile_schema import (
    CommandConfig, IdentityConfig, InstrumentMetadata, InstrumentProfile, ParameterConfig as P, SDKCallConfig,
)
from galois_edge.tracing import ResolvedSCPI, WriteTarget
from galois_edge.validation import ParamValidationError

pytestmark = pytest.mark.critical
ADDR = "GPIB0::24::INSTR"


def _manager() -> CapabilityManager:
    profile = InstrumentProfile(
        instrument=InstrumentMetadata(manufacturer="Keithley", model="2400", instrument_class="smu"),
        identity=IdentityConfig(patterns=["KEITHLEY"]),
        commands={
            "set_voltage": CommandConfig(scpi=":SOUR:VOLT {value}", type="write",
                                         params={"value": P(type="float", unit="V", min=-200.0, max=200.0)}),
            "source_voltage": CommandConfig(type="property", getter=":SOUR{channel}:VOLT?",
                                            setter=":SOUR{channel}:VOLT {value}",
                                            params={"channel": P(type="int", min=1, max=2, default=1),
                                                    "value": P(type="float", min=0.0, max=30.0, default=0.0)}),
            "sdk_cmd": CommandConfig(sdk_call=SDKCallConfig(method="m"), params={"x": P(type="float", min=0.0)}),
        },
    )
    cm = CapabilityManager()
    cm.register_instrument(ADDR, ADDR, "KEITHLEY", profile)
    return cm


def test_caps_resolve_command_returns_command_and_validated_params():
    caps = _manager().get_instrument_caps(ADDR)
    cmd, params = caps.resolve_command("set_voltage", {"value": "1.5"}, is_query=False)
    assert cmd.scpi == ":SOUR:VOLT {value}" and params == {"value": 1.5}
    with pytest.raises(KeyError):
        caps.resolve_command("nope", {})
    caps.disable_command("set_voltage")
    with pytest.raises(KeyError):
        caps.resolve_command("set_voltage", {"value": "1"})


def test_caps_resolve_command_is_query_is_an_additive_keyword_only_parameter():
    """edge-api.md §4 as amended for CI-1: ``resolve_command(name_or_alias, params, *, is_query=True)``."""
    import inspect

    caps = _manager().get_instrument_caps(ADDR)
    param = inspect.signature(caps.resolve_command).parameters["is_query"]
    assert (param.kind, param.default) == (inspect.Parameter.KEYWORD_ONLY, True)
    assert caps.resolve_command("source_voltage", {})[1] == {"channel": 1}  # the 2-arg form still works


def test_manager_resolve_raises_param_validation_error():
    with pytest.raises(ParamValidationError) as ei:
        _manager().resolve_command(ADDR, "set_voltage", {"value": "999"}, is_query=False)
    assert ei.value.field == "value" and ei.value.code == -222
    assert ei.value.message == "value: 999.0 is out of range [-200.0, 200.0]"


def test_manager_resolve_returns_resolved_scpi_with_context():
    out = _manager().resolve_command(ADDR, "set_voltage", {"value": "1.5"}, is_query=False)
    assert isinstance(out, ResolvedSCPI) and out == ":SOUR:VOLT 1.5"
    ctx = out.context
    assert (ctx.instrument_id, ctx.path, dict(ctx.params), ctx.form, ctx.is_query) == (
        ADDR, "set_voltage", {"value": 1.5}, "write", False)


def test_property_read_with_is_query_false_and_no_value():  # Review Focus 1, end to end
    out = _manager().resolve_command(ADDR, "source_voltage", None, is_query=False)
    assert out == ":SOUR1:VOLT?" and out.context.form == "getter"


def test_unknown_instrument_or_command_still_returns_none():
    cm = _manager()
    assert cm.resolve_command("NOPE", "set_voltage", {}) is None
    assert cm.resolve_command(ADDR, "nope", {}) is None


def test_sdk_request_keeps_original_params_after_validation():
    cm = _manager()
    req = cm.resolve_command(ADDR, "sdk_cmd", {"x": "1"})
    assert isinstance(req, SDKCommandRequest) and req.params == {"x": "1"}
    with pytest.raises(ParamValidationError):
        cm.resolve_command(ADDR, "sdk_cmd", {"x": "-1"})


def test_writes_for_uses_the_profile_hook_when_present():
    cm = _manager()
    caps = cm.get_instrument_caps(ADDR)
    assert caps.writes_for("set_voltage") == ()
    target = WriteTarget("output.voltage", "channel", 1, 2, "float")
    caps.profile.writes_for = lambda path: (target,) if path == "source_voltage" else ()
    out = cm.resolve_command(ADDR, "source_voltage", {"value": "5"}, is_query=False)
    assert out.context.writes == (target,) and out == ":SOUR1:VOLT 5"
