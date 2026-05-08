"""Production-grade shared CAN bus transport manager.

This module replaces ``drivers/can_transport.py`` once Phase F integration
moves things over.  Until then, both modules coexist; the new one lives
under the ``drivers/can/`` package and is reached via the registry.

Key differences vs. the legacy transport:

* **Filter-aware bus keying.**  The cache key is ``(channel, interface,
  bitrate, frozenset(filters))``.  Two instruments on the same physical
  channel but with different OS-level filter masks get separate
  ``python_can.Bus`` instances so receive paths do not collide.

* **BusOff recovery thread.**  Per-channel watcher.  When ``python-can``
  reports the bus has entered the BusOff state (or the application
  signals one via :py:meth:`CANBusManager.notify_bus_off`), the manager
  shuts the existing bus down, sleeps with exponential backoff
  (1, 2, 4, ..., capped at 16 seconds), recreates the bus with the
  original parameters and filters, and signals listeners to resume any
  active subscriptions.  Drivers register a callback via
  :py:meth:`CANBusManager.register_recovery_callback`.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Iterable

logger = logging.getLogger(__name__)

# Guarded import — python-can is optional
try:
    import can as python_can

    CAN_AVAILABLE = True
except ImportError:  # pragma: no cover — exercised only when python-can missing
    python_can = None  # type: ignore[assignment]
    CAN_AVAILABLE = False
    logger.warning("python-can not available — CAN transport disabled")


# Backoff sequence (seconds).  After exhausting the explicit list, the cap is
# reused for every subsequent attempt until reconnect succeeds.
_BUSOFF_BACKOFF_SCHEDULE = (1.0, 2.0, 4.0, 8.0, 16.0)


def _normalize_filters(filters: Iterable[dict[str, int]] | None) -> tuple[tuple[int, int], ...]:
    """Normalise a filter list into a hashable tuple of ``(can_id, can_mask)`` pairs.

    Sorted so equivalent filter sets in different orders produce the same key.
    """
    if not filters:
        return ()
    normalised = []
    for f in filters:
        if not isinstance(f, dict):
            continue
        can_id = int(f.get("can_id", 0))
        can_mask = int(f.get("can_mask", 0x7FF))
        extended = bool(f.get("extended", False))
        # Encode "extended" flag into the mask tuple so 11-bit and 29-bit
        # filters with the same numeric ID are kept distinct in the cache.
        normalised.append((can_id, can_mask, int(extended)))
    return tuple(sorted(normalised))


def _filters_to_python_can(
    filters: tuple[tuple[int, int, int], ...] | None,
) -> list[dict[str, Any]] | None:
    """Convert a normalised filter tuple back to python-can's ``can_filters`` shape."""
    if not filters:
        return None
    out: list[dict[str, Any]] = []
    for entry in filters:
        # Tolerate (id, mask) and (id, mask, extended) shapes
        if len(entry) == 2:
            can_id, can_mask = entry
            extended = False
        else:
            can_id, can_mask, ext_flag = entry
            extended = bool(ext_flag)
        out.append({"can_id": can_id, "can_mask": can_mask, "extended": extended})
    return out


class CANBusManager:
    """Manages shared :class:`python_can.Bus` instances.

    Keys buses on ``(interface, channel, bitrate, filter_set)`` so two
    instruments that listen to disjoint CAN ID ranges on the same physical
    interface do not interfere with each other's receive paths.
    """

    def __init__(self) -> None:
        self._buses: dict[tuple, dict[str, Any]] = {}
        self._mgr_lock = threading.RLock()
        # Map channel name -> set of bus keys (so a BusOff event can
        # invalidate every bus on a physical channel at once).
        self._channel_keys: dict[str, set[tuple]] = {}
        # Recovery callbacks registered by drivers; key -> list of callbacks.
        self._recovery_callbacks: dict[tuple, list[Callable[[Any], None]]] = {}
        # Backoff override (tests may shorten this).
        self._backoff_schedule: tuple[float, ...] = _BUSOFF_BACKOFF_SCHEDULE

    # ------------------------------------------------------------------
    # Key helpers
    # ------------------------------------------------------------------

    def _bus_key(
        self,
        channel: str,
        interface: str,
        bitrate: int,
        filters: tuple[tuple[int, int, int], ...],
    ) -> tuple:
        return (interface, channel, int(bitrate), filters)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_bus(
        self,
        channel: str,
        bitrate: int = 500000,
        interface: str = "socketcan",
        filters: Iterable[dict[str, int]] | None = None,
    ) -> tuple[Any, threading.RLock]:
        """Return ``(bus, lock)`` for the given parameters.

        Creates and opens the bus on first request.  Subsequent requests
        with identical ``(channel, interface, bitrate, filters)`` return
        the same instance.

        The lock is an :class:`threading.RLock` so the same thread can
        call back into the bus reentrantly during error handling.
        """
        if not CAN_AVAILABLE:
            raise RuntimeError("python-can is not installed")

        normalised = _normalize_filters(filters)

        with self._mgr_lock:
            key = self._bus_key(channel, interface, bitrate, normalised)
            entry = self._buses.get(key)
            if entry is None:
                bus = self._open_bus(channel, interface, bitrate, normalised)
                entry = {
                    "bus": bus,
                    "lock": threading.RLock(),
                    "ref_count": 0,
                    "channel": channel,
                    "interface": interface,
                    "bitrate": int(bitrate),
                    "filters": normalised,
                    "key": key,
                }
                self._buses[key] = entry
                self._channel_keys.setdefault(channel, set()).add(key)
                logger.info("Created CAN bus: %r", key)
            entry["ref_count"] += 1
            return entry["bus"], entry["lock"]

    def release(
        self,
        channel: str,
        bitrate: int = 500000,
        interface: str = "socketcan",
        filters: Iterable[dict[str, int]] | None = None,
    ) -> None:
        """Release a reference; shuts the bus down on last release."""
        normalised = _normalize_filters(filters)
        with self._mgr_lock:
            key = self._bus_key(channel, interface, bitrate, normalised)
            entry = self._buses.get(key)
            if entry is None:
                return
            entry["ref_count"] -= 1
            if entry["ref_count"] <= 0:
                self._shutdown_entry(entry)
                self._buses.pop(key, None)
                self._channel_keys.get(channel, set()).discard(key)
                self._recovery_callbacks.pop(key, None)
                logger.info("Closed CAN bus: %r", key)

    def register_recovery_callback(
        self,
        channel: str,
        bitrate: int,
        interface: str,
        filters: Iterable[dict[str, int]] | None,
        callback: Callable[[Any], None],
    ) -> None:
        """Register a callback invoked when the bus has been recreated.

        The callback receives the new ``python_can.Bus`` instance.  Drivers
        use this to re-install listeners and resume subscriptions after a
        BusOff/recovery cycle.
        """
        normalised = _normalize_filters(filters)
        key = self._bus_key(channel, interface, bitrate, normalised)
        with self._mgr_lock:
            self._recovery_callbacks.setdefault(key, []).append(callback)

    def notify_bus_off(self, channel: str) -> None:
        """Trigger BusOff recovery for every bus on ``channel``.

        Spawns a daemon recovery thread per affected key.  The thread
        applies the exponential backoff, recreates the bus, re-installs
        filters, and invokes registered recovery callbacks with the new
        Bus instance.
        """
        with self._mgr_lock:
            keys = list(self._channel_keys.get(channel, set()))
        for key in keys:
            t = threading.Thread(
                target=self._recover_bus,
                args=(key,),
                daemon=True,
                name=f"can-recover-{channel}",
            )
            t.start()

    def get_lock(
        self,
        channel: str,
        bitrate: int,
        interface: str,
        filters: Iterable[dict[str, int]] | None,
    ) -> threading.RLock | None:
        """Return the RLock for an already-open bus, or ``None``."""
        normalised = _normalize_filters(filters)
        key = self._bus_key(channel, interface, bitrate, normalised)
        with self._mgr_lock:
            entry = self._buses.get(key)
            return entry["lock"] if entry else None

    def get_current_bus(
        self,
        channel: str,
        bitrate: int,
        interface: str,
        filters: Iterable[dict[str, int]] | None,
    ) -> Any:
        """Return the *current* bus instance for the key.

        Used by drivers that cached a Bus reference before recovery; after
        recovery the manager has the fresh instance.
        """
        normalised = _normalize_filters(filters)
        key = self._bus_key(channel, interface, bitrate, normalised)
        with self._mgr_lock:
            entry = self._buses.get(key)
            return entry["bus"] if entry else None

    def shutdown_all(self) -> None:
        """Close every managed bus.  Used by tests."""
        with self._mgr_lock:
            for entry in list(self._buses.values()):
                self._shutdown_entry(entry)
            self._buses.clear()
            self._channel_keys.clear()
            self._recovery_callbacks.clear()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _open_bus(
        self,
        channel: str,
        interface: str,
        bitrate: int,
        normalised_filters: tuple[tuple[int, int, int], ...],
    ) -> Any:
        """Open a new python-can Bus.

        ``virtual`` interface ignores ``bitrate`` but accepts it cleanly.
        """
        kwargs: dict[str, Any] = {
            "channel": channel,
            "interface": interface,
        }
        # The virtual interface in python-can does not take bitrate; pass it
        # for hardware backends only.
        if interface != "virtual":
            kwargs["bitrate"] = int(bitrate)
        can_filters = _filters_to_python_can(normalised_filters)
        if can_filters:
            kwargs["can_filters"] = can_filters
        return python_can.Bus(**kwargs)

    def _shutdown_entry(self, entry: dict[str, Any]) -> None:
        bus = entry.get("bus")
        if bus is None:
            return
        try:
            bus.shutdown()
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("Bus shutdown raised: %s", exc)

    def _recover_bus(self, key: tuple) -> None:
        """Run BusOff recovery for a single key.

        Tries each backoff in sequence until ``_open_bus`` succeeds.
        """
        with self._mgr_lock:
            entry = self._buses.get(key)
            if entry is None:
                return
            channel = entry["channel"]
            interface = entry["interface"]
            bitrate = entry["bitrate"]
            normalised = entry["filters"]
            # Tear the dead bus down so subsequent recv() calls fail fast.
            self._shutdown_entry(entry)
            entry["bus"] = None

        attempt = 0
        backoff = self._backoff_schedule
        while True:
            delay = backoff[min(attempt, len(backoff) - 1)]
            logger.warning(
                "BusOff recovery: sleeping %.1fs before attempt %d on %r",
                delay,
                attempt + 1,
                key,
            )
            time.sleep(delay)
            try:
                new_bus = self._open_bus(channel, interface, bitrate, normalised)
            except Exception as exc:
                logger.warning("BusOff recovery attempt %d failed: %s", attempt + 1, exc)
                attempt += 1
                continue

            with self._mgr_lock:
                entry = self._buses.get(key)
                if entry is None:
                    # The bus was released while we were recovering.  Drop
                    # the new instance.
                    try:
                        new_bus.shutdown()
                    except Exception:  # pragma: no cover
                        pass
                    return
                entry["bus"] = new_bus
                callbacks = list(self._recovery_callbacks.get(key, ()))

            logger.info("BusOff recovery succeeded for %r on attempt %d", key, attempt + 1)
            for cb in callbacks:
                try:
                    cb(new_bus)
                except Exception as exc:  # pragma: no cover — log only
                    logger.warning("Recovery callback raised: %s", exc)
            return

    # Test-only helper
    def _set_backoff_schedule(self, schedule: tuple[float, ...]) -> None:
        """Override the BusOff backoff sequence (used by tests)."""
        self._backoff_schedule = schedule
