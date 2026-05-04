"""Generic serial transport with declarative framing and checksums.

Provides the building blocks the GenericSerialDriver uses to talk to
arbitrary RS-232 / RS-485 / USB-CDC devices whose protocols are
described in YAML:

- Framer: turns a payload into wire bytes and reads framed responses
  back from a stream (line, length-prefix, STX/ETX, fixed-length, raw).
- Checksum: pluggable algorithms (xor8, sum8, crc8, crc16-ccitt,
  crc16-modbus). Optionally appended on send and verified on receive.
- SerialBusManager: pools pyserial.Serial handles by physical bus key
  so RS-485 multi-drop devices share one port + one lock, mirroring
  ModbusBusManager.

The transport is deliberately codec-only: no command catalog, no point
model. GenericSerialDriver layers profile-driven semantics on top.
"""

from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
import re
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlparse

import serial  # pyserial

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# pyserial constant maps
# ---------------------------------------------------------------------------

PARITY_MAP = {
    "none": serial.PARITY_NONE,
    "n": serial.PARITY_NONE,
    "even": serial.PARITY_EVEN,
    "e": serial.PARITY_EVEN,
    "odd": serial.PARITY_ODD,
    "o": serial.PARITY_ODD,
    "mark": serial.PARITY_MARK,
    "space": serial.PARITY_SPACE,
}

STOPBITS_MAP = {
    1: serial.STOPBITS_ONE,
    1.0: serial.STOPBITS_ONE,
    1.5: serial.STOPBITS_ONE_POINT_FIVE,
    2: serial.STOPBITS_TWO,
    2.0: serial.STOPBITS_TWO,
}

DATABITS_MAP = {
    5: serial.FIVEBITS,
    6: serial.SIXBITS,
    7: serial.SEVENBITS,
    8: serial.EIGHTBITS,
}


def normalize_parity(name: str) -> str:
    key = (name or "none").strip().lower()
    if key not in PARITY_MAP:
        raise ValueError(f"Unknown parity: {name!r}")
    return PARITY_MAP[key]


# ---------------------------------------------------------------------------
# Reader protocol — abstracts pyserial.Serial for testability
# ---------------------------------------------------------------------------

class ByteReader(Protocol):
    """Minimal interface a Framer needs to consume a response stream."""

    def read(self, size: int) -> bytes: ...
    def read_until(self, terminator: bytes, size: int | None = None) -> bytes: ...


# ---------------------------------------------------------------------------
# Checksum algorithms
# ---------------------------------------------------------------------------

class Checksum(ABC):
    """Strategy for computing a checksum over arbitrary bytes."""

    width: int  # bytes appended/expected

    @abstractmethod
    def compute(self, data: bytes) -> bytes:
        """Return the checksum for ``data`` as raw bytes."""


class _Xor8(Checksum):
    width = 1

    def compute(self, data: bytes) -> bytes:
        x = 0
        for b in data:
            x ^= b
        return bytes([x & 0xFF])


class _Sum8(Checksum):
    width = 1

    def compute(self, data: bytes) -> bytes:
        return bytes([sum(data) & 0xFF])


class _Crc16Modbus(Checksum):
    """CRC-16/MODBUS — poly 0xA001, init 0xFFFF, little-endian on wire."""

    width = 2

    def compute(self, data: bytes) -> bytes:
        crc = 0xFFFF
        for b in data:
            crc ^= b
            for _ in range(8):
                if crc & 1:
                    crc = (crc >> 1) ^ 0xA001
                else:
                    crc >>= 1
        return bytes([crc & 0xFF, (crc >> 8) & 0xFF])  # little-endian


class _Crc16Ccitt(Checksum):
    """CRC-16/CCITT-FALSE — poly 0x1021, init 0xFFFF, big-endian on wire."""

    width = 2

    def compute(self, data: bytes) -> bytes:
        crc = 0xFFFF
        for b in data:
            crc ^= b << 8
            for _ in range(8):
                if crc & 0x8000:
                    crc = ((crc << 1) ^ 0x1021) & 0xFFFF
                else:
                    crc = (crc << 1) & 0xFFFF
        return bytes([(crc >> 8) & 0xFF, crc & 0xFF])  # big-endian


_CHECKSUMS: dict[str, type[Checksum]] = {
    "xor8": _Xor8,
    "sum8": _Sum8,
    "crc16_modbus": _Crc16Modbus,
    "crc16_ccitt": _Crc16Ccitt,
}


def build_checksum(spec: dict[str, Any] | None) -> tuple[Checksum | None, dict[str, Any]]:
    """Return (Checksum or None, normalized spec).

    ``spec`` is the YAML dict (e.g. ``{type: crc16_modbus, append: true,
    verify: true}``). ``None`` or ``{type: none}`` disables checksumming.
    """
    if not spec:
        return None, {"type": "none", "append": False, "verify": False}
    ctype = (spec.get("type") or "none").lower()
    if ctype == "none":
        return None, {"type": "none", "append": False, "verify": False}
    cls = _CHECKSUMS.get(ctype)
    if cls is None:
        raise ValueError(f"Unknown checksum type: {ctype!r}")
    normalized = {
        "type": ctype,
        "append": bool(spec.get("append", True)),
        "verify": bool(spec.get("verify", True)),
    }
    return cls(), normalized


# ---------------------------------------------------------------------------
# Framers
# ---------------------------------------------------------------------------

@dataclass
class FrameSpec:
    """Parsed framing specification for one direction (request or response)."""

    framing: str = "line"
    terminator: bytes = b"\n"
    stx: int | None = None
    etx: int | None = None
    length_offset: int = 0
    length_size: int = 1
    length_endian: str = "big"
    length_includes_self: bool = False
    length_includes_header: bool = False
    fixed_length: int | None = None
    max_response_bytes: int = 65536  # safety cap


def _coerce_byte(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value & 0xFF
    if isinstance(value, str):
        s = value.strip()
        if s.startswith(("0x", "0X")):
            return int(s, 16) & 0xFF
        if len(s) == 1:
            return ord(s)
        return int(s) & 0xFF
    raise ValueError(f"Cannot coerce to byte: {value!r}")


_TERMINATOR_ESCAPE_MAP = {
    "\\r": "\r",
    "\\n": "\n",
    "\\t": "\t",
    "\\0": "\x00",
    "\\\\": "\\",
}


def _coerce_terminator(value: Any) -> bytes:
    if value is None:
        return b"\n"
    if isinstance(value, bytes):
        return value
    if not isinstance(value, str):
        raise ValueError(f"Cannot coerce terminator: {value!r}")
    # Handle YAML single-quoted form where escapes arrive as literal backslashes
    # (e.g. '\\r\\n'). Already-decoded forms (e.g. '\r\n') pass through unchanged.
    out = re.sub(
        r"\\[rnt0\\]",
        lambda m: _TERMINATOR_ESCAPE_MAP[m.group(0)],
        value,
    )
    return out.encode("latin-1")


def parse_frame_spec(spec: dict[str, Any] | None) -> FrameSpec:
    """Build a FrameSpec from a YAML dict. Missing fields use safe defaults."""
    if not spec:
        return FrameSpec()
    framing = (spec.get("framing") or "line").lower()
    fs = FrameSpec(framing=framing)
    if framing == "line":
        fs.terminator = _coerce_terminator(spec.get("terminator", "\n"))
    elif framing == "stx_etx":
        fs.stx = _coerce_byte(spec.get("stx", 0x02))
        fs.etx = _coerce_byte(spec.get("etx", 0x03))
    elif framing == "length_prefix":
        lf = spec.get("length_field") or {}
        fs.length_offset = int(lf.get("offset", 0))
        fs.length_size = int(lf.get("size", 1))
        fs.length_endian = (lf.get("endian") or "big").lower()
        fs.length_includes_self = bool(lf.get("includes_self", False))
        fs.length_includes_header = bool(lf.get("includes_header", False))
    elif framing == "fixed":
        fs.fixed_length = int(spec["length"])
    elif framing == "raw":
        # caller decides when read is complete (uses inter-char timeout)
        pass
    else:
        raise ValueError(f"Unknown framing: {framing!r}")
    fs.max_response_bytes = int(spec.get("max_response_bytes", 65536))
    return fs


class Framer(ABC):
    """Strategy for framing outgoing payloads and reading framed responses."""

    def __init__(self, spec: FrameSpec, checksum: Checksum | None, checksum_cfg: dict[str, Any]):
        self.spec = spec
        self.checksum = checksum
        self.checksum_cfg = checksum_cfg

    def frame(self, payload: bytes) -> bytes:
        """Wrap an outgoing payload according to the framing rules."""
        if self.checksum is not None and self.checksum_cfg.get("append"):
            payload = payload + self.checksum.compute(payload)
        return self._wrap(payload)

    def validate_for_send(self) -> None:
        """Hook for framers to reject configurations that only work on receive.

        Called by the driver immediately after constructing a request-side
        framer so misconfigurations surface at profile load time rather than
        on the first transaction. Default: no-op.
        """
        return None

    def unframe(self, reader: ByteReader) -> bytes:
        """Read one full frame and return the inner payload (post-checksum strip)."""
        raw = self._read_frame(reader)
        if self.checksum is not None and self.checksum_cfg.get("verify"):
            w = self.checksum.width
            if len(raw) < w:
                raise ValueError(f"Frame too short for checksum: {len(raw)} < {w}")
            payload, observed = raw[:-w], raw[-w:]
            expected = self.checksum.compute(payload)
            if observed != expected:
                raise ValueError(
                    f"Checksum mismatch: expected {expected.hex()}, got {observed.hex()}"
                )
            return payload
        return raw

    @abstractmethod
    def _wrap(self, payload: bytes) -> bytes: ...

    @abstractmethod
    def _read_frame(self, reader: ByteReader) -> bytes: ...


class _LineFramer(Framer):
    def _wrap(self, payload: bytes) -> bytes:
        return payload + self.spec.terminator

    def _read_frame(self, reader: ByteReader) -> bytes:
        data = reader.read_until(self.spec.terminator, self.spec.max_response_bytes)
        if not data.endswith(self.spec.terminator):
            raise TimeoutError(
                f"Line framer: terminator {self.spec.terminator!r} not received "
                f"(read {len(data)} bytes)"
            )
        return data[: -len(self.spec.terminator)]


class _StxEtxFramer(Framer):
    def _wrap(self, payload: bytes) -> bytes:
        return bytes([self.spec.stx]) + payload + bytes([self.spec.etx])

    def _read_frame(self, reader: ByteReader) -> bytes:
        # discard any noise until STX
        budget = self.spec.max_response_bytes
        while budget > 0:
            b = reader.read(1)
            if not b:
                raise TimeoutError("STX/ETX framer: no STX received")
            if b[0] == self.spec.stx:
                break
            budget -= 1
        # accumulate until ETX
        data = reader.read_until(bytes([self.spec.etx]), budget)
        if not data.endswith(bytes([self.spec.etx])):
            raise TimeoutError("STX/ETX framer: no ETX received")
        return data[:-1]


class _LengthPrefixFramer(Framer):
    def validate_for_send(self) -> None:
        if self.spec.length_offset != 0:
            raise ValueError(
                "length_prefix outgoing framing requires offset=0; for "
                "protocols with a header before the length field, set "
                "framing: raw on the request side and include the header in "
                "request_bytes."
            )

    def _wrap(self, payload: bytes) -> bytes:
        # Outgoing framing for length-prefix requires the caller to provide
        # a payload that already encodes the length byte(s) at the right
        # offset, OR we can prepend them. We choose to PREPEND so the
        # request template can stay terse.
        body_len = len(payload)
        if self.spec.length_includes_self:
            body_len += self.spec.length_size
        if self.spec.length_includes_header:
            body_len += self.spec.length_offset
        prefix = self._encode_length(body_len)
        # offset>0 means there's a static header before the length field;
        # outgoing wrap can't fabricate that, so we don't support it on send.
        if self.spec.length_offset != 0:
            raise NotImplementedError(
                "length_prefix outgoing framing with offset>0 is not auto-wrapped; "
                "include the header bytes in the request template and set "
                "framing: raw on the request side."
            )
        return prefix + payload

    def _encode_length(self, n: int) -> bytes:
        return n.to_bytes(self.spec.length_size, byteorder=self.spec.length_endian, signed=False)

    def _read_frame(self, reader: ByteReader) -> bytes:
        # Read fixed header + length field, then declared body
        header = reader.read(self.spec.length_offset + self.spec.length_size)
        if len(header) < self.spec.length_offset + self.spec.length_size:
            raise TimeoutError("length_prefix framer: header truncated")
        len_bytes = header[self.spec.length_offset : self.spec.length_offset + self.spec.length_size]
        declared = int.from_bytes(len_bytes, byteorder=self.spec.length_endian, signed=False)
        body_len = declared
        if self.spec.length_includes_self:
            body_len -= self.spec.length_size
        if self.spec.length_includes_header:
            body_len -= self.spec.length_offset
        if body_len < 0 or body_len > self.spec.max_response_bytes:
            raise ValueError(f"length_prefix framer: implausible length {body_len}")
        body = reader.read(body_len)
        if len(body) < body_len:
            raise TimeoutError(
                f"length_prefix framer: body truncated ({len(body)}/{body_len})"
            )
        # Return header (minus length field) + body so checksum verify covers it
        # We strip just the length bytes; any pre-length header is data.
        keep_header = header[: self.spec.length_offset]
        return keep_header + body


class _FixedFramer(Framer):
    def _wrap(self, payload: bytes) -> bytes:
        if self.spec.fixed_length is None:
            return payload
        if len(payload) > self.spec.fixed_length:
            raise ValueError(
                f"fixed framer: payload {len(payload)} exceeds {self.spec.fixed_length}"
            )
        return payload.ljust(self.spec.fixed_length, b"\x00")

    def _read_frame(self, reader: ByteReader) -> bytes:
        n = self.spec.fixed_length or 0
        data = reader.read(n)
        if len(data) < n:
            raise TimeoutError(f"fixed framer: got {len(data)}/{n} bytes")
        return data


class _RawFramer(Framer):
    """No framing — write payload as-is, read until inter-char timeout."""

    def _wrap(self, payload: bytes) -> bytes:
        return payload

    def _read_frame(self, reader: ByteReader) -> bytes:
        # Read whatever fits in the buffer up to the cap.
        return reader.read(self.spec.max_response_bytes)


_FRAMERS: dict[str, type[Framer]] = {
    "line": _LineFramer,
    "stx_etx": _StxEtxFramer,
    "length_prefix": _LengthPrefixFramer,
    "fixed": _FixedFramer,
    "raw": _RawFramer,
}


def build_framer(
    spec: dict[str, Any] | None,
    checksum_spec: dict[str, Any] | None = None,
) -> Framer:
    fs = parse_frame_spec(spec)
    cls = _FRAMERS.get(fs.framing)
    if cls is None:
        raise ValueError(f"Unknown framing: {fs.framing}")
    cs, cs_cfg = build_checksum(checksum_spec)
    return cls(fs, cs, cs_cfg)


# ---------------------------------------------------------------------------
# SerialTransport — pyserial wrapper presenting the ByteReader interface
# ---------------------------------------------------------------------------

@dataclass
class SerialSettings:
    port: str
    baudrate: int = 9600
    parity: str = "none"
    databits: int = 8
    stopbits: float = 1
    flow_control: str = "none"  # none | rtscts | xonxoff | dsrdtr
    timeout: float = 1.0
    write_timeout: float = 1.0
    inter_char_timeout: float | None = None
    flush_before_write: bool = True
    rs485_mode: dict[str, Any] | None = None

    def open(self) -> serial.Serial:
        """Open the underlying pyserial.Serial handle.

        Handles Windows COM>=10 normalization to ``\\\\.\\COMn`` form and
        optional RS-485 manual DE/RE control via ``rs485_mode`` (passed
        through to ``serial.rs485.RS485Settings``). Unsupported RS-485
        backends log a warning and continue with the port still open.
        """
        port = self.port
        # Windows COM>=10 needs \\.\COM10 form
        if port.upper().startswith("COM"):
            port = port.strip().rstrip(":")
            try:
                num = int(port[3:])
                if num >= 10:
                    port = f"\\\\.\\{port.upper()}"
            except ValueError:
                pass
        flow = self.flow_control.lower()
        ser = serial.Serial(
            port=port,
            baudrate=int(self.baudrate),
            parity=normalize_parity(self.parity),
            bytesize=DATABITS_MAP[int(self.databits)],
            stopbits=STOPBITS_MAP[float(self.stopbits)],
            timeout=float(self.timeout),
            write_timeout=float(self.write_timeout),
            inter_byte_timeout=self.inter_char_timeout,
            rtscts=(flow == "rtscts"),
            xonxoff=(flow == "xonxoff"),
            dsrdtr=(flow == "dsrdtr"),
        )
        if self.rs485_mode is not None:
            try:
                import serial.rs485 as _rs485

                ser.rs485_mode = _rs485.RS485Settings(**self.rs485_mode)
            except Exception as exc:  # pragma: no cover - platform-dependent
                logger.warning(
                    "RS-485 mode not applied on %s: %s", port, exc
                )
        return ser


class SerialTransport:
    """Bus-level handle: owns a pyserial.Serial + a lock, shared across drivers.

    Drivers acquire ``transport.lock`` for the duration of one
    request/response transaction, then call ``write_frame()`` and
    ``read_frame()``. The transport itself is framing-agnostic — Framer
    instances live on the driver side.
    """

    def __init__(self, settings: SerialSettings, ser: serial.Serial):
        self.settings = settings
        self._ser = ser
        self.lock = threading.Lock()
        self.ref_count = 0
        self.last_io_at: float = 0.0

    def close(self) -> None:
        try:
            self._ser.close()
        except Exception:
            pass

    # -- ByteReader interface used by Framers --

    def read(self, size: int) -> bytes:
        return self._ser.read(size)

    def read_until(self, terminator: bytes, size: int | None = None) -> bytes:
        return self._ser.read_until(terminator, size)

    # -- High-level transaction primitives --

    def write_bytes(self, data: bytes, inter_command_delay_ms: int = 0) -> None:
        if inter_command_delay_ms > 0:
            elapsed = (time.monotonic() - self.last_io_at) * 1000
            wait = (inter_command_delay_ms - elapsed) / 1000
            if wait > 0:
                time.sleep(wait)
        if self.settings.flush_before_write:
            try:
                self._ser.reset_input_buffer()
            except Exception:
                pass
        self._ser.write(data)
        self._ser.flush()
        self.last_io_at = time.monotonic()


# ---------------------------------------------------------------------------
# SerialBusManager — pools SerialTransport handles by physical bus key
# ---------------------------------------------------------------------------

class SerialBusManager:
    """Pools SerialTransport instances keyed by (port, baud, parity, databits, stopbits).

    Mirrors ModbusBusManager. RS-485 multi-drop devices that share a
    single USB-serial adapter should resolve to the same transport.
    """

    def __init__(self) -> None:
        self._buses: dict[str, SerialTransport] = {}
        self._mgr_lock = threading.Lock()

    @staticmethod
    def _bus_key(settings: SerialSettings) -> str:
        parity = normalize_parity(settings.parity)
        return (
            f"serial:{settings.port}:{settings.baudrate}:{parity}:"
            f"{settings.databits}:{settings.stopbits}"
        )

    @staticmethod
    def parse_uri(transport_uri: str) -> str:
        """Extract a port path from a ``serial://...`` URI or accept a raw path."""
        if "://" not in transport_uri:
            return transport_uri
        parsed = urlparse(transport_uri)
        if parsed.scheme != "serial":
            raise ValueError(f"Expected serial:// URI, got {transport_uri!r}")
        return parsed.path or parsed.netloc

    def get(self, settings: SerialSettings) -> SerialTransport:
        with self._mgr_lock:
            key = self._bus_key(settings)
            transport = self._buses.get(key)
            if transport is None:
                ser = settings.open()
                transport = SerialTransport(settings, ser)
                self._buses[key] = transport
                logger.info("Opened serial bus: %s", key)
            transport.ref_count += 1
            return transport

    def release(self, settings: SerialSettings) -> None:
        with self._mgr_lock:
            key = self._bus_key(settings)
            transport = self._buses.get(key)
            if transport is None:
                return
            transport.ref_count -= 1
            if transport.ref_count <= 0:
                transport.close()
                del self._buses[key]
                logger.info("Closed serial bus: %s", key)
