"""Generic profile-driven serial driver.

Loads a YAML profile with ``protocol: serial`` and executes commands
declared in the YAML against any pyserial-supported port. Framing,
checksums, encoding, and per-command request/response shape come from
the YAML — no Python wrapper per device.

Note on ``length_prefix`` framing with ``length_field.offset > 0``:
the framer keeps any header bytes *before* the length field as part of
the returned payload (so checksums computed over the whole frame still
verify cleanly). Use ``parser: { type: bytes_slice, start: <offset> }``
to drop them when extracting the inner payload.

Example: a frame ``\\x55\\x03ABC`` (1-byte SYNC + 1-byte length=3 +
3-byte body ``ABC``) is returned by the framer as ``\\x55ABC``. To
recover just ``ABC``, use ``parser: { type: bytes_slice, start: 1,
cast: bytes }``.

Note on parser ``cast`` defaults: ``bytes_hex`` honors ``cast``: with
``cast: null`` (default) or ``cast: string`` it returns a hex string;
with ``cast: int`` it returns the integer value of the hex; with
``cast: bytes`` it returns the raw bytes unchanged. ``bytes_slice``
currently defaults to ``cast: bytes`` for backwards compatibility;
explicit ``cast`` is recommended for new profiles.

Schema (sketch):

    protocol: serial
    identity: { manufacturer, model, description }
    connection:
      transport: serial
      port: /dev/ttyUSB0          # optional default; usually injected at connect
      default_baudrate: 9600
      default_parity: none
      default_databits: 8
      default_stopbits: 1
      default_flow_control: none
      default_timeout: 1.0
      inter_char_timeout: 0.05
      inter_command_delay_ms: 0
      encoding: ascii             # ascii | latin-1 | utf-8 | binary
      request:
        framing: line
        terminator: "\\r\\n"
        checksum: { type: none }
      response:
        framing: line
        terminator: "\\r\\n"
        checksum: { type: none, verify: true }

    commands:
      read_voltage:
        type: query
        request: "MEAS:VOLT?"
        response:
          parser: { type: regex, pattern: "([\\d.]+)", group: 1, cast: float }
      set_voltage:
        type: action
        request: "VOLT {value}"
        params:
          value: { type: float, range: [0, 30] }
      read_burst:
        type: query
        request_bytes: ["0x55", "{addr:u16_be}", "{count:u16_le}"]
        request_framing:           # per-command override
          framing: raw
        response_framing:
          framing: length_prefix
          length_field: { offset: 0, size: 2, endian: big }
        response:
          parser: { type: bytes_hex }

A command's ``request`` (string) is encoded with the connection's
``encoding`` and framed with the connection's request framer.
``request_bytes`` (list) is assembled into raw bytes and framed with
``framing: raw`` by default — useful for binary protocols where the
request is fully specified by the bytes.

Per-command ``request_framing`` / ``response_framing`` blocks override
the connection defaults so quirky single commands can deviate.

The response parser supports the same ``regex|strip|split|passthrough``
modes as ReturnConfig, plus ``bytes_hex`` and ``bytes_slice`` for binary
payloads. Result is cast to the requested type (``float|int|string|bool|bytes``).
"""

from __future__ import annotations

import logging
import re
import struct
import threading
from typing import Any

from galois_edge.drivers.base import BaseProtocolDriver
from galois_edge.drivers.point import Point
from galois_edge.drivers.serial_transport import (
    Framer,
    SerialBusManager,
    SerialSettings,
    SerialTransport,
    build_framer,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Parameter substitution
# ---------------------------------------------------------------------------

_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)(?::([^}]+))?\}")

_BYTE_FORMATS: dict[str, tuple[str, int]] = {
    "u8":     (">B", 1),
    "i8":     (">b", 1),
    "u16_be": (">H", 2),
    "u16_le": ("<H", 2),
    "i16_be": (">h", 2),
    "i16_le": ("<h", 2),
    "u32_be": (">I", 4),
    "u32_le": ("<I", 4),
    "i32_be": (">i", 4),
    "i32_le": ("<i", 4),
    "f32_be": (">f", 4),
    "f32_le": ("<f", 4),
    "f64_be": (">d", 8),
    "f64_le": ("<d", 8),
}


def _substitute_string(template: str, params: dict[str, Any]) -> str:
    """Apply ``{name}`` substitution to a request string template."""

    def repl(m: re.Match) -> str:
        name, fmt = m.group(1), m.group(2)
        if name not in params:
            raise KeyError(f"Missing parameter: {name}")
        v = params[name]
        if fmt:
            # format spec on a string template — fall back to format()
            return format(v, fmt)
        return str(v)

    return _PLACEHOLDER_RE.sub(repl, template)


def _coerce_byte_token(token: Any, params: dict[str, Any]) -> bytes:
    """Turn one entry of a ``request_bytes`` list into bytes.

    Supported forms:
    - int: emitted as a single byte
    - "0x1A": hex literal (1 byte)
    - "0x1A2B": multi-byte hex literal (big-endian by token order)
    - "str:HELLO": ASCII string literal
    - "{name}": raw byte from params (must be 0..255)
    - "{name:u16_be}": packed multi-byte from params
    - "{name:str}": string from params, ASCII-encoded
    """
    if isinstance(token, int):
        return bytes([token & 0xFF])
    if isinstance(token, (bytes, bytearray)):
        return bytes(token)
    if not isinstance(token, str):
        raise TypeError(f"Unsupported request_bytes token: {token!r}")
    s = token.strip()

    m = _PLACEHOLDER_RE.fullmatch(s)
    if m:
        name, fmt = m.group(1), m.group(2)
        if name not in params:
            raise KeyError(f"Missing parameter: {name}")
        v = params[name]
        if not fmt or fmt == "u8":
            return bytes([int(v) & 0xFF])
        if fmt == "str":
            return str(v).encode("ascii")
        spec = _BYTE_FORMATS.get(fmt)
        if spec is None:
            raise ValueError(f"Unknown byte format: {fmt}")
        pack_fmt, _ = spec
        return struct.pack(pack_fmt, v)

    if s.startswith("str:"):
        return s[4:].encode("ascii")

    if s.startswith(("0x", "0X")):
        hex_body = s[2:]
        if len(hex_body) % 2 != 0:
            hex_body = "0" + hex_body
        return bytes.fromhex(hex_body)

    # bare decimal int
    return bytes([int(s) & 0xFF])


def _render_request(cmd: dict[str, Any], params: dict[str, Any], encoding: str) -> bytes:
    if "request_bytes" in cmd:
        chunks = [_coerce_byte_token(t, params) for t in cmd["request_bytes"]]
        return b"".join(chunks)
    if "request" in cmd:
        rendered = _substitute_string(cmd["request"], params)
        return rendered.encode(encoding)
    raise ValueError("Command has neither 'request' nor 'request_bytes'")


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def _cast_value(raw: str | bytes, cast: str | None) -> Any:
    if cast is None or cast == "string":
        return raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else raw
    if cast == "bytes":
        return raw if isinstance(raw, (bytes, bytearray)) else raw.encode("latin-1")
    if isinstance(raw, (bytes, bytearray)):
        text = raw.decode("utf-8", errors="replace")
    else:
        text = raw
    text = text.strip()
    if cast == "float":
        return float(text)
    if cast == "int":
        return int(text, 0) if text.startswith(("0x", "0X")) else int(text)
    if cast == "bool":
        return text.lower() in {"1", "true", "on", "yes", "high"}
    raise ValueError(f"Unknown cast: {cast}")


def _parse_response(raw: bytes, parser: dict[str, Any] | None, encoding: str) -> Any:
    """Apply a parser spec to a framed response payload, returning the final value."""
    if not parser:
        return raw.decode(encoding, errors="replace") if encoding != "binary" else raw

    ptype = (parser.get("type") or "passthrough").lower()
    cast = parser.get("cast")

    if ptype == "bytes_hex":
        # Honor cast: None/"string" → hex string (default); "int" → integer
        # value of the hex; "bytes" → raw bytes unchanged (lenient — caller
        # probably meant ``bytes_slice``).
        if cast is None or cast == "string":
            return raw.hex()
        if cast == "int":
            hx = raw.hex()
            return int(hx, 16) if hx else 0
        if cast == "bytes":
            return raw
        raise ValueError(f"Unknown cast for bytes_hex: {cast}")
    if ptype == "bytes_slice":
        start = int(parser.get("start", 0))
        end = parser.get("end")
        sliced = raw[start:end] if end is not None else raw[start:]
        # Note: ``bytes_slice`` defaults to ``cast: bytes`` (raw bytes) for
        # backwards compatibility. Other parsers default to ``string``. New
        # profiles should specify ``cast`` explicitly.
        return _cast_value(sliced, cast or "bytes")

    text = raw.decode(parser.get("encoding") or encoding or "ascii", errors="replace")

    if ptype == "passthrough":
        return _cast_value(text, cast)
    if ptype == "regex":
        pattern = parser["pattern"]
        group = int(parser.get("group", 0))
        m = re.search(pattern, text)
        if not m:
            raise ValueError(f"Response did not match regex: {pattern!r}")
        return _cast_value(m.group(group), cast)
    if ptype == "strip":
        out = text
        prefix = parser.get("prefix", "")
        suffix = parser.get("suffix", "")
        if prefix and out.startswith(prefix):
            out = out[len(prefix):]
        if suffix and out.endswith(suffix):
            out = out[: -len(suffix)]
        return _cast_value(out, cast)
    if ptype == "split":
        delim = parser.get("delimiter", ",")
        idx = int(parser.get("index", 0))
        parts = text.split(delim)
        if idx >= len(parts):
            raise ValueError(f"split parser: index {idx} out of range ({len(parts)} parts)")
        return _cast_value(parts[idx].strip(), cast)
    raise ValueError(f"Unknown response parser type: {ptype}")


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

class GenericSerialDriver(BaseProtocolDriver):
    """Generic, profile-driven serial driver.

    Construct via ``DriverRegistry.instantiate(profile_name, instrument_id,
    transport_uri)``. ``transport_uri`` is either a ``serial://<port>`` URI
    or a bare port path (``/dev/ttyUSB0``, ``COM3``, ``/dev/serial0``).
    """

    def __init__(
        self,
        instrument_id: str,
        transport_uri: str,
        profile: dict[str, Any],
        bus_manager: SerialBusManager,
        **kwargs: Any,
    ) -> None:
        super().__init__(instrument_id, transport_uri, **kwargs)
        # Reentrant lock: ``BaseProtocolDriver.subscribe`` polling threads
        # acquire ``self.lock`` and then call ``read_points`` → ``read_point``
        # → ``execute_command``, which re-acquires the same lock. A plain
        # ``threading.Lock`` would deadlock; ``RLock`` allows re-entry from
        # the same thread while still serializing across threads.
        self.lock = threading.RLock()
        self.profile = profile
        self.bus_manager = bus_manager
        self.transport: SerialTransport | None = None

        conn = profile.get("connection") or {}
        port = SerialBusManager.parse_uri(transport_uri) or conn.get("port")
        if not port:
            raise ValueError(
                f"No port specified for {instrument_id}: pass it in the transport URI "
                f"(e.g. serial:///dev/ttyUSB0) or set connection.port in the profile."
            )
        self.settings = SerialSettings(
            port=port,
            baudrate=int(conn.get("default_baudrate", 9600)),
            parity=str(conn.get("default_parity", "none")),
            databits=int(conn.get("default_databits", 8)),
            stopbits=float(conn.get("default_stopbits", 1)),
            flow_control=str(conn.get("default_flow_control", "none")),
            timeout=float(conn.get("default_timeout", 1.0)),
            write_timeout=float(conn.get("default_write_timeout", 1.0)),
            inter_char_timeout=conn.get("inter_char_timeout"),
        )
        self.encoding: str = conn.get("encoding", "ascii")
        self.inter_command_delay_ms: int = int(conn.get("inter_command_delay_ms", 0))

        self._default_request_framer: Framer = build_framer(
            conn.get("request") or {"framing": "line", "terminator": "\n"},
            (conn.get("request") or {}).get("checksum"),
        )
        self._default_response_framer: Framer = build_framer(
            conn.get("response") or {"framing": "line", "terminator": "\n"},
            (conn.get("response") or {}).get("checksum"),
        )

        self._commands = profile.get("commands") or {}

        # Optional points layer: each point references a read/write command.
        for name, p_def in (profile.get("points") or {}).items():
            self._points[name] = Point(
                name=name,
                data_type=p_def.get("data_type", "string"),
                access=p_def.get("access", "read"),
                scale=float(p_def.get("scale", 1.0)),
                unit=p_def.get("unit", ""),
                description=p_def.get("description", ""),
                addressing={
                    "read_command": p_def.get("read_command"),
                    "write_command": p_def.get("write_command"),
                },
            )

    # -- Lifecycle --

    def connect(self) -> None:
        with self.lock:
            self.transport = self.bus_manager.get(self.settings)
            self._connected = True
        logger.info(
            "Serial driver connected: %s @ %d baud",
            self.settings.port,
            self.settings.baudrate,
        )

    def disconnect(self) -> None:
        with self.lock:
            if self.transport is not None:
                self.bus_manager.release(self.settings)
                self.transport = None
            self._connected = False

    def identify(self) -> str:
        ident = self.profile.get("identity") or {}
        return f"{ident.get('manufacturer', '?')} {ident.get('model', '?')} @ {self.settings.port}"

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "protocol": "serial",
            "profile": (self.profile.get("identity") or {}).get("model", "unknown"),
            "commands": list(self._commands.keys()),
            "points": [p.to_dict() for p in self._points.values()],
            "transport": {
                "port": self.settings.port,
                "baudrate": self.settings.baudrate,
                "parity": self.settings.parity,
                "databits": self.settings.databits,
                "stopbits": self.settings.stopbits,
                "flow_control": self.settings.flow_control,
            },
        }

    # -- Command execution --

    def execute_command(self, command_name: str, params: dict[str, Any] | None = None) -> Any:
        # Outer driver lock guards self.transport mutation so a concurrent
        # disconnect() can't null it out under us. Lock ordering is always
        # ``self.lock`` → ``self.transport.lock``.
        with self.lock:
            cmd = self._commands.get(command_name)
            if cmd is None:
                raise ValueError(f"Unknown command: {command_name}")
            if self.transport is None:
                raise RuntimeError(f"Driver {self.instrument_id} is not connected")
            params = params or {}

            # Validate parameters against declared schema (range only, for now)
            for pname, pspec in (cmd.get("params") or {}).items():
                if pname in params and "range" in pspec:
                    lo, hi = pspec["range"]
                    if not (float(lo) <= float(params[pname]) <= float(hi)):
                        raise ValueError(
                            f"Parameter {pname}={params[pname]} out of range [{lo}, {hi}]"
                        )

            request_framer = (
                build_framer(
                    cmd["request_framing"],
                    (cmd.get("request_framing") or {}).get("checksum"),
                )
                if "request_framing" in cmd
                else self._default_request_framer
            )
            response_framer = (
                build_framer(
                    cmd["response_framing"],
                    (cmd.get("response_framing") or {}).get("checksum"),
                )
                if "response_framing" in cmd
                else self._default_response_framer
            )

            # request_bytes implies raw outgoing framing unless overridden
            if "request_bytes" in cmd and "request_framing" not in cmd:
                request_framer = build_framer({"framing": "raw"}, None)

            payload = _render_request(cmd, params, self.encoding)
            wire = request_framer.frame(payload)

            with self.transport.lock:
                self.transport.write_bytes(wire, self.inter_command_delay_ms)
                cmd_type = (cmd.get("type") or "query").lower()
                if cmd_type == "action":
                    return {"status": "ok"}
                raw = response_framer.unframe(self.transport)

            return _parse_response(raw, cmd.get("response", {}).get("parser"), self.encoding)

    # -- Point I/O (delegates to commands when defined) --

    def read_point(self, point: Point) -> Any:
        cmd_name = point.addressing.get("read_command")
        if not cmd_name:
            raise NotImplementedError(
                f"Point {point.name} has no read_command; "
                f"use execute_command directly."
            )
        value = self.execute_command(cmd_name)
        if point.scale != 1.0:
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return value * point.scale
            # Try once to coerce to float — supports e.g. parser cast=string
            # returning a numeric-looking string. If coercion fails, that's a
            # profile authoring bug: scale on a truly non-numeric point.
            try:
                return float(value) * point.scale
            except (TypeError, ValueError):
                raise TypeError(
                    f"Point {point.name!r} has scale={point.scale} but "
                    f"read_command returned non-numeric value of type "
                    f"{type(value).__name__}"
                )
        return value

    def write_point(self, point: Point, value: Any) -> None:
        if point.access == "read":
            raise PermissionError(f"Point '{point.name}' is read-only")
        cmd_name = point.addressing.get("write_command")
        if not cmd_name:
            raise NotImplementedError(
                f"Point {point.name} has no write_command; "
                f"use execute_command directly."
            )
        if point.scale != 1.0:
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                value = value / point.scale
            else:
                try:
                    value = float(value) / point.scale
                except (TypeError, ValueError):
                    raise TypeError(
                        f"Point {point.name!r} has scale={point.scale} but "
                        f"write received non-numeric value of type "
                        f"{type(value).__name__}"
                    )
        self.execute_command(cmd_name, {"value": value})
