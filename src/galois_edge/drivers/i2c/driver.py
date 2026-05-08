"""Generic I²C driver that interprets the I²C YAML profile at runtime.

Mirrors `GenericModbusDriver` in shape: register-map driven, bitfield
decode lifted from the Modbus driver pattern, scale + enum mapping, and a
shared bus manager. Wire primitives come from `smbus2`.

Supported per-register read patterns:

- `length_bytes == 1` → `read_byte_data(addr, register)`
- `length_bytes == 2` → `read_word_data(addr, register)` (with byte_order)
- `length_bytes >= 3` → `read_i2c_block_data(addr, register, length)`
- `read_command.block_read: true` → `read_block_data(addr, register)`
  (SMBus length-prefixed block read; the device returns the length first)
- `read_command.pec: true` → enable SMBus PEC validation on the bus before
  reads/writes; CRC errors surface as `IOError`.

The bitfield handling block mirrors Modbus: each named sub-field declares
either `bit: <int>` (single-bit boolean) or `bits: [lo, ..., hi]` (multi-
bit enum/integer).
"""

from __future__ import annotations

import logging
import struct
import threading
from typing import Any

from galois_edge.drivers.base import BaseProtocolDriver
from galois_edge.drivers.i2c.transport import I2CBusManager
from galois_edge.drivers.point import Point

logger = logging.getLogger(__name__)


# data_type → (default length in bytes, struct format for fixed-width decodes)
# - "uint20" / "uint24" use 3 bytes packed big-endian and need custom decode.
# - "string" reads a block but defers length to the profile.
TYPE_INFO: dict[str, tuple[int, str | None]] = {
    "bool": (1, None),
    "uint8": (1, "B"),
    "int8": (1, "b"),
    "uint16": (2, "H"),
    "int16": (2, "h"),
    "uint20": (3, None),
    "uint24": (3, None),
    "int24": (3, None),
    "uint32": (4, "I"),
    "int32": (4, "i"),
    "float32": (4, "f"),
    "string": (0, None),
}


def _decode_bytes(
    raw: bytes,
    data_type: str,
    byte_order: str,
) -> int | float:
    """Decode a byte-buffer per data_type and byte_order ("big" | "little")."""
    if data_type == "bool":
        return int(bool(raw[0]))
    if data_type in ("uint8", "int8"):
        fmt = "b" if data_type == "int8" else "B"
        return struct.unpack(fmt, raw[:1])[0]

    # Normalise endianness
    if byte_order == "little":
        ordered = bytes(raw[::-1]) if data_type not in ("string",) else raw
        endian = ">"
        # We've reversed, so always decode big-endian on the reversed buffer.
        be_bytes = ordered
    else:
        endian = ">"
        be_bytes = bytes(raw)

    if data_type in ("uint20", "uint24"):
        # 3 bytes, unsigned big-endian. uint20 masks the top 4 bits off.
        if len(be_bytes) < 3:
            raise ValueError(f"Need 3 bytes for {data_type}, got {len(be_bytes)}")
        v = (be_bytes[0] << 16) | (be_bytes[1] << 8) | be_bytes[2]
        if data_type == "uint20":
            v &= 0x0FFFFF
        return v
    if data_type == "int24":
        if len(be_bytes) < 3:
            raise ValueError("Need 3 bytes for int24")
        v = (be_bytes[0] << 16) | (be_bytes[1] << 8) | be_bytes[2]
        if v & 0x800000:
            v -= 0x1000000
        return v

    info = TYPE_INFO.get(data_type)
    if info is None or info[1] is None:
        raise ValueError(f"Unsupported data_type for I²C decode: {data_type}")
    width = info[0]
    fmt = info[1]
    return struct.unpack(endian + fmt, be_bytes[:width])[0]


def _encode_value(
    value: int | float,
    data_type: str,
    byte_order: str,
    length_bytes: int,
) -> bytes:
    """Encode `value` into raw bytes for writing on the wire."""
    if data_type == "bool":
        return bytes([1 if value else 0])
    if data_type in ("uint8", "int8"):
        fmt = "b" if data_type == "int8" else "B"
        return struct.pack(fmt, int(value))

    if data_type in ("uint20", "uint24"):
        v = int(value) & 0xFFFFFF
        be = bytes([(v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF])
        return be if byte_order == "big" else bytes(reversed(be))
    if data_type == "int24":
        v = int(value)
        if v < 0:
            v += 0x1000000
        v &= 0xFFFFFF
        be = bytes([(v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF])
        return be if byte_order == "big" else bytes(reversed(be))

    info = TYPE_INFO.get(data_type)
    if info is None or info[1] is None:
        raise ValueError(f"Unsupported data_type for I²C encode: {data_type}")
    fmt = info[1]
    if "int" in data_type:
        value = int(value)
    elif "float" in data_type:
        value = float(value)
    if byte_order == "little":
        return struct.pack("<" + fmt, value).ljust(length_bytes, b"\x00")
    return struct.pack(">" + fmt, value).rjust(length_bytes, b"\x00")


def _decode_bitfield(raw_int: int, bitfield: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Decode a bitfield block.

    Each sub-field declares either:
    - `bit: N`               → a single bit, returned as bool
    - `bits: [b0, b1, ...]`  → a contiguous run, returned as int (or enum-mapped
                                if `enum:` is present on the sub-field)
    """
    result: dict[str, Any] = {}
    for name, spec in bitfield.items():
        if "bit" in spec:
            bit_index = int(spec["bit"])
            result[name] = bool(raw_int & (1 << bit_index))
            continue
        if "bits" in spec:
            bits = list(spec["bits"])
            # Mask out the bits in `bits`, shift down to start at bit 0.
            lo = min(bits)
            hi = max(bits)
            width = hi - lo + 1
            mask = (1 << width) - 1
            value = (raw_int >> lo) & mask
            enum = spec.get("enum")
            if enum and value in enum:
                result[name] = enum[value]
            else:
                result[name] = value
            continue
        raise ValueError(f"Bitfield '{name}' must declare 'bit' or 'bits'")
    return result


class GenericI2cDriver(BaseProtocolDriver):
    """Loads an I²C profile YAML and interprets it at runtime."""

    def __init__(
        self,
        instrument_id: str,
        transport_uri: str,
        profile: dict[str, Any],
        bus_manager: I2CBusManager,
        **kwargs: Any,
    ) -> None:
        super().__init__(instrument_id, transport_uri, **kwargs)
        self.profile = profile
        self.bus_manager = bus_manager
        self.smbus: Any = None

        conn = profile.get("connection", {})
        self.bus_num: int = int(kwargs.get("bus_num", conn.get("bus", 1)))
        self.device_address: int = int(
            kwargs.get("device_address", conn.get("device_address", 0))
        )
        self.ten_bit: bool = bool(conn.get("ten_bit", False))
        self.smbus_compatible: bool = bool(conn.get("smbus_compatible", True))
        self.default_byte_order: str = conn.get("byte_order", "big")

        read_cmd = profile.get("read_command", {}) or {}
        self.read_pattern: str = read_cmd.get("pattern", "register_then_read")
        self.block_read: bool = bool(read_cmd.get("block_read", False))
        self.pec_enabled: bool = bool(read_cmd.get("pec", False))

        # Per-device lock acquired from the bus manager on connect().
        self.device_lock: threading.RLock | None = None

        # Build Point objects from YAML registers.
        for name, reg_def in profile.get("registers", {}).items():
            dt = reg_def.get("data_type", "uint8")
            default_len = TYPE_INFO.get(dt, (1, None))[0]
            length_bytes = int(reg_def.get("length_bytes", default_len or 1))
            addressing: dict[str, Any] = {
                "address": reg_def["address"],
                "length_bytes": length_bytes,
                "byte_order": reg_def.get("byte_order", self.default_byte_order),
                "expected": reg_def.get("expected"),
                "block_read": bool(reg_def.get("block_read", False)),
            }

            range_val: tuple[float, float] | None = None
            if "range" in reg_def:
                r = reg_def["range"]
                range_val = (float(r[0]), float(r[1]))

            self._points[name] = Point(
                name=name,
                data_type=dt,
                access=reg_def.get("access", "read"),
                scale=float(reg_def.get("scale", 1.0)),
                unit=reg_def.get("unit", ""),
                range=range_val,
                enum=reg_def.get("enum"),
                bitfield=reg_def.get("bitfield"),
                description=reg_def.get("description", ""),
                addressing=addressing,
            )

        self._commands = profile.get("commands", {}) or {}

    # -- Lifecycle --

    def connect(self) -> None:
        self.smbus = self.bus_manager.get_smbus(self.bus_num)
        self.device_lock = self.bus_manager.device_lock(
            self.bus_num, self.device_address
        )
        if self.pec_enabled:
            try:
                self.smbus.enable_pec(True)
            except Exception:  # pragma: no cover - kernel driver may not support PEC
                logger.warning("Failed to enable PEC on bus %d", self.bus_num)
        self._connected = True
        logger.info(
            "I²C driver connected: bus=%d addr=0x%02X",
            self.bus_num,
            self.device_address,
        )

    def disconnect(self) -> None:
        if self.smbus is not None:
            try:
                self.bus_manager.release(self.bus_num)
            except Exception:  # pragma: no cover
                logger.exception("Error releasing I²C bus")
            self.smbus = None
        self.device_lock = None
        self._connected = False

    def identify(self) -> str:
        """Read the chip-id register (if declared with `expected:`) and verify."""
        identity = self.profile.get("identity", {})
        mfr = identity.get("manufacturer", "?")
        model = identity.get("model", "?")
        descriptor = (
            f"{mfr} {model} @ bus={self.bus_num} addr=0x{self.device_address:02X}"
        )

        chip_point = self._chip_id_point()
        if chip_point is None:
            return descriptor

        actual = self.read_point(chip_point)
        expected = chip_point.addressing.get("expected")
        if expected is not None and int(actual) != int(expected):
            raise IOError(
                f"identify(): chip_id mismatch on {descriptor} — "
                f"expected 0x{int(expected):02X}, got 0x{int(actual):02X}"
            )
        return descriptor

    def _chip_id_point(self) -> Point | None:
        """Return the first register that declares an `expected:` value."""
        for p in self._points.values():
            if p.addressing.get("expected") is not None:
                return p
        return None

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "protocol": "i2c",
            "profile": self.profile.get("identity", {}).get("model", "unknown"),
            "bus": self.bus_num,
            "device_address": self.device_address,
            "commands": list(self._commands.keys()),
            "points": [p.to_dict() for p in self._points.values()],
            "registers": len(self._points),
            "writable": sum(
                1 for p in self._points.values() if p.access == "read_write"
            ),
            "block_read": self.block_read,
            "pec": self.pec_enabled,
        }

    # -- Point I/O --

    def read_point(self, point: Point) -> Any:
        if self.device_lock is None:
            raise RuntimeError("I²C driver not connected")
        with self.device_lock:
            return self._read_point_locked(point)

    def _read_point_locked(self, point: Point) -> Any:
        addr = int(point.addressing["address"])
        length = int(point.addressing.get("length_bytes", 1))
        dt = point.data_type
        byte_order = point.addressing.get("byte_order", self.default_byte_order)

        # Read the raw bytes. The choice of SMBus primitive is:
        #
        # - block_read flag (per-register or profile-wide) → SMBus block read
        #   (length-prefixed, device returns its own length)
        # - length == 1                                    → read_byte_data
        # - length == 2 AND byte_order == "little"          → read_word_data
        #   (SMBus word reads are LSB-first on the wire by spec, which
        #   matches little-endian devices)
        # - everything else                                → read_i2c_block_data
        #   (raw byte-stream read; the profile's byte_order controls decode)
        per_register_block = bool(point.addressing.get("block_read", False))
        if per_register_block or self.block_read:
            raw_list = self.smbus.read_block_data(self.device_address, addr)
            raw = bytes(raw_list)
        elif length == 1:
            raw = bytes([self.smbus.read_byte_data(self.device_address, addr)])
        elif length == 2 and byte_order == "little":
            word = self.smbus.read_word_data(self.device_address, addr)
            # smbus2 returns the word with the wire-LSB in the low byte.
            raw = bytes([word & 0xFF, (word >> 8) & 0xFF])
        else:
            raw_list = self.smbus.read_i2c_block_data(
                self.device_address, addr, length
            )
            raw = bytes(raw_list[:length])

        if dt == "string":
            encoding = point.addressing.get("string_encoding", "ascii")
            return raw.decode(encoding, errors="replace").rstrip("\x00").strip()

        decoded = _decode_bytes(raw, dt, byte_order)

        # Bitfield extraction first — masks/enums apply on the raw int.
        if point.bitfield:
            return _decode_bitfield(int(decoded), point.bitfield)

        # Enum mapping
        if point.enum:
            return point.enum.get(int(decoded), str(decoded))

        # Scale (numeric only)
        if isinstance(decoded, (int, float)) and point.scale != 1.0:
            return decoded * point.scale
        return decoded

    def write_point(self, point: Point, value: Any) -> None:
        if point.access == "read":
            raise PermissionError(
                f"Point '{point.name}' (addr 0x{int(point.addressing['address']):02X}) is read-only"
            )

        if point.range is not None and isinstance(value, (int, float)):
            lo, hi = point.range
            if not (lo <= float(value) <= hi):
                raise ValueError(
                    f"Value {value} out of range [{lo}, {hi}] for '{point.name}'"
                )

        if self.device_lock is None:
            raise RuntimeError("I²C driver not connected")
        with self.device_lock:
            self._write_point_locked(point, value)

    def _write_point_locked(self, point: Point, value: Any) -> None:
        addr = int(point.addressing["address"])
        length = int(point.addressing.get("length_bytes", 1))
        dt = point.data_type
        byte_order = point.addressing.get("byte_order", self.default_byte_order)

        # Inverse enum mapping: caller passed a string label.
        if point.enum and isinstance(value, str):
            inv = {v: k for k, v in point.enum.items()}
            if value in inv:
                value = inv[value]

        # Inverse scale (numeric values only)
        if (
            isinstance(value, (int, float))
            and point.scale
            and point.scale != 1.0
        ):
            value = value / point.scale

        raw = _encode_value(value, dt, byte_order, length)

        if length == 1:
            self.smbus.write_byte_data(self.device_address, addr, raw[0])
        elif length == 2 and byte_order == "little":
            # smbus2 word writes send LSB first on the wire.
            lo, hi = raw[0], raw[1]
            word = (hi << 8) | lo
            self.smbus.write_word_data(self.device_address, addr, word)
        else:
            # Big-endian or >2 bytes — emit a raw block write so the wire
            # bytes match the profile's declared byte_order exactly.
            self.smbus.write_i2c_block_data(
                self.device_address, addr, list(raw)
            )

    def read_points(self, points: list[Point]) -> dict[str, Any]:
        """Batch read — single device-lock acquisition for all points."""
        if self.device_lock is None:
            raise RuntimeError("I²C driver not connected")
        with self.device_lock:
            return {p.name: self._read_point_locked(p) for p in points}
