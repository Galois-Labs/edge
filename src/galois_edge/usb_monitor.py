"""
USB hotplug monitor using pyudev.

Watches for USB device add/remove events and dispatches callbacks to the
instrument discovery system. Runs a blocking pyudev.Monitor loop in a
dedicated daemon thread (NOT the I/O executor thread, to avoid blocking
instrument commands).

Subsystem filtering:
  - "usb" subsystem, DEVTYPE="usb_device" -- catches all USB devices
  - Filters by known identifiers to route to correct handler

Event routing:
  USB-GPIB adapter add    -> on_gpib_adapter_added(sysfs_path)
  USB-GPIB adapter remove -> on_gpib_adapter_removed(sysfs_path)
  USB-TMC device add      -> on_usbtmc_added(sysfs_path)
  USB-TMC device remove   -> on_usbtmc_removed(sysfs_path)
  Serial adapter add      -> on_serial_added(sysfs_path)
  Serial adapter remove   -> on_serial_removed(sysfs_path)

Thread safety:
  The monitor thread does NO GPIB/VISA I/O. It only classifies USB events
  and dispatches async callbacks to the asyncio event loop via
  loop.call_soon_threadsafe(). All actual I/O is delegated to the single
  _io_executor thread by the callback handlers in main.py.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

# Guarded import -- pyudev is Linux-only and optional
try:
    import pyudev
    PYUDEV_AVAILABLE = True
except ImportError:
    pyudev = None  # type: ignore[assignment]
    PYUDEV_AVAILABLE = False
    logger.info("pyudev not available -- USB hotplug monitoring disabled")


# ---------------------------------------------------------------------------
# Known device identifiers
# ---------------------------------------------------------------------------

# Known GPIB adapter VID:PID pairs (lowercase hex, no 0x prefix)
_GPIB_ADAPTERS = {
    ("3923", "709b"),  # NI GPIB-USB-HS
    ("3923", "709a"),  # NI GPIB-USB-HS+
    ("0957", "0518"),  # Keysight/Agilent 82357B
    ("0957", "0718"),  # Keysight 82357A
}

# USB-serial adapter kernel drivers
_SERIAL_DRIVERS = {"ftdi_sio", "ch341", "cp210x", "pl2303"}

# Known raw USB instrument vendor IDs
_RAW_USB_VIDS = {"0a2d"}  # Signal Recovery

# Additional GPIB adapter VIDs from environment (user-extensible)
_EXTRA_GPIB_VIDS = os.environ.get("GPIB_ADAPTER_VIDS", "")


def _parse_extra_gpib_adapters() -> set[tuple[str, str]]:
    """Parse GPIB_ADAPTER_VIDS env var (format: 'vid:pid,vid:pid,...')."""
    extras: set[tuple[str, str]] = set()
    if not _EXTRA_GPIB_VIDS:
        return extras
    for entry in _EXTRA_GPIB_VIDS.split(","):
        entry = entry.strip()
        if ":" in entry:
            parts = entry.split(":")
            if len(parts) == 2:
                extras.add((parts[0].strip().lower(), parts[1].strip().lower()))
    return extras


# Merge built-in and user-defined GPIB adapters
GPIB_ADAPTERS = _GPIB_ADAPTERS | _parse_extra_gpib_adapters()


# ---------------------------------------------------------------------------
# USB Monitor
# ---------------------------------------------------------------------------


class USBMonitor:
    """Event-driven USB hotplug monitor.

    Runs pyudev.Monitor.poll() in a dedicated daemon thread. Events are
    classified and dispatched as async callbacks to the event loop.

    The monitor thread does NO instrument I/O -- it only reads udev
    properties and dispatches callbacks.

    Usage::

        monitor = USBMonitor()
        monitor.on_gpib_adapter_added = my_gpib_add_handler
        monitor.on_gpib_adapter_removed = my_gpib_remove_handler
        monitor.start(asyncio.get_running_loop())
        # ... later ...
        monitor.stop()
    """

    def __init__(self) -> None:
        if not PYUDEV_AVAILABLE:
            raise RuntimeError(
                "pyudev is not installed -- USBMonitor requires pyudev"
            )

        self._context: pyudev.Context = pyudev.Context()
        self._monitor: Optional[pyudev.Monitor] = None
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._running: bool = False

        # Callbacks (set by caller before start())
        self.on_gpib_adapter_added: Optional[
            Callable[[str], Awaitable[None]]
        ] = None
        self.on_gpib_adapter_removed: Optional[
            Callable[[str], Awaitable[None]]
        ] = None
        self.on_usbtmc_added: Optional[
            Callable[[str], Awaitable[None]]
        ] = None
        self.on_usbtmc_removed: Optional[
            Callable[[str], Awaitable[None]]
        ] = None
        self.on_serial_added: Optional[
            Callable[[str], Awaitable[None]]
        ] = None
        self.on_serial_removed: Optional[
            Callable[[str], Awaitable[None]]
        ] = None

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Create and start the monitor thread.

        Parameters
        ----------
        loop:
            The asyncio event loop for dispatching callbacks via
            ``call_soon_threadsafe()``.
        """
        if self._running:
            logger.warning("USBMonitor already running")
            return

        self._loop = loop
        self._running = True

        # Set up pyudev monitor
        self._monitor = pyudev.Monitor.from_netlink(self._context)
        self._monitor.filter_by(subsystem="usb", device_type="usb_device")

        self._thread = threading.Thread(
            target=self._monitor_loop,
            name="usb-hotplug-monitor",
            daemon=True,
        )
        self._thread.start()
        logger.info("USB hotplug monitor thread started")

    def stop(self) -> None:
        """Signal the monitor thread to exit and join with timeout."""
        if not self._running:
            return

        self._running = False

        # The monitor thread will exit on the next poll timeout
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            if self._thread.is_alive():
                logger.warning(
                    "USB monitor thread did not exit within timeout"
                )
            self._thread = None

        logger.info("USB hotplug monitor stopped")

    # ------------------------------------------------------------------
    # Monitor loop (runs in dedicated thread)
    # ------------------------------------------------------------------

    def _monitor_loop(self) -> None:
        """Blocking loop that polls for USB events.

        Runs in the dedicated daemon thread. Never touches any
        instrument I/O -- only reads udev properties and dispatches
        callbacks.
        """
        if self._monitor is None:
            return

        logger.debug("USB monitor loop started")

        while self._running:
            try:
                # Poll with a 1-second timeout so we can check _running
                device = self._monitor.poll(timeout=1.0)
                if device is None:
                    continue

                self._handle_event(device)

            except Exception as exc:
                if self._running:
                    logger.warning("USB monitor event error: %s", exc)

        logger.debug("USB monitor loop exited")

    def _handle_event(self, device: pyudev.Device) -> None:
        """Classify a USB event and dispatch the appropriate callback."""
        action = device.action
        sysfs_path = device.sys_path

        if action not in ("add", "remove"):
            return

        category = self._classify_device(device)

        logger.debug(
            "USB event: action=%s category=%s path=%s",
            action, category, sysfs_path,
        )

        if category == "unknown":
            return

        if action == "add":
            if category == "gpib_adapter":
                self._dispatch_callback(self.on_gpib_adapter_added, sysfs_path)
            elif category == "usbtmc":
                self._dispatch_callback(self.on_usbtmc_added, sysfs_path)
            elif category == "serial":
                self._dispatch_callback(self.on_serial_added, sysfs_path)
            elif category == "usb_raw":
                self._dispatch_callback(self.on_usbtmc_added, sysfs_path)

        elif action == "remove":
            if category == "gpib_adapter":
                self._dispatch_callback(
                    self.on_gpib_adapter_removed, sysfs_path
                )
            elif category == "usbtmc":
                self._dispatch_callback(self.on_usbtmc_removed, sysfs_path)
            elif category == "serial":
                self._dispatch_callback(self.on_serial_removed, sysfs_path)
            elif category == "usb_raw":
                self._dispatch_callback(self.on_usbtmc_removed, sysfs_path)

    # ------------------------------------------------------------------
    # Device classification
    # ------------------------------------------------------------------

    @staticmethod
    def _classify_device(device: pyudev.Device) -> str:
        """Classify a USB device into a category.

        Categories:
          "gpib_adapter"  -- NI GPIB-USB-HS, Keysight 82357B, etc.
          "usbtmc"        -- USB Test & Measurement Class device
          "serial"        -- USB-serial adapter (FTDI, CH340, CP210x)
          "usb_raw"       -- Known vendor-specific USB (e.g. Signal Recovery)
          "unknown"       -- Not instrument-related
        """
        vid = device.get("ID_VENDOR_ID", "")
        pid = device.get("ID_MODEL_ID", "")
        driver = device.get("ID_USB_DRIVER", "")

        # Known GPIB adapter VID:PID pairs
        if (vid.lower(), pid.lower()) in GPIB_ADAPTERS:
            return "gpib_adapter"

        # USB-TMC class devices
        if driver == "usbtmc":
            return "usbtmc"

        # USB-serial adapters
        if driver in _SERIAL_DRIVERS:
            return "serial"

        # Known raw USB instruments
        if vid.lower() in _RAW_USB_VIDS:
            return "usb_raw"

        return "unknown"

    # ------------------------------------------------------------------
    # Thread-safe callback dispatch
    # ------------------------------------------------------------------

    def _dispatch_callback(
        self,
        callback: Optional[Callable[[str], Awaitable[None]]],
        sysfs_path: str,
    ) -> None:
        """Schedule an async callback from the monitor thread.

        Uses loop.call_soon_threadsafe() to safely cross the
        thread boundary into the asyncio event loop.
        """
        if callback is None:
            return
        if self._loop is None or not self._loop.is_running():
            return

        try:
            self._loop.call_soon_threadsafe(
                asyncio.ensure_future,
                callback(sysfs_path),
            )
        except RuntimeError:
            # Event loop is closing
            pass
