"""
Profile schema tests for returns.type == binary / format ieee_block
(doc §2.3): the optional binary sub-block (dtype, byte_order,
preamble_command, index-only preamble_map) and the ieee_binary alias
normalisation.
"""

import pytest

from galois_edge.profile_schema import (
    BinaryConfig,
    PreambleMap,
    ReturnConfig,
    profile_from_dict,
)


def _profile_with_returns(returns_dict):
    return profile_from_dict(
        {
            "instrument": {"manufacturer": "Test", "model": "T1"},
            "identity": {"pattern": "TEST"},
            "commands": {
                "waveform_data": {
                    "scpi": ":WAVeform:DATA?",
                    "type": "query",
                    "returns": returns_dict,
                },
            },
        }
    )


class TestIEEEBlockFormat:
    def test_ieee_block_accepted(self):
        profile = _profile_with_returns({"type": "binary", "format": "ieee_block"})
        returns = profile.commands["waveform_data"].returns
        assert returns.format == "ieee_block"
        assert returns.is_ieee_block

    def test_ieee_binary_alias_normalised(self):
        profile = _profile_with_returns({"type": "binary", "format": "ieee_binary"})
        returns = profile.commands["waveform_data"].returns
        assert returns.format == "ieee_block"
        assert returns.is_ieee_block

    def test_binary_without_format_defaults_to_block(self):
        profile = _profile_with_returns({"type": "binary"})
        returns = profile.commands["waveform_data"].returns
        assert returns.is_ieee_block
        # effective_binary supplies safe defaults.
        binary = returns.effective_binary
        assert binary is not None
        assert binary.dtype == "uint8"
        assert binary.byte_order == "little"

    def test_non_binary_type_is_not_block(self):
        profile = _profile_with_returns({"type": "vector", "format": "ieee_binary"})
        returns = profile.commands["waveform_data"].returns
        assert not returns.is_ieee_block
        assert returns.effective_binary is None


class TestBinarySubBlock:
    def test_full_dsox_shape(self):
        profile = _profile_with_returns(
            {
                "type": "binary",
                "format": "ieee_block",
                "unit": "V",
                "binary": {
                    "dtype": "int16",
                    "byte_order": "little",
                    "preamble_command": "waveform_preamble",
                    "preamble_map": {
                        "x_increment": 4,
                        "x_start": 5,
                        "x_reference": 6,
                        "y_scale": 7,
                        "y_offset": 8,
                        "y_reference": 9,
                    },
                },
            }
        )
        profile.validate()
        binary = profile.commands["waveform_data"].returns.binary
        assert binary.dtype == "int16"
        assert binary.byte_order == "little"
        assert binary.preamble_command == "waveform_preamble"
        assert binary.preamble_map.to_index_dict() == {
            "x_increment": 4,
            "x_start": 5,
            "x_reference": 6,
            "y_scale": 7,
            "y_offset": 8,
            "y_reference": 9,
        }

    def test_invalid_dtype_rejected(self):
        profile = _profile_with_returns(
            {"type": "binary", "binary": {"dtype": "int64"}}
        )
        with pytest.raises(ValueError, match="binary.dtype"):
            profile.validate()

    def test_uint16_dtype_rejected(self):
        cfg = BinaryConfig(dtype="uint16")
        with pytest.raises(ValueError, match="binary.dtype"):
            cfg.validate()

    def test_invalid_byte_order_rejected(self):
        cfg = BinaryConfig(dtype="int16", byte_order="native")
        with pytest.raises(ValueError, match="byte_order"):
            cfg.validate()

    def test_negative_preamble_index_rejected(self):
        cfg = BinaryConfig(
            preamble_command="p",
            preamble_map=PreambleMap(y_scale=-1),
        )
        with pytest.raises(ValueError, match="non-negative"):
            cfg.validate()

    def test_preamble_map_requires_command(self):
        cfg = BinaryConfig(preamble_map=PreambleMap(y_scale=7))
        with pytest.raises(ValueError, match="requires binary.preamble_command"):
            cfg.validate()

    def test_binary_on_non_binary_type_rejected(self):
        returns = ReturnConfig(type="float", binary=BinaryConfig())
        with pytest.raises(ValueError, match="only valid"):
            returns.validate()


class TestSourceCommand:
    """binary.source_command — the multi-channel knob (doc §3.5)."""

    def test_source_command_parsed(self):
        profile = _profile_with_returns(
            {
                "type": "binary",
                "binary": {
                    "dtype": "int16",
                    "source_command": "waveform_source",
                },
            }
        )
        profile.validate()
        binary = profile.commands["waveform_data"].returns.binary
        assert binary.source_command == "waveform_source"

    def test_source_command_defaults_to_none(self):
        profile = _profile_with_returns(
            {"type": "binary", "binary": {"dtype": "int16"}}
        )
        binary = profile.commands["waveform_data"].returns.binary
        assert binary.source_command is None

    def test_resolve_source_ref_sibling_setter(self):
        profile = profile_from_dict(
            {
                "instrument": {"manufacturer": "Test", "model": "T1"},
                "identity": {"pattern": "TEST"},
                "commands": {
                    "waveform_source": {
                        "getter": ":WAVeform:SOURce?",
                        "setter": ":WAVeform:SOURce {source}",
                        "type": "property",
                    },
                },
            }
        )
        assert (
            profile.resolve_source_ref("waveform_source", "CHANnel2")
            == ":WAVeform:SOURce CHANnel2"
        )

    def test_resolve_source_ref_raw_scpi_template(self):
        profile = _profile_with_returns({"type": "binary"})
        assert (
            profile.resolve_source_ref(":WAVeform:SOURce {channel}", "CHANnel3")
            == ":WAVeform:SOURce CHANnel3"
        )


class TestPreambleResolution:
    def _profile(self):
        return profile_from_dict(
            {
                "instrument": {"manufacturer": "Test", "model": "T1"},
                "identity": {"pattern": "TEST"},
                "commands": {
                    "waveform_preamble": {
                        "scpi": ":WAVeform:PREamble?",
                        "type": "query",
                    },
                    "waveform_data": {
                        "scpi": ":WAVeform:DATA?",
                        "type": "query",
                        "returns": {
                            "type": "binary",
                            "binary": {
                                "dtype": "uint8",
                                "preamble_command": "waveform_preamble",
                            },
                        },
                    },
                },
            }
        )

    def test_sibling_command_name_resolved(self):
        profile = self._profile()
        assert (
            profile.resolve_scpi_ref("waveform_preamble") == ":WAVeform:PREamble?"
        )

    def test_raw_scpi_passes_through(self):
        profile = self._profile()
        assert (
            profile.resolve_scpi_ref(":WAVeform:PREamble?") == ":WAVeform:PREamble?"
        )
