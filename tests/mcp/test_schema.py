"""Unit tests for ParameterConfig -> JSON Schema mapping."""

from __future__ import annotations

import pytest

from galois_edge.mcp.schema import (
    command_to_input_schema,
    parameter_to_json_schema,
)
from galois_edge.profile_schema import (
    CommandConfig,
    ParameterConfig,
)


def test_float_parameter_maps_to_number():
    schema = parameter_to_json_schema(ParameterConfig(type="float"))
    assert schema["type"] == "number"


def test_int_parameter_maps_to_integer():
    schema = parameter_to_json_schema(ParameterConfig(type="int"))
    assert schema["type"] == "integer"


def test_string_parameter_maps_to_string():
    schema = parameter_to_json_schema(ParameterConfig(type="string"))
    assert schema["type"] == "string"


def test_bool_parameter_maps_to_boolean():
    schema = parameter_to_json_schema(ParameterConfig(type="bool"))
    assert schema["type"] == "boolean"


def test_enum_with_options_emits_enum():
    schema = parameter_to_json_schema(
        ParameterConfig(type="enum", options=["VOLT", "CURR"]),
    )
    assert schema["type"] == "string"
    assert schema["enum"] == ["VOLT", "CURR"]


def test_enum_without_options_raises():
    with pytest.raises(ValueError):
        parameter_to_json_schema(ParameterConfig(type="enum"))


def test_min_max_apply_to_numeric_only():
    schema = parameter_to_json_schema(
        ParameterConfig(type="float", min=-10.0, max=10.0),
    )
    assert schema["minimum"] == -10.0
    assert schema["maximum"] == 10.0

    no_bounds = parameter_to_json_schema(
        ParameterConfig(type="string", min=1, max=5),
    )
    assert "minimum" not in no_bounds
    assert "maximum" not in no_bounds


def test_default_propagates():
    schema = parameter_to_json_schema(
        ParameterConfig(type="int", default=3),
    )
    assert schema["default"] == 3


def test_unit_appended_to_description():
    schema = parameter_to_json_schema(
        ParameterConfig(
            type="float",
            description="Target voltage",
            unit="V",
        ),
    )
    assert "Target voltage" in schema["description"]
    assert "(unit: V)" in schema["description"]


def test_map_keys_become_enum_labels():
    schema = parameter_to_json_schema(
        ParameterConfig(
            type="string",
            map={"high": 1, "low": 0},
        ),
    )
    assert schema["type"] == "string"
    assert set(schema["enum"]) == {"high", "low"}


def test_command_input_schema_required_only_when_no_default():
    cmd = CommandConfig(
        scpi=":SOUR:VOLT {value}",
        params={
            "value": ParameterConfig(type="float"),
            "channel": ParameterConfig(type="int", default=1),
        },
    )
    schema = command_to_input_schema(cmd)
    assert schema["type"] == "object"
    assert set(schema["properties"].keys()) == {"value", "channel"}
    assert schema["required"] == ["value"]


def test_command_with_no_params_has_empty_properties():
    cmd = CommandConfig(scpi="*IDN?")
    schema = command_to_input_schema(cmd)
    assert schema["type"] == "object"
    assert schema["properties"] == {}
    assert "required" not in schema
