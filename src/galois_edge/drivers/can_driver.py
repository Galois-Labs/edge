"""Generic CAN driver that interprets a CAN profile YAML at runtime.

This is the PRIMARY CAN driver path.  No generated Python code needed — the
driver reads the YAML profile and uses bit-level extraction/packing for
signal encoding within CAN frames.
"""

from __future__ import annotations

import logging
from typing import Any

from galois_edge.drivers.base import BaseProtocolDriver
from galois_edge.drivers.can_transport import CANBusManager, CAN_AVAILABLE
from galois_edge.drivers.point import Point

logger = logging.getLogger(__name__)

# Guarded import — python-can is optional
if CAN_AVAILABLE:
    try:
        import can as python_can
    except ImportError:
        python_can = None  # type: ignore[assignment]
else:
    python_can = None  # type: ignore[assignment]


class GenericCANDriver(BaseProtocolDriver):
    """Loads a CAN profile YAML and interprets it at runtime."""

    def __init__(
        self,
        instrument_id: str,
        transport_uri: str,
        profile: dict[str, Any],
        bus_manager: CANBusManager,
        **kwargs: Any,
    ) -> None:
        super().__init__(instrument_id, transport_uri, **kwargs)
        self.profile = profile
        self.bus_manager = bus_manager
        self.bus: Any = None
        self.bus_lock: Any = None

        conn = profile.get("connection", {})
        self.channel: str = kwargs.get(
            "channel", conn.get("channel", "can0")
        )
        self.interface: str = kwargs.get(
            "interface", conn.get("interface", "socketcan")
        )
        self.bitrate: int = int(kwargs.get(
            "bitrate", conn.get("bitrate", 500000)
        ))
        self.recv_timeout: float = float(kwargs.get(
            "recv_timeout", conn.get("recv_timeout", 1.0)
        ))

        # Mapping from signal name → message definition (for CAN ID lookup)
        self._signal_message_map: dict[str, dict[str, Any]] = {}

        # Build Point objects from YAML messages → signals
        for msg_name, msg_def in profile.get("messages", {}).items():
            can_id = msg_def.get("can_id", 0)
            dlc = msg_def.get("dlc", 8)
            direction = msg_def.get("direction", "rx")

            for sig_name, sig_def in msg_def.get("signals", {}).items():
                access = "read" if direction == "rx" else "read_write"
                dt = "int16" if sig_def.get("signed", False) else "uint16"
                if sig_def.get("bit_length", 16) > 16:
                    dt = "int32" if sig_def.get("signed", False) else "uint32"

                addressing = {
                    "can_id": can_id,
                    "dlc": dlc,
                    "direction": direction,
                    "start_bit": sig_def.get("start_bit", 0),
                    "bit_length": sig_def.get("bit_length", 8),
                    "byte_order": sig_def.get("byte_order", "little_endian"),
                    "signed": sig_def.get("signed", False),
                    "offset": float(sig_def.get("offset", 0)),
                }

                range_val = None
                if "range" in sig_def:
                    r = sig_def["range"]
                    range_val = (float(r[0]), float(r[1]))

                self._points[sig_name] = Point(
                    name=sig_name,
                    data_type=dt,
                    access=access,
                    scale=float(sig_def.get("scale", 1.0)),
                    unit=sig_def.get("unit", ""),
                    range=range_val,
                    enum=sig_def.get("enum"),
                    description=sig_def.get("description", ""),
                    addressing=addressing,
                )

                self._signal_message_map[sig_name] = msg_def

        # Commands from YAML
        self._commands = profile.get("commands", {})

    # -- Lifecycle --

    def connect(self) -> None:
        self.bus, self.bus_lock = self.bus_manager.get_bus(
            channel=self.channel,
            bitrate=self.bitrate,
            interface=self.interface,
        )
        self._connected = True
        logger.info(
            "CAN driver connected: %s (channel %s, bitrate %d)",
            self.transport_uri, self.channel, self.bitrate,
        )

    def disconnect(self) -> None:
        if self.bus:
            self.bus_manager.release(
                channel=self.channel,
                bitrate=self.bitrate,
                interface=self.interface,
            )
            self.bus = None
            self.bus_lock = None
        self._connected = False

    def identify(self) -> str:
        identity = self.profile.get("identity", {})
        mfr = identity.get("manufacturer", "?")
        model = identity.get("model", "?")
        return f"{mfr} {model} @ {self.channel} (bitrate {self.bitrate})"

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "commands": list(self._commands.keys()),
            "points": [p.to_dict() for p in self._points.values()],
            "protocol": "can",
            "profile": self.profile.get("identity", {}).get("model", "unknown"),
            "signals": len(self._points),
            "writable": sum(1 for p in self._points.values() if p.access == "read_write"),
        }

    # -- Bit extraction / packing helpers --

    @staticmethod
    def _extract_signal(
        data: bytes,
        start_bit: int,
        bit_length: int,
        byte_order: str,
        signed: bool,
    ) -> int:
        """Extract a signal value from CAN frame data bytes.

        For little-endian (Intel) byte order, ``start_bit`` is the LSB
        position in a flat bit array (bit 0 = byte 0 bit 0).

        For big-endian (Motorola) byte order, ``start_bit`` is the MSB
        position using Motorola bit numbering (bit 7 = byte 0 bit 7).
        """
        if byte_order == "little_endian":
            # Intel byte order: straightforward contiguous bit extraction
            raw = int.from_bytes(data, byteorder="little")
            mask = (1 << bit_length) - 1
            value = (raw >> start_bit) & mask
        else:
            # Motorola byte order: start_bit is MSB position
            raw = int.from_bytes(data, byteorder="big")
            total_bits = len(data) * 8
            shift = total_bits - start_bit - bit_length
            if shift < 0:
                shift = 0
            mask = (1 << bit_length) - 1
            value = (raw >> shift) & mask

        # Apply sign extension if needed
        if signed and (value & (1 << (bit_length - 1))):
            value -= 1 << bit_length

        return value

    @staticmethod
    def _pack_signal(
        value: int,
        start_bit: int,
        bit_length: int,
        byte_order: str,
        dlc: int,
    ) -> bytes:
        """Pack a signal value into CAN frame data bytes.

        Returns ``dlc`` bytes with the signal placed at the correct
        bit position.  Other bits are zero.
        """
        mask = (1 << bit_length) - 1
        # Clamp to unsigned range for packing
        raw_val = value & mask

        if byte_order == "little_endian":
            frame_int = raw_val << start_bit
            return frame_int.to_bytes(dlc, byteorder="little")
        else:
            total_bits = dlc * 8
            shift = total_bits - start_bit - bit_length
            if shift < 0:
                shift = 0
            frame_int = raw_val << shift
            return frame_int.to_bytes(dlc, byteorder="big")

    # -- Point I/O --

    def read_point(self, point: Point) -> Any:
        """Read a CAN signal by receiving a frame and extracting the signal bits."""
        with self.bus_lock:
            return self._read_point_locked(point)

    def _read_point_locked(self, point: Point) -> Any:
        addr = point.addressing
        can_id = addr["can_id"]

        # Receive frames until we get one matching our CAN ID
        msg = self.bus.recv(timeout=self.recv_timeout)
        if msg is None:
            raise IOError(
                f"CAN receive timeout waiting for ID 0x{can_id:03X}"
            )

        # If the received message doesn't match, keep trying briefly
        deadline_attempts = 10
        while msg.arbitration_id != can_id and deadline_attempts > 0:
            msg = self.bus.recv(timeout=self.recv_timeout)
            if msg is None:
                raise IOError(
                    f"CAN receive timeout waiting for ID 0x{can_id:03X}"
                )
            deadline_attempts -= 1

        if msg.arbitration_id != can_id:
            raise IOError(
                f"Did not receive CAN frame with ID 0x{can_id:03X}"
            )

        raw_int = self._extract_signal(
            msg.data,
            addr["start_bit"],
            addr["bit_length"],
            addr["byte_order"],
            addr["signed"],
        )

        # Enum mapping
        if point.enum:
            return point.enum.get(int(raw_int), str(raw_int))

        # Scale + offset: engineering = raw * scale + offset
        offset = float(addr.get("offset", 0))
        return raw_int * point.scale + offset

    def write_point(self, point: Point, value: Any) -> None:
        """Encode a value into signal bits and send a CAN frame."""
        if point.access == "read":
            raise PermissionError(
                f"Point '{point.name}' (CAN ID 0x{point.addressing.get('can_id', 0):03X}) "
                f"is read-only"
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
        addr = point.addressing
        can_id = addr["can_id"]
        dlc = addr.get("dlc", 8)

        # Inverse enum mapping
        if point.enum:
            inv = {v: k for k, v in point.enum.items()}
            if value in inv:
                value = inv[value]

        # Inverse scale + offset: raw = round((engineering - offset) / scale)
        offset = float(addr.get("offset", 0))
        scale = point.scale if point.scale != 0 else 1.0
        raw_int = round((float(value) - offset) / scale)

        data = self._pack_signal(
            raw_int,
            addr["start_bit"],
            addr["bit_length"],
            addr["byte_order"],
            dlc,
        )

        msg = python_can.Message(
            arbitration_id=can_id,
            data=data,
            is_extended_id=can_id > 0x7FF,
        )
        self.bus.send(msg)

    def read_points(self, points: list[Point]) -> dict[str, Any]:
        """Batch read — acquires bus lock once for all reads."""
        with self.bus_lock:
            return {p.name: self._read_point_locked(p) for p in points}
