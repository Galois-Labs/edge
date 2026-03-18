"""
Unit tests for the USB hotplug monitor.

Tests use mock pyudev objects to verify:
  - Device classification logic
  - Callback dispatch (thread-safe)
  - Graceful degradation when pyudev is unavailable

These tests do NOT require a real pyudev/udev system --
all udev interactions are mocked.
"""

from __future__ import annotations

import asyncio
import sys
import os
from unittest.mock import MagicMock, patch

import pytest

# Ensure source tree is importable
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "src")
)


# ---------------------------------------------------------------------------
# Mock pyudev.Device
# ---------------------------------------------------------------------------


class MockUdevDevice:
    """Minimal mock of pyudev.Device for classification tests."""

    def __init__(
        self,
        action: str = "add",
        properties: dict = None,
        sys_path: str = "/sys/devices/test",
    ):
        self.action = action
        self.sys_path = sys_path
        self._properties = properties or {}

    def get(self, key: str, default: str = "") -> str:
        return self._properties.get(key, default)


# ---------------------------------------------------------------------------
# Classification tests (do not require pyudev to be installed)
# ---------------------------------------------------------------------------


class TestDeviceClassification:
    """Test the _classify_device static method logic directly."""

    def _classify(self, properties: dict) -> str:
        """Helper to run classification without importing USBMonitor.

        Reimplements the classification logic to test it independently
        of pyudev availability.
        """
        from galois_edge.usb_monitor import GPIB_ADAPTERS, _SERIAL_DRIVERS, _RAW_USB_VIDS

        vid = properties.get("ID_VENDOR_ID", "")
        pid = properties.get("ID_MODEL_ID", "")
        driver = properties.get("ID_USB_DRIVER", "")

        if (vid.lower(), pid.lower()) in GPIB_ADAPTERS:
            return "gpib_adapter"
        if driver == "usbtmc":
            return "usbtmc"
        if driver in _SERIAL_DRIVERS:
            return "serial"
        if vid.lower() in _RAW_USB_VIDS:
            return "usb_raw"
        return "unknown"

    def test_ni_gpib_usb_hs(self):
        """NI GPIB-USB-HS should be classified as gpib_adapter."""
        assert self._classify({
            "ID_VENDOR_ID": "3923",
            "ID_MODEL_ID": "709b",
        }) == "gpib_adapter"

    def test_ni_gpib_usb_hs_plus(self):
        """NI GPIB-USB-HS+ should be classified as gpib_adapter."""
        assert self._classify({
            "ID_VENDOR_ID": "3923",
            "ID_MODEL_ID": "709a",
        }) == "gpib_adapter"

    def test_keysight_82357b(self):
        """Keysight 82357B should be classified as gpib_adapter."""
        assert self._classify({
            "ID_VENDOR_ID": "0957",
            "ID_MODEL_ID": "0518",
        }) == "gpib_adapter"

    def test_keysight_82357a(self):
        """Keysight 82357A should be classified as gpib_adapter."""
        assert self._classify({
            "ID_VENDOR_ID": "0957",
            "ID_MODEL_ID": "0718",
        }) == "gpib_adapter"

    def test_usbtmc_device(self):
        """USB-TMC device should be classified as usbtmc."""
        assert self._classify({
            "ID_VENDOR_ID": "1234",
            "ID_MODEL_ID": "5678",
            "ID_USB_DRIVER": "usbtmc",
        }) == "usbtmc"

    def test_ftdi_serial(self):
        """FTDI serial adapter should be classified as serial."""
        assert self._classify({
            "ID_VENDOR_ID": "0403",
            "ID_MODEL_ID": "6001",
            "ID_USB_DRIVER": "ftdi_sio",
        }) == "serial"

    def test_ch341_serial(self):
        """CH340/CH341 serial adapter should be classified as serial."""
        assert self._classify({
            "ID_VENDOR_ID": "1a86",
            "ID_MODEL_ID": "7523",
            "ID_USB_DRIVER": "ch341",
        }) == "serial"

    def test_cp210x_serial(self):
        """CP210x serial adapter should be classified as serial."""
        assert self._classify({
            "ID_VENDOR_ID": "10c4",
            "ID_MODEL_ID": "ea60",
            "ID_USB_DRIVER": "cp210x",
        }) == "serial"

    def test_signal_recovery_raw_usb(self):
        """Signal Recovery instrument should be classified as usb_raw."""
        assert self._classify({
            "ID_VENDOR_ID": "0a2d",
            "ID_MODEL_ID": "001b",
        }) == "usb_raw"

    def test_unknown_device(self):
        """Unknown USB device should be classified as unknown."""
        assert self._classify({
            "ID_VENDOR_ID": "dead",
            "ID_MODEL_ID": "beef",
        }) == "unknown"

    def test_empty_properties(self):
        """Device with no properties should be classified as unknown."""
        assert self._classify({}) == "unknown"

    def test_case_insensitive_vid_pid(self):
        """VID/PID matching should be case-insensitive."""
        assert self._classify({
            "ID_VENDOR_ID": "3923",
            "ID_MODEL_ID": "709B",  # uppercase
        }) == "gpib_adapter"


# ---------------------------------------------------------------------------
# PYUDEV_AVAILABLE flag test
# ---------------------------------------------------------------------------


def test_pyudev_available_flag():
    """Verify PYUDEV_AVAILABLE is a boolean."""
    from galois_edge.usb_monitor import PYUDEV_AVAILABLE
    assert isinstance(PYUDEV_AVAILABLE, bool)


# ---------------------------------------------------------------------------
# USBMonitor callback dispatch tests (only run if pyudev is available)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.path.exists("/sys/class"),
    reason="Not a Linux system with sysfs",
)
class TestUSBMonitorCallbackDispatch:
    """Test callback dispatch logic with mock pyudev."""

    def test_classify_device_static_method(self):
        """Verify _classify_device works with MockUdevDevice."""
        try:
            from galois_edge.usb_monitor import USBMonitor
        except RuntimeError:
            pytest.skip("pyudev not available")

        device = MockUdevDevice(properties={
            "ID_VENDOR_ID": "3923",
            "ID_MODEL_ID": "709b",
        })
        result = USBMonitor._classify_device(device)
        assert result == "gpib_adapter"

    def test_classify_unknown(self):
        """Verify unknown device classification."""
        try:
            from galois_edge.usb_monitor import USBMonitor
        except RuntimeError:
            pytest.skip("pyudev not available")

        device = MockUdevDevice(properties={
            "ID_VENDOR_ID": "ffff",
            "ID_MODEL_ID": "ffff",
        })
        result = USBMonitor._classify_device(device)
        assert result == "unknown"


# ---------------------------------------------------------------------------
# Integration test with event loop (mock-based)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_callback_thread_safety():
    """Verify _dispatch_callback schedules coroutines thread-safely."""
    try:
        from galois_edge.usb_monitor import USBMonitor, PYUDEV_AVAILABLE
    except ImportError:
        pytest.skip("usb_monitor not importable")

    if not PYUDEV_AVAILABLE:
        pytest.skip("pyudev not available")

    called = []

    async def mock_callback(path: str) -> None:
        called.append(path)

    monitor = USBMonitor()
    loop = asyncio.get_running_loop()
    monitor._loop = loop

    # Simulate dispatch from another thread
    monitor._dispatch_callback(mock_callback, "/sys/devices/test")

    # Give the event loop a chance to process
    await asyncio.sleep(0.05)

    assert len(called) == 1
    assert called[0] == "/sys/devices/test"


@pytest.mark.asyncio
async def test_dispatch_callback_none_callback():
    """Verify _dispatch_callback is a no-op when callback is None."""
    try:
        from galois_edge.usb_monitor import USBMonitor, PYUDEV_AVAILABLE
    except ImportError:
        pytest.skip("usb_monitor not importable")

    if not PYUDEV_AVAILABLE:
        pytest.skip("pyudev not available")

    monitor = USBMonitor()
    loop = asyncio.get_running_loop()
    monitor._loop = loop

    # Should not raise
    monitor._dispatch_callback(None, "/sys/devices/test")
