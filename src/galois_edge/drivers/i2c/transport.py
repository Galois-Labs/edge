"""Shared I²C transport manager.

`I2CBusManager` caches `smbus2.SMBus` handles per bus number with reference
counting, and provides per-(bus, device_address) RLocks so two drivers on
the same bus don't interleave transactions.

Capability gating: the transport advertises itself as available only on
hosts where `smbus2` imports cleanly AND at least one `/dev/i2c-N` device
node is present. On macOS / Windows dev hosts, callers can still inject a
mock `SMBus` for unit testing — `is_available()` is purely advisory.
"""

from __future__ import annotations

import glob
import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

try:  # pragma: no cover - import guard
    import smbus2  # type: ignore

    _SMBUS2_AVAILABLE = True
except ImportError:  # pragma: no cover - covered by capability tests with monkeypatch
    smbus2 = None  # type: ignore
    _SMBUS2_AVAILABLE = False


def _smbus2_imported() -> bool:
    """Return True if `smbus2` imported successfully."""
    return _SMBUS2_AVAILABLE


def _i2c_devices_present() -> bool:
    """Return True if at least one /dev/i2c-N exists on this host."""
    return bool(glob.glob("/dev/i2c-*"))


class I2CBusManager:
    """Cache and share `smbus2.SMBus` handles across drivers.

    One `SMBus` handle per bus number, reference-counted. Per-device locks
    keyed by `(bus_num, device_address)` ensure register transactions for
    the same chip are serialized while still allowing concurrent traffic to
    different chips on the same bus (the underlying kernel driver
    serialises bus access, but device-level locking keeps register-pointer
    transactions atomic).
    """

    def __init__(self, smbus_factory: Any | None = None) -> None:
        # `smbus_factory` lets tests substitute a fake SMBus class without
        # monkey-patching the smbus2 module.
        self._smbus_factory = smbus_factory or (smbus2.SMBus if _SMBUS2_AVAILABLE else None)
        self._buses: dict[int, dict[str, Any]] = {}
        self._device_locks: dict[tuple[int, int], threading.RLock] = {}
        self._mgr_lock = threading.Lock()

    # -- Capability gating --

    @staticmethod
    def is_available() -> bool:
        """Return True iff `smbus2` is importable and a /dev/i2c-N exists."""
        return _smbus2_imported() and _i2c_devices_present()

    @staticmethod
    def unsupported_reason() -> str | None:
        """Human-readable reason I²C is unavailable on this host, or None."""
        if not _smbus2_imported():
            return "smbus2 not installed"
        if not _i2c_devices_present():
            return "no /dev/i2c-* devices found"
        return None

    # -- Handle lifecycle --

    def get_smbus(self, bus_num: int) -> Any:
        """Return a shared `SMBus` handle for the given bus number.

        Increments the reference count. The first caller opens the bus;
        subsequent callers reuse the open handle.
        """
        with self._mgr_lock:
            entry = self._buses.get(bus_num)
            if entry is None:
                if self._smbus_factory is None:
                    raise RuntimeError(
                        "I²C bus manager has no SMBus factory; install smbus2 "
                        "or inject one via the constructor."
                    )
                bus = self._smbus_factory(bus_num)
                entry = {"bus": bus, "ref_count": 0}
                self._buses[bus_num] = entry
                logger.info("Opened I²C bus %d", bus_num)
            entry["ref_count"] += 1
            return entry["bus"]

    def release(self, bus_num: int) -> None:
        """Drop a reference; close the handle when the count hits 0."""
        with self._mgr_lock:
            entry = self._buses.get(bus_num)
            if entry is None:
                return
            entry["ref_count"] -= 1
            if entry["ref_count"] <= 0:
                bus = entry["bus"]
                try:
                    bus.close()
                except Exception:  # pragma: no cover
                    logger.exception("Error closing I²C bus %d", bus_num)
                del self._buses[bus_num]
                # Drop any per-device locks that referenced this bus.
                for key in list(self._device_locks.keys()):
                    if key[0] == bus_num:
                        del self._device_locks[key]
                logger.info("Closed I²C bus %d", bus_num)

    # -- Per-device locking --

    def device_lock(self, bus_num: int, device_address: int) -> threading.RLock:
        """Return the RLock for `(bus_num, device_address)`, creating on demand."""
        key = (bus_num, device_address)
        with self._mgr_lock:
            lock = self._device_locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._device_locks[key] = lock
            return lock
