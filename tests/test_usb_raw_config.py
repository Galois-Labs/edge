"""F2: USB_RAW_ENABLED reaches InstrumentManager, so raw-USB scanning can be turned off.

The Go supervisor writes USB_RAW_ENABLED (default true) into the engine's environment;
the engine used to ignore it and always build a USBTransport when pyusb was present.
"""
from __future__ import annotations

import pytest

from galois_edge.config import Config
from galois_edge.main import EdgeDaemon

pytestmark = pytest.mark.critical


class _RecordingUSBTransport:
    """Stands in for pyusb's USBTransport: records construction, touches no device."""

    built = 0

    def __init__(self) -> None:
        type(self).built += 1

    is_available = True


@pytest.fixture
def build_instrument_manager(tmp_path, monkeypatch):
    """main.py's real _build_instrument_manager with pyusb 'present' and nothing else enumerating."""
    monkeypatch.setattr("galois_edge.instrument_manager.PYVISA_AVAILABLE", False)
    monkeypatch.setattr("galois_edge.instrument_manager.USB_AVAILABLE", True)
    monkeypatch.setattr(_RecordingUSBTransport, "built", 0)
    monkeypatch.setattr("galois_edge.instrument_manager.USBTransport", _RecordingUSBTransport)
    daemons = []

    def build():
        cfg = Config(gpib_enabled=False, usb_monitor_enabled=False, lan_instruments="",
                     include_serial_ports=False, profile_dir=str(tmp_path / "bundled"),
                     dynamic_profile_dir=str(tmp_path / "dynamic"), driver_profile_dir=str(tmp_path / "drivers"),
                     mcp_enabled=False, demo=False, sim_mode=False, visa_backend="", trace_dir="")
        daemon = EdgeDaemon(cfg)
        daemons.append(daemon)
        return daemon._build_instrument_manager()

    yield build
    for daemon in daemons:
        daemon._io_executor.shutdown(wait=False)


@pytest.mark.parametrize("raw, expected", [(None, True), ("true", True), ("false", False), ("0", False)])
def test_config_reads_usb_raw_enabled_default_true(monkeypatch, raw, expected):
    if raw is not None:
        monkeypatch.setenv("USB_RAW_ENABLED", raw)
    assert Config().usb_raw_enabled is expected


def test_usb_raw_enabled_false_creates_no_usb_transport(monkeypatch, build_instrument_manager):
    monkeypatch.setenv("USB_RAW_ENABLED", "false")

    im = build_instrument_manager()

    assert _RecordingUSBTransport.built == 0
    assert im.usb_available is False


def test_usb_raw_enabled_by_default_still_creates_the_usb_transport(build_instrument_manager):
    im = build_instrument_manager()

    assert _RecordingUSBTransport.built == 1
    assert im.usb_available is True
