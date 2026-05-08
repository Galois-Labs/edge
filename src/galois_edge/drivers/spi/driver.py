"""Generic SPI driver — interprets a YAML SPI profile at runtime.

The driver is profile-driven in exactly the same shape as
``GenericModbusDriver``: registers come from ``profile["registers"]``,
each register becomes a :class:`~galois_edge.drivers.point.Point`, and
``read_point`` / ``write_point`` decode/encode bytes per the register's
data type and the profile's ``read_command`` block.

The hard part of SPI is **wire-protocol pattern abstraction**. Datasheets
describe SPI in many slightly-different shapes; this driver covers three
of them, selected via ``read_command.pattern``:

``register_address`` (Analog Devices AD7124 family, most ADI parts)
    Wire: ``[opcode | addr] + [0x00] * length_bytes``. The ``read_opcode``
    bits and the ``write_opcode`` bits are OR'd directly into the address
    byte. ``address_bytes=1`` covers the common case; larger devices that
    need 16-bit addresses use ``address_bytes=2`` and the address is
    written big-endian after the opcode.

``start_bit`` (Microchip MCP3008 / MCP3208, similar SAR ADCs)
    Wire: ``[start_byte, channel_mux, 0x00 ...]``. ``start_byte`` is a
    fixed value (e.g. ``0x01``). ``channel_mux`` packs single-ended/diff
    flag plus channel selection in the high nibble. Result lives in the
    response bytes after the prefix; ``data_offset_bits`` says how many
    bits of the response prefix to skip before the result MSB. The reply
    on byte 0 is always discarded by the chip.

``command_byte`` (Maxim/Dallas DS18B20-style, some sensors)
    Wire: ``[command_byte, addr_byte_0, ...] + [0x00] * length_bytes``.
    Distinct command byte (e.g. ``0xBE`` for "Read Scratchpad") followed
    by the address bytes (often zero), then read bytes. Write opcodes use
    a different command byte (``0x4E`` "Write Scratchpad"). This pattern
    is the most flexible because opcode and address live in separate
    bytes; it's the fallback shape for chips that don't match the first
    two patterns.

All three patterns share the same response-decode logic (skip the
``[address_bytes + dummy_bytes]`` prefix on the MISO line, then decode
``length_bytes`` per ``data_type`` and ``byte_order``). Bitfield decode is
lifted from ``modbus_driver.py`` so single-byte status registers work
identically across protocols.
"""

from __future__ import annotations

import logging
from typing import Any

from galois_edge.drivers.base import BaseProtocolDriver
from galois_edge.drivers.point import Point
from galois_edge.drivers.spi.transport import SPIBusManager

logger = logging.getLogger(__name__)


# data_type → (byte_count, signed). int24/uint24 are 3-byte ADC reads.
_TYPE_INFO: dict[str, tuple[int, bool]] = {
    "int8": (1, True),
    "uint8": (1, False),
    "int16": (2, True),
    "uint16": (2, False),
    "int24": (3, True),
    "uint24": (3, False),
    "int32": (4, True),
    "uint32": (4, False),
}


def _decode_int(buf: bytes, dt: str, byte_order: str) -> int:
    """Decode ``buf`` as integer per ``data_type`` and ``byte_order``."""
    info = _TYPE_INFO.get(dt)
    if info is None:
        raise ValueError(f"Unsupported SPI data_type: {dt}")
    nbytes, signed = info
    if len(buf) < nbytes:
        raise ValueError(
            f"SPI decode: {dt} needs {nbytes} bytes, got {len(buf)}"
        )
    chunk = bytes(buf[:nbytes])
    order = "big" if byte_order in ("big", "msb", "be") else "little"
    return int.from_bytes(chunk, byteorder=order, signed=signed)


def _encode_int(value: int, dt: str, byte_order: str) -> bytes:
    info = _TYPE_INFO.get(dt)
    if info is None:
        raise ValueError(f"Unsupported SPI data_type for write: {dt}")
    nbytes, signed = info
    order = "big" if byte_order in ("big", "msb", "be") else "little"
    return int(value).to_bytes(nbytes, byteorder=order, signed=signed)


def _extract_bits(raw: int, offset_bits: int, length_bits: int) -> int:
    """Extract a bit-aligned field from an integer (MSB-first window).

    ``offset_bits`` counts from the MSB of the entire received word.
    ``length_bits`` is the field width. Used by ``start_bit`` pattern
    where the data spans byte boundaries (e.g. MCP3008's 10-bit result
    starts 6 bits into the third response byte counting from MSB).
    """
    # Convert to LSB-aligned via right-shift if we know total width.
    # Caller has already computed raw as a single int spanning the response
    # bytes; we just need to mask out the requested window.
    shift_right = 0  # ``offset_bits`` is interpreted relative to the MSB
    # by callers, but ``raw`` is right-justified by from_bytes — so the
    # MSB is at position ``total_bits - 1``. Callers passing a
    # ``data_offset_bits`` of 0 mean "data is right-aligned in raw".
    return (raw >> shift_right) & ((1 << length_bits) - 1)


class GenericSpiDriver(BaseProtocolDriver):
    """Profile-driven SPI driver."""

    def __init__(
        self,
        instrument_id: str,
        transport_uri: str,
        profile: dict[str, Any],
        bus_manager: SPIBusManager,
        **kwargs: Any,
    ) -> None:
        super().__init__(instrument_id, transport_uri, **kwargs)
        self.profile = profile
        self.bus_manager = bus_manager
        self.spi: Any = None
        self.bus_lock: Any = None

        # -- connection params (kwargs override profile) --
        conn = profile.get("connection", {})
        self.bus: int = int(kwargs.get("bus", conn.get("bus", 0)))
        self.device: int = int(kwargs.get("device", conn.get("device", 0)))
        self.mode: int = int(kwargs.get("mode", conn.get("mode", 0)))
        self.bits_per_word: int = int(
            kwargs.get("bits_per_word", conn.get("bits_per_word", 8))
        )
        self.max_speed_hz: int = int(
            kwargs.get("max_speed_hz", conn.get("max_speed_hz", 1_000_000))
        )
        self.cs_high: bool = bool(kwargs.get("cs_high", conn.get("cs_high", False)))
        self.lsb_first: bool = bool(
            kwargs.get("lsb_first", conn.get("lsb_first", False))
        )
        self.three_wire: bool = bool(
            kwargs.get("three_wire", conn.get("three_wire", False))
        )
        self.default_byte_order: str = conn.get("byte_order", "big")

        # -- read_command block: how the chip frames a read transaction --
        rc = profile.get("read_command", {}) or {}
        # Accept both 'pattern' and 'framing' for forward-compat.
        self.pattern: str = rc.get("pattern", rc.get("framing", "register_address"))
        self.read_opcode: int = int(rc.get("read_opcode", 0x00))
        self.write_opcode: int = int(rc.get("write_opcode", 0x00))
        self.address_bytes: int = int(rc.get("address_bytes", 1))
        self.dummy_bytes: int = int(rc.get("dummy_bytes", 0))
        self.start_byte: int = int(rc.get("start_byte", 0x01))
        # MCP-style: how to compose the channel mux byte:
        #   ``mux_template`` — int OR'd with (channel << ``mux_shift``)
        self.mux_template: int = int(rc.get("mux_template", 0x80))
        self.mux_shift: int = int(rc.get("mux_shift", 4))
        # Default identify register (chip_id / version).
        self.identify_register: str | None = rc.get("identify_register") or rc.get(
            "id_register"
        )

        # -- build Point objects from registers --
        for name, reg_def in (profile.get("registers", {}) or {}).items():
            dt = reg_def.get("data_type", "uint8")
            length_bytes = int(reg_def.get("length_bytes", _TYPE_INFO.get(dt, (1, False))[0]))
            addressing = {
                "address": reg_def["address"],
                "length_bytes": length_bytes,
                "byte_order": reg_def.get("byte_order", self.default_byte_order),
                # Per-register pattern overrides allowed (rare).
                "pattern": reg_def.get("pattern", self.pattern),
                "read_opcode": reg_def.get("read_opcode", self.read_opcode),
                "write_opcode": reg_def.get("write_opcode", self.write_opcode),
                "address_bytes": reg_def.get("address_bytes", self.address_bytes),
                "dummy_bytes": reg_def.get("dummy_bytes", self.dummy_bytes),
                "channel": reg_def.get("channel"),  # start_bit pattern only
                "data_offset_bits": int(reg_def.get("data_offset_bits", 0)),
                "data_length_bits": int(reg_def.get("data_length_bits", length_bytes * 8)),
                "expected": reg_def.get("expected"),
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

        self._commands = profile.get("commands", {}) or {}

    # -- Lifecycle --

    def connect(self) -> None:
        self.spi, self.bus_lock = self.bus_manager.get_spi(
            self.bus,
            self.device,
            mode=self.mode,
            bits_per_word=self.bits_per_word,
            max_speed_hz=self.max_speed_hz,
            cs_high=self.cs_high,
            lsb_first=self.lsb_first,
            three_wire=self.three_wire,
        )
        self._connected = True
        logger.info(
            "SPI driver connected: /dev/spidev%d.%d mode=%d speed=%d",
            self.bus,
            self.device,
            self.mode,
            self.max_speed_hz,
        )

    def disconnect(self) -> None:
        if self.spi is not None:
            self.bus_manager.release(self.bus, self.device)
            self.spi = None
            self.bus_lock = None
        self._connected = False

    def identify(self) -> str:
        identity = self.profile.get("identity", {})
        mfr = identity.get("manufacturer", "?")
        model = identity.get("model", "?")
        suffix = ""
        if self.identify_register and self.identify_register in self._points:
            try:
                value = self.read_point(self._points[self.identify_register])
                if isinstance(value, (int, float)):
                    suffix = f" ({self.identify_register}=0x{int(value):X})"
                else:
                    suffix = f" ({self.identify_register}={value})"
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("identify read failed: %s", exc)
        return f"{mfr} {model}{suffix}"

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "protocol": "spi",
            "profile": self.profile.get("identity", {}).get("model", "unknown"),
            "bus": self.bus,
            "device": self.device,
            "mode": self.mode,
            "max_speed_hz": self.max_speed_hz,
            "lsb_first": self.lsb_first,
            "three_wire": self.three_wire,
            "pattern": self.pattern,
            "registers": len(self._points),
            "writable": sum(1 for p in self._points.values() if p.access == "read_write"),
            "points": [p.to_dict() for p in self._points.values()],
            "commands": list(self._commands.keys()),
        }

    # -- Wire-protocol pattern construction --

    def _build_read_frame(self, point: Point) -> tuple[list[int], int]:
        """Construct the outgoing TX bytes for a read transaction.

        Returns ``(tx_bytes, prefix_len)`` where ``prefix_len`` is the
        number of leading MISO bytes to discard before decoding data.

        Pattern-specific construction:

        ``register_address`` and ``command_byte``
            The chip waits for the full header before driving MISO. We
            send ``header + [0x00]*dummy + [0x00]*length`` and the data
            sits in the trailing ``length`` bytes.

        ``start_bit``
            SAR-ADC chips like MCP3008 begin shifting the result out
            mid-transaction — specifically, while the *second* TX byte
            (channel mux) is still being clocked. The result therefore
            spans the second TX byte (low 2 bits) and the third TX byte
            (low 8 bits). We always send a 3-byte transaction and the
            trailing ``length_bytes`` is read off the end. With
            ``length_bytes=2`` the data occupies ``rx[1:3]`` and
            ``data_offset_bits=6`` skips the upper 6 padding bits.
        """
        a = point.addressing
        pattern = a["pattern"]
        addr = int(a["address"])
        length = int(a["length_bytes"])
        addr_bytes = int(a["address_bytes"])
        dummy_bytes = int(a["dummy_bytes"])
        read_opcode = int(a["read_opcode"])

        if pattern == "register_address":
            # First byte = read_opcode | addr (or address big-endian for >1 byte addresses)
            if addr_bytes == 1:
                header = [(read_opcode | addr) & 0xFF]
            else:
                # Multi-byte address: first byte is read_opcode, remaining are addr
                # in big-endian order.
                header = [read_opcode & 0xFF]
                header += list(addr.to_bytes(addr_bytes, byteorder="big", signed=False))
            tx = header + [0x00] * dummy_bytes + [0x00] * length
            prefix_len = len(tx) - length
            return tx, prefix_len

        if pattern == "command_byte":
            # Distinct command byte then address bytes then dummy then data.
            header = [read_opcode & 0xFF]
            if addr_bytes > 0:
                header += list(addr.to_bytes(addr_bytes, byteorder="big", signed=False))
            tx = header + [0x00] * dummy_bytes + [0x00] * length
            prefix_len = len(tx) - length
            return tx, prefix_len

        if pattern == "start_bit":
            # MCP3008-style: [start_byte, mux_template | (ch<<shift), 0x00 ...]
            # The chip outputs the result during the SECOND and THIRD TX
            # bytes — we send a frame whose trailing ``length_bytes`` bytes
            # are the response window. Total length = 1 (start) + length.
            channel = a.get("channel")
            if channel is None:
                channel = addr  # treat register address as channel index
            mux_byte = (
                int(self.mux_template) | (int(channel) << int(self.mux_shift))
            ) & 0xFF
            # Layout: [start_byte] + [mux_byte+padding (length-1 zeros)] + [final 0x00]
            # so that mux is clocked while the chip is still latching the
            # selection and the result occupies the trailing ``length`` bytes.
            tx = [int(self.start_byte) & 0xFF, mux_byte] + [0x00] * (length - 1)
            tx += [0x00] * dummy_bytes
            prefix_len = len(tx) - length
            if prefix_len < 1:
                # Defensive: there must always be at least the start byte
                # ahead of the response window.
                tx = [int(self.start_byte) & 0xFF] + tx
                prefix_len = len(tx) - length
            return tx, prefix_len

        raise ValueError(f"Unknown SPI read pattern: {pattern}")

    def _build_write_frame(self, point: Point, value_bytes: bytes) -> list[int]:
        """Construct outgoing TX bytes for a write transaction."""
        a = point.addressing
        pattern = a["pattern"]
        addr = int(a["address"])
        addr_bytes = int(a["address_bytes"])
        write_opcode = int(a["write_opcode"])

        if pattern == "register_address":
            if addr_bytes == 1:
                header = [(write_opcode | addr) & 0xFF]
            else:
                header = [write_opcode & 0xFF]
                header += list(addr.to_bytes(addr_bytes, byteorder="big", signed=False))
            return header + list(value_bytes)

        if pattern == "command_byte":
            header = [write_opcode & 0xFF]
            if addr_bytes > 0:
                header += list(addr.to_bytes(addr_bytes, byteorder="big", signed=False))
            return header + list(value_bytes)

        if pattern == "start_bit":
            raise PermissionError(
                "start_bit pattern is read-only (used by SAR ADCs); writes not supported"
            )

        raise ValueError(f"Unknown SPI write pattern: {pattern}")

    # -- Point I/O --

    def read_point(self, point: Point) -> Any:
        if self.spi is None:
            raise RuntimeError("SPI driver not connected")
        with self.bus_lock:
            return self._read_point_locked(point)

    def _read_point_locked(self, point: Point) -> Any:
        tx, prefix_len = self._build_read_frame(point)
        rx = self.spi.xfer2(list(tx))
        rx_bytes = bytes(rx[prefix_len:])

        a = point.addressing
        dt = point.data_type
        byte_order = a["byte_order"]
        length = int(a["length_bytes"])
        offset_bits = int(a.get("data_offset_bits", 0))
        length_bits = int(a.get("data_length_bits", length * 8))

        # -- Bit-level extraction (start_bit pattern with non-byte-aligned data) --
        if offset_bits or length_bits != length * 8:
            raw_word = int.from_bytes(rx_bytes[:length], byteorder="big", signed=False)
            total_bits = length * 8
            # offset_bits is measured from the MSB of the response window;
            # shift right by (total_bits - offset_bits - length_bits) to
            # right-align the field.
            shift = total_bits - offset_bits - length_bits
            if shift < 0:
                raise ValueError(
                    f"Bitfield window out of bounds: total={total_bits} "
                    f"offset={offset_bits} length={length_bits}"
                )
            mask = (1 << length_bits) - 1
            raw = (raw_word >> shift) & mask
            # Sign extension if the data type is signed.
            if dt.startswith("int") and (raw & (1 << (length_bits - 1))):
                raw -= 1 << length_bits
        else:
            raw = _decode_int(rx_bytes, dt, byte_order)

        # -- Bitfield decode --
        if point.bitfield:
            return self._decode_bitfield(raw, point.bitfield)

        # -- Enum mapping --
        if point.enum:
            return point.enum.get(int(raw), int(raw))

        # -- Scale --
        return raw * point.scale

    @staticmethod
    def _decode_bitfield(raw: int, bitfield: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """Decode a register integer into named bit fields.

        Supports both single-bit (``bit: 7``) and multi-bit (``bits: [0,1,2]``)
        fields. Lifted from ``modbus_driver.py`` to keep the abstraction
        identical across protocols.
        """
        out: dict[str, Any] = {}
        for bit_name, bit_def in bitfield.items():
            if "bit" in bit_def:
                bit = int(bit_def["bit"])
                out[bit_name] = bool(raw & (1 << bit))
            elif "bits" in bit_def:
                bits = bit_def["bits"]
                if not bits:
                    continue
                lo = min(int(b) for b in bits)
                hi = max(int(b) for b in bits)
                width = hi - lo + 1
                value = (raw >> lo) & ((1 << width) - 1)
                # Optional enum decode for multi-bit fields
                if "enum" in bit_def and isinstance(bit_def["enum"], dict):
                    out[bit_name] = bit_def["enum"].get(value, value)
                else:
                    out[bit_name] = value
            else:
                # Field without bit info — skip silently
                continue
        return out

    def write_point(self, point: Point, value: Any) -> None:
        if self.spi is None:
            raise RuntimeError("SPI driver not connected")
        if point.access == "read":
            raise PermissionError(
                f"Point '{point.name}' is read-only on the wire"
            )
        if point.range is not None:
            lo, hi = point.range
            if not (lo <= float(value) <= hi):
                raise ValueError(
                    f"Value {value} out of range [{lo}, {hi}] for '{point.name}'"
                )

        # Inverse enum
        if point.enum:
            inv = {v: k for k, v in point.enum.items()}
            if value in inv:
                value = inv[value]

        # Inverse scale
        if point.scale and point.scale != 0:
            scaled = value / point.scale
        else:
            scaled = value

        dt = point.data_type
        byte_order = point.addressing["byte_order"]
        if "int" in dt:
            value_bytes = _encode_int(int(round(scaled)), dt, byte_order)
        else:
            raise ValueError(f"Unsupported write type: {dt}")

        tx = self._build_write_frame(point, value_bytes)
        with self.bus_lock:
            self.spi.xfer2(list(tx))

    def read_points(self, points: list[Point]) -> dict[str, Any]:
        """Batch read — single lock acquisition for all transactions."""
        if self.spi is None:
            raise RuntimeError("SPI driver not connected")
        with self.bus_lock:
            return {p.name: self._read_point_locked(p) for p in points}
