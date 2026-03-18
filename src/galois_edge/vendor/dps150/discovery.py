"""USB device discovery for the FNIRSI DPS-150.

The DPS-150 enumerates as USB VID 0x2E3C / PID 0x5740
(Artery AT32 Virtual Com Port).
"""

from __future__ import annotations

from serial.tools import list_ports

DPS150_VID = 0x2E3C
DPS150_PID = 0x5740


def find_dps150_port() -> str | None:
    """Return the first serial port matching the DPS-150, or None."""
    for info in list_ports.comports():
        if info.vid == DPS150_VID and info.pid == DPS150_PID:
            return info.device
    return None


def list_dps150_ports() -> list[str]:
    """Return all serial ports matching the DPS-150."""
    return [
        info.device
        for info in list_ports.comports()
        if info.vid == DPS150_VID and info.pid == DPS150_PID
    ]
