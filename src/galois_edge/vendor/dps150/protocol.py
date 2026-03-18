"""FNIRSI DPS-150 binary serial protocol implementation.

Pure logic — no I/O dependencies. Handles packet construction,
checksum calculation, and response parsing.
"""

from __future__ import annotations

import struct
from typing import Any

# ---------------------------------------------------------------------------
# Frame headers
# ---------------------------------------------------------------------------
HEADER_INPUT = 0xF0   # RX from device
HEADER_OUTPUT = 0xF1  # TX to device

# ---------------------------------------------------------------------------
# Command groups
# ---------------------------------------------------------------------------
CMD_GET = 0xA1
CMD_BAUD = 0xB0
CMD_SET = 0xB1
CMD_SESSION = 0xC1

# ---------------------------------------------------------------------------
# Register addresses — float-valued
# ---------------------------------------------------------------------------
VOLTAGE_SET = 193          # 0xC1
CURRENT_SET = 194          # 0xC2

GROUP1_VOLTAGE_SET = 197   # 0xC5
GROUP1_CURRENT_SET = 198
GROUP2_VOLTAGE_SET = 199
GROUP2_CURRENT_SET = 200
GROUP3_VOLTAGE_SET = 201
GROUP3_CURRENT_SET = 202
GROUP4_VOLTAGE_SET = 203
GROUP4_CURRENT_SET = 204
GROUP5_VOLTAGE_SET = 205
GROUP5_CURRENT_SET = 206
GROUP6_VOLTAGE_SET = 207
GROUP6_CURRENT_SET = 208

# Protection thresholds (float)
OVP = 209   # Over-voltage protection
OCP = 210   # Over-current protection
OPP = 211   # Over-power protection
OTP = 212   # Over-temperature protection
LVP = 213   # Low-voltage protection

# ---------------------------------------------------------------------------
# Register addresses — byte-valued
# ---------------------------------------------------------------------------
BRIGHTNESS = 214
VOLUME = 215
METERING_ENABLE = 216
OUTPUT_ENABLE = 219

# ---------------------------------------------------------------------------
# Info / query registers
# ---------------------------------------------------------------------------
MODEL_NAME = 222
HARDWARE_VERSION = 223
FIRMWARE_VERSION = 224
ALL = 255

# ---------------------------------------------------------------------------
# Baud rate mapping (value sent in the BAUD command data byte)
# ---------------------------------------------------------------------------
BAUD_RATES = {9600: 1, 19200: 2, 38400: 3, 57600: 4, 115200: 5}

# ---------------------------------------------------------------------------
# Protection state lookup (index → label)
# ---------------------------------------------------------------------------
PROTECTION_STATES = ["", "OVP", "OCP", "OPP", "OTP", "LVP", "REP"]


# ===================================================================
# Packet building
# ===================================================================

def calculate_checksum(register: int, data: bytes | bytearray) -> int:
    """Compute the single-byte checksum for a DPS-150 packet.

    checksum = (register + len(data) + sum(data)) & 0xFF
    """
    return (register + len(data) + sum(data)) & 0xFF


def build_packet(header: int, command: int, register: int,
                 data: int | bytes | bytearray) -> bytes:
    """Build a complete DPS-150 packet ready to send over the wire.

    If *data* is a single int it is wrapped as ``bytes([data])``.
    """
    if isinstance(data, int):
        data = bytes([data])
    data = bytes(data)
    chk = calculate_checksum(register, data)
    return bytes([header, command, register, len(data), *data, chk])


def build_float_packet(header: int, command: int, register: int,
                       value: float) -> bytes:
    """Build a packet with a little-endian IEEE-754 float32 payload."""
    return build_packet(header, command, register,
                        struct.pack('<f', value))


# Pre-built session / baud packets
SESSION_ENABLE = build_packet(HEADER_OUTPUT, CMD_SESSION, 0, 1)
SESSION_DISABLE = build_packet(HEADER_OUTPUT, CMD_SESSION, 0, 0)
BAUD_SELECT_115200 = build_packet(HEADER_OUTPUT, CMD_BAUD, 0, 5)


# ===================================================================
# Response parsing
# ===================================================================

def parse_register(register: int, payload: bytes) -> dict[str, Any]:
    """Decode a response payload for the given register.

    Returns a dict with snake_case keys matching the device state fields.
    """
    if register == 192:  # 0xC0 — input voltage
        return {"input_voltage": _f32(payload, 0)}

    if register == 195:  # 0xC3 — output V / I / P
        return {
            "output_voltage": _f32(payload, 0),
            "output_current": _f32(payload, 4),
            "output_power": _f32(payload, 8),
        }

    if register == 196:  # 0xC4 — temperature
        return {"temperature": _f32(payload, 0)}

    if register == 217:  # 0xD9 — output capacity (Ah)
        return {"output_capacity": _f32(payload, 0)}

    if register == 218:  # 0xDA — output energy (Wh)
        return {"output_energy": _f32(payload, 0)}

    if register == 219:  # 0xDB — output on/off
        return {"output_closed": payload[0] == 1}

    if register == 220:  # 0xDC — protection state
        idx = payload[0]
        return {"protection_state": PROTECTION_STATES[idx] if idx < len(PROTECTION_STATES) else ""}

    if register == 221:  # 0xDD — CC / CV mode
        return {"mode": "CC" if payload[0] == 0 else "CV"}

    if register == 222:  # 0xDE — model name
        return {"model_name": payload.decode("ascii", errors="replace")}

    if register == 223:  # 0xDF — hardware version
        return {"hardware_version": payload.decode("ascii", errors="replace")}

    if register == 224:  # 0xE0 — firmware version
        return {"firmware_version": payload.decode("ascii", errors="replace")}

    if register == 226:  # 0xE2 — upper limit voltage
        return {"upper_limit_voltage": _f32(payload, 0)}

    if register == 227:  # 0xE3 — upper limit current
        return {"upper_limit_current": _f32(payload, 0)}

    if register == 255:  # 0xFF — full memory dump (139 bytes)
        return _parse_full_dump(payload)

    return {}


def _parse_full_dump(p: bytes) -> dict[str, Any]:
    """Decode the 139-byte ALL register response."""
    return {
        "input_voltage": _f32(p, 0),
        "set_voltage": _f32(p, 4),
        "set_current": _f32(p, 8),
        "output_voltage": _f32(p, 12),
        "output_current": _f32(p, 16),
        "output_power": _f32(p, 20),
        "temperature": _f32(p, 24),

        "group1_set_voltage": _f32(p, 28),
        "group1_set_current": _f32(p, 32),
        "group2_set_voltage": _f32(p, 36),
        "group2_set_current": _f32(p, 40),
        "group3_set_voltage": _f32(p, 44),
        "group3_set_current": _f32(p, 48),
        "group4_set_voltage": _f32(p, 52),
        "group4_set_current": _f32(p, 56),
        "group5_set_voltage": _f32(p, 60),
        "group5_set_current": _f32(p, 64),
        "group6_set_voltage": _f32(p, 68),
        "group6_set_current": _f32(p, 72),

        "over_voltage_protection": _f32(p, 76),
        "over_current_protection": _f32(p, 80),
        "over_power_protection": _f32(p, 84),
        "over_temperature_protection": _f32(p, 88),
        "low_voltage_protection": _f32(p, 92),

        "brightness": p[96],
        "volume": p[97],
        "metering_closed": p[98] == 0,

        "output_capacity": _f32(p, 99),
        "output_energy": _f32(p, 103),

        "output_closed": p[107] == 1,
        "protection_state": PROTECTION_STATES[p[108]] if p[108] < len(PROTECTION_STATES) else "",
        "mode": "CC" if p[109] == 0 else "CV",

        "upper_limit_voltage": _f32(p, 111),
        "upper_limit_current": _f32(p, 115),
    }


def _f32(data: bytes, offset: int) -> float:
    """Unpack a little-endian float32 from *data* at *offset*."""
    return struct.unpack_from('<f', data, offset)[0]


# ===================================================================
# Stream reassembly
# ===================================================================

class PacketBuffer:
    """Accumulates raw serial bytes and extracts validated DPS-150 packets.

    Port of the inline buffer-scanning logic in the JS ``startReader``.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes | bytearray) -> list[tuple[int, int, int, bytes]]:
        """Append *data* and return any complete, checksum-valid packets.

        Each returned tuple is ``(header, command, register, payload)``.
        """
        self._buf.extend(data)
        packets: list[tuple[int, int, int, bytes]] = []
        i = 0
        while i < len(self._buf) - 5:  # need at least 6 bytes for a minimal packet
            if self._buf[i] == HEADER_INPUT and self._buf[i + 1] == CMD_GET:
                reg = self._buf[i + 2]
                dlen = self._buf[i + 3]
                end = i + 4 + dlen  # index of checksum byte
                if end >= len(self._buf):
                    break  # wait for more data
                payload = bytes(self._buf[i + 4 : i + 4 + dlen])
                chk_byte = self._buf[end]
                expected = calculate_checksum(reg, payload)
                if chk_byte != expected:
                    i += 1
                    continue  # skip this byte, try next position
                packets.append((self._buf[i], self._buf[i + 1], reg, payload))
                # advance past the consumed packet (header..checksum)
                self._buf = self._buf[end + 1:]
                i = 0
                continue
            i += 1
        # trim scanned non-header bytes from the front
        if i > 0 and not packets:
            self._buf = self._buf[i:]
        return packets
