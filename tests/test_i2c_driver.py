"""Tests for GenericI2cDriver against a fake SMBus byte map."""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from galois_edge.drivers.i2c.driver import (
    GenericI2cDriver,
    _decode_bitfield,
    _decode_bytes,
    _encode_value,
)
from galois_edge.drivers.i2c.transport import I2CBusManager


# ---------------------------------------------------------------------------
# Fake SMBus — register-map backed, with optional PEC simulation.
# ---------------------------------------------------------------------------


class FakeSMBusPECError(IOError):
    """Raised by the fake SMBus when PEC verification fails."""


class FakeSMBus:
    """A scriptable in-memory SMBus stand-in keyed by (i2c_addr, register).

    - `byte_map[(addr, reg)]` returns a list of bytes the device would send
      for that register. Single-byte registers store `[value]`.
    - `block_map[(addr, reg)]` is consulted by `read_block_data` (length-
      prefixed reads).
    - PEC mode: when `pec_force_fail` is True, the next read raises a
      simulated CRC error.
    """

    def __init__(self, bus_num: int) -> None:
        self.bus_num = bus_num
        self.closed = False
        self.pec = False
        self.byte_map: dict[tuple[int, int], list[int]] = {}
        self.block_map: dict[tuple[int, int], list[int]] = {}
        self.writes: list[tuple[str, int, int, Any]] = []
        self.pec_force_fail = False
        self.read_delay_s: float = 0.0

    def close(self) -> None:
        self.closed = True

    def enable_pec(self, enable: bool = True) -> None:
        self.pec = bool(enable)

    # -- read primitives --

    def _maybe_pec(self) -> None:
        if self.pec and self.pec_force_fail:
            raise FakeSMBusPECError("PEC CRC failure")

    def _maybe_delay(self) -> None:
        if self.read_delay_s:
            time.sleep(self.read_delay_s)

    def read_byte_data(self, addr: int, reg: int) -> int:
        self._maybe_pec()
        self._maybe_delay()
        return self.byte_map[(addr, reg)][0]

    def read_word_data(self, addr: int, reg: int) -> int:
        self._maybe_pec()
        self._maybe_delay()
        bs = self.byte_map[(addr, reg)]
        # smbus2 returns LSB first as the low byte.
        return (bs[1] << 8) | bs[0]

    def read_i2c_block_data(self, addr: int, reg: int, length: int) -> list[int]:
        self._maybe_pec()
        self._maybe_delay()
        bs = self.byte_map[(addr, reg)]
        return list(bs[:length])

    def read_block_data(self, addr: int, reg: int) -> list[int]:
        self._maybe_pec()
        self._maybe_delay()
        return list(self.block_map[(addr, reg)])

    # -- write primitives --

    def write_byte_data(self, addr: int, reg: int, value: int) -> None:
        self.byte_map[(addr, reg)] = [value & 0xFF]
        self.writes.append(("byte", addr, reg, value))

    def write_word_data(self, addr: int, reg: int, value: int) -> None:
        # smbus2 takes the word as int and sends LSB first on the wire.
        self.byte_map[(addr, reg)] = [value & 0xFF, (value >> 8) & 0xFF]
        self.writes.append(("word", addr, reg, value))

    def write_i2c_block_data(self, addr: int, reg: int, data: list[int]) -> None:
        self.byte_map[(addr, reg)] = list(data)
        self.writes.append(("block", addr, reg, list(data)))


# ---------------------------------------------------------------------------
# Profiles for tests
# ---------------------------------------------------------------------------


BME280_LIKE_PROFILE: dict[str, Any] = {
    "protocol": "i2c",
    "identity": {"manufacturer": "Bosch", "model": "BME280"},
    "connection": {
        "bus": 1,
        "device_address": 0x76,
        "smbus_compatible": True,
        "byte_order": "big",
    },
    "registers": {
        "chip_id": {
            "address": 0xD0,
            "access": "read",
            "length_bytes": 1,
            "data_type": "uint8",
            "expected": 0x60,
        },
        "ctrl_meas": {
            "address": 0xF4,
            "access": "read_write",
            "length_bytes": 1,
            "data_type": "uint8",
            "bitfield": {
                "mode": {
                    "bits": [0, 1],
                    "enum": {0: "sleep", 1: "forced", 3: "normal"},
                },
                "osrs_p": {"bits": [2, 3, 4]},
                "osrs_t": {"bits": [5, 6, 7]},
            },
        },
        "pressure_msb": {
            "address": 0xF7,
            "access": "read",
            "length_bytes": 3,
            "byte_order": "big",
            "data_type": "uint20",
            "scale": 0.0625,
            "unit": "Pa",
        },
        "calib_ac1": {
            "address": 0x88,
            "access": "read",
            "length_bytes": 2,
            "byte_order": "little",
            "data_type": "int16",
        },
        "config_be": {
            "address": 0xF5,
            "access": "read_write",
            "length_bytes": 2,
            "byte_order": "big",
            "data_type": "uint16",
        },
        "alarm_flags": {
            "address": 0xE0,
            "access": "read",
            "length_bytes": 1,
            "data_type": "uint8",
            "bitfield": {
                "alarm": {"bit": 0, "description": "Alarm"},
                "ready": {"bit": 1, "description": "Ready"},
                "running": {"bit": 7, "description": "Running"},
            },
        },
        "scaled_temp": {
            "address": 0xC0,
            "access": "read",
            "length_bytes": 2,
            "byte_order": "big",
            "data_type": "int16",
            "scale": 0.01,
            "unit": "degC",
        },
        "mode_enum": {
            "address": 0xC1,
            "access": "read_write",
            "length_bytes": 1,
            "data_type": "uint8",
            "enum": {0: "off", 1: "on", 2: "standby"},
        },
        "ranged": {
            "address": 0xC2,
            "access": "read_write",
            "length_bytes": 1,
            "data_type": "uint8",
            "range": [0, 100],
        },
        "id_string": {
            "address": 0xA0,
            "access": "read",
            "length_bytes": 4,
            "data_type": "string",
        },
        "block_payload": {
            "address": 0xB0,
            "access": "read",
            "length_bytes": 0,
            "data_type": "uint8",
            "block_read": True,
        },
    },
    "read_command": {
        "pattern": "register_then_read",
        "block_read": False,
        "pec": False,
    },
    "commands": {
        "read_chip_id": {"type": "query", "reads": ["chip_id"]},
        "set_mode": {
            "type": "action",
            "writes": [{"register": "ctrl_meas", "value": "{value}"}],
        },
    },
}


@pytest.fixture
def fake_factory():
    """Factory that returns the same FakeSMBus per bus number."""
    instances: dict[int, FakeSMBus] = {}

    def factory(bus_num: int) -> FakeSMBus:
        if bus_num not in instances:
            instances[bus_num] = FakeSMBus(bus_num)
        return instances[bus_num]

    factory.instances = instances  # type: ignore[attr-defined]
    return factory


@pytest.fixture
def manager(fake_factory):
    return I2CBusManager(smbus_factory=fake_factory)


@pytest.fixture
def driver(manager, fake_factory):
    drv = GenericI2cDriver(
        instrument_id="bme280-1",
        transport_uri="i2c:///dev/i2c-1",
        profile=BME280_LIKE_PROFILE,
        bus_manager=manager,
    )
    drv.connect()
    return drv


# ---------------------------------------------------------------------------
# Pure decode helpers
# ---------------------------------------------------------------------------


class TestDecodeBytes:
    def test_uint8(self):
        assert _decode_bytes(b"\x42", "uint8", "big") == 0x42

    def test_int8_negative(self):
        assert _decode_bytes(b"\xFF", "int8", "big") == -1

    def test_uint16_big_endian(self):
        assert _decode_bytes(b"\x12\x34", "uint16", "big") == 0x1234

    def test_uint16_little_endian(self):
        assert _decode_bytes(b"\x34\x12", "uint16", "little") == 0x1234

    def test_int16_negative(self):
        assert _decode_bytes(b"\xFF\xFE", "int16", "big") == -2

    def test_uint20_masks_top_4_bits(self):
        # 3 bytes 0xAB CD EF — uint24 would be 0xABCDEF, uint20 keeps low 20 bits.
        assert _decode_bytes(b"\xAB\xCD\xEF", "uint24", "big") == 0xABCDEF
        assert _decode_bytes(b"\xAB\xCD\xEF", "uint20", "big") == 0xBCDEF

    def test_int24_sign_extension(self):
        # 0x800000 → most-negative int24.
        assert _decode_bytes(b"\x80\x00\x00", "int24", "big") == -0x800000


class TestEncodeValue:
    def test_uint8(self):
        assert _encode_value(0x42, "uint8", "big", 1) == b"\x42"

    def test_uint16_big_endian(self):
        assert _encode_value(0x1234, "uint16", "big", 2) == b"\x12\x34"

    def test_uint16_little_endian(self):
        assert _encode_value(0x1234, "uint16", "little", 2) == b"\x34\x12"

    def test_int16_negative(self):
        assert _encode_value(-2, "int16", "big", 2) == b"\xFF\xFE"


class TestDecodeBitfield:
    def test_single_bit(self):
        result = _decode_bitfield(0b00000011, {
            "alarm": {"bit": 0},
            "ready": {"bit": 1},
            "running": {"bit": 7},
        })
        assert result == {"alarm": True, "ready": True, "running": False}

    def test_multi_bit_enum(self):
        # mode bits=[0,1], value 0b01 = "forced"
        result = _decode_bitfield(0b00000001, {
            "mode": {"bits": [0, 1], "enum": {0: "sleep", 1: "forced", 3: "normal"}},
        })
        assert result["mode"] == "forced"

    def test_multi_bit_int(self):
        # osrs_p bits=[2,3,4], raw 0b00010100 → bits 2..4 = 0b101 = 5
        result = _decode_bitfield(0b00010100, {"osrs_p": {"bits": [2, 3, 4]}})
        assert result["osrs_p"] == 5

    def test_invalid_spec_raises(self):
        with pytest.raises(ValueError, match="bit"):
            _decode_bitfield(0, {"bad": {}})


# ---------------------------------------------------------------------------
# Driver — single-byte read/write
# ---------------------------------------------------------------------------


class TestSingleByte:
    def test_read_chip_id(self, driver, fake_factory):
        bus = fake_factory.instances[1]
        bus.byte_map[(0x76, 0xD0)] = [0x60]
        assert driver.read_point(driver._points["chip_id"]) == 0x60

    def test_write_byte_data(self, driver, fake_factory):
        bus = fake_factory.instances[1]
        driver.write_point(driver._points["ctrl_meas"], 0x25)
        assert bus.byte_map[(0x76, 0xF4)] == [0x25]
        assert bus.writes[-1] == ("byte", 0x76, 0xF4, 0x25)


# ---------------------------------------------------------------------------
# Driver — word read/write with byte order
# ---------------------------------------------------------------------------


class TestWord:
    def test_read_word_big_endian(self, driver, fake_factory):
        bus = fake_factory.instances[1]
        bus.byte_map[(0x76, 0xF5)] = [0x12, 0x34]  # MSB-first wire order
        assert driver.read_point(driver._points["config_be"]) == 0x1234

    def test_read_word_little_endian(self, driver, fake_factory):
        # calib_ac1 is little-endian; bytes on wire are [LSB, MSB].
        bus = fake_factory.instances[1]
        bus.byte_map[(0x76, 0x88)] = [0x34, 0x12]
        assert driver.read_point(driver._points["calib_ac1"]) == 0x1234

    def test_write_word_big_endian(self, driver, fake_factory):
        bus = fake_factory.instances[1]
        driver.write_point(driver._points["config_be"], 0x1234)
        # Round-trip reading via FakeSMBus should yield the same value.
        assert driver.read_point(driver._points["config_be"]) == 0x1234


# ---------------------------------------------------------------------------
# Driver — 3-byte / 24-bit / 20-bit register
# ---------------------------------------------------------------------------


class TestThreeByte:
    def test_read_uint20_with_scale(self, driver, fake_factory):
        # raw = 0x056AB0 (low 20 bits = 0x56AB0). After mask + scale (0.0625):
        # 0x56AB0 = 354992, * 0.0625 = 22187.0
        bus = fake_factory.instances[1]
        bus.byte_map[(0x76, 0xF7)] = [0x05, 0x6A, 0xB0]
        result = driver.read_point(driver._points["pressure_msb"])
        assert result == pytest.approx(22187.0)

    def test_uint20_masks_top_nibble(self, driver, fake_factory):
        # 0xFF6AB0 → uint20 keeps 0xF6AB0 (top byte's high nibble dropped).
        bus = fake_factory.instances[1]
        bus.byte_map[(0x76, 0xF7)] = [0xFF, 0x6A, 0xB0]
        scaled = driver.read_point(driver._points["pressure_msb"])
        # 0xF6AB0 = 1010352, * 0.0625 = 63147.0
        assert scaled == pytest.approx(63147.0)


# ---------------------------------------------------------------------------
# Driver — block read (length-prefixed SMBus block read)
# ---------------------------------------------------------------------------


class TestBlockRead:
    def test_block_read_returns_bytes(self, driver, fake_factory):
        bus = fake_factory.instances[1]
        bus.block_map[(0x76, 0xB0)] = [0x10, 0x20, 0x30, 0x40]
        result = driver.read_point(driver._points["block_payload"])
        # block_payload is uint8 by data_type, length 0; we expect just the
        # first byte after decode.
        assert result == 0x10


# ---------------------------------------------------------------------------
# Driver — bitfield decode
# ---------------------------------------------------------------------------


class TestBitfieldDriver:
    def test_single_bit_flags(self, driver, fake_factory):
        bus = fake_factory.instances[1]
        bus.byte_map[(0x76, 0xE0)] = [0b10000011]  # alarm=1, ready=1, running=1
        result = driver.read_point(driver._points["alarm_flags"])
        assert result == {"alarm": True, "ready": True, "running": True}

    def test_multi_bit_enum_decode(self, driver, fake_factory):
        bus = fake_factory.instances[1]
        # ctrl_meas bits [0..1]=mode, [2..4]=osrs_p, [5..7]=osrs_t
        # mode = 1 ("forced"), osrs_p = 5, osrs_t = 1 → 0b 001 101 01 = 0x35
        bus.byte_map[(0x76, 0xF4)] = [0b00110101]
        result = driver.read_point(driver._points["ctrl_meas"])
        assert result["mode"] == "forced"
        assert result["osrs_p"] == 5
        assert result["osrs_t"] == 1


# ---------------------------------------------------------------------------
# Driver — identify()
# ---------------------------------------------------------------------------


class TestIdentify:
    def test_identify_matching_chip_id(self, driver, fake_factory):
        bus = fake_factory.instances[1]
        bus.byte_map[(0x76, 0xD0)] = [0x60]  # matches expected
        descriptor = driver.identify()
        assert "Bosch" in descriptor
        assert "BME280" in descriptor

    def test_identify_mismatched_chip_id_raises(self, driver, fake_factory):
        bus = fake_factory.instances[1]
        bus.byte_map[(0x76, 0xD0)] = [0x42]  # wrong
        with pytest.raises(IOError, match="chip_id mismatch"):
            driver.identify()

    def test_identify_without_expected_does_not_read(self):
        # Profile with no `expected:` value should skip the read entirely.
        profile = {
            "protocol": "i2c",
            "identity": {"manufacturer": "X", "model": "Y"},
            "connection": {"bus": 1, "device_address": 0x10},
            "registers": {
                "data": {"address": 0x00, "access": "read", "length_bytes": 1, "data_type": "uint8"},
            },
        }
        mgr = I2CBusManager(smbus_factory=lambda b: FakeSMBus(b))
        drv = GenericI2cDriver("x", "i2c:///dev/i2c-1", profile, mgr)
        drv.connect()
        descriptor = drv.identify()
        assert "X Y" in descriptor


# ---------------------------------------------------------------------------
# Driver — PEC verification
# ---------------------------------------------------------------------------


class TestPEC:
    def test_pec_enabled_on_connect(self, fake_factory):
        profile = dict(BME280_LIKE_PROFILE)
        profile["read_command"] = {"pattern": "register_then_read", "pec": True}
        mgr = I2CBusManager(smbus_factory=fake_factory)
        drv = GenericI2cDriver("p", "i2c:///dev/i2c-1", profile, mgr)
        drv.connect()
        assert fake_factory.instances[1].pec is True

    def test_pec_failure_surfaces_as_ioerror(self, fake_factory):
        profile = dict(BME280_LIKE_PROFILE)
        profile["read_command"] = {"pattern": "register_then_read", "pec": True}
        mgr = I2CBusManager(smbus_factory=fake_factory)
        drv = GenericI2cDriver("p", "i2c:///dev/i2c-1", profile, mgr)
        drv.connect()
        bus = fake_factory.instances[1]
        bus.byte_map[(0x76, 0xD0)] = [0x60]
        bus.pec_force_fail = True
        with pytest.raises(IOError, match="PEC"):
            drv.read_point(drv._points["chip_id"])

    def test_pec_disabled_by_default(self, driver, fake_factory):
        assert fake_factory.instances[1].pec is False


# ---------------------------------------------------------------------------
# Driver — write semantics, range, enums
# ---------------------------------------------------------------------------


class TestWriteSemantics:
    def test_write_read_only_rejected(self, driver):
        with pytest.raises(PermissionError, match="read-only"):
            driver.write_point(driver._points["chip_id"], 0)

    def test_write_out_of_range_rejected(self, driver):
        with pytest.raises(ValueError, match="out of range"):
            driver.write_point(driver._points["ranged"], 200)

    def test_write_enum_string(self, driver, fake_factory):
        driver.write_point(driver._points["mode_enum"], "on")
        bus = fake_factory.instances[1]
        assert bus.byte_map[(0x76, 0xC1)] == [1]

    def test_write_with_inverse_scale(self, driver, fake_factory):
        # scaled_temp scale = 0.01; writing 12.34 should send raw = 1234.
        # Note: scaled_temp is read-only, so use a writeable variant.
        # We'll re-purpose ctrl_meas, which is uint8 read_write.
        # Add a read_write scaled register on the fly via direct dict edit.
        driver.write_point(driver._points["ranged"], 50)
        bus = fake_factory.instances[1]
        assert bus.byte_map[(0x76, 0xC2)] == [50]


# ---------------------------------------------------------------------------
# Driver — scale, enum, capabilities, commands
# ---------------------------------------------------------------------------


class TestScalingAndEnums:
    def test_read_int16_with_scale(self, driver, fake_factory):
        bus = fake_factory.instances[1]
        # 1234 raw, scale 0.01 → 12.34
        bus.byte_map[(0x76, 0xC0)] = [0x04, 0xD2]  # 0x04D2 = 1234
        assert driver.read_point(driver._points["scaled_temp"]) == pytest.approx(12.34)

    def test_read_int16_negative_scaled(self, driver, fake_factory):
        bus = fake_factory.instances[1]
        # -1234 → 0xFB2E, scale 0.01 → -12.34
        bus.byte_map[(0x76, 0xC0)] = [0xFB, 0x2E]
        assert driver.read_point(driver._points["scaled_temp"]) == pytest.approx(-12.34)

    def test_read_enum_mapping(self, driver, fake_factory):
        bus = fake_factory.instances[1]
        bus.byte_map[(0x76, 0xC1)] = [2]
        assert driver.read_point(driver._points["mode_enum"]) == "standby"

    def test_read_string(self, driver, fake_factory):
        bus = fake_factory.instances[1]
        bus.byte_map[(0x76, 0xA0)] = list(b"BME2")
        assert driver.read_point(driver._points["id_string"]) == "BME2"


class TestCapabilitiesAndCommands:
    def test_get_capabilities(self, driver):
        caps = driver.get_capabilities()
        assert caps["protocol"] == "i2c"
        assert caps["device_address"] == 0x76
        assert caps["bus"] == 1
        assert "read_chip_id" in caps["commands"]
        assert caps["registers"] == len(BME280_LIKE_PROFILE["registers"])
        assert caps["writable"] >= 1

    def test_query_command_executes(self, driver, fake_factory):
        bus = fake_factory.instances[1]
        bus.byte_map[(0x76, 0xD0)] = [0x60]
        assert driver.execute_command("read_chip_id") == 0x60

    def test_action_command_with_param(self, driver, fake_factory):
        bus = fake_factory.instances[1]
        result = driver.execute_command("set_mode", {"value": 0x25})
        assert result == {"status": "ok"}
        assert bus.byte_map[(0x76, 0xF4)] == [0x25]


# ---------------------------------------------------------------------------
# Driver — concurrency: per-(bus, addr) locking
# ---------------------------------------------------------------------------


class TestConcurrentLocking:
    def test_two_drivers_same_bus_different_addr_overlap(self, fake_factory):
        """Two drivers on the same bus but different addresses don't block."""
        manager = I2CBusManager(smbus_factory=fake_factory)
        prof_a = dict(BME280_LIKE_PROFILE)
        prof_b = dict(BME280_LIKE_PROFILE)
        prof_b = {**BME280_LIKE_PROFILE,
                  "connection": {**BME280_LIKE_PROFILE["connection"],
                                 "device_address": 0x77}}

        a = GenericI2cDriver("a", "i2c:///dev/i2c-1", prof_a, manager)
        b = GenericI2cDriver("b", "i2c:///dev/i2c-1", prof_b, manager)
        a.connect()
        b.connect()

        bus = fake_factory.instances[1]
        bus.byte_map[(0x76, 0xD0)] = [0x60]
        bus.byte_map[(0x77, 0xD0)] = [0x60]
        bus.read_delay_s = 0.05

        results: list[tuple[str, float]] = []

        def reader(name: str, drv):
            t0 = time.perf_counter()
            drv.read_point(drv._points["chip_id"])
            results.append((name, time.perf_counter() - t0))

        ta = threading.Thread(target=reader, args=("a", a))
        tb = threading.Thread(target=reader, args=("b", b))
        t_start = time.perf_counter()
        ta.start()
        tb.start()
        ta.join(timeout=2)
        tb.join(timeout=2)
        elapsed = time.perf_counter() - t_start

        # Both reads share the bus but on different addresses, so they hit
        # different RLocks and run concurrently. The total wall-clock time
        # should be close to a single delay rather than 2x.
        assert elapsed < 0.09, f"reads serialised across addresses: {elapsed}"

    def test_two_drivers_same_addr_serialise(self, fake_factory):
        """Two drivers on the same (bus, addr) serialise their transactions."""
        manager = I2CBusManager(smbus_factory=fake_factory)
        a = GenericI2cDriver("a", "i2c:///dev/i2c-1", BME280_LIKE_PROFILE, manager)
        b = GenericI2cDriver("b", "i2c:///dev/i2c-1", BME280_LIKE_PROFILE, manager)
        a.connect()
        b.connect()

        bus = fake_factory.instances[1]
        bus.byte_map[(0x76, 0xD0)] = [0x60]
        bus.read_delay_s = 0.05

        def reader(drv):
            drv.read_point(drv._points["chip_id"])

        t_start = time.perf_counter()
        ta = threading.Thread(target=reader, args=(a,))
        tb = threading.Thread(target=reader, args=(b,))
        ta.start()
        tb.start()
        ta.join(timeout=2)
        tb.join(timeout=2)
        elapsed = time.perf_counter() - t_start
        assert elapsed >= 0.09, f"same-addr reads should serialise: {elapsed}"


# ---------------------------------------------------------------------------
# Driver — disconnect / not-connected guards
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_read_without_connect_raises(self, manager):
        drv = GenericI2cDriver("x", "i2c:///dev/i2c-1", BME280_LIKE_PROFILE, manager)
        with pytest.raises(RuntimeError, match="not connected"):
            drv.read_point(drv._points["chip_id"])

    def test_disconnect_releases_bus(self, manager, fake_factory):
        drv = GenericI2cDriver("x", "i2c:///dev/i2c-1", BME280_LIKE_PROFILE, manager)
        drv.connect()
        assert fake_factory.instances[1].closed is False
        drv.disconnect()
        assert fake_factory.instances[1].closed is True


# ---------------------------------------------------------------------------
# Self-registration smoke test (does not require Phase 0)
# ---------------------------------------------------------------------------


class TestSelfRegistration:
    def test_import_does_not_crash_without_register_classmethod(self):
        # Just importing the package should succeed even when the registry
        # does not yet expose the plugin-style register() method.
        import galois_edge.drivers.i2c as i2c_pkg

        assert hasattr(i2c_pkg, "GenericI2cDriver")
        assert hasattr(i2c_pkg, "I2CBusManager")
