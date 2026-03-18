"""Generic Modbus driver that interprets register_profile.yaml at runtime.

This is the PRIMARY driver path.  No generated Python code needed — the
driver reads the YAML profile and uses ``pymodbus.payload`` for correct
multi-register reads, endianness handling, and function code selection.
"""

from __future__ import annotations

import logging
import struct
from typing import Any

from pymodbus.constants import Endian
from pymodbus.payload import BinaryPayloadBuilder, BinaryPayloadDecoder

from galois_edge.drivers.base import BaseProtocolDriver
from galois_edge.drivers.modbus_transport import ModbusBusManager
from galois_edge.drivers.point import Point

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Endianness mapping
# ---------------------------------------------------------------------------

ENDIAN_MAP = {
    "big": Endian.BIG,
    "little": Endian.LITTLE,
}

# data_type → (default_length_words, decoder_method, builder_method)
TYPE_INFO: dict[str, tuple[int, str | None, str | None]] = {
    "bool": (0, None, None),
    "int16": (1, "decode_16bit_int", "add_16bit_int"),
    "uint16": (1, "decode_16bit_uint", "add_16bit_uint"),
    "int32": (2, "decode_32bit_int", "add_32bit_int"),
    "uint32": (2, "decode_32bit_uint", "add_32bit_uint"),
    "float32": (2, "decode_32bit_float", "add_32bit_float"),
    "float64": (4, "decode_64bit_float", "add_64bit_float"),
    "string": (0, None, None),  # length_words from profile
}


class GenericModbusDriver(BaseProtocolDriver):
    """Loads a register_profile.yaml and interprets it at runtime."""

    def __init__(
        self,
        instrument_id: str,
        transport_uri: str,
        profile: dict[str, Any],
        bus_manager: ModbusBusManager,
        **kwargs: Any,
    ) -> None:
        super().__init__(instrument_id, transport_uri, **kwargs)
        self.profile = profile
        self.bus_manager = bus_manager
        self.client: Any = None
        self.bus_lock: Any = None

        conn = profile.get("connection", {})
        self.slave_id: int = kwargs.get(
            "slave_id", conn.get("default_slave_id", 1)
        )
        self.default_byte_order: str = conn.get("byte_order", "big")
        self.default_word_order: str = conn.get("word_order", "big")

        # Build Point objects from YAML registers
        for name, reg_def in profile.get("registers", {}).items():
            dt = reg_def.get("data_type", "uint16")
            default_len = TYPE_INFO.get(dt, (1, None, None))[0]
            addressing = {
                "address": reg_def["address"],
                "register_type": reg_def.get("register_type", "holding"),
                "length_words": reg_def.get("length_words", default_len or 1),
                "byte_order": reg_def.get("byte_order", self.default_byte_order),
                "word_order": reg_def.get("word_order", self.default_word_order),
                "write_function_code": reg_def.get("write_function_code"),
                "string_encoding": reg_def.get("string_encoding", "ascii"),
            }

            range_val = None
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

        # Commands from YAML
        self._commands = profile.get("commands", {})

    # -- Lifecycle --

    def connect(self) -> None:
        conn = self.profile.get("connection", {})
        parity_raw = conn.get("default_parity", "N")
        # Normalize: "even" → "E", "odd" → "O", "none" → "N"
        parity = parity_raw[0].upper() if len(parity_raw) > 1 else parity_raw.upper()

        self.client, self.bus_lock = self.bus_manager.get_client(
            self.transport_uri,
            baudrate=conn.get("default_baudrate", 9600),
            parity=parity,
            stopbits=conn.get("default_stopbits", 1),
            timeout=conn.get("default_timeout", 1.0),
        )
        self._connected = True
        logger.info("Modbus driver connected: %s (slave %d)", self.transport_uri, self.slave_id)

    def disconnect(self) -> None:
        if self.client:
            conn = self.profile.get("connection", {})
            parity_raw = conn.get("default_parity", "N")
            parity = parity_raw[0].upper() if len(parity_raw) > 1 else parity_raw.upper()
            self.bus_manager.release(
                self.transport_uri,
                baudrate=conn.get("default_baudrate", 9600),
                parity=parity,
                stopbits=conn.get("default_stopbits", 1),
            )
            self.client = None
            self.bus_lock = None
        self._connected = False

    def identify(self) -> str:
        identity = self.profile.get("identity", {})
        mfr = identity.get("manufacturer", "?")
        model = identity.get("model", "?")
        return f"{mfr} {model} @ {self.transport_uri} (slave {self.slave_id})"

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "commands": list(self._commands.keys()),
            "points": [p.to_dict() for p in self._points.values()],
            "protocol": "modbus",
            "profile": self.profile.get("identity", {}).get("model", "unknown"),
            "registers": len(self._points),
            "writable": sum(1 for p in self._points.values() if p.access == "read_write"),
        }

    # -- Point I/O --

    def read_point(self, point: Point) -> Any:
        """Read a point with proper multi-register, endianness, and error handling."""
        with self.bus_lock:
            return self._read_point_locked(point)

    def _read_point_locked(self, point: Point) -> Any:
        reg_type = point.register_type
        address = point.modbus_address
        length = point.length_words
        dt = point.data_type

        # -- Boolean types (coils / discrete inputs) --
        if reg_type == "coil":
            result = self.client.read_coils(address, 1, slave=self.slave_id)
            if result.isError():
                raise IOError(f"Modbus error reading coil {address}: {result}")
            return result.bits[0]

        if reg_type == "discrete":
            result = self.client.read_discrete_inputs(address, 1, slave=self.slave_id)
            if result.isError():
                raise IOError(f"Modbus error reading discrete {address}: {result}")
            return result.bits[0]

        # -- Register types (holding / input) --
        if reg_type == "holding":
            result = self.client.read_holding_registers(address, length, slave=self.slave_id)
        elif reg_type == "input":
            result = self.client.read_input_registers(address, length, slave=self.slave_id)
        else:
            raise ValueError(f"Unknown register type: {reg_type}")

        if result.isError():
            raise IOError(f"Modbus error reading {reg_type} {address}: {result}")

        # -- String type --
        if dt == "string":
            raw_bytes = b""
            for reg_val in result.registers:
                raw_bytes += struct.pack(">H", reg_val)
            encoding = point.addressing.get("string_encoding", "ascii")
            return raw_bytes.decode(encoding).rstrip("\x00").strip()

        # -- Numeric types: BinaryPayloadDecoder for endianness --
        byte_order = ENDIAN_MAP[point.byte_order]
        word_order = ENDIAN_MAP[point.word_order]
        decoder = BinaryPayloadDecoder.fromRegisters(
            result.registers, byteorder=byte_order, wordorder=word_order
        )

        type_info = TYPE_INFO.get(dt)
        if not type_info or not type_info[1]:
            raise ValueError(f"Unsupported data type for decode: {dt}")

        raw = getattr(decoder, type_info[1])()

        # -- Bitfield extraction --
        if point.bitfield:
            raw_int = int(raw)
            return {
                bit_name: bool(raw_int & (1 << bit_def["bit"]))
                for bit_name, bit_def in point.bitfield.items()
            }

        # -- Enum mapping --
        if point.enum:
            return point.enum.get(int(raw), str(raw))

        # -- Scale --
        return raw * point.scale

    def write_point(self, point: Point, value: Any) -> None:
        """Write with validation, inverse scaling, and correct function codes."""
        if point.access == "read":
            raise PermissionError(
                f"Point '{point.name}' (addr {point.modbus_address}) is read-only"
            )

        if point.range is not None:
            lo, hi = point.range
            if not (lo <= float(value) <= hi):
                raise ValueError(
                    f"Value {value} out of range [{lo}, {hi}] for '{point.name}'"
                )

        with self.bus_lock:
            self._write_point_locked(point, value)

    def _write_point_locked(self, point: Point, value: Any) -> None:
        address = point.modbus_address
        dt = point.data_type
        write_fc = point.write_function_code

        # -- Coil write --
        if point.register_type == "coil":
            result = self.client.write_coil(address, bool(value), slave=self.slave_id)
            if result.isError():
                raise IOError(f"Modbus error writing coil {address}: {result}")
            return

        # -- Inverse enum mapping --
        if point.enum:
            inv = {v: k for k, v in point.enum.items()}
            if value in inv:
                value = inv[value]

        # -- Inverse scale --
        if point.scale and point.scale != 0:
            raw = value / point.scale
        else:
            raw = value

        # -- Build payload with correct endianness --
        byte_order = ENDIAN_MAP[point.byte_order]
        word_order = ENDIAN_MAP[point.word_order]
        builder = BinaryPayloadBuilder(byteorder=byte_order, wordorder=word_order)

        type_info = TYPE_INFO.get(dt)
        if not type_info or not type_info[2]:
            raise ValueError(f"Unsupported data type for write: {dt}")

        # Cast to correct Python type
        if "int" in dt:
            raw = int(raw)
        elif "float" in dt:
            raw = float(raw)

        getattr(builder, type_info[2])(raw)
        registers = builder.to_registers()

        # -- Write with correct function code --
        if write_fc == 16 or len(registers) > 1:
            result = self.client.write_registers(address, registers, slave=self.slave_id)
        else:
            result = self.client.write_register(address, registers[0], slave=self.slave_id)

        if result.isError():
            raise IOError(f"Modbus error writing register {address}: {result}")

    def read_points(self, points: list[Point]) -> dict[str, Any]:
        """Batch read — acquires bus lock once for all reads."""
        with self.bus_lock:
            return {p.name: self._read_point_locked(p) for p in points}
