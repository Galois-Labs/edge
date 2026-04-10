"""
Unit tests for profile_schema.py — ReturnConfig.parse_response(), CommandConfig.format_scpi() with map.
"""

import os
import sys

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "src")
)

from galois_edge.profile_schema import (
    CommandConfig,
    ParameterConfig,
    ReturnConfig,
    SweepConfig,
    profile_from_dict,
)


# ---------------------------------------------------------------------------
# ReturnConfig.parse_response()
# ---------------------------------------------------------------------------


class TestParseResponseRegex:
    """Test regex parser mode in ReturnConfig.parse_response()."""

    def test_regex_with_capture_group(self):
        rc = ReturnConfig(
            type="string",
            parser={"type": "regex", "pattern": r"VOLT\s+([\d.]+)", "group": 1},
        )
        assert rc.parse_response("VOLT 3.14159") == "3.14159"

    def test_regex_group_zero_returns_full_match(self):
        rc = ReturnConfig(
            type="string",
            parser={"type": "regex", "pattern": r"OK:\d+", "group": 0},
        )
        assert rc.parse_response("status OK:42 done") == "OK:42"

    def test_regex_no_match_falls_through(self):
        rc = ReturnConfig(
            type="string",
            parser={"type": "regex", "pattern": r"NOTHERE_(\d+)", "group": 1},
        )
        raw = "something else entirely"
        assert rc.parse_response(raw) == raw  # passthrough fallback


class TestParseResponseStrip:
    """Test strip parser mode in ReturnConfig.parse_response()."""

    def test_strip_prefix(self):
        rc = ReturnConfig(
            type="string",
            parser={"type": "strip", "prefix": "VOLT "},
        )
        assert rc.parse_response("VOLT 1.234") == "1.234"

    def test_strip_suffix(self):
        rc = ReturnConfig(
            type="string",
            parser={"type": "strip", "suffix": " V"},
        )
        assert rc.parse_response("1.234 V") == "1.234"

    def test_strip_prefix_and_suffix(self):
        rc = ReturnConfig(
            type="string",
            parser={"type": "strip", "prefix": "[", "suffix": "]"},
        )
        assert rc.parse_response("[hello]") == "hello"

    def test_strip_prefix_not_present(self):
        """When prefix doesn't match, string is untouched by prefix logic."""
        rc = ReturnConfig(
            type="string",
            parser={"type": "strip", "prefix": "XYZ"},
        )
        assert rc.parse_response("ABCDEF") == "ABCDEF"

    def test_strip_suffix_not_present(self):
        """When suffix doesn't match, string is untouched by suffix logic."""
        rc = ReturnConfig(
            type="string",
            parser={"type": "strip", "suffix": "ZZZ"},
        )
        assert rc.parse_response("ABCDEF") == "ABCDEF"


class TestParseResponseSplit:
    """Test split parser mode in ReturnConfig.parse_response()."""

    def test_split_comma_index(self):
        rc = ReturnConfig(
            type="string",
            parser={"type": "split", "delimiter": ",", "index": 2},
        )
        assert rc.parse_response("A, B, C, D") == "C"

    def test_split_default_delimiter_and_index(self):
        """Default delimiter=',' and index=0."""
        rc = ReturnConfig(
            type="string",
            parser={"type": "split"},
        )
        assert rc.parse_response("first,second,third") == "first"

    def test_split_index_out_of_range(self):
        """Index beyond available parts falls through to raw."""
        rc = ReturnConfig(
            type="string",
            parser={"type": "split", "delimiter": ",", "index": 10},
        )
        raw = "A,B"
        assert rc.parse_response(raw) == raw


class TestParseResponsePassthrough:
    """Test that no parser means passthrough."""

    def test_no_parser_returns_raw(self):
        rc = ReturnConfig(type="string", parser=None)
        raw = "  raw response with spaces  "
        assert rc.parse_response(raw) == raw

    def test_empty_parser_dict_returns_raw(self):
        """An empty parser dict should still use the default regex type which
        has an empty pattern and fails to match, falling through to raw."""
        rc = ReturnConfig(type="string", parser={})
        raw = "some value"
        assert rc.parse_response(raw) == raw


# ---------------------------------------------------------------------------
# CommandConfig.format_scpi() with map transformations
# ---------------------------------------------------------------------------


class TestFormatScpiWithMap:
    """Test format_scpi() applies map forward-transformations."""

    def test_map_substitutes_label_to_wire_value(self):
        cmd = CommandConfig(
            scpi=":OUTP:STAT {state}",
            type="write",
            params={
                "state": ParameterConfig(
                    type="enum",
                    options=["ON", "OFF"],
                    map={"ON": 1, "OFF": 0},
                ),
            },
        )
        result = cmd.format_scpi(params={"state": "ON"}, is_query=False)
        assert result == ":OUTP:STAT 1"

    def test_map_value_not_in_map_passes_through(self):
        """If the value is not in the map, it should be used as-is (no crash)."""
        cmd = CommandConfig(
            scpi=":OUTP:STAT {state}",
            type="write",
            params={
                "state": ParameterConfig(
                    type="enum",
                    options=["ON", "OFF", "TOGGLE"],
                    map={"ON": 1, "OFF": 0},
                ),
            },
        )
        result = cmd.format_scpi(params={"state": "TOGGLE"}, is_query=False)
        assert result == ":OUTP:STAT TOGGLE"

    def test_no_map_on_param_normal_substitution(self):
        """No map attribute -> normal substitution without error."""
        cmd = CommandConfig(
            scpi=":SOUR:VOLT {voltage}",
            type="write",
            params={
                "voltage": ParameterConfig(type="float", unit="V"),
            },
        )
        result = cmd.format_scpi(params={"voltage": 3.14}, is_query=False)
        assert result == ":SOUR:VOLT 3.14"

    def test_map_with_numeric_value_stringified(self):
        """Numeric param values are stringified before map lookup."""
        cmd = CommandConfig(
            scpi=":CHAN {ch}",
            type="write",
            params={
                "ch": ParameterConfig(
                    type="int",
                    map={"1": "A", "2": "B"},
                ),
            },
        )
        # Passing integer 1 -> str(1) = "1" -> mapped to "A"
        result = cmd.format_scpi(params={"ch": 1}, is_query=False)
        assert result == ":CHAN A"

    def test_format_scpi_no_params_no_crash(self):
        """Calling format_scpi with no params and no param defs is fine."""
        cmd = CommandConfig(scpi="*RST", type="write")
        assert cmd.format_scpi() == "*RST"

    def test_format_scpi_params_but_no_param_defs(self):
        """Params dict without matching CommandConfig.params uses simple substitution."""
        cmd = CommandConfig(scpi=":FREQ {freq}", type="write", params=None)
        result = cmd.format_scpi(params={"freq": 1000})
        assert result == ":FREQ 1000"


# ---------------------------------------------------------------------------
# Property-command setter -> getter fallback
# (defensive fix for clients that hardcode is_query=False on property reads)
# ---------------------------------------------------------------------------


class TestFormatScpiPropertyFallback:
    """Test that property commands fall back to the getter template when the
    setter is invoked with no value (e.g., CommandPanel hardcoding
    is_query=False)."""

    def test_format_scpi_property_read_without_value_param_uses_getter(self):
        """Property setter invoked with no value -> falls back to getter."""
        cmd = CommandConfig(
            getter=":SOURce1:POWer? {param}",
            setter=":SOURce1:POWer {value}",
            type="property",
            params={
                "param": ParameterConfig(type="string"),
                "value": ParameterConfig(type="float"),
            },
        )
        # Simulates broken CommandPanel path: is_query=False but no `value`.
        result = cmd.format_scpi(params={"param": "ACT"}, is_query=False)
        assert result == ":SOURce1:POWer? ACT"
        assert "{value}" not in result

    def test_format_scpi_property_write_with_value_param_uses_setter(self):
        """Property setter invoked WITH value -> uses setter normally."""
        cmd = CommandConfig(
            getter=":SOURce1:POWer? {param}",
            setter=":SOURce1:POWer {value}",
            type="property",
            params={
                "param": ParameterConfig(type="string"),
                "value": ParameterConfig(type="float"),
            },
        )
        result = cmd.format_scpi(params={"value": "10.0"}, is_query=False)
        assert result == ":SOURce1:POWer 10.0"

    def test_format_scpi_property_explicit_query_uses_getter(self):
        """Explicit is_query=True always uses the getter."""
        cmd = CommandConfig(
            getter=":SOURce1:POWer? {param}",
            setter=":SOURce1:POWer {value}",
            type="property",
            params={
                "param": ParameterConfig(type="string"),
                "value": ParameterConfig(type="float"),
            },
        )
        result = cmd.format_scpi(params={"param": "ACT"}, is_query=True)
        assert result == ":SOURce1:POWer? ACT"

    def test_format_scpi_non_property_unaffected(self):
        """Non-property commands always use the scpi field, regardless of is_query."""
        cmd = CommandConfig(
            scpi=":MEASure:POWer?",
            type="query",
        )
        # is_query=True
        assert cmd.format_scpi(is_query=True) == ":MEASure:POWer?"
        # is_query=False (the type is "query", so no fallback should happen)
        assert cmd.format_scpi(is_query=False) == ":MEASure:POWer?"

    def test_format_scpi_property_no_getter_no_fallback(self):
        """If a property has no getter, the unresolved setter is returned as-is
        (no fallback possible). This preserves prior behavior."""
        cmd = CommandConfig(
            setter=":SOURce1:POWer {value}",
            type="property",
            params={
                "value": ParameterConfig(type="float"),
            },
        )
        result = cmd.format_scpi(params={}, is_query=False)
        # Unresolved placeholder remains; no getter to fall back to.
        assert result == ":SOURce1:POWer {value}"


# ---------------------------------------------------------------------------
# SweepConfig and requires_sweep
# ---------------------------------------------------------------------------


class TestSweepConfig:
    """Test SweepConfig dataclass and its integration with CommandConfig."""

    def test_sweep_config_defaults(self):
        sc = SweepConfig()
        assert sc.rate_param == "sweep_rate"
        assert sc.command == ""
        assert sc.check_command == ""
        assert sc.check_idle_match == ""
        assert sc.stop_command == ""
        assert sc.poll_interval_ms == 1000

    def test_sweep_config_from_kwargs(self):
        sc = SweepConfig(
            rate_param="ramp_rate",
            command="RAMP {value} {ramp_rate}",
            check_command="STATUS?",
            check_idle_match="IDLE",
            stop_command="ABORT",
            poll_interval_ms=500,
        )
        assert sc.rate_param == "ramp_rate"
        assert sc.command == "RAMP {value} {ramp_rate}"
        assert sc.check_idle_match == "IDLE"
        assert sc.stop_command == "ABORT"
        assert sc.poll_interval_ms == 500

    def test_command_config_requires_sweep_default_false(self):
        cmd = CommandConfig(scpi="SET {value}", type="write")
        assert cmd.requires_sweep is False
        assert cmd.sweep is None

    def test_command_config_with_sweep(self):
        sweep = SweepConfig(
            command="T{sweep_rate}\nJ{value}\nA1",
            check_command="X",
            check_idle_match="X0",
            stop_command="A0",
        )
        cmd = CommandConfig(
            getter="R7",
            setter="J{value}",
            type="property",
            requires_sweep=True,
            sweep=sweep,
        )
        assert cmd.requires_sweep is True
        assert cmd.sweep is not None
        assert cmd.sweep.stop_command == "A0"


class TestSweepConfigFromYAML:
    """Test SweepConfig loading through profile_from_dict."""

    def test_sweep_config_loads_from_dict(self):
        """SweepConfig is correctly loaded from a YAML-style dict."""
        data = {
            "instrument": {
                "manufacturer": "Oxford",
                "model": "IPS120",
                "class": "magnet_controller",
            },
            "identity": {"pattern": "Oxford.*IPS120"},
            "commands": {
                "b": {
                    "getter": "R7",
                    "setter": "J{value}",
                    "type": "property",
                    "force_query": True,
                    "requires_sweep": True,
                    "sweep": {
                        "rate_param": "sweep_rate",
                        "command": "T{sweep_rate}\nJ{value}\nA1",
                        "check_command": "X",
                        "check_idle_match": "X0",
                        "stop_command": "A0",
                        "poll_interval_ms": 1000,
                    },
                },
                "identify": {
                    "scpi": "*IDN?",
                    "type": "query",
                },
            },
        }
        profile = profile_from_dict(data)
        b_cmd = profile.get_command("b")
        assert b_cmd is not None
        assert b_cmd.requires_sweep is True
        assert b_cmd.sweep is not None
        assert b_cmd.sweep.rate_param == "sweep_rate"
        assert b_cmd.sweep.command == "T{sweep_rate}\nJ{value}\nA1"
        assert b_cmd.sweep.check_command == "X"
        assert b_cmd.sweep.check_idle_match == "X0"
        assert b_cmd.sweep.stop_command == "A0"
        assert b_cmd.sweep.poll_interval_ms == 1000

    def test_requires_sweep_false_by_default_from_dict(self):
        """Commands without requires_sweep in YAML default to False."""
        data = {
            "instrument": {"manufacturer": "Test", "model": "T1"},
            "identity": {"pattern": "Test.*T1"},
            "commands": {
                "measure": {
                    "scpi": ":MEAS?",
                    "type": "query",
                },
            },
        }
        profile = profile_from_dict(data)
        cmd = profile.get_command("measure")
        assert cmd is not None
        assert cmd.requires_sweep is False
        assert cmd.sweep is None

    def test_requires_sweep_without_sweep_block(self):
        """requires_sweep can be True even without a sweep block (for validation)."""
        data = {
            "instrument": {"manufacturer": "Test", "model": "T2"},
            "identity": {"pattern": "Test.*T2"},
            "commands": {
                "set_field": {
                    "scpi": "SET:FIELD {value}",
                    "type": "write",
                    "requires_sweep": True,
                },
            },
        }
        profile = profile_from_dict(data)
        cmd = profile.get_command("set_field")
        assert cmd is not None
        assert cmd.requires_sweep is True
        assert cmd.sweep is None
