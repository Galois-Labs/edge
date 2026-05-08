"""Production-grade generic CAN driver.

Successor to ``drivers/can_driver.py``.  Lives in the ``drivers/can/``
package so Phase F integration can switch the registry over without
touching this file.

What's new vs. the legacy driver:

* **Filter installation on connect.**  ``connection.filters`` is read from
  the YAML profile and passed to ``python_can.Bus(can_filters=...)`` so
  the OS kernel filters at receive — no more linear ``bus.recv()`` scan
  through unrelated traffic.

* **Native subscription.**  ``subscribe()`` is overridden to register a
  ``python_can.Listener`` with a ``Notifier`` instead of polling.  The
  listener decodes signals into engineering values and fires the user
  callback per matching frame.

* **Multiplex signal support.**  ``messages.<name>.multiplex.mux_signal``
  identifies the mux selector; signals with a ``mux_value`` are decoded
  only when the mux selector reads as that value.  J1939-style and
  CANopen extended-mux profiles are both expressible.

* **BusOff detection and recovery.**  Bus errors and BusOff transitions
  are tracked through a dedicated ``Listener``.  On BusOff the driver
  asks the bus manager to recover (exponential backoff, recreate, reapply
  filters) and re-installs subscriptions on the fresh Bus instance.

* **Error frame counters.**  Counts of bus errors and BusOff events are
  exposed via :py:meth:`get_capabilities`.

The signal extract / pack helpers are ported verbatim from
``drivers/can_driver.py`` — they are protocol arithmetic and don't need
reinvention.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any, Callable

from galois_edge.drivers.base import BaseProtocolDriver
from galois_edge.drivers.can.transport import CANBusManager, CAN_AVAILABLE
from galois_edge.drivers.point import Point

logger = logging.getLogger(__name__)

# Guarded import — python-can is optional
if CAN_AVAILABLE:
    try:
        import can as python_can
    except ImportError:  # pragma: no cover
        python_can = None  # type: ignore[assignment]
else:  # pragma: no cover
    python_can = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _select_data_type(bit_length: int, signed: bool) -> str:
    """Pick a Modbus-style data_type label for a given bit width."""
    if bit_length <= 8:
        return "int8" if signed else "uint8"
    if bit_length <= 16:
        return "int16" if signed else "uint16"
    if bit_length <= 32:
        return "int32" if signed else "uint32"
    return "int64" if signed else "uint64"


# ---------------------------------------------------------------------------
# Listeners
# ---------------------------------------------------------------------------


if python_can is not None:
    _ListenerBase = python_can.Listener
else:  # pragma: no cover — exercised only when python-can missing

    class _ListenerBase:  # type: ignore[no-redef]
        """Stand-in base class when python-can is unavailable."""

        def on_message_received(self, msg: Any) -> None: ...

        def __call__(self, msg: Any) -> None:
            self.on_message_received(msg)

        def on_error(self, exc: Exception) -> None: ...

        def stop(self) -> None: ...


class _ErrorListener(_ListenerBase):  # type: ignore[misc, valid-type]
    """Listener that counts error frames and triggers BusOff recovery."""

    def __init__(self, driver: "GenericCANDriver") -> None:
        self._driver = driver

    def on_message_received(self, msg: Any) -> None:
        if getattr(msg, "is_error_frame", False):
            self._driver._on_error_frame(msg)

    def on_error(self, exc: Exception) -> None:  # pragma: no cover — log path
        logger.warning("CAN bus error: %s", exc)
        self._driver._on_bus_error(exc)

    def stop(self) -> None:  # pragma: no cover — Notifier hook
        pass


class _SignalListener(_ListenerBase):  # type: ignore[misc, valid-type]
    """Listener that decodes incoming frames into signal values.

    Used by :py:meth:`GenericCANDriver.subscribe`.
    """

    def __init__(
        self,
        driver: "GenericCANDriver",
        points: list[Point],
        callback: Callable[[dict[str, Any]], None],
    ) -> None:
        self._driver = driver
        self._callback = callback
        # Group points by CAN ID so a single inbound frame fans out into
        # all signals defined on it.
        self._by_id: dict[int, list[Point]] = {}
        for p in points:
            cid = int(p.addressing.get("can_id", 0))
            self._by_id.setdefault(cid, []).append(p)

    def on_message_received(self, msg: Any) -> None:
        if getattr(msg, "is_error_frame", False):
            return
        cid = int(getattr(msg, "arbitration_id", 0))
        bucket = self._by_id.get(cid)
        if not bucket:
            return
        try:
            decoded = self._driver._decode_frame(msg, bucket)
        except Exception as exc:
            logger.warning("Signal decode failed for ID 0x%X: %s", cid, exc)
            return
        if decoded:
            try:
                self._callback(decoded)
            except Exception as exc:  # pragma: no cover — user code
                logger.warning("Subscription callback raised: %s", exc)

    def on_error(self, exc: Exception) -> None:  # pragma: no cover — log path
        logger.warning("CAN subscription listener error: %s", exc)

    def stop(self) -> None:  # pragma: no cover — Notifier hook
        pass


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


class GenericCANDriver(BaseProtocolDriver):
    """Production-grade generic CAN driver.

    See module docstring for the feature delta vs. the legacy driver.
    """

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
        self._notifier: Any = None
        self._error_listener: _ErrorListener | None = None
        # BufferedReader registered with the Notifier so read_point() can
        # call recv() semantics without competing with Notifier consumers.
        self._reader: Any = None
        # Map subscription_id -> dict(listener, points, callback).  We keep
        # enough state to re-attach listeners after BusOff recovery.
        self._native_subscriptions: dict[str, dict[str, Any]] = {}
        # Counters surfaced via get_capabilities()
        self._error_frame_count = 0
        self._bus_off_count = 0
        self._reconnect_count = 0
        self._last_error_at: float | None = None

        conn = profile.get("connection", {}) or {}
        self.channel: str = kwargs.get("channel", conn.get("channel", "can0"))
        self.interface: str = kwargs.get("interface", conn.get("interface", "socketcan"))
        self.bitrate: int = int(kwargs.get("bitrate", conn.get("bitrate", 500000)))
        self.recv_timeout: float = float(kwargs.get("recv_timeout", conn.get("recv_timeout", 1.0)))
        # Filters from YAML; keep raw form for re-passing to python-can,
        # plus a normalised tuple for the bus manager cache key.
        raw_filters = conn.get("filters") or []
        self._raw_filters: list[dict[str, int]] = [dict(f) for f in raw_filters if isinstance(f, dict)]

        # Mapping from message_name -> raw message def (for multiplex lookup)
        self._messages: dict[str, dict[str, Any]] = profile.get("messages", {}) or {}
        # Map signal_name -> message_name (for fast mux lookup)
        self._signal_to_message: dict[str, str] = {}
        # Cache: message_name -> mux_signal_name (or None)
        self._mux_for_message: dict[str, str | None] = {}

        # Build Point objects from YAML messages -> signals
        for msg_name, msg_def in self._messages.items():
            can_id = int(msg_def.get("can_id", 0))
            dlc = int(msg_def.get("dlc", 8))
            direction = msg_def.get("direction", "rx")
            is_extended = bool(msg_def.get("is_extended", can_id > 0x7FF))
            mux_signal = None
            multiplex = msg_def.get("multiplex") or {}
            if isinstance(multiplex, dict):
                mux_signal = multiplex.get("mux_signal")
            self._mux_for_message[msg_name] = mux_signal

            for sig_name, sig_def in (msg_def.get("signals") or {}).items():
                bit_length = int(sig_def.get("bit_length", 8))
                signed = bool(sig_def.get("signed", False))
                access = "read" if direction == "rx" else "read_write"
                dt = _select_data_type(bit_length, signed)

                addressing: dict[str, Any] = {
                    "can_id": can_id,
                    "is_extended": is_extended,
                    "dlc": dlc,
                    "direction": direction,
                    "start_bit": int(sig_def.get("start_bit", 0)),
                    "bit_length": bit_length,
                    "byte_order": sig_def.get("byte_order", "little_endian"),
                    "signed": signed,
                    "offset": float(sig_def.get("offset", 0)),
                    "message_name": msg_name,
                }
                if "mux_value" in sig_def:
                    addressing["mux_value"] = int(sig_def["mux_value"])
                if mux_signal:
                    addressing["mux_signal"] = mux_signal

                range_val: tuple[float, float] | None = None
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
                self._signal_to_message[sig_name] = msg_name

        self._commands = profile.get("commands", {}) or {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        self.bus, self.bus_lock = self.bus_manager.get_bus(
            channel=self.channel,
            bitrate=self.bitrate,
            interface=self.interface,
            filters=self._raw_filters,
        )
        # Install error listener via Notifier for bus error / BusOff
        # detection.  python-can 4.x forbids attaching the same Bus to
        # multiple Notifier instances, so we keep ONE Notifier per bus
        # and add subscription listeners to it via ``add_listener``.
        self._error_listener = _ErrorListener(self)
        self._reader = python_can.BufferedReader() if python_can is not None else None
        if python_can is not None:
            try:
                listeners: list[Any] = [self._error_listener]
                if self._reader is not None:
                    listeners.append(self._reader)
                self._notifier = python_can.Notifier(self.bus, listeners)
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning("Failed to start CAN Notifier: %s", exc)
                self._notifier = None

        # Register a recovery callback so the bus manager can hand us a
        # fresh Bus instance after BusOff.
        self.bus_manager.register_recovery_callback(
            channel=self.channel,
            bitrate=self.bitrate,
            interface=self.interface,
            filters=self._raw_filters,
            callback=self._on_bus_recovered,
        )
        self._connected = True
        logger.info(
            "CAN driver connected: %s (channel %s, bitrate %d, %d filter(s))",
            self.transport_uri,
            self.channel,
            self.bitrate,
            len(self._raw_filters),
        )

    def disconnect(self) -> None:
        # Tear down subscription listeners (they share self._notifier)
        if self._notifier is not None:
            for sub in self._native_subscriptions.values():
                listener = sub.get("listener")
                if listener is not None:
                    try:
                        self._notifier.remove_listener(listener)
                    except Exception:  # pragma: no cover
                        pass
        self._native_subscriptions.clear()
        if self._notifier is not None:
            try:
                self._notifier.stop()
            except Exception:  # pragma: no cover
                pass
            self._notifier = None
        if self._reader is not None:
            try:
                self._reader.stop()
            except Exception:  # pragma: no cover
                pass
            self._reader = None
        if self.bus is not None:
            self.bus_manager.release(
                channel=self.channel,
                bitrate=self.bitrate,
                interface=self.interface,
                filters=self._raw_filters,
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
            "filters": list(self._raw_filters),
            "error_frames": self._error_frame_count,
            "bus_off_events": self._bus_off_count,
            "reconnects": self._reconnect_count,
            "last_error_at": self._last_error_at,
        }

    # ------------------------------------------------------------------
    # Bit extraction / packing helpers (ported from legacy driver)
    # ------------------------------------------------------------------

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
            raw = int.from_bytes(data, byteorder="little")
            mask = (1 << bit_length) - 1
            value = (raw >> start_bit) & mask
        else:
            raw = int.from_bytes(data, byteorder="big")
            total_bits = len(data) * 8
            shift = total_bits - start_bit - bit_length
            if shift < 0:
                shift = 0
            mask = (1 << bit_length) - 1
            value = (raw >> shift) & mask

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
        """Pack a signal value into ``dlc`` bytes of CAN frame data."""
        mask = (1 << bit_length) - 1
        raw_val = value & mask
        if byte_order == "little_endian":
            frame_int = raw_val << start_bit
            return frame_int.to_bytes(dlc, byteorder="little")
        total_bits = dlc * 8
        shift = total_bits - start_bit - bit_length
        if shift < 0:
            shift = 0
        frame_int = raw_val << shift
        return frame_int.to_bytes(dlc, byteorder="big")

    # ------------------------------------------------------------------
    # Frame decode / mux logic
    # ------------------------------------------------------------------

    def _signal_applies(self, point: Point, mux_value: int | None) -> bool:
        """Return True if ``point`` should be decoded given ``mux_value``."""
        addr = point.addressing
        sig_mux = addr.get("mux_value")
        if sig_mux is None:
            # Plain signal — always applies.
            return True
        if mux_value is None:
            # Frame's mux selector wasn't decoded; signal cannot apply.
            return False
        return int(sig_mux) == int(mux_value)

    def _decode_signal(self, point: Point, data: bytes) -> Any:
        """Apply scale/offset/enum decoding to a single signal."""
        addr = point.addressing
        raw_int = self._extract_signal(
            data,
            addr["start_bit"],
            addr["bit_length"],
            addr["byte_order"],
            addr["signed"],
        )
        if point.enum:
            return point.enum.get(int(raw_int), int(raw_int))
        offset = float(addr.get("offset", 0))
        return raw_int * point.scale + offset

    def _decode_frame(self, msg: Any, points: list[Point]) -> dict[str, Any]:
        """Decode all matching signals on a frame into a dict.

        Honours multiplex selectors: the frame's mux signal is decoded
        first, and only signals whose ``mux_value`` matches (or which
        carry no mux_value) are emitted.
        """
        data = bytes(msg.data) if msg.data is not None else b""
        # Pad data to dlc if shorter (defensive)
        msg_name = points[0].addressing.get("message_name") if points else None
        mux_sig_name = self._mux_for_message.get(msg_name) if msg_name else None
        mux_value: int | None = None
        if mux_sig_name:
            mux_point = self._points.get(mux_sig_name)
            if mux_point is not None:
                addr = mux_point.addressing
                mux_value = self._extract_signal(
                    data,
                    addr["start_bit"],
                    addr["bit_length"],
                    addr["byte_order"],
                    addr["signed"],
                )
        out: dict[str, Any] = {}
        for p in points:
            if not self._signal_applies(p, mux_value):
                continue
            try:
                out[p.name] = self._decode_signal(p, data)
            except Exception as exc:
                logger.debug("Skipping signal %s on frame decode: %s", p.name, exc)
        return out

    # ------------------------------------------------------------------
    # Point I/O
    # ------------------------------------------------------------------

    def read_point(self, point: Point) -> Any:
        """Read a CAN signal by receiving a frame matching the signal's CAN ID.

        With kernel filters installed at connect time, ``bus.recv()``
        returns only frames matching one of the configured filters, so we
        no longer need a linear scan loop.  We do still tolerate one
        spurious frame (e.g., a different ID under the same filter mask)
        before timing out.
        """
        if self.bus is None:
            raise IOError("CAN bus not connected")
        with self.bus_lock:
            return self._read_point_locked(point)

    def _read_point_locked(self, point: Point) -> Any:
        addr = point.addressing
        can_id = int(addr["can_id"])
        # Refresh bus reference after potential recovery
        bus = self.bus_manager.get_current_bus(
            channel=self.channel,
            bitrate=self.bitrate,
            interface=self.interface,
            filters=self._raw_filters,
        ) or self.bus
        self.bus = bus

        # With filters installed at the kernel/python-can layer, frames
        # matching the filter set arrive quickly.  We pull from the
        # BufferedReader (registered with the Notifier) rather than
        # calling bus.recv() directly so we don't compete with the
        # Notifier dispatcher.  Allow a small number of spurious frames
        # in case the mask covers more than one ID.
        attempts = 8
        msg = None
        while attempts > 0:
            if self._reader is not None:
                msg = self._reader.get_message(timeout=self.recv_timeout)
            else:  # pragma: no cover — Notifier failed at connect
                msg = bus.recv(timeout=self.recv_timeout)
            if msg is None:
                raise IOError(f"CAN receive timeout waiting for ID 0x{can_id:X}")
            if getattr(msg, "is_error_frame", False):
                self._on_error_frame(msg)
                attempts -= 1
                continue
            if int(msg.arbitration_id) == can_id:
                break
            attempts -= 1
        if msg is None or int(msg.arbitration_id) != can_id:
            raise IOError(f"Did not receive CAN frame with ID 0x{can_id:X}")

        data = bytes(msg.data) if msg.data is not None else b""
        # Multiplex: if this message defines a mux signal AND the point
        # carries a mux_value, only return the value if the frame's mux
        # selector matches.
        msg_name = addr.get("message_name")
        mux_sig_name = self._mux_for_message.get(msg_name) if msg_name else None
        if mux_sig_name and "mux_value" in addr:
            mux_point = self._points.get(mux_sig_name)
            if mux_point is None:
                raise IOError(f"Mux signal '{mux_sig_name}' not in profile")
            mux_addr = mux_point.addressing
            actual_mux = self._extract_signal(
                data,
                mux_addr["start_bit"],
                mux_addr["bit_length"],
                mux_addr["byte_order"],
                mux_addr["signed"],
            )
            if int(actual_mux) != int(addr["mux_value"]):
                raise IOError(
                    f"Frame mux value {actual_mux} does not match expected "
                    f"{addr['mux_value']} for signal '{point.name}'"
                )

        return self._decode_signal(point, data)

    def write_point(self, point: Point, value: Any) -> None:
        if point.access == "read":
            raise PermissionError(
                f"Point '{point.name}' (CAN ID 0x{point.addressing.get('can_id', 0):X}) "
                f"is read-only"
            )
        if point.range is not None:
            lo, hi = point.range
            try:
                num = float(value)
            except (TypeError, ValueError):
                num = None
            if num is not None and not (lo <= num <= hi):
                raise ValueError(
                    f"Value {value} out of range [{lo}, {hi}] for '{point.name}'"
                )
        if self.bus is None:
            raise IOError("CAN bus not connected")
        with self.bus_lock:
            self._write_point_locked(point, value)

    def _write_point_locked(self, point: Point, value: Any) -> None:
        addr = point.addressing
        can_id = int(addr["can_id"])
        is_extended = bool(addr.get("is_extended", can_id > 0x7FF))
        dlc = int(addr.get("dlc", 8))

        # Inverse enum mapping
        if point.enum:
            inv = {v: k for k, v in point.enum.items()}
            if value in inv:
                value = inv[value]

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
        bus = self.bus_manager.get_current_bus(
            channel=self.channel,
            bitrate=self.bitrate,
            interface=self.interface,
            filters=self._raw_filters,
        ) or self.bus
        self.bus = bus
        if python_can is None:
            raise RuntimeError("python-can not installed")
        msg = python_can.Message(
            arbitration_id=can_id,
            data=data,
            is_extended_id=is_extended,
        )
        bus.send(msg)

    def read_points(self, points: list[Point]) -> dict[str, Any]:
        with self.bus_lock:
            return {p.name: self._read_point_locked(p) for p in points}

    # ------------------------------------------------------------------
    # Native subscription
    # ------------------------------------------------------------------

    def subscribe(
        self,
        points: list[Point],
        callback: Callable[[dict[str, Any]], None],
        interval_ms: int = 1000,
    ) -> str:
        """Subscribe to CAN signal changes via python-can's Notifier.

        Overrides the polling default in :class:`BaseProtocolDriver`.
        Frames matching the configured filter set are decoded as they
        arrive — there is no polling interval; ``interval_ms`` is accepted
        for API compatibility but ignored at the transport layer.

        The driver maintains a single :class:`python_can.Notifier` per
        bus (created in :py:meth:`connect`); each subscription adds a
        listener to it.  This sidesteps python-can 4.x's restriction that
        a Bus may belong to only one Notifier at a time.
        """
        if self.bus is None:
            raise IOError("CAN bus not connected")
        if python_can is None:
            raise RuntimeError("python-can not installed")
        if self._notifier is None:
            # Notifier failed to start at connect; fall back to polling
            # via the base class implementation.
            return super().subscribe(points, callback, interval_ms)
        sub_id = str(uuid.uuid4())
        listener = _SignalListener(self, points, callback)
        try:
            self._notifier.add_listener(listener)
        except Exception as exc:  # pragma: no cover
            logger.warning("Failed to add CAN listener: %s", exc)
            raise
        self._native_subscriptions[sub_id] = {
            "listener": listener,
            "points": list(points),
            "callback": callback,
        }
        return sub_id

    def unsubscribe(self, subscription_id: str) -> None:
        sub = self._native_subscriptions.pop(subscription_id, None)
        if sub is None:
            super().unsubscribe(subscription_id)
            return
        listener = sub.get("listener")
        if listener is not None and self._notifier is not None:
            try:
                self._notifier.remove_listener(listener)
            except Exception:  # pragma: no cover
                pass

    # ------------------------------------------------------------------
    # Error / recovery hooks
    # ------------------------------------------------------------------

    def _on_error_frame(self, msg: Any) -> None:
        self._error_frame_count += 1
        self._last_error_at = time.time()
        # python-can reports BusOff via the bus state, not via the error
        # frame attributes directly.  We sample bus state and surface it.
        try:
            state = getattr(self.bus, "state", None)
        except Exception:  # pragma: no cover
            state = None
        if python_can is not None and state is not None:
            try:
                if state == python_can.BusState.ERROR:  # type: ignore[attr-defined]
                    pass
            except Exception:  # pragma: no cover
                pass
        bus_off_state = getattr(python_can, "BusState", None)
        if bus_off_state is not None:
            try:
                # Only some BusState enums declare PASSIVE/ERROR/OFF; guard.
                off_value = getattr(bus_off_state, "ERROR", None)
                if off_value is not None and state == off_value:
                    self._notify_bus_off()
            except Exception:  # pragma: no cover
                pass

    def _on_bus_error(self, exc: Exception) -> None:
        # Ignore exceptions raised because the bus is shutting down.
        # python-can's Notifier polls bus.recv() in a thread; on shutdown
        # the bus raises "Cannot operate on a closed bus" which we don't
        # treat as an instrument error.
        msg = str(exc).lower()
        if "closed bus" in msg or not self._connected:
            return
        self._error_frame_count += 1
        self._last_error_at = time.time()
        # Heuristic: treat "bus off" exceptions as BusOff for recovery
        # purposes.  Real socketcan reports this via CanOperationError;
        # the virtual interface never does.
        if "bus" in msg and "off" in msg:
            self._notify_bus_off()

    def _notify_bus_off(self) -> None:
        """Trigger BusOff recovery via the bus manager."""
        self._bus_off_count += 1
        logger.warning("CAN BusOff detected on %s; initiating recovery", self.channel)
        # Capture active subscriptions; they will be re-attached after recovery.
        self.bus_manager.notify_bus_off(self.channel)

    def force_bus_off_recovery(self) -> None:
        """Public test hook: trigger a BusOff recovery cycle.

        Useful for unit tests that want to exercise the recovery path
        without needing a hardware bus error.
        """
        self._notify_bus_off()

    def _on_bus_recovered(self, new_bus: Any) -> None:
        """Called by the bus manager after a successful BusOff recovery.

        Replaces our cached Bus reference, recreates the single shared
        Notifier on the fresh Bus, and re-attaches every active
        subscription's listener.
        """
        logger.info("Re-installing CAN listeners after recovery on %s", self.channel)
        self._reconnect_count += 1
        # Tear down stale notifier
        if self._notifier is not None:
            try:
                self._notifier.stop()
            except Exception:  # pragma: no cover
                pass
            self._notifier = None

        # Swap bus reference
        self.bus = new_bus

        # Restart shared notifier with error listener, the BufferedReader,
        # and all subscription listeners.  The reader is replaced so
        # stale frames buffered before recovery don't bleed into the
        # post-recovery read path.
        if python_can is None:
            return
        if self._reader is not None:
            try:
                self._reader.stop()
            except Exception:  # pragma: no cover
                pass
        self._reader = python_can.BufferedReader()
        listeners: list[Any] = []
        if self._error_listener is not None:
            listeners.append(self._error_listener)
        listeners.append(self._reader)
        for sub in self._native_subscriptions.values():
            l = sub.get("listener")
            if l is not None:
                listeners.append(l)
        try:
            self._notifier = python_can.Notifier(new_bus, listeners)
        except Exception as exc:  # pragma: no cover
            logger.warning("Failed to restart Notifier after recovery: %s", exc)
            self._notifier = None
