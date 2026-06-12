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
