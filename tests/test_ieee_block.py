"""
IEEE 488.2 definite-length block parser tests (doc §2.2).

Golden byte fixtures live in tests/fixtures/: captured-shape DSOX3000
':WAVeform:PREamble?' and ':WAVeform:DATA?' responses, including a
payload that contains 0x0A bytes (which the text query() path can
never survive), a truncated payload, a bad '#' header, and a
declared != received length block.
"""

from pathlib import Path

import pytest

from galois_edge.waveform_assembly import (
    IEEEBlockError,
    decode_block_samples,
    decode_ieee_block,
)

FIXTURES = Path(__file__).parent / "fixtures"

GOLDEN_PAYLOAD = bytes(i % 256 for i in range(1000))


# ---------------------------------------------------------------------------
# decode_ieee_block — well-formed blocks
# ---------------------------------------------------------------------------


class TestDecodeIEEEBlock:
    def test_golden_dsox_block_with_0x0a_payload(self):
        raw = (FIXTURES / "dsox3000_wav_data.bin").read_bytes()
        payload = decode_ieee_block(raw)
        assert payload == GOLDEN_PAYLOAD
        assert b"\x0a" in payload  # the byte that breaks the text path

    def test_minimal_block(self):
        assert decode_ieee_block(b"#15hello") == b"hello"

    def test_multi_digit_length_field(self):
        payload = b"\x00" * 12345
        assert decode_ieee_block(b"#512345" + payload) == payload

    def test_trailing_lf_tolerated(self):
        assert decode_ieee_block(b"#13abc\n") == b"abc"

    def test_trailing_crlf_tolerated(self):
        assert decode_ieee_block(b"#13abc\r\n") == b"abc"

    def test_leading_whitespace_tolerated(self):
        assert decode_ieee_block(b" \n#13abc") == b"abc"

    def test_empty_payload_block(self):
        assert decode_ieee_block(b"#10") == b""

    def test_byte_transparency_nul_hash_lf(self):
        # Payload containing 0x00, '#', and 0x0A must pass untouched.
        payload = b"\x00#\x0a\xff#0\x0a\x00"
        raw = b"#18" + payload + b"\n"
        assert decode_ieee_block(raw) == payload

    def test_payload_of_only_newlines(self):
        # 0x0A payload bytes must not be confused with the terminator.
        assert decode_ieee_block(b"#14\n\n\n\n") == b"\n\n\n\n"


# ---------------------------------------------------------------------------
# decode_ieee_block — malformed blocks (error result, never partial data)
# ---------------------------------------------------------------------------


class TestDecodeIEEEBlockErrors:
    def test_empty_response(self):
        with pytest.raises(IEEEBlockError, match="empty response"):
            decode_ieee_block(b"")

    def test_bad_header_fixture(self):
        raw = (FIXTURES / "dsox3000_wav_data_bad_header.bin").read_bytes()
        with pytest.raises(IEEEBlockError, match="expected '#'"):
            decode_ieee_block(raw)

    def test_missing_hash_header(self):
        with pytest.raises(IEEEBlockError, match="expected '#'"):
            decode_ieee_block(b"1.234E+00\n")

    def test_header_only(self):
        with pytest.raises(IEEEBlockError, match="missing digit count"):
            decode_ieee_block(b"#")

    def test_non_digit_count(self):
        with pytest.raises(IEEEBlockError, match="digit count"):
            decode_ieee_block(b"#Xabc")

    def test_indefinite_length_rejected(self):
        # '#0' blocks are explicitly unsupported (doc §2.2 rule 1).
        with pytest.raises(IEEEBlockError, match="indefinite-length"):
            decode_ieee_block(b"#0abc\n")

    def test_truncated_length_field(self):
        with pytest.raises(IEEEBlockError, match="incomplete length field"):
            decode_ieee_block(b"#412")

    def test_non_numeric_length_field(self):
        with pytest.raises(IEEEBlockError, match="non-numeric"):
            decode_ieee_block(b"#4a000xxxx")

    def test_truncated_payload_fixture(self):
        # Short read: declared 1000 bytes, received 900 — error, never
        # a partial vector emitted as good data.
        raw = (FIXTURES / "dsox3000_wav_data_truncated.bin").read_bytes()
        with pytest.raises(IEEEBlockError, match="declared 1000 bytes, received 900"):
            decode_ieee_block(raw)

    def test_long_read_fixture(self):
        # Long read: declared 8 bytes, received far more.
        raw = (FIXTURES / "dsox3000_wav_data_long.bin").read_bytes()
        with pytest.raises(IEEEBlockError, match="trailing bytes"):
            decode_ieee_block(raw)

    def test_trailing_garbage_rejected(self):
        with pytest.raises(IEEEBlockError, match="trailing bytes"):
            decode_ieee_block(b"#13abcGARBAGE")

    def test_is_a_value_error(self):
        # Poll loops catch ValueError — IEEEBlockError must be one.
        with pytest.raises(ValueError):
            decode_ieee_block(b"#0")


# ---------------------------------------------------------------------------
# decode_block_samples — wire normalisation (little-endian, wire dtypes)
# ---------------------------------------------------------------------------


class TestDecodeBlockSamples:
    def test_uint8_passthrough(self):
        data, count, dtype = decode_block_samples(GOLDEN_PAYLOAD, "uint8", "little")
        assert data == GOLDEN_PAYLOAD
        assert count == 1000
        assert dtype == "uint8"

    def test_int16_little_passthrough(self):
        payload = (1000).to_bytes(2, "little", signed=True) + (-2000).to_bytes(
            2, "little", signed=True
        )
        data, count, dtype = decode_block_samples(payload, "int16", "little")
        assert data == payload
        assert count == 2
        assert dtype == "int16"

    def test_int16_big_endian_swapped_to_little(self):
        payload = (1000).to_bytes(2, "big", signed=True) + (-2000).to_bytes(
            2, "big", signed=True
        )
        data, count, dtype = decode_block_samples(payload, "int16", "big")
        assert count == 2
        assert dtype == "int16"
        assert int.from_bytes(data[0:2], "little", signed=True) == 1000
        assert int.from_bytes(data[2:4], "little", signed=True) == -2000

    def test_float64_big_endian_swapped(self):
        import struct

        payload = struct.pack(">2d", 1.5, -2.25)
        data, count, dtype = decode_block_samples(payload, "float64", "big")
        assert count == 2
        assert struct.unpack("<2d", data) == (1.5, -2.25)

    def test_int8_widened_to_int16(self):
        # int8 is not a wire dtype (doc §2.4) — raw counts preserved,
        # but emitted as little-endian int16.
        payload = bytes([0x7F, 0x80, 0x00])  # 127, -128, 0
        data, count, dtype = decode_block_samples(payload, "int8", "little")
        assert dtype == "int16"
        assert count == 3
        assert int.from_bytes(data[0:2], "little", signed=True) == 127
        assert int.from_bytes(data[2:4], "little", signed=True) == -128
        assert int.from_bytes(data[4:6], "little", signed=True) == 0

    def test_float32_size_validation(self):
        with pytest.raises(IEEEBlockError, match="not a multiple"):
            decode_block_samples(b"\x00" * 6, "float32", "little")

    def test_unknown_dtype(self):
        with pytest.raises(IEEEBlockError, match="unsupported binary dtype"):
            decode_block_samples(b"\x00\x00", "int64", "little")

    def test_uint16_rejected(self):
        # uint16 must never be emitted (doc §2.4) — not even accepted.
        with pytest.raises(IEEEBlockError, match="unsupported binary dtype"):
            decode_block_samples(b"\x00\x00", "uint16", "little")

    def test_unknown_byte_order(self):
        with pytest.raises(IEEEBlockError, match="unsupported byte order"):
            decode_block_samples(b"\x00\x00", "int16", "native")


# ---------------------------------------------------------------------------
# CommandHandler.execute_binary_block_query (fake transport)
# ---------------------------------------------------------------------------

from galois_edge.command_handler import CommandHandler  # noqa: E402
from galois_edge.profile_schema import BinaryConfig, PreambleMap  # noqa: E402


class FakeInstrumentManager:
    """Minimal InstrumentManager double for the binary block path."""

    def __init__(self, raw=b"", preamble="", connected=True, raw_error=None):
        self.raw = raw
        self.preamble = preamble
        self.connected = connected
        self.raw_error = raw_error
        self.raw_commands = []
        self.text_commands = []

    def is_connected(self, instrument_id):
        return self.connected

    def connect(self, instrument_id, timeout=5000):
        return instrument_id if self.connected else None

    def query(self, instrument_id, command):
        self.text_commands.append(command)
        return self.preamble

    def query_raw(self, instrument_id, command):
        self.raw_commands.append(command)
        if self.raw_error is not None:
            raise self.raw_error
        return self.raw

    def write(self, instrument_id, command):
        pass


DSOX_BINARY_CONFIG = BinaryConfig(
    dtype="uint8",
    byte_order="little",
    preamble_command=":WAVeform:PREamble?",
    preamble_map=PreambleMap(
        x_increment=4, x_start=5, x_reference=6, y_scale=7,
        y_offset=8, y_reference=9,
    ),
)


class TestExecuteBinaryBlockQuery:
    def make_handler(self, fake):
        return CommandHandler(instrument_manager=fake)

    def test_golden_dsox_waveform(self):
        fake = FakeInstrumentManager(
            raw=(FIXTURES / "dsox3000_wav_data.bin").read_bytes(),
            preamble=(FIXTURES / "dsox3000_wav_pre.txt").read_text().strip(),
        )
        handler = self.make_handler(fake)

        result = handler.execute_binary_block_query(
            ":WAVeform:DATA?", "TCPIP0::1.2.3.4::INSTR", DSOX_BINARY_CONFIG
        )

        assert result["success"] is True
        assert result["error"] == ""
        block = result["block"]
        assert block["y_data"] == GOLDEN_PAYLOAD
        assert block["y_dtype"] == "uint8"
        assert block["y_length"] == 1000
        assert block["x_increment"] == pytest.approx(2.0e-6)
        # x_start = xorigin - xreference*xincrement (xref = 0 here)
        assert block["x_start"] == pytest.approx(-1.0e-3)
        assert block["y_scale"] == pytest.approx(4.0e-3)
        # y_offset = yorigin - yreference*yincrement = 1.25 - 128*4e-3
        assert block["y_offset"] == pytest.approx(0.738)
        # Producer invariant: y_scale never 0.
        assert block["y_scale"] != 0.0
        # Preamble went through the text path; data through the raw path.
        assert fake.text_commands == [":WAVeform:PREamble?"]
        assert fake.raw_commands == [":WAVeform:DATA?"]

    def test_volts_match_keysight_formula(self):
        fake = FakeInstrumentManager(
            raw=(FIXTURES / "dsox3000_wav_data.bin").read_bytes(),
            preamble=(FIXTURES / "dsox3000_wav_pre.txt").read_text().strip(),
        )
        handler = self.make_handler(fake)
        block = handler.execute_binary_block_query(
            ":WAVeform:DATA?", "X", DSOX_BINARY_CONFIG
        )["block"]

        # volts(i) = (raw(i) - yref)*yinc + yorig
        yinc, yorig, yref = 4.0e-3, 1.25, 128
        for i in (0, 1, 127, 500, 999):
            raw_count = GOLDEN_PAYLOAD[i]
            wire = raw_count * block["y_scale"] + block["y_offset"]
            assert wire == pytest.approx((raw_count - yref) * yinc + yorig)

    def test_preamble_scpi_override(self):
        # When the profile names a sibling command, the caller resolves
        # it and passes the SCPI string explicitly.
        fake = FakeInstrumentManager(
            raw=b"#13abc\n",
            preamble="0,0,3,1,1e-3,0,0,1.0,0,0",
        )
        handler = self.make_handler(fake)

        result = handler.execute_binary_block_query(
            ":WAVeform:DATA?",
            "X",
            DSOX_BINARY_CONFIG,
            preamble_scpi=":WAVeform:PREamble? CHANnel2",
        )

        assert result["success"] is True
        assert fake.text_commands == [":WAVeform:PREamble? CHANnel2"]

    def test_no_preamble_uses_explicit_default_scaling(self):
        fake = FakeInstrumentManager(raw=b"#14\x01\x02\x03\x04\n")
        handler = self.make_handler(fake)
        cfg = BinaryConfig(dtype="uint8", byte_order="little")

        result = handler.execute_binary_block_query("CURV?", "Y", cfg)

        assert result["success"] is True
        block = result["block"]
        assert block["y_data"] == b"\x01\x02\x03\x04"
        assert block["y_length"] == 4
        assert block["x_start"] == 0.0
        assert block["x_increment"] == 1.0  # explicit, never 0
        assert block["y_scale"] == 1.0     # explicit, never 0
        assert block["y_offset"] == 0.0
        assert fake.text_commands == []

    def test_int16_big_endian_normalised_to_wire(self):
        payload = (1000).to_bytes(2, "big", signed=True) + (-2).to_bytes(
            2, "big", signed=True
        )
        fake = FakeInstrumentManager(raw=b"#14" + payload + b"\n")
        handler = self.make_handler(fake)
        cfg = BinaryConfig(dtype="int16", byte_order="big")

        block = handler.execute_binary_block_query("CURV?", "Y", cfg)["block"]

        assert block["y_dtype"] == "int16"
        assert block["y_length"] == 2
        assert int.from_bytes(block["y_data"][0:2], "little", signed=True) == 1000
        assert int.from_bytes(block["y_data"][2:4], "little", signed=True) == -2

    def test_truncated_block_is_error_not_crash(self):
        fake = FakeInstrumentManager(
            raw=(FIXTURES / "dsox3000_wav_data_truncated.bin").read_bytes(),
        )
        handler = self.make_handler(fake)
        cfg = BinaryConfig(dtype="uint8", byte_order="little")

        result = handler.execute_binary_block_query(":WAVeform:DATA?", "X", cfg)

        assert result["success"] is False
        assert "Malformed binary block" in result["error"]
        assert "declared 1000 bytes, received 900" in result["error"]
        assert "block" not in result

    def test_bad_header_is_error(self):
        fake = FakeInstrumentManager(
            raw=(FIXTURES / "dsox3000_wav_data_bad_header.bin").read_bytes()
        )
        handler = self.make_handler(fake)
        cfg = BinaryConfig(dtype="uint8", byte_order="little")

        result = handler.execute_binary_block_query(":WAVeform:DATA?", "X", cfg)

        assert result["success"] is False
        assert "Malformed binary block" in result["error"]
        assert "block" not in result

    def test_declared_ne_received_is_error(self):
        fake = FakeInstrumentManager(
            raw=(FIXTURES / "dsox3000_wav_data_long.bin").read_bytes()
        )
        handler = self.make_handler(fake)
        cfg = BinaryConfig(dtype="uint8", byte_order="little")

        result = handler.execute_binary_block_query(":WAVeform:DATA?", "X", cfg)

        assert result["success"] is False
        assert "Malformed binary block" in result["error"]

    def test_zero_y_scale_preamble_is_error(self):
        # A preamble carrying yincrement == 0 must produce an error
        # result, never a vector with y_scale == 0.
        fake = FakeInstrumentManager(
            raw=b"#13abc\n",
            preamble="0,0,3,1,1e-3,0,0,0.0,1.25,128",
        )
        handler = self.make_handler(fake)

        result = handler.execute_binary_block_query(
            ":WAVeform:DATA?", "X", DSOX_BINARY_CONFIG
        )

        assert result["success"] is False
        assert "y_scale == 0" in result["error"]
        assert "block" not in result

    def test_preamble_index_out_of_range(self):
        fake = FakeInstrumentManager(raw=b"#13abc\n", preamble="1,2,3")
        handler = self.make_handler(fake)

        result = handler.execute_binary_block_query(
            ":WAVeform:DATA?", "X", DSOX_BINARY_CONFIG
        )

        assert result["success"] is False
        assert "out of range" in result["error"]

    def test_non_numeric_preamble_field(self):
        fake = FakeInstrumentManager(
            raw=b"#13abc\n",
            preamble="0,0,1000,1,abc,0,0,1,0,128",
        )
        handler = self.make_handler(fake)

        result = handler.execute_binary_block_query(
            ":WAVeform:DATA?", "X", DSOX_BINARY_CONFIG
        )

        assert result["success"] is False
        assert "non-numeric preamble field" in result["error"]

    def test_unsupported_transport_is_error(self):
        fake = FakeInstrumentManager(
            raw_error=ValueError(
                "Binary (raw) reads are not supported on this transport: GPIB0::7"
            )
        )
        handler = self.make_handler(fake)
        cfg = BinaryConfig(dtype="uint8", byte_order="little")

        result = handler.execute_binary_block_query("CURV?", "GPIB0::7", cfg)

        assert result["success"] is False
        assert "not supported on this transport" in result["error"]

    def test_cannot_connect(self):
        fake = FakeInstrumentManager(connected=False)
        handler = self.make_handler(fake)
        cfg = BinaryConfig(dtype="uint8", byte_order="little")

        result = handler.execute_binary_block_query("CURV?", "X", cfg)

        assert result["success"] is False
        assert "Cannot connect" in result["error"]

    def test_block_feeds_vector_data_builder(self):
        # End-to-end: handler block dict -> validated VectorData proto.
        from galois_edge.waveform_assembly import vector_data_from_block

        fake = FakeInstrumentManager(
            raw=(FIXTURES / "dsox3000_wav_data.bin").read_bytes(),
            preamble=(FIXTURES / "dsox3000_wav_pre.txt").read_text().strip(),
        )
        handler = self.make_handler(fake)
        block = handler.execute_binary_block_query(
            ":WAVeform:DATA?", "X", DSOX_BINARY_CONFIG
        )["block"]

        vector = vector_data_from_block(block, channel="CH1")
        assert vector.y_length == 1000
        assert len(vector.y_data) == vector.y_length  # uint8: 1 byte/sample
        assert vector.y_scale != 0.0
        assert vector.x_increment != 0.0
        assert vector.channel == "CH1"
