"""Shared CAN bus transport manager.

Multiple CAN devices on the same physical bus share one python-can Bus
object and one lock.  The ``CANBusManager`` owns Bus instances keyed by
``(interface, channel, bitrate)``.
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

# Guarded import — python-can is optional
try:
    import can as python_can
    CAN_AVAILABLE = True
except ImportError:
    python_can = None  # type: ignore[assignment]
    CAN_AVAILABLE = False
    logger.warning("python-can not available — CAN transport disabled")


class CANBusManager:
    """Manages shared python-can Bus instances for physical CAN interfaces."""

    def __init__(self) -> None:
        self._buses: dict[str, dict] = {}
        self._mgr_lock = threading.Lock()

    def _bus_key(self, channel: str, bitrate: int = 500000, interface: str = "socketcan") -> str:
        """Generate unique key for a physical CAN bus."""
        return f"{interface}:{channel}:{bitrate}"

    def get_bus(self, channel: str, bitrate: int = 500000, interface: str = "socketcan") -> tuple:
        """Return ``(bus, lock)`` for the given CAN interface.

        Creates and opens the bus on first request.  Subsequent requests
        for the same physical bus return the same instance.
        """
        if not CAN_AVAILABLE:
            raise RuntimeError("python-can is not installed")

        with self._mgr_lock:
            key = self._bus_key(channel, bitrate, interface)

            if key not in self._buses:
                bus = python_can.Bus(
                    channel=channel,
                    interface=interface,
                    bitrate=bitrate,
                )
                self._buses[key] = {
                    "bus": bus,
                    "lock": threading.Lock(),
                    "ref_count": 0,
                }
                logger.info("Created CAN bus: %s", key)

            entry = self._buses[key]
            entry["ref_count"] += 1
            return entry["bus"], entry["lock"]

    def release(self, channel: str, bitrate: int = 500000, interface: str = "socketcan") -> None:
        """Release a reference.  Shuts down bus when ref_count hits 0."""
        with self._mgr_lock:
            key = self._bus_key(channel, bitrate, interface)
            if key not in self._buses:
                return

            entry = self._buses[key]
            entry["ref_count"] -= 1
            if entry["ref_count"] <= 0:
                try:
                    entry["bus"].shutdown()
                except Exception:
                    pass
                del self._buses[key]
                logger.info("Closed CAN bus: %s", key)
