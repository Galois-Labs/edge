"""Unit tests for the generic serial driver.

Covers:
- checksum implementations (xor8, sum8, crc16-modbus, crc16-ccitt)
- framers (line, stx_etx, length_prefix, fixed, raw) — encode + decode roundtrip
- parameter substitution (string and request_bytes forms)
- response parser modes
- end-to-end execute_command via a mocked SerialTransport
"""

from __future__ import annotations

import threading
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from galois_edge.drivers.serial_driver import (
    GenericSerialDriver,
    _coerce_byte_token,
    _parse_response,
    _render_request,
    _substitute_string,
)
from galois_edge.drivers.point import Point
from galois_edge.drivers.serial_transport import (
    FrameSpec,
    SerialBusManager,
    SerialSettings,
    SerialTransport,
    _coerce_terminator,
    build_checksum,
    build_framer,
    parse_frame_spec,
)


# ---------------------------------------------------------------------------
# Fake reader for framer tests — exposes the ByteReader interface
# ---------------------------------------------------------------------------

class FakeReader:
    """Implements the ByteReader protocol against a bytes buffer."""

    def __init__(self, data: bytes) -> None:
        self.buf = bytearray(data)

    def read(self, size: int) -> bytes:
        chunk = bytes(self.buf[:size])
        del self.buf[:size]
        return chunk

    def read_until(self, terminator: bytes, size: int | None = None) -> bytes:
        idx = self.buf.find(terminator)
        if idx == -1:
            chunk = bytes(self.buf)
            self.buf.clear()
            return chunk
        end = idx + len(terminator)
        if size is not None:
            end = min(end, size)
        chunk = bytes(self.buf[:end])
        del self.buf[:end]
        return chunk


# ---------------------------------------------------------------------------
# Checksums
# ---------------------------------------------------------------------------

class TestChecksums:
    def test_xor8(self):
        cs, _ = build_checksum({"type": "xor8"})
        assert cs.compute(b"\x01\x02\x03") == bytes([0x00])
        assert cs.compute(b"hello") == bytes([0x68 ^ 0x65 ^ 0x6C ^ 0x6C ^ 0x6F])

    def test_sum8(self):
        cs, _ = build_checksum({"type": "sum8"})
        assert cs.compute(b"\x10\x20\x30") == bytes([0x60])
        # wraps at 256
        assert cs.compute(b"\xFF\x02") == bytes([0x01])

    def test_crc16_modbus(self):
        # Standard reference vector: CRC-16/MODBUS over b"123456789" == 0x4B37,
        # serialized little-endian on wire as 0x37 0x4B.
        cs, _ = build_checksum({"type": "crc16_modbus"})
        assert cs.compute(b"123456789") == bytes([0x37, 0x4B])

    def test_crc16_ccitt(self):
        # CRC-16/CCITT-FALSE over b"123456789" == 0x29B1, big-endian on wire.
        cs, _ = build_checksum({"type": "crc16_ccitt"})
        assert cs.compute(b"123456789") == bytes([0x29, 0xB1])

    def test_none_returns_disabled(self):
        cs, cfg = build_checksum(None)
        assert cs is None
        assert cfg["append"] is False
        assert cfg["verify"] is False

    def test_unknown_type_raises(self):
        with pytest.raises(ValueError):
            build_checksum({"type": "fletcher42"})


# ---------------------------------------------------------------------------
# Framers
# ---------------------------------------------------------------------------

class TestLineFramer:
    def test_wrap_appends_terminator(self):
        f = build_framer({"framing": "line", "terminator": "\r\n"})
        assert f.frame(b"PING") == b"PING\r\n"

    def test_unframe_strips_terminator(self):
        f = build_framer({"framing": "line", "terminator": "\r\n"})
        reader = FakeReader(b"PONG\r\nleftover")
        assert f.unframe(reader) == b"PONG"

    def test_unframe_timeout_when_no_terminator(self):
        f = build_framer({"framing": "line", "terminator": "\r\n"})
        reader = FakeReader(b"PONG")  # no terminator
        with pytest.raises(TimeoutError):
            f.unframe(reader)

    def test_yaml_escape_terminator(self):
        # YAML strings might come in as literal "\r\n" — should decode.
        spec = parse_frame_spec({"framing": "line", "terminator": "\\r\\n"})
        assert spec.terminator == b"\r\n"


class TestStxEtxFramer:
    def test_wrap(self):
        f = build_framer({"framing": "stx_etx", "stx": 0x02, "etx": 0x03})
        assert f.frame(b"DATA") == b"\x02DATA\x03"

    def test_unframe_skips_noise_before_stx(self):
        f = build_framer({"framing": "stx_etx", "stx": 0x02, "etx": 0x03})
        reader = FakeReader(b"\xFF\x00\x02HELLO\x03")
        assert f.unframe(reader) == b"HELLO"


class TestLengthPrefixFramer:
    def test_wrap_prepends_length(self):
        f = build_framer({
            "framing": "length_prefix",
            "length_field": {"offset": 0, "size": 2, "endian": "big"},
        })
        assert f.frame(b"HELLO") == b"\x00\x05HELLO"

    def test_unframe(self):
        f = build_framer({
            "framing": "length_prefix",
            "length_field": {"offset": 0, "size": 2, "endian": "big"},
        })
        reader = FakeReader(b"\x00\x05WORLD")
        assert f.unframe(reader) == b"WORLD"

    def test_unframe_with_header_offset(self):
        f = build_framer({
            "framing": "length_prefix",
            "length_field": {"offset": 1, "size": 1, "endian": "big"},
        })
        # 1-byte sync + 1-byte length + body
        reader = FakeReader(b"\x55\x03ABC")
        # Framer keeps the pre-length header (the SYNC byte) + body
        assert f.unframe(reader) == b"\x55ABC"

    def test_unframe_truncated_body_raises(self):
        f = build_framer({
            "framing": "length_prefix",
            "length_field": {"offset": 0, "size": 1, "endian": "big"},
        })
        reader = FakeReader(b"\x05AB")  # claims 5 bytes, only 2 available
        with pytest.raises(TimeoutError):
            f.unframe(reader)

    def test_implausible_length_raises(self):
        f = build_framer({
            "framing": "length_prefix",
            "length_field": {"offset": 0, "size": 2, "endian": "big"},
            "max_response_bytes": 100,
        })
        reader = FakeReader(b"\xFF\xFFxxx")
        with pytest.raises(ValueError):
            f.unframe(reader)


class TestFixedFramer:
    def test_wrap_pads_with_zeros(self):
        f = build_framer({"framing": "fixed", "length": 8})
        assert f.frame(b"AB") == b"AB\x00\x00\x00\x00\x00\x00"

    def test_unframe_reads_exact(self):
        f = build_framer({"framing": "fixed", "length": 4})
        reader = FakeReader(b"WXYZmore")
        assert f.unframe(reader) == b"WXYZ"


class TestRawFramer:
    def test_wrap_passthrough(self):
        f = build_framer({"framing": "raw"})
        assert f.frame(b"XYZ") == b"XYZ"


class TestFramerWithChecksum:
    def test_append_and_verify_xor8(self):
        f_send = build_framer(
            {"framing": "line", "terminator": "\n"},
            {"type": "xor8", "append": True},
        )
        wire = f_send.frame(b"\x01\x02\x03")
        # payload + xor8(=0x00) + terminator
        assert wire == b"\x01\x02\x03\x00\n"

    def test_verify_mismatch_raises(self):
        f_recv = build_framer(
            {"framing": "line", "terminator": "\n"},
            {"type": "xor8", "verify": True},
        )
        # wrong checksum byte (0xFF instead of 0x00)
        reader = FakeReader(b"\x01\x02\x03\xFF\n")
        with pytest.raises(ValueError, match="Checksum mismatch"):
            f_recv.unframe(reader)


# ---------------------------------------------------------------------------
# Parameter substitution
# ---------------------------------------------------------------------------

class TestStringSubstitution:
    def test_basic(self):
        assert _substitute_string("VOLT {value}", {"value": 12.5}) == "VOLT 12.5"

    def test_multiple(self):
        out = _substitute_string("SET {ch} {v}", {"ch": 1, "v": 3.3})
        assert out == "SET 1 3.3"

    def test_format_spec(self):
        out = _substitute_string("VAL {x:0.2f}", {"x": 1.23456})
        assert out == "VAL 1.23"

    def test_missing_param_raises(self):
        with pytest.raises(KeyError):
            _substitute_string("VOLT {missing}", {})


class TestByteCoercion:
    def test_int(self):
        assert _coerce_byte_token(0x42, {}) == b"\x42"

    def test_hex_str(self):
        assert _coerce_byte_token("0x55", {}) == b"\x55"
        assert _coerce_byte_token("0xAABB", {}) == b"\xAA\xBB"

    def test_string_literal(self):
        assert _coerce_byte_token("str:HI", {}) == b"HI"

    def test_placeholder_u8(self):
        assert _coerce_byte_token("{n}", {"n": 7}) == b"\x07"

    def test_placeholder_u16_be(self):
        assert _coerce_byte_token("{n:u16_be}", {"n": 0x1234}) == b"\x12\x34"

    def test_placeholder_u16_le(self):
        assert _coerce_byte_token("{n:u16_le}", {"n": 0x1234}) == b"\x34\x12"

    def test_placeholder_f32_be(self):
        # 1.0 in IEEE 754 single = 0x3F800000
        assert _coerce_byte_token("{x:f32_be}", {"x": 1.0}) == b"\x3F\x80\x00\x00"

    def test_unknown_format_raises(self):
        with pytest.raises(ValueError):
            _coerce_byte_token("{n:bogus}", {"n": 1})


class TestRenderRequest:
    def test_string_form(self):
        cmd = {"request": "VOLT {v}"}
        assert _render_request(cmd, {"v": 5}, "ascii") == b"VOLT 5"

    def test_bytes_form(self):
        cmd = {"request_bytes": ["0x55", "{addr:u16_be}", "{count:u8}"]}
        wire = _render_request(cmd, {"addr": 0x100, "count": 4}, "binary")
        assert wire == b"\x55\x01\x00\x04"

    def test_neither_raises(self):
        with pytest.raises(ValueError):
            _render_request({"type": "query"}, {}, "ascii")


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

class TestParseResponse:
    def test_passthrough_string(self):
        assert _parse_response(b"hello", {"type": "passthrough"}, "ascii") == "hello"

    def test_passthrough_with_cast_float(self):
        assert _parse_response(b"3.14", {"type": "passthrough", "cast": "float"}, "ascii") == 3.14

    def test_regex_capture_group(self):
        v = _parse_response(
            b"VOLT 12.345 V",
            {"type": "regex", "pattern": r"([\d.]+)", "group": 1, "cast": "float"},
            "ascii",
        )
        assert v == 12.345

    def test_regex_no_match_raises(self):
        with pytest.raises(ValueError):
            _parse_response(b"NOPE", {"type": "regex", "pattern": r"\d+"}, "ascii")

    def test_strip_prefix(self):
        v = _parse_response(b"R+12.5", {"type": "strip", "prefix": "R", "cast": "float"}, "ascii")
        assert v == 12.5

    def test_split_index(self):
        v = _parse_response(
            b"1.0,2.0,3.0",
            {"type": "split", "delimiter": ",", "index": 1, "cast": "float"},
            "ascii",
        )
        assert v == 2.0

    def test_bytes_hex(self):
        assert _parse_response(b"\xDE\xAD", {"type": "bytes_hex"}, "binary") == "dead"

    def test_bytes_slice(self):
        v = _parse_response(b"\xAA\x01\x02\x03", {"type": "bytes_slice", "start": 1}, "binary")
        assert v == b"\x01\x02\x03"

    def test_no_parser_returns_decoded(self):
        assert _parse_response(b"raw text", None, "ascii") == "raw text"


# ---------------------------------------------------------------------------
# End-to-end driver test with a mocked transport
# ---------------------------------------------------------------------------

def _fake_transport_for(response_bytes: bytes) -> tuple[SerialTransport, list[bytes]]:
    """Build a SerialTransport whose pyserial side returns ``response_bytes`` and
    captures every write into the returned list.
    """
    written: list[bytes] = []
    fake_ser = MagicMock()
    fake_ser.write.side_effect = lambda data: written.append(bytes(data)) or len(data)
    fake_ser.flush.return_value = None
    fake_ser.reset_input_buffer.return_value = None

    buf = bytearray(response_bytes)

    def _read(size: int) -> bytes:
        chunk = bytes(buf[:size])
        del buf[:size]
        return chunk

    def _read_until(term: bytes, size: int | None = None) -> bytes:
        idx = buf.find(term)
        if idx == -1:
            chunk = bytes(buf)
            buf.clear()
            return chunk
        end = idx + len(term)
        if size is not None:
            end = min(end, size)
        chunk = bytes(buf[:end])
        del buf[:end]
        return chunk

    fake_ser.read.side_effect = _read
    fake_ser.read_until.side_effect = _read_until

    settings = SerialSettings(port="/dev/null-test", baudrate=9600)
    transport = SerialTransport(settings, fake_ser)
    return transport, written


PROFILE_ASCII = {
    "protocol": "serial",
    "identity": {"manufacturer": "TestCo", "model": "T1"},
    "connection": {
        "default_baudrate": 9600,
        "encoding": "ascii",
        "request": {"framing": "line", "terminator": "\r\n"},
        "response": {"framing": "line", "terminator": "\r\n"},
    },
    "commands": {
        "identify": {
            "type": "query",
            "request": "*IDN?",
            "response": {"parser": {"type": "passthrough", "cast": "string"}},
        },
        "set_voltage": {
            "type": "action",
            "request": "VOLT {value}",
            "params": {"value": {"type": "float", "range": [0, 30]}},
        },
        "measure": {
            "type": "query",
            "request": "MEAS?",
            "response": {
                "parser": {"type": "regex", "pattern": r"([\d.]+)", "group": 1, "cast": "float"}
            },
        },
    },
}


class TestDriverEndToEnd:
    def _build(self, response: bytes, profile=PROFILE_ASCII):
        transport, written = _fake_transport_for(response)
        bus_mgr = MagicMock(spec=SerialBusManager)
        bus_mgr.get.return_value = transport
        bus_mgr.parse_uri = SerialBusManager.parse_uri  # static method passthrough
        driver = GenericSerialDriver(
            instrument_id="test1",
            transport_uri="/dev/null-test",
            profile=profile,
            bus_manager=bus_mgr,
        )
        driver.connect()
        return driver, written

    def test_query_roundtrip(self):
        driver, written = self._build(b"TestCo,T1,SN1234,1.0\r\n")
        result = driver.execute_command("identify")
        assert result == "TestCo,T1,SN1234,1.0"
        assert written == [b"*IDN?\r\n"]

    def test_action_returns_ok_and_does_not_read(self):
        driver, written = self._build(b"")
        result = driver.execute_command("set_voltage", {"value": 12.0})
        assert result == {"status": "ok"}
        assert written == [b"VOLT 12.0\r\n"]

    def test_range_validation_rejects(self):
        driver, _ = self._build(b"")
        with pytest.raises(ValueError, match="out of range"):
            driver.execute_command("set_voltage", {"value": 99.0})

    def test_measure_with_regex_parser(self):
        driver, written = self._build(b"V=12.345 V\r\n")
        assert driver.execute_command("measure") == 12.345
        assert written == [b"MEAS?\r\n"]

    def test_unknown_command_raises(self):
        driver, _ = self._build(b"")
        with pytest.raises(ValueError, match="Unknown command"):
            driver.execute_command("does_not_exist")

    def test_capabilities_summary(self):
        driver, _ = self._build(b"")
        caps = driver.get_capabilities()
        assert caps["protocol"] == "serial"
        assert "identify" in caps["commands"]
        assert caps["transport"]["baudrate"] == 9600


PROFILE_BINARY = {
    "protocol": "serial",
    "identity": {"model": "BinSensor"},
    "connection": {
        "default_baudrate": 115200,
        "encoding": "binary",
        "request": {"framing": "raw"},
        "response": {"framing": "length_prefix",
                     "length_field": {"offset": 0, "size": 2, "endian": "big"}},
    },
    "commands": {
        "read": {
            "type": "query",
            "request_bytes": ["0x03", "{addr:u16_be}"],
            "response": {"parser": {"type": "bytes_hex"}},
        },
    },
}


class TestBinaryDriverEndToEnd:
    def test_read_with_length_prefix_response(self):
        # response: 2-byte BE length (0x0003) + 3 payload bytes
        transport, written = _fake_transport_for(b"\x00\x03\xCA\xFE\x42")
        bus_mgr = MagicMock(spec=SerialBusManager)
        bus_mgr.get.return_value = transport
        bus_mgr.parse_uri = SerialBusManager.parse_uri
        driver = GenericSerialDriver(
            instrument_id="bin1",
            transport_uri="/dev/null-binary",
            profile=PROFILE_BINARY,
            bus_manager=bus_mgr,
        )
        driver.connect()

        result = driver.execute_command("read", {"addr": 0x1234})
        # response_framer auto-keeps any pre-length header; offset=0 here so payload only.
        assert result == "cafe42"
        assert written == [b"\x03\x12\x34"]


# ---------------------------------------------------------------------------
# Helper for the driver lifecycle tests below
# ---------------------------------------------------------------------------

def _make_driver(
    response: bytes = b"",
    profile: dict[str, Any] | None = None,
    settings_overrides: dict[str, Any] | None = None,
) -> tuple[GenericSerialDriver, list[bytes], MagicMock, SerialTransport]:
    """Build a driver wired to a fake transport.

    Returns (driver, written_buffer, bus_manager_mock, transport).
    """
    transport, written = _fake_transport_for(response)
    if settings_overrides:
        for k, v in settings_overrides.items():
            setattr(transport.settings, k, v)
    bus_mgr = MagicMock(spec=SerialBusManager)
    bus_mgr.get.return_value = transport
    bus_mgr.parse_uri = SerialBusManager.parse_uri
    driver = GenericSerialDriver(
        instrument_id="test_drv",
        transport_uri="/dev/null-test",
        profile=profile or PROFILE_ASCII,
        bus_manager=bus_mgr,
    )
    return driver, written, bus_mgr, transport


# ---------------------------------------------------------------------------
# F5 — length_prefix offset>0 send-time validation
# ---------------------------------------------------------------------------

class TestLengthPrefixSendValidation:
    def test_offset_nonzero_raises_on_validate_for_send(self):
        f = build_framer({
            "framing": "length_prefix",
            "length_field": {"offset": 1, "size": 1, "endian": "big"},
        })
        with pytest.raises(ValueError):
            f.validate_for_send()

    def test_offset_zero_validates_cleanly(self):
        f = build_framer({
            "framing": "length_prefix",
            "length_field": {"offset": 0, "size": 1, "endian": "big"},
        })
        # No raise
        assert f.validate_for_send() is None

    def test_other_framers_validate_for_send_is_noop(self):
        # line, stx_etx, fixed, raw should all be no-ops
        for spec in [
            {"framing": "line", "terminator": "\n"},
            {"framing": "stx_etx", "stx": 0x02, "etx": 0x03},
            {"framing": "fixed", "length": 4},
            {"framing": "raw"},
        ]:
            f = build_framer(spec)
            assert f.validate_for_send() is None


# ---------------------------------------------------------------------------
# F1 — flush_before_write flag
# ---------------------------------------------------------------------------

class TestFlushBeforeWrite:
    def test_flush_default_calls_reset_input_buffer_once(self):
        fake_ser = MagicMock()
        settings = SerialSettings(port="/dev/null-test", baudrate=9600)
        # default: flush_before_write=True
        assert settings.flush_before_write is True
        transport = SerialTransport(settings, fake_ser)
        transport.write_bytes(b"PING\r\n")
        assert fake_ser.reset_input_buffer.call_count == 1
        fake_ser.write.assert_called_once_with(b"PING\r\n")

    def test_flush_disabled_skips_reset_input_buffer(self):
        fake_ser = MagicMock()
        settings = SerialSettings(
            port="/dev/null-test", baudrate=9600, flush_before_write=False,
        )
        transport = SerialTransport(settings, fake_ser)
        transport.write_bytes(b"PING\r\n")
        assert fake_ser.reset_input_buffer.call_count == 0
        fake_ser.write.assert_called_once_with(b"PING\r\n")


# ---------------------------------------------------------------------------
# F4 — _coerce_terminator escape handling
# ---------------------------------------------------------------------------

class TestCoerceTerminator:
    def test_none_returns_default_lf(self):
        assert _coerce_terminator(None) == b"\n"

    def test_already_decoded_crlf(self):
        # 2-character Python string b"\r\n"
        assert _coerce_terminator("\r\n") == b"\r\n"

    def test_yaml_literal_escape_crlf(self):
        # 4-character YAML-form literal: backslash-r-backslash-n
        assert _coerce_terminator("\\r\\n") == b"\r\n"

    def test_yaml_literal_escape_tab(self):
        # 2-character YAML-form: backslash-t
        assert _coerce_terminator("\\t") == b"\t"

    def test_bytes_passthrough(self):
        assert _coerce_terminator(b"\xFF\xFE") == b"\xFF\xFE"

    def test_escaped_double_backslash(self):
        # 2-char string of two backslashes -> single backslash byte.
        # Critical: regex must not strand a stray backslash.
        assert _coerce_terminator("\\\\") == b"\\"


# ---------------------------------------------------------------------------
# F8 — Windows COM port hardening
# ---------------------------------------------------------------------------

class TestPortNormalization:
    def _open_and_capture_port(self, port: str) -> str:
        settings = SerialSettings(port=port, baudrate=9600)
        with patch("galois_edge.drivers.serial_transport.serial.Serial") as mock_serial:
            settings.open()
            mock_serial.assert_called_once()
            return mock_serial.call_args.kwargs["port"]

    def test_com3_unchanged(self):
        assert self._open_and_capture_port("COM3") == "COM3"

    def test_com10_wrapped_with_unc_prefix(self):
        assert self._open_and_capture_port("COM10") == "\\\\.\\COM10"

    def test_com10_with_trailing_whitespace_stripped_and_wrapped(self):
        assert self._open_and_capture_port("COM10 ") == "\\\\.\\COM10"

    def test_com3_with_trailing_colon_stripped(self):
        # COM3: → COM3 (num<10, no UNC wrap)
        assert self._open_and_capture_port("COM3:") == "COM3"

    def test_already_wrapped_passes_through(self):
        # Already \\.\COM10 should not be re-wrapped or mangled. It does not
        # start with "COM" (uppercased) at index 0 — the implementation only
        # touches names starting with "COM", so this passes through unchanged.
        assert self._open_and_capture_port("\\\\.\\COM10") == "\\\\.\\COM10"

    def test_unix_path_passthrough(self):
        # Linux path must not be touched (no whitespace stripping etc.)
        assert self._open_and_capture_port("/dev/ttyUSB0") == "/dev/ttyUSB0"


# ---------------------------------------------------------------------------
# F6 — rs485_mode plumbing
# ---------------------------------------------------------------------------

class TestRs485Mode:
    def test_rs485_none_does_not_instantiate_settings(self):
        settings = SerialSettings(port="/dev/ttyUSB0", baudrate=9600, rs485_mode=None)
        with patch("galois_edge.drivers.serial_transport.serial.Serial") as mock_serial, \
             patch("serial.rs485.RS485Settings") as mock_rs485:
            ser_instance = mock_serial.return_value
            settings.open()
            assert mock_rs485.call_count == 0
            # ensure assignment never happened
            # (MagicMock auto-creates attributes; check via mock_calls list)
            assignments = [
                call for call in ser_instance.method_calls
                if "rs485_mode" in str(call)
            ]
            # We can't easily assert the *attribute* set, but we can ensure
            # rs485 module wasn't touched at all:
            assert mock_rs485.mock_calls == []

    def test_rs485_dict_instantiates_and_assigns(self):
        rs_kwargs = {"rts_level_for_tx": True, "rts_level_for_rx": False}
        settings = SerialSettings(
            port="/dev/ttyUSB0", baudrate=9600, rs485_mode=rs_kwargs,
        )
        with patch("galois_edge.drivers.serial_transport.serial.Serial") as mock_serial, \
             patch("serial.rs485.RS485Settings") as mock_rs485:
            sentinel = MagicMock(name="rs485-settings")
            mock_rs485.return_value = sentinel
            ser_instance = mock_serial.return_value
            settings.open()
            mock_rs485.assert_called_once_with(**rs_kwargs)
            # assignment to ser.rs485_mode must equal the sentinel
            assert ser_instance.rs485_mode is sentinel

    def test_rs485_assignment_failure_swallowed(self):
        rs_kwargs = {"rts_level_for_tx": True}
        settings = SerialSettings(
            port="/dev/ttyUSB0", baudrate=9600, rs485_mode=rs_kwargs,
        )

        class _BadSer:
            """Fake Serial-like object whose rs485_mode setter raises."""
            def __init__(self, **kwargs):
                self._closed = False
                self.kwargs = kwargs

            @property
            def rs485_mode(self):
                return None

            @rs485_mode.setter
            def rs485_mode(self, value):
                raise RuntimeError("backend does not support RS-485")

            def close(self):
                self._closed = True

        with patch("galois_edge.drivers.serial_transport.serial.Serial", new=_BadSer):
            ser = settings.open()
            # Exception was swallowed; port object returned
            assert ser is not None
            assert ser._closed is False  # not closed by error path


# ---------------------------------------------------------------------------
# F2 — driver lock prevents disconnect/execute race
# ---------------------------------------------------------------------------

class TestLockingDiscipline:
    def test_disconnect_blocks_during_inflight_execute_command(self):
        """Thread A holds the driver during a slow read; thread B's
        disconnect() must block until A releases the lock.
        """
        # Synchronization primitives for deterministic ordering
        a_inside_execute = threading.Event()
        b_can_release = threading.Event()
        b_disconnect_done = threading.Event()

        # Build a transport whose read_until blocks until b_can_release fires
        fake_ser = MagicMock()
        fake_ser.write.return_value = None
        fake_ser.flush.return_value = None
        fake_ser.reset_input_buffer.return_value = None

        def _slow_read_until(term: bytes, size: int | None = None) -> bytes:
            # Notify A is inside the locked region, then wait for the test
            # to release us. The driver is holding self.lock the whole time.
            a_inside_execute.set()
            assert b_can_release.wait(timeout=2.0), "B did not signal release"
            return b"OK\r\n"

        fake_ser.read_until.side_effect = _slow_read_until

        settings = SerialSettings(port="/dev/null-test", baudrate=9600)
        transport = SerialTransport(settings, fake_ser)
        bus_mgr = MagicMock(spec=SerialBusManager)
        bus_mgr.get.return_value = transport
        bus_mgr.parse_uri = SerialBusManager.parse_uri
        driver = GenericSerialDriver(
            instrument_id="lock_test",
            transport_uri="/dev/null-test",
            profile=PROFILE_ASCII,
            bus_manager=bus_mgr,
        )
        driver.connect()

        timestamps: dict[str, float] = {}

        def _thread_a():
            driver.execute_command("identify")
            timestamps["a_done"] = time.monotonic()

        def _thread_b():
            assert a_inside_execute.wait(timeout=2.0), "A did not enter execute"
            # A is now inside execute_command holding driver.lock.
            # B attempts disconnect — must block until A releases.
            t_start_b = time.monotonic()
            timestamps["b_start"] = t_start_b
            # Release A *just after* B enters the contended call. We must
            # release from a 3rd thread because if we release before
            # disconnect, we cannot prove the disconnect was blocked.
            timer = threading.Timer(0.05, b_can_release.set)
            timer.start()
            driver.disconnect()
            timestamps["b_done"] = time.monotonic()
            b_disconnect_done.set()

        t_a = threading.Thread(target=_thread_a)
        t_b = threading.Thread(target=_thread_b)
        t_a.start()
        t_b.start()
        t_a.join(timeout=2.0)
        t_b.join(timeout=2.0)

        assert not t_a.is_alive(), "Thread A deadlocked"
        assert not t_b.is_alive(), "Thread B deadlocked (lock not released?)"
        # Disconnect happened only after execute_command returned.
        assert timestamps["b_done"] >= timestamps["a_done"], (
            f"Disconnect (t={timestamps['b_done']:.4f}) happened before "
            f"execute_command finished (t={timestamps['a_done']:.4f})"
        )

    def test_lock_is_reentrant_for_subscribe_pattern(self):
        """BaseProtocolDriver.subscribe acquires self.lock then calls
        read_points → read_point → execute_command which re-enters the
        same lock. This must not deadlock (RLock allows reentry).
        """
        driver, _, _, _ = _make_driver(b"TestCo,T1,SN,1.0\r\n")
        driver.connect()
        # Mimic the subscribe pattern: outer caller holds the lock, then
        # invokes execute_command which must re-acquire.
        with driver.lock:
            result = driver.execute_command("identify")
        assert result == "TestCo,T1,SN,1.0"


# ---------------------------------------------------------------------------
# New#1 — silent-scale bug coverage
# ---------------------------------------------------------------------------

PROFILE_SCALE = {
    "protocol": "serial",
    "identity": {"manufacturer": "TestCo", "model": "Scale1"},
    "connection": {
        "default_baudrate": 9600,
        "encoding": "ascii",
        "request": {"framing": "line", "terminator": "\r\n"},
        "response": {"framing": "line", "terminator": "\r\n"},
    },
    "commands": {
        "read_num": {
            "type": "query",
            "request": "READ?",
            "response": {"parser": {"type": "passthrough", "cast": "float"}},
        },
        "read_str": {
            "type": "query",
            "request": "READS?",
            "response": {"parser": {"type": "passthrough", "cast": "string"}},
        },
        "write_num": {
            "type": "action",
            "request": "WRITE {value}",
        },
    },
    "points": {
        "scaled_num": {
            "data_type": "float",
            "access": "read_write",
            "scale": 0.1,
            "read_command": "read_num",
            "write_command": "write_num",
        },
        "scaled_str": {
            "data_type": "float",
            "access": "read",
            "scale": 0.1,
            "read_command": "read_str",
        },
    },
}


class TestPointScaling:
    def _build(self, response: bytes):
        transport, written = _fake_transport_for(response)
        bus_mgr = MagicMock(spec=SerialBusManager)
        bus_mgr.get.return_value = transport
        bus_mgr.parse_uri = SerialBusManager.parse_uri
        driver = GenericSerialDriver(
            instrument_id="scaletest",
            transport_uri="/dev/null-test",
            profile=PROFILE_SCALE,
            bus_manager=bus_mgr,
        )
        driver.connect()
        return driver, written

    def test_read_point_numeric_value_scales(self):
        driver, _ = self._build(b"12.34\r\n")
        pt = driver._points["scaled_num"]
        assert driver.read_point(pt) == pytest.approx(1.234)

    def test_read_point_numeric_string_coerced_then_scaled(self):
        driver, _ = self._build(b"12.34\r\n")
        # read_str cast=string, so driver receives "12.34" and must coerce
        pt = driver._points["scaled_str"]
        assert driver.read_point(pt) == pytest.approx(1.234)

    def test_read_point_non_numeric_string_raises_typeerror(self):
        driver, _ = self._build(b"hello\r\n")
        pt = driver._points["scaled_str"]
        with pytest.raises(TypeError, match="scaled_str"):
            driver.read_point(pt)

    def test_write_point_numeric_string_coerced(self):
        driver, written = self._build(b"")
        pt = driver._points["scaled_num"]
        driver.write_point(pt, "5.0")
        # value /scale = 5.0 / 0.1 = 50.0 → rendered as "WRITE 50.0\r\n"
        assert written == [b"WRITE 50.0\r\n"]

    def test_write_point_non_numeric_string_raises_typeerror(self):
        driver, _ = self._build(b"")
        pt = driver._points["scaled_num"]
        with pytest.raises(TypeError, match="scaled_num"):
            driver.write_point(pt, "hello")


# ---------------------------------------------------------------------------
# F3 — bytes_hex honors cast
# ---------------------------------------------------------------------------

class TestBytesHexCast:
    PAYLOAD = b"\xde\xad\xbe\xef"

    def test_default_cast_returns_hex_string(self):
        assert _parse_response(
            self.PAYLOAD, {"type": "bytes_hex"}, "binary"
        ) == "deadbeef"

    def test_cast_int_returns_integer(self):
        assert _parse_response(
            self.PAYLOAD, {"type": "bytes_hex", "cast": "int"}, "binary"
        ) == 0xDEADBEEF

    def test_cast_bytes_returns_raw_bytes(self):
        assert _parse_response(
            self.PAYLOAD, {"type": "bytes_hex", "cast": "bytes"}, "binary"
        ) == self.PAYLOAD

    def test_cast_string_returns_hex_string(self):
        assert _parse_response(
            self.PAYLOAD, {"type": "bytes_hex", "cast": "string"}, "binary"
        ) == "deadbeef"


# ---------------------------------------------------------------------------
# inter_command_delay_ms minimum-gap enforcement
# ---------------------------------------------------------------------------

class TestInterCommandDelay:
    def test_delay_enforced_when_recent_io(self):
        fake_ser = MagicMock()
        settings = SerialSettings(port="/dev/null-test", baudrate=9600)
        transport = SerialTransport(settings, fake_ser)
        # Pretend a write happened "now" — next write must wait for the gap
        transport.last_io_at = time.monotonic()
        with patch(
            "galois_edge.drivers.serial_transport.time.sleep"
        ) as mock_sleep:
            transport.write_bytes(b"PING\r\n", inter_command_delay_ms=100)
            assert mock_sleep.call_count == 1
            wait_arg = mock_sleep.call_args.args[0]
            # Must be >0 and <= 100ms (in seconds: 0..0.1)
            assert wait_arg > 0
            assert wait_arg <= 0.1

    def test_no_delay_when_zero_ms(self):
        fake_ser = MagicMock()
        settings = SerialSettings(port="/dev/null-test", baudrate=9600)
        transport = SerialTransport(settings, fake_ser)
        transport.last_io_at = time.monotonic()
        with patch(
            "galois_edge.drivers.serial_transport.time.sleep"
        ) as mock_sleep:
            transport.write_bytes(b"PING\r\n", inter_command_delay_ms=0)
            assert mock_sleep.call_count == 0

    def test_no_delay_if_enough_time_already_elapsed(self):
        fake_ser = MagicMock()
        settings = SerialSettings(port="/dev/null-test", baudrate=9600)
        transport = SerialTransport(settings, fake_ser)
        # Set last_io to far in the past so no wait is needed
        transport.last_io_at = time.monotonic() - 10.0
        with patch(
            "galois_edge.drivers.serial_transport.time.sleep"
        ) as mock_sleep:
            transport.write_bytes(b"PING\r\n", inter_command_delay_ms=100)
            assert mock_sleep.call_count == 0


# ---------------------------------------------------------------------------
# Double-disconnect safety
# ---------------------------------------------------------------------------

class TestDoubleDisconnect:
    def test_double_disconnect_does_not_raise(self):
        driver, _, bus_mgr, _ = _make_driver(b"")
        driver.connect()
        driver.disconnect()
        # Second call must not raise
        driver.disconnect()

    def test_double_disconnect_releases_only_once(self):
        driver, _, bus_mgr, _ = _make_driver(b"")
        driver.connect()
        driver.disconnect()
        driver.disconnect()
        # release() should have been called only once across two disconnects
        assert bus_mgr.release.call_count == 1


# ---------------------------------------------------------------------------
# length_prefix + checksum round-trip
# ---------------------------------------------------------------------------

class TestLengthPrefixWithChecksum:
    def test_send_and_receive_round_trip(self):
        # Send: payload "HELLO" → CRC16-modbus appended → length-prefix wraps total.
        # Order in Framer.frame: payload + checksum, then _wrap.
        framer = build_framer(
            {
                "framing": "length_prefix",
                "length_field": {"offset": 0, "size": 2, "endian": "big"},
            },
            {"type": "crc16_modbus", "append": True, "verify": True},
        )
        payload = b"HELLO"
        # Compute expected CRC16-modbus over "HELLO"
        cs, _ = build_checksum({"type": "crc16_modbus"})
        crc = cs.compute(payload)
        # length = len(payload) + len(crc) = 5 + 2 = 7
        expected = bytes([0x00, 0x07]) + payload + crc
        assert framer.frame(payload) == expected

        # Now unframe the same wire bytes — verify=true should strip CRC
        # and return the inner payload.
        reader = FakeReader(expected)
        out = framer.unframe(reader)
        assert out == b"HELLO"

    def test_receive_detects_corrupt_crc(self):
        framer = build_framer(
            {
                "framing": "length_prefix",
                "length_field": {"offset": 0, "size": 2, "endian": "big"},
            },
            {"type": "crc16_modbus", "verify": True},
        )
        # Hand-craft a corrupt frame: claim 7 bytes, body+bad CRC
        wire = bytes([0x00, 0x07]) + b"HELLO" + b"\x00\x00"
        reader = FakeReader(wire)
        with pytest.raises(ValueError, match="Checksum mismatch"):
            framer.unframe(reader)


# ---------------------------------------------------------------------------
# Registry summary fix (New#3) — register_count vs command_count/point_count
# ---------------------------------------------------------------------------

class TestRegistrySummary:
    def test_serial_profile_has_command_and_point_counts(self, tmp_path):
        # Skip if pymodbus is unavailable — registry imports it transitively.
        pytest.importorskip("pymodbus")

        from galois_edge.drivers.registry import DriverRegistry

        # Build a minimal serial profile and a minimal modbus profile under
        # tmp_path/<protocol>/*.yaml — the registry layout.
        import yaml

        serial_dir = tmp_path / "generic_serial"
        serial_dir.mkdir()
        serial_profile = {
            "protocol": "serial",
            "identity": {
                "manufacturer": "Test",
                "model": "Serial1",
                "description": "test serial",
            },
            "connection": {"default_baudrate": 9600, "encoding": "ascii"},
            "commands": {
                "ping": {"type": "query", "request": "*PING?"},
                "pong": {"type": "query", "request": "*PONG?"},
            },
            "points": {
                "p1": {"data_type": "float", "read_command": "ping"},
            },
        }
        (serial_dir / "test_serial.yaml").write_text(yaml.safe_dump(serial_profile))

        modbus_dir = tmp_path / "modbus"
        modbus_dir.mkdir()
        modbus_profile = {
            "protocol": "modbus",
            "identity": {
                "manufacturer": "Test",
                "model": "Modbus1",
                "description": "test modbus",
            },
            "registers": {
                "reg1": {"address": 0, "type": "holding"},
                "reg2": {"address": 1, "type": "holding"},
                "reg3": {"address": 2, "type": "holding"},
            },
        }
        (modbus_dir / "test_modbus.yaml").write_text(yaml.safe_dump(modbus_profile))

        reg = DriverRegistry(profiles_dir=str(tmp_path))
        n = reg.discover()
        assert n == 2

        summaries = {s["name"]: s for s in reg.list_profiles()}

        # serial entry
        s_summary = summaries["test_serial"]
        assert s_summary["protocol"] == "serial"
        assert "command_count" in s_summary
        assert s_summary["command_count"] == 2
        assert "point_count" in s_summary
        assert s_summary["point_count"] == 1
        assert "register_count" not in s_summary

        # modbus entry
        m_summary = summaries["test_modbus"]
        assert m_summary["protocol"] == "modbus"
        assert "register_count" in m_summary
        assert m_summary["register_count"] == 3
        # modbus must NOT carry serial-flavored fields
        assert "command_count" not in m_summary
        assert "point_count" not in m_summary
