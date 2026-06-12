"""
VectorData population semantics tests (doc §3.0–§3.5).

The single most dangerous default in the message is y_scale: proto3
zero-defaults mean an unset y_scale arrives as 0 and the cloud computes
y = raw * y_scale + y_offset — every sample collapses to y_offset.
These tests assert no producer path can emit y_scale == 0 (and the
same for x_increment on uniform sweeps).
"""

import struct

import pytest

from galois_edge import edge_pb2
from galois_edge.waveform_assembly import (
    IEEEBlockError,
    WaveformAssemblyConfig,
    assemble_waveform_vector_data,
    build_spectrum_info,
    build_vector_data,
    compose_block_scaling,
    populate_point_vectors,
    vector_data_from_block,
)

F64 = "<{}d"


def pack_f64(*values):
    return struct.pack(F64.format(len(values)), *values)


# ---------------------------------------------------------------------------
# build_vector_data — §3.0/§3.1 uniform waveforms
# ---------------------------------------------------------------------------


class TestBuildVectorData:
    def test_uniform_waveform_defaults_are_explicit(self):
        v = build_vector_data(
            y_data=pack_f64(1.0, 2.0, 3.0),
            x_start=-1e-3,
            x_increment=2e-6,
            x_unit="s",
            y_unit="V",
            x_name="Time",
            channel="CH1",
        )
        assert v.y_dtype == "float64"
        assert v.y_length == 3
        assert v.y_scale == 1.0  # EXPLICIT 1.0, never the proto3 zero
        assert v.y_offset == 0.0
        assert v.x_increment == 2e-6
        assert v.channel == "CH1"

    def test_empty_dtype_means_float64(self):
        v = build_vector_data(y_data=pack_f64(1.0), y_dtype="")
        assert v.y_dtype == "float64"
        assert v.y_length == 1

    def test_raw_counts_with_real_scale(self):
        y = struct.pack("<4h", -32768, -1, 0, 32767)
        v = build_vector_data(
            y_data=y, y_dtype="int16", y_scale=4.0e-3, y_offset=1.25
        )
        assert v.y_length == 4
        assert len(v.y_data) == v.y_length * 2
        assert v.y_scale == pytest.approx(4.0e-3)

    def test_y_scale_zero_rejected(self):
        with pytest.raises(ValueError, match="y_scale must never be 0"):
            build_vector_data(y_data=pack_f64(1.0), y_scale=0.0)

    def test_x_increment_zero_rejected_on_uniform_sweep(self):
        with pytest.raises(ValueError, match="x_increment must never be 0"):
            build_vector_data(y_data=pack_f64(1.0), x_increment=0.0)

    def test_misaligned_y_data_rejected(self):
        with pytest.raises(ValueError, match="not a multiple"):
            build_vector_data(y_data=b"\x00" * 7, y_dtype="int16")

    def test_non_wire_dtype_rejected(self):
        for bad in ("uint16", "int8", "int64", "bogus"):
            with pytest.raises(ValueError, match="not a wire dtype"):
                build_vector_data(y_data=b"\x00\x00", y_dtype=bad)

    def test_length_matches_dtype_size_invariant(self):
        # len(y_data) == y_length * sizeof(dtype) — the cloud validates
        # this and raises a typed decode error on mismatch.
        for dtype, size, sample in (
            ("float64", 8, b"\x00" * 8),
            ("float32", 4, b"\x00" * 4),
            ("int32", 4, b"\x00" * 4),
            ("int16", 2, b"\x00" * 2),
            ("uint8", 1, b"\x00"),
        ):
            v = build_vector_data(y_data=sample * 5, y_dtype=dtype)
            assert len(v.y_data) == v.y_length * size


# ---------------------------------------------------------------------------
# build_vector_data — §3.2 non-uniform x
# ---------------------------------------------------------------------------


class TestNonUniformX:
    def test_x_data_same_count_as_y(self):
        v = build_vector_data(
            y_data=pack_f64(1.0, 2.0, 3.0),
            x_data=pack_f64(1e3, 1e4, 1e6),
            x_unit="Hz",
        )
        assert v.x_data == pack_f64(1e3, 1e4, 1e6)
        assert v.x_dtype == ""  # "" = float64, the recommended default
        # x_start/x_increment are ignored by consumers — left at 0.
        assert v.x_start == 0.0
        assert v.x_increment == 0.0

    def test_x_data_count_mismatch_rejected(self):
        with pytest.raises(ValueError, match="x_data carries 2 samples"):
            build_vector_data(
                y_data=pack_f64(1.0, 2.0, 3.0),
                x_data=pack_f64(1e3, 1e4),
            )

    def test_explicit_x_dtype(self):
        v = build_vector_data(
            y_data=pack_f64(1.0, 2.0),
            x_data=struct.pack("<2f", 1.0, 2.0),
            x_dtype="float32",
        )
        assert v.x_dtype == "float32"

    def test_no_x_increment_guard_when_x_data_present(self):
        # x_increment == 0 is fine when x_data carries the axis.
        v = build_vector_data(
            y_data=pack_f64(1.0),
            x_data=pack_f64(5.0),
            x_increment=0.0,
        )
        assert v.y_length == 1


# ---------------------------------------------------------------------------
# build_vector_data — §3.3 pairs
# ---------------------------------------------------------------------------


class TestPairedVectors:
    def test_iq_pair(self):
        i = pack_f64(1.0, 2.0)
        q = pack_f64(0.5, -0.5)
        v = build_vector_data(y_data=i, y2_data=q, pair_kind="iq")
        assert v.pair_kind == "iq"
        assert v.y2_data == q

    def test_magphase_pair_with_unit(self):
        v = build_vector_data(
            y_data=pack_f64(1.0),
            y2_data=pack_f64(90.0),
            pair_kind="magphase",
            y2_unit="deg",
        )
        assert v.y2_unit == "deg"

    def test_y2_without_pair_kind_rejected(self):
        with pytest.raises(ValueError, match="requires a non-empty pair_kind"):
            build_vector_data(y_data=pack_f64(1.0), y2_data=pack_f64(2.0))

    def test_pair_kind_without_y2_rejected(self):
        with pytest.raises(ValueError, match="requires y2_data"):
            build_vector_data(y_data=pack_f64(1.0), pair_kind="xy")

    def test_unknown_pair_kind_rejected(self):
        with pytest.raises(ValueError, match="pair_kind"):
            build_vector_data(
                y_data=pack_f64(1.0), y2_data=pack_f64(2.0), pair_kind="polar"
            )

    def test_length_mismatch_rejected(self):
        with pytest.raises(ValueError, match="y2_data length"):
            build_vector_data(
                y_data=pack_f64(1.0, 2.0),
                y2_data=pack_f64(1.0),
                pair_kind="iq",
            )


# ---------------------------------------------------------------------------
# build_spectrum_info — §1.1 / §3.4
# ---------------------------------------------------------------------------


class TestSpectrumInfo:
    def test_basic_spectrum(self):
        info = build_spectrum_info(
            amplitude="dbm", scale="log", rbw_hz=100.0, window="hann", averages=10
        )
        assert info.amplitude == "dbm"
        assert info.scale == "log"
        assert info.rbw_hz == 100.0
        assert info.averages == 10

    def test_ref_level_explicit_presence(self):
        # 0 dBm is a VALID ref level — explicit presence, no zero sentinel.
        with_ref = build_spectrum_info(amplitude="dbm", scale="log", ref_level=0.0)
        without_ref = build_spectrum_info(amplitude="dbm", scale="log")
        assert with_ref.HasField("ref_level")
        assert with_ref.ref_level == 0.0
        assert not without_ref.HasField("ref_level")

    def test_invalid_amplitude_rejected(self):
        with pytest.raises(ValueError, match="amplitude"):
            build_spectrum_info(amplitude="dBm", scale="log")  # case matters

    def test_invalid_scale_rejected(self):
        with pytest.raises(ValueError, match="scale"):
            build_spectrum_info(amplitude="dbm", scale="db")

    def test_spectrum_attached_to_vector(self):
        info = build_spectrum_info(amplitude="vrms", scale="linear")
        v = build_vector_data(
            y_data=pack_f64(1.0), x_unit="Hz", spectrum=info
        )
        assert v.HasField("spectrum")
        assert v.spectrum.amplitude == "vrms"
        # x_unit='Hz' stays set — legacy fallback signal for old clouds.
        assert v.x_unit == "Hz"


# ---------------------------------------------------------------------------
# populate_point_vectors — §3.5 multi-channel + back-compat
# ---------------------------------------------------------------------------


class TestMultiChannelFrames:
    def _vec(self, channel, value):
        return build_vector_data(y_data=pack_f64(value), channel=channel)

    def test_multi_channel_fills_vectors_and_field8(self):
        point = edge_pb2.MeasurementDataPoint(stream_id="s1")
        ch1 = self._vec("CH1", 1.0)
        ch2 = self._vec("CH2", 2.0)

        populate_point_vectors(point, [ch1, ch2])

        assert len(point.vectors) == 2
        assert point.vectors[0].channel == "CH1"
        assert point.vectors[1].channel == "CH2"
        # Back-compat producer rule: field 8 carries the FIRST channel.
        assert point.vector_data.channel == "CH1"
        assert point.vector_data.y_data == ch1.y_data

    def test_single_channel_uses_field8_only(self):
        point = edge_pb2.MeasurementDataPoint(stream_id="s1")
        populate_point_vectors(point, [self._vec("CH1", 1.0)])
        assert point.vector_data.channel == "CH1"
        assert len(point.vectors) == 0

    def test_empty_vectors_rejected(self):
        point = edge_pb2.MeasurementDataPoint(stream_id="s1")
        with pytest.raises(ValueError, match="at least one vector"):
            populate_point_vectors(point, [])


# ---------------------------------------------------------------------------
# compose_block_scaling — §2.4 reference-point math
# ---------------------------------------------------------------------------


class TestComposeBlockScaling:
    def test_keysight_reference_composition(self):
        # Known values: xinc=2µs, xorig=-1ms, xref=100,
        # yinc=4mV, yorig=1.25V, yref=128.
        scaling = compose_block_scaling(
            {
                "x_increment": 2.0e-6,
                "x_start": -1.0e-3,
                "x_reference": 100,
                "y_scale": 4.0e-3,
                "y_offset": 1.25,
                "y_reference": 128,
            }
        )
        # x_start = xorigin - xreference*xincrement
        assert scaling["x_start"] == pytest.approx(-1.0e-3 - 100 * 2.0e-6)
        assert scaling["x_increment"] == pytest.approx(2.0e-6)
        # y_scale = yincrement; y_offset = yorigin - yreference*yincrement
        assert scaling["y_scale"] == pytest.approx(4.0e-3)
        assert scaling["y_offset"] == pytest.approx(1.25 - 128 * 4.0e-3)

    def test_volts_roundtrip_against_keysight_formula(self):
        # volts(i) = (raw(i) - yref)*yinc + yorig must equal
        # raw(i)*y_scale + y_offset for the composed pair.
        yinc, yorig, yref = 4.0e-3, 1.25, 128.0
        scaling = compose_block_scaling(
            {"y_scale": yinc, "y_offset": yorig, "y_reference": yref}
        )
        for raw in (0, 1, 127, 128, 255):
            keysight = (raw - yref) * yinc + yorig
            wire = raw * scaling["y_scale"] + scaling["y_offset"]
            assert wire == pytest.approx(keysight)

    def test_unmapped_fields_get_explicit_defaults(self):
        scaling = compose_block_scaling({})
        assert scaling == {
            "x_start": 0.0,
            "x_increment": 1.0,
            "y_scale": 1.0,
            "y_offset": 0.0,
        }

    def test_zero_y_scale_rejected(self):
        with pytest.raises(IEEEBlockError, match="y_scale == 0"):
            compose_block_scaling({"y_scale": 0.0})

    def test_zero_x_increment_rejected(self):
        with pytest.raises(IEEEBlockError, match="x_increment == 0"):
            compose_block_scaling({"x_increment": 0.0})


# ---------------------------------------------------------------------------
# assemble_waveform_vector_data — legacy waveform_assembly pipeline
# ---------------------------------------------------------------------------

DSOX_PREAMBLE = (
    "+0,+0,+4,+1,+2.000000E-06,-1.000000E-03,+100,"
    "+4.000000E-03,+1.250000E+00,+128"
)


class TestAssembleWaveformVectorData:
    def test_scaling_and_composition(self):
        raw = b"#14" + bytes([0, 128, 255, 1]) + b"\n"
        result = assemble_waveform_vector_data(
            DSOX_PREAMBLE, raw, WaveformAssemblyConfig(data_format="byte")
        )
        assert result["y_dtype"] == "float64"
        assert result["y_length"] == 4
        # Pre-scaled float path: y_scale EXPLICITLY 1.0 (doc §3.0).
        assert result["y_scale"] == 1.0
        assert result["y_offset"] == 0.0
        # x_start composed with the reference point.
        assert result["x_start"] == pytest.approx(-1.0e-3 - 100 * 2.0e-6)
        volts = struct.unpack("<4d", result["y_data"])
        assert volts[0] == pytest.approx((0 - 128) * 4.0e-3 + 1.25)
        assert volts[1] == pytest.approx(1.25)
        assert volts[2] == pytest.approx((255 - 128) * 4.0e-3 + 1.25)

    def test_points_mismatch_rejected(self):
        raw = b"#13" + bytes([0, 128, 255]) + b"\n"  # 3 samples, preamble says 4
        with pytest.raises(IEEEBlockError, match="declares 4 points"):
            assemble_waveform_vector_data(
                DSOX_PREAMBLE, raw, WaveformAssemblyConfig(data_format="byte")
            )

    def test_malformed_block_raises_not_partial(self):
        truncated = b"#14\x00\x01"
        with pytest.raises(IEEEBlockError, match="length mismatch"):
            assemble_waveform_vector_data(
                DSOX_PREAMBLE, truncated, WaveformAssemblyConfig(data_format="byte")
            )

    def test_zero_xincrement_rejected(self):
        pre = "+0,+0,+1,+1,+0.0,+0.0,+0,+1.0,+0.0,+0"
        with pytest.raises(IEEEBlockError, match="xincrement is 0"):
            assemble_waveform_vector_data(
                pre, b"#11\x05", WaveformAssemblyConfig(data_format="byte")
            )


# ---------------------------------------------------------------------------
# vector_data_from_block — handler block dict -> VectorData
# ---------------------------------------------------------------------------


class TestVectorDataFromBlock:
    def test_round_trip(self):
        block = {
            "y_data": struct.pack("<3h", -100, 0, 100),
            "y_dtype": "int16",
            "y_length": 3,
            "x_start": -1.0e-3,
            "x_increment": 2.0e-6,
            "y_scale": 4.0e-3,
            "y_offset": 0.738,
        }
        v = vector_data_from_block(block, channel="CH1")
        assert v.y_length == 3
        assert v.y_scale == pytest.approx(4.0e-3)
        assert v.channel == "CH1"
        assert v.x_unit == "s"
        assert v.y_unit == "V"
        assert v.x_name == "Time"

    def test_length_mismatch_rejected(self):
        block = {
            "y_data": struct.pack("<3h", 1, 2, 3),
            "y_dtype": "int16",
            "y_length": 99,
        }
        with pytest.raises(ValueError, match="declares 99 samples"):
            vector_data_from_block(block)

    def test_zero_scale_block_rejected(self):
        block = {
            "y_data": b"\x00\x00",
            "y_dtype": "int16",
            "y_length": 1,
            "y_scale": 0.0,
        }
        with pytest.raises(ValueError, match="y_scale must never be 0"):
            vector_data_from_block(block)
