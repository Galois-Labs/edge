"""
Acceptance-profile tests for keysight_dsox3000.yaml (doc §2.4).

The DSOX3000 profile is the acceptance vehicle for the IEEE 488.2
block path: WORD + signed -> int16 little-endian transfers, a 10-field
CSV preamble mapped by index, and the reference-point composition done
in daemon code.
"""

from pathlib import Path

import pytest
import yaml

from galois_edge.profile_schema import profile_from_dict

PROFILE_PATH = (
    Path(__file__).parent.parent
    / "src" / "galois_edge" / "profiles" / "scpi" / "keysight_dsox3000.yaml"
)


@pytest.fixture(scope="module")
def profile():
    with open(PROFILE_PATH) as fh:
        data = yaml.safe_load(fh)
    profile = profile_from_dict(data)
    profile.validate()
    return profile


class TestDSOX3000Profile:
    def test_identity_matches_dsox_idn(self, profile):
        assert profile.matches_idn(
            "KEYSIGHT TECHNOLOGIES,DSOX3024T,MY58492329,07.50.2021102830"
        )
        assert profile.matches_idn(
            "KEYSIGHT TECHNOLOGIES,MSOX3104A,MY00000001,02.65.0000000000"
        )
        assert not profile.matches_idn("RIGOL TECHNOLOGIES,DS1054Z,X,1")

    def test_waveform_data_is_ieee_block(self, profile):
        cmd = profile.get_command("waveform_data")
        assert cmd is not None
        assert cmd.streamable
        assert cmd.returns.type == "binary"
        assert cmd.returns.format == "ieee_block"
        assert cmd.returns.is_ieee_block

    def test_waveform_data_binary_config(self, profile):
        binary = profile.get_command("waveform_data").returns.binary
        assert binary is not None
        # WORD + signed -> int16 preferred; never uint16/int8 (doc §2.4).
        assert binary.dtype == "int16"
        assert binary.byte_order == "little"
        assert binary.preamble_command == "waveform_preamble"
        # 10-field CSV: format,type,points,count,xincrement,xorigin,
        # xreference,yincrement,yorigin,yreference
        assert binary.preamble_map.to_index_dict() == {
            "x_increment": 4,
            "x_start": 5,
            "x_reference": 6,
            "y_scale": 7,
            "y_offset": 8,
            "y_reference": 9,
        }

    def test_preamble_command_resolves_to_scpi(self, profile):
        binary = profile.get_command("waveform_data").returns.binary
        scpi = profile.resolve_scpi_ref(binary.preamble_command)
        assert scpi == ":WAVeform:PREamble?"

    def test_source_command_declared_and_resolves(self, profile):
        # Multi-channel frames (doc §3.5): the per-channel source
        # selector named in the binary block must resolve to the
        # waveform_source setter for each channel label.
        binary = profile.get_command("waveform_data").returns.binary
        assert binary.source_command == "waveform_source"
        assert (
            profile.resolve_source_ref(binary.source_command, "CHANnel2")
            == ":WAVeform:SOURce CHANnel2"
        )

    def test_explicit_setup_commands_declared(self, profile):
        # Setup is driven explicitly, never trusted to defaults
        # (doc §2.4): format, signedness, byte order.
        init = profile.settings.init_commands
        assert ":WAVeform:FORMat WORD" in init
        assert ":WAVeform:UNSigned 0" in init
        assert ":WAVeform:BYTeorder LSBFirst" in init

    def test_supporting_setup_commands_exist(self, profile):
        for name in (
            "waveform_source",
            "waveform_format",
            "waveform_points_mode",
            "waveform_points",
            "waveform_byteorder",
            "waveform_unsigned",
        ):
            assert profile.get_command(name) is not None, name

    def test_preamble_command_declares_ten_fields(self, profile):
        cmd = profile.get_command("waveform_preamble")
        assert cmd is not None
        fields = [f["name"] for f in cmd.returns.fields]
        assert fields == [
            "format", "type", "points", "count",
            "xincrement", "xorigin", "xreference",
            "yincrement", "yorigin", "yreference",
        ]

    def test_profile_loads_via_loader(self, tmp_path):
        import shutil

        from galois_edge.profile_loader import ProfileLoader

        # Copy to a temp dir so the loader's pickle cache does not
        # litter the source tree.
        shutil.copy(PROFILE_PATH, tmp_path / PROFILE_PATH.name)
        loader = ProfileLoader(str(tmp_path))
        loader.load_all()
        profile = loader.get_profile("keysight_infiniivision_3000_x-series")
        assert profile is not None
        assert profile.get_command("waveform_data").returns.is_ieee_block
