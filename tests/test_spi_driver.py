"""Tests for GenericSpiDriver against MockSPI.

Coverage matrix (32+ cases):

- connect/disconnect lifecycle, mode 0/1/2/3 configuration
- register_address pattern: 1-byte, 2-byte, 3-byte (24-bit), 4-byte reads
- byte_order big vs little
- start_bit pattern (MCP3008-style 10-bit result, channel mux)
- command_byte pattern (DS18B20-style read scratchpad)
- write_point: register_address + command_byte; signed/unsigned
- write-then-readback roundtrip via stateful mock
- bitfield decode (single-bit + multi-bit)
- enum decode
- scale + range validation
- read-only enforcement
- multi-byte address (16-bit register address)
- batch reads via read_points
- start_bit pattern rejects writes
- LSB-first config plumbed to handle
- 3-wire flag plumbed
- get_capabilities shape
- identify() reads designated register
"""

from __future__ import annotations

import pytest

from galois_edge.drivers.spi.driver import GenericSpiDriver, _decode_int, _encode_int
from galois_edge.drivers.spi.transport import MockSPI, SPIBusManager


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def make_driver(profile, response_map=None, **kwargs):
    """Build a connected driver against a MockSPI."""
    mock = MockSPI(response_map=response_map or {})
    mgr = SPIBusManager(mock_instance=mock)
    drv = GenericSpiDriver(
        instrument_id="test",
        transport_uri="spi:///dev/spidev0.0",
        profile=profile,
        bus_manager=mgr,
        **kwargs,
    )
    drv.connect()
    return drv, mock, mgr


AD7124_PROFILE = {
    "protocol": "spi",
    "identity": {"manufacturer": "Analog Devices", "model": "AD7124-8"},
    "connection": {
        "bus": 0,
        "device": 0,
        "mode": 3,
        "bits_per_word": 8,
        "max_speed_hz": 5_000_000,
        "byte_order": "big",
    },
    "read_command": {
        "pattern": "register_address",
        "read_opcode": 0x40,
        "write_opcode": 0x00,
        "address_bytes": 1,
        "identify_register": "id",
    },
    "registers": {
        "status": {
            "address": 0x00,
            "access": "read",
            "length_bytes": 1,
            "data_type": "uint8",
            "bitfield": {
                "not_ready": {"bit": 7},
                "error": {"bit": 6},
                "active_channel": {"bits": [0, 1, 2, 3]},
            },
        },
        "ctrl": {
            "address": 0x01,
            "access": "read_write",
            "length_bytes": 2,
            "data_type": "uint16",
        },
        "data": {
            "address": 0x02,
            "access": "read",
            "length_bytes": 3,
            "data_type": "int24",
            "scale": 1e-6,
            "unit": "V",
        },
        "id": {
            "address": 0x05,
            "access": "read",
            "length_bytes": 1,
            "data_type": "uint8",
        },
        "mode": {
            "address": 0x07,
            "access": "read_write",
            "length_bytes": 2,
            "data_type": "uint16",
            "enum": {0: "continuous", 1: "single", 2: "standby"},
        },
        "config": {
            "address": 0x08,
            "access": "read_write",
            "length_bytes": 2,
            "data_type": "int16",
            "range": [-1000, 1000],
        },
    },
    "commands": {
        "read_data": {"type": "query", "reads": ["data"]},
    },
}


MCP3008_PROFILE = {
    "protocol": "spi",
    "identity": {"manufacturer": "Microchip", "model": "MCP3008"},
    "connection": {
        "bus": 0,
        "device": 0,
        "mode": 0,
    },
    "read_command": {
        "pattern": "start_bit",
        "start_byte": 0x01,
        "mux_template": 0x80,
        "mux_shift": 4,
    },
    "registers": {
        "ch0": {
            "address": 0,
            "channel": 0,
            "access": "read",
            "length_bytes": 2,
            "data_type": "uint16",
            "data_offset_bits": 6,
            "data_length_bits": 10,
            "scale": 1.0,
        },
        "ch3": {
            "address": 3,
            "channel": 3,
            "access": "read",
            "length_bytes": 2,
            "data_type": "uint16",
            "data_offset_bits": 6,
            "data_length_bits": 10,
        },
    },
}


DS18B20_LIKE_PROFILE = {
    "protocol": "spi",
    "identity": {"manufacturer": "Maxim", "model": "DS18B20-Stub"},
    "connection": {"bus": 0, "device": 1, "mode": 0},
    "read_command": {
        "pattern": "command_byte",
        "read_opcode": 0xBE,    # Read Scratchpad
        "write_opcode": 0x4E,   # Write Scratchpad
        "address_bytes": 0,
        "dummy_bytes": 0,
    },
    "registers": {
        "scratchpad_temp": {
            "address": 0,
            "access": "read",
            "length_bytes": 2,
            "data_type": "int16",
            "byte_order": "little",
            "scale": 0.0625,
            "unit": "C",
        },
        "alarm_high": {
            "address": 0,
            "access": "read_write",
            "length_bytes": 1,
            "data_type": "int8",
        },
    },
}


LITTLE_ENDIAN_PROFILE = {
    "protocol": "spi",
    "identity": {"manufacturer": "X", "model": "Y"},
    "connection": {"bus": 0, "device": 0, "mode": 0, "byte_order": "little"},
    "read_command": {
        "pattern": "register_address",
        "read_opcode": 0x80,
        "write_opcode": 0x00,
        "address_bytes": 1,
    },
    "registers": {
        "u32_le": {
            "address": 0x10,
            "access": "read",
            "length_bytes": 4,
            "data_type": "uint32",
        },
    },
}


WIDE_ADDRESS_PROFILE = {
    "protocol": "spi",
    "identity": {"manufacturer": "X", "model": "Y"},
    "connection": {"bus": 0, "device": 0, "mode": 0},
    "read_command": {
        "pattern": "register_address",
        "read_opcode": 0x80,
        "address_bytes": 2,
    },
    "registers": {
        "wide_reg": {
            "address": 0x1234,
            "access": "read",
            "length_bytes": 1,
            "data_type": "uint8",
        },
    },
}


# ---------------------------------------------------------------------------
# Pure decode/encode helpers
# ---------------------------------------------------------------------------


class TestPureHelpers:
    def test_decode_int24_big(self):
        # 0x123456 (1193046)
        assert _decode_int(b"\x12\x34\x56", "int24", "big") == 0x123456

    def test_decode_int24_negative(self):
        # 0xFFFFFF = -1 in two's complement int24
        assert _decode_int(b"\xff\xff\xff", "int24", "big") == -1

    def test_decode_uint24_max(self):
        assert _decode_int(b"\xff\xff\xff", "uint24", "big") == 0xFFFFFF

    def test_decode_uint16_little(self):
        # 0x1234 little-endian = bytes 0x34, 0x12
        assert _decode_int(b"\x34\x12", "uint16", "little") == 0x1234

    def test_encode_int16_big(self):
        assert _encode_int(-1, "int16", "big") == b"\xff\xff"
        assert _encode_int(256, "int16", "big") == b"\x01\x00"

    def test_encode_int16_little(self):
        assert _encode_int(256, "int16", "little") == b"\x00\x01"

    def test_decode_int8(self):
        assert _decode_int(b"\xff", "int8", "big") == -1
        assert _decode_int(b"\x7f", "int8", "big") == 127

    def test_unknown_type_raises(self):
        with pytest.raises(ValueError, match="Unsupported"):
            _decode_int(b"\x00", "float64", "big")


# ---------------------------------------------------------------------------
# Lifecycle + configuration plumbing
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_connect_opens_handle_with_mode(self):
        drv, mock, mgr = make_driver(AD7124_PROFILE)
        assert drv.connected is True
        assert mock._opened == (0, 0)
        assert mock.mode == 3
        assert mock.max_speed_hz == 5_000_000
        drv.disconnect()
        assert drv.connected is False
        assert mock._closed is True

    @pytest.mark.parametrize("mode", [0, 1, 2, 3])
    def test_all_four_modes(self, mode):
        prof = dict(AD7124_PROFILE)
        prof = {**prof, "connection": {**prof["connection"], "mode": mode}}
        drv, mock, _ = make_driver(prof)
        assert mock.mode == mode

    def test_lsb_first_propagates(self):
        prof = dict(AD7124_PROFILE)
        prof = {**prof, "connection": {**prof["connection"], "lsb_first": True}}
        drv, mock, _ = make_driver(prof)
        assert mock.lsbfirst is True

    def test_three_wire_propagates(self):
        prof = dict(AD7124_PROFILE)
        prof = {**prof, "connection": {**prof["connection"], "three_wire": True}}
        drv, mock, _ = make_driver(prof)
        assert mock.threewire is True

    def test_cs_high_propagates(self):
        prof = dict(AD7124_PROFILE)
        prof = {**prof, "connection": {**prof["connection"], "cs_high": True}}
        drv, mock, _ = make_driver(prof)
        assert mock.cshigh is True

    def test_read_before_connect_raises(self):
        mgr = SPIBusManager(mock_instance=MockSPI())
        drv = GenericSpiDriver("t", "spi://", AD7124_PROFILE, mgr)
        with pytest.raises(RuntimeError, match="not connected"):
            drv.read_point(drv._points["status"])


# ---------------------------------------------------------------------------
# register_address pattern
# ---------------------------------------------------------------------------


class TestRegisterAddressPattern:
    def test_read_status_byte(self):
        # status has bit 7 set ("not_ready") and active_channel = 5
        # Returned byte: 0b1000_0101 = 0x85
        # Mock returns junk on byte 0, status on byte 1.
        # TX = [0x40 | 0x00, 0x00] = [0x40, 0x00]
        drv, mock, _ = make_driver(
            AD7124_PROFILE,
            response_map={(0x40, 0x00): [0xFF, 0x85]},
        )
        result = drv.read_point(drv._points["status"])
        assert result == {
            "not_ready": True,
            "error": False,
            "active_channel": 5,
        }
        # Verify wire shape
        assert mock.transactions[-1] == [0x40, 0x00]

    def test_read_id_register(self):
        # AD7124-8 device ID = 0x14
        # TX = [0x40 | 0x05, 0x00] = [0x45, 0x00]
        drv, mock, _ = make_driver(
            AD7124_PROFILE,
            response_map={(0x45, 0x00): [0xFF, 0x14]},
        )
        result = drv.read_point(drv._points["id"])
        assert result == 0x14
        assert mock.transactions[-1] == [0x45, 0x00]

    def test_read_int24_data_register(self):
        # 24-bit reading of 0x100000 = 1048576 (mid-scale-ish)
        # TX = [0x40 | 0x02, 0x00, 0x00, 0x00] = [0x42, 0x00, 0x00, 0x00]
        drv, mock, _ = make_driver(
            AD7124_PROFILE,
            response_map={(0x42, 0x00): [0xFF, 0x10, 0x00, 0x00]},
        )
        result = drv.read_point(drv._points["data"])
        # raw = 0x100000 → scaled by 1e-6
        assert result == pytest.approx(0x100000 * 1e-6)

    def test_read_int24_negative(self):
        # 0xFFFFFE = -2 as int24 → scaled
        drv, mock, _ = make_driver(
            AD7124_PROFILE,
            response_map={(0x42, 0x00): [0xFF, 0xFF, 0xFF, 0xFE]},
        )
        result = drv.read_point(drv._points["data"])
        assert result == pytest.approx(-2 * 1e-6)

    def test_read_uint16_ctrl(self):
        # ctrl register 0x01, 16-bit big-endian read of 0xABCD
        # TX = [0x40 | 0x01, 0x00, 0x00] = [0x41, 0x00, 0x00]
        drv, mock, _ = make_driver(
            AD7124_PROFILE,
            response_map={(0x41, 0x00): [0xFF, 0xAB, 0xCD]},
        )
        result = drv.read_point(drv._points["ctrl"])
        assert result == 0xABCD

    def test_read_enum(self):
        # mode register: raw=1 → "single"
        drv, mock, _ = make_driver(
            AD7124_PROFILE,
            response_map={(0x47, 0x00): [0xFF, 0x00, 0x01]},
        )
        result = drv.read_point(drv._points["mode"])
        assert result == "single"

    def test_write_register_address(self):
        # Write ctrl (addr 0x01) = 0x1234
        # write_opcode = 0x00 → header = [0x00 | 0x01, ...] = [0x01, 0x12, 0x34]
        drv, mock, _ = make_driver(AD7124_PROFILE)
        drv.write_point(drv._points["ctrl"], 0x1234)
        assert mock.transactions[-1] == [0x01, 0x12, 0x34]

    def test_write_with_inverse_enum(self):
        # Write "standby" → 2
        drv, mock, _ = make_driver(AD7124_PROFILE)
        drv.write_point(drv._points["mode"], "standby")
        # mode addr = 0x07, write_opcode | addr = 0x07
        assert mock.transactions[-1] == [0x07, 0x00, 0x02]

    def test_write_signed_int16(self):
        drv, mock, _ = make_driver(AD7124_PROFILE)
        drv.write_point(drv._points["config"], -1)
        # int16 -1 big-endian = 0xFF 0xFF
        assert mock.transactions[-1] == [0x08, 0xFF, 0xFF]

    def test_write_range_violation(self):
        drv, mock, _ = make_driver(AD7124_PROFILE)
        with pytest.raises(ValueError, match="out of range"):
            drv.write_point(drv._points["config"], 5000)

    def test_read_only_register_rejects_write(self):
        drv, mock, _ = make_driver(AD7124_PROFILE)
        with pytest.raises(PermissionError):
            drv.write_point(drv._points["status"], 0xFF)

    def test_write_then_readback_roundtrip(self):
        """Stateful mock: writes update an internal register file, reads return it."""

        class StatefulMock(MockSPI):
            def __init__(self):
                super().__init__()
                self.regs: dict[int, int] = {}

            def xfer2(self, data, *args, **kwargs):
                tx = list(data)
                # Record raw transaction first
                self.transactions.append(list(tx))
                if not tx:
                    return []
                first = tx[0]
                # Read = top bit of opcode set (read_opcode=0x40 in our profile)
                if first & 0x40:
                    addr = first & 0x3F
                    val = self.regs.get(addr, 0)
                    # length is len(tx) - 1 (one address byte)
                    length = len(tx) - 1
                    rx = [0xFF] + list(
                        val.to_bytes(length, byteorder="big", signed=False)
                    )
                    return rx[: len(tx)]
                # Write
                addr = first & 0x3F
                length = len(tx) - 1
                if length > 0:
                    val = int.from_bytes(
                        bytes(tx[1:1 + length]), byteorder="big", signed=False
                    )
                    self.regs[addr] = val
                return [0] * len(tx)

        mock = StatefulMock()
        mgr = SPIBusManager(mock_instance=mock)
        drv = GenericSpiDriver("t", "spi://", AD7124_PROFILE, mgr)
        drv.connect()

        drv.write_point(drv._points["ctrl"], 0xCAFE)
        readback = drv.read_point(drv._points["ctrl"])
        assert readback == 0xCAFE


# ---------------------------------------------------------------------------
# Multi-byte address (16-bit register addresses)
# ---------------------------------------------------------------------------


class TestWideAddress:
    def test_two_byte_address_in_frame(self):
        # addr=0x1234, expect TX prefix = [opcode, 0x12, 0x34, 0x00]
        drv, mock, _ = make_driver(
            WIDE_ADDRESS_PROFILE,
            response_map={(0x80, 0x12): [0xFF, 0xFF, 0xFF, 0xCD]},
        )
        result = drv.read_point(drv._points["wide_reg"])
        # length=1 byte after the 3-byte prefix, returned 0xCD
        assert mock.transactions[-1] == [0x80, 0x12, 0x34, 0x00]
        assert result == 0xCD


# ---------------------------------------------------------------------------
# Little-endian byte order
# ---------------------------------------------------------------------------


class TestByteOrder:
    def test_little_endian_uint32(self):
        # raw=0xDEADBEEF, little-endian on the wire = EF BE AD DE after prefix
        drv, mock, _ = make_driver(
            LITTLE_ENDIAN_PROFILE,
            response_map={(0x90, 0x00): [0xFF, 0xEF, 0xBE, 0xAD, 0xDE]},
        )
        result = drv.read_point(drv._points["u32_le"])
        # opcode 0x80 OR 0x10 = 0x90
        assert mock.transactions[-1] == [0x90, 0x00, 0x00, 0x00, 0x00]
        assert result == 0xDEADBEEF


# ---------------------------------------------------------------------------
# start_bit pattern (MCP3008)
# ---------------------------------------------------------------------------


class TestStartBitPattern:
    def test_mcp3008_channel_0_max(self):
        # Channel 0, value = 1023 (0x3FF, full-scale)
        # TX = [0x01, 0x80 | (0<<4), 0x00] = [0x01, 0x80, 0x00]
        # MISO byte 0 = junk. Bytes [1:3] = (raw_word >> 0) & 0x3FF when offset_bits=6
        # raw_word should be 0x03FF → bytes 0x03, 0xFF
        drv, mock, _ = make_driver(
            MCP3008_PROFILE,
            response_map={(0x01, 0x80): [0xFF, 0x03, 0xFF]},
        )
        result = drv.read_point(drv._points["ch0"])
        assert result == 1023
        assert mock.transactions[-1] == [0x01, 0x80, 0x00]

    def test_mcp3008_channel_3_midscale(self):
        # ch3: TX = [0x01, 0x80 | (3<<4), 0x00] = [0x01, 0xB0, 0x00]
        # value = 512 (0x200) → response bytes [junk, 0x02, 0x00]
        drv, mock, _ = make_driver(
            MCP3008_PROFILE,
            response_map={(0x01, 0xB0): [0xFF, 0x02, 0x00]},
        )
        result = drv.read_point(drv._points["ch3"])
        assert result == 512
        assert mock.transactions[-1] == [0x01, 0xB0, 0x00]

    def test_mcp3008_zero_reading(self):
        drv, mock, _ = make_driver(
            MCP3008_PROFILE,
            response_map={(0x01, 0x80): [0xFF, 0x00, 0x00]},
        )
        assert drv.read_point(drv._points["ch0"]) == 0

    def test_start_bit_pattern_rejects_writes(self):
        drv, mock, _ = make_driver(MCP3008_PROFILE)
        # The MCP3008 profile has all read-only registers — patch one to
        # be read_write to exercise the pattern check.
        ch = drv._points["ch0"]
        ch.access = "read_write"
        with pytest.raises(PermissionError, match="read-only"):
            drv.write_point(ch, 100)


# ---------------------------------------------------------------------------
# command_byte pattern (DS18B20-style)
# ---------------------------------------------------------------------------


class TestCommandBytePattern:
    def test_read_scratchpad_temperature(self):
        # DS18B20 scratchpad: bytes 0-1 are temperature (little-endian, signed)
        # +25.0625 °C → raw = 401 (0x0191) → little-endian wire = 0x91, 0x01
        # TX = [0xBE, 0x00, 0x00] (read opcode 0xBE, length=2)
        drv, mock, _ = make_driver(
            DS18B20_LIKE_PROFILE,
            response_map={0xBE: [0xFF, 0x91, 0x01]},
        )
        result = drv.read_point(drv._points["scratchpad_temp"])
        assert result == pytest.approx(25.0625)
        assert mock.transactions[-1] == [0xBE, 0x00, 0x00]

    def test_command_byte_write_opcode(self):
        # Write alarm_high = -10 (int8): TX = [0x4E, 0xF6]
        drv, mock, _ = make_driver(DS18B20_LIKE_PROFILE)
        drv.write_point(drv._points["alarm_high"], -10)
        assert mock.transactions[-1] == [0x4E, 0xF6]


# ---------------------------------------------------------------------------
# Bitfield decode (single + multi-bit)
# ---------------------------------------------------------------------------


class TestBitfieldDecode:
    def test_single_bit_fields(self):
        # bit 7 set, bit 6 clear, bit 0..3 = 5
        drv, mock, _ = make_driver(
            AD7124_PROFILE,
            response_map={(0x40, 0x00): [0xFF, 0b1000_0101]},
        )
        result = drv.read_point(drv._points["status"])
        assert result["not_ready"] is True
        assert result["error"] is False
        assert result["active_channel"] == 5

    def test_all_zero_status(self):
        drv, mock, _ = make_driver(
            AD7124_PROFILE,
            response_map={(0x40, 0x00): [0xFF, 0x00]},
        )
        result = drv.read_point(drv._points["status"])
        assert result["not_ready"] is False
        assert result["error"] is False
        assert result["active_channel"] == 0

    def test_multibit_field_with_enum(self):
        prof = {
            "protocol": "spi",
            "identity": {"manufacturer": "X", "model": "Y"},
            "connection": {"bus": 0, "device": 0, "mode": 0},
            "read_command": {
                "pattern": "register_address",
                "read_opcode": 0x80,
                "address_bytes": 1,
            },
            "registers": {
                "ctrl": {
                    "address": 0x10,
                    "access": "read",
                    "length_bytes": 1,
                    "data_type": "uint8",
                    "bitfield": {
                        "mode": {
                            "bits": [0, 1],
                            "enum": {0: "off", 1: "on", 3: "auto"},
                        },
                        "ready": {"bit": 7},
                    },
                },
            },
        }
        # raw = 0b1000_0011 → mode=3 (→ "auto"), ready=True
        drv, mock, _ = make_driver(
            prof, response_map={(0x90, 0x00): [0xFF, 0b1000_0011]}
        )
        result = drv.read_point(drv._points["ctrl"])
        assert result == {"mode": "auto", "ready": True}


# ---------------------------------------------------------------------------
# Batch + capabilities + identify
# ---------------------------------------------------------------------------


class TestBatchAndCapabilities:
    def test_read_points_batch(self):
        drv, mock, _ = make_driver(
            AD7124_PROFILE,
            response_map={
                (0x40, 0x00): [0xFF, 0x00],   # status all-zero
                (0x45, 0x00): [0xFF, 0x14],   # id = 0x14
            },
        )
        results = drv.read_points([drv._points["status"], drv._points["id"]])
        assert results["id"] == 0x14
        assert isinstance(results["status"], dict)

    def test_get_capabilities_shape(self):
        drv, mock, _ = make_driver(AD7124_PROFILE)
        caps = drv.get_capabilities()
        assert caps["protocol"] == "spi"
        assert caps["bus"] == 0
        assert caps["mode"] == 3
        assert caps["pattern"] == "register_address"
        assert caps["registers"] == len(AD7124_PROFILE["registers"])
        assert caps["writable"] >= 1
        assert "read_data" in caps["commands"]

    def test_identify_reads_id_register(self):
        drv, mock, _ = make_driver(
            AD7124_PROFILE,
            response_map={(0x45, 0x00): [0xFF, 0x14]},
        )
        s = drv.identify()
        assert "AD7124-8" in s
        assert "0x14" in s

    def test_identify_no_id_register_falls_back_to_metadata(self):
        prof = dict(AD7124_PROFILE)
        prof = {**prof, "read_command": {**prof["read_command"]}}
        prof["read_command"].pop("identify_register", None)
        drv, mock, _ = make_driver(prof)
        s = drv.identify()
        assert "AD7124-8" in s
        # Must NOT have attempted a register read.
        assert mock.transactions == []


# ---------------------------------------------------------------------------
# YAML command execution path (BaseProtocolDriver._exec_query plumbing)
# ---------------------------------------------------------------------------


class TestCommandExecution:
    def test_execute_command_query(self):
        drv, mock, _ = make_driver(
            AD7124_PROFILE,
            response_map={(0x42, 0x00): [0xFF, 0x00, 0x10, 0x00]},
        )
        # read_data is defined in profile as type=query reads=[data]
        result = drv.execute_command("read_data")
        assert result == pytest.approx(0x1000 * 1e-6)

    def test_execute_command_unknown_raises(self):
        drv, mock, _ = make_driver(AD7124_PROFILE)
        with pytest.raises(ValueError, match="Unknown command"):
            drv.execute_command("read_nothing")
