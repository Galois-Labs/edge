"""FNIRSI DPS-150 programmable DC power supply wrapper.

The DPS-150 is a USB-PD powered DC power supply that communicates via
a custom binary register protocol over USB CDC serial (not SCPI).
The vendored driver at ``galois_edge.vendor.dps150`` handles all
protocol details; this wrapper adapts it to the daemon's SDK interface.

Key difference from SCPI instruments: the DPS-150 pushes telemetry
every ~500ms. All "read" methods return from a cached state dict
updated by a background thread — they do not perform I/O.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class DPS150Client:
    """SDK wrapper for the FNIRSI DPS-150 power supply."""

    def __init__(self, address: Optional[str] = None) -> None:
        self._address: Optional[str] = address
        self._device: object = None  # DPS150 instance when connected
        self._connected: bool = False

    # -- lifecycle -----------------------------------------------------------

    def connect(self, address: Optional[str] = None) -> None:
        if address is not None:
            self._address = address

        try:
            from galois_edge.vendor.dps150 import DPS150
        except ImportError:
            raise ImportError(
                "pyserial is required for DPS-150: pip install pyserial"
            )

        port = self._address
        if not port:
            from galois_edge.vendor.dps150 import find_dps150_port
            port = find_dps150_port()
            if not port:
                raise ConnectionError("No DPS-150 found — specify a port")

        self._device = DPS150(port)
        self._device.open()
        self._connected = True
        logger.info("DPS-150 connected on %s", port)

    def disconnect(self) -> None:
        if self._device is not None:
            try:
                self._device.close()
            except Exception:
                pass
        self._device = None
        self._connected = False

    def get_identity(self) -> str:
        d = self._dev
        model = d.model_name or "DPS-150"
        hw = d.hardware_version or "unknown"
        fw = d.firmware_version or "unknown"
        return f"FNIRSI,{model},{hw},{fw}"

    # -- output control ------------------------------------------------------

    def enable_output(self) -> str:
        self._dev.enable()
        return "OK"

    def disable_output(self) -> str:
        self._dev.disable()
        return "OK"

    # -- setpoints -----------------------------------------------------------

    def set_voltage(self, value: float = 0.0) -> str:
        self._dev.set_voltage(float(value))
        return "OK"

    def set_current(self, value: float = 0.0) -> str:
        self._dev.set_current(float(value))
        return "OK"

    # -- readback (from cached telemetry) ------------------------------------

    def get_output_voltage(self) -> str:
        return str(self._dev.output_voltage or 0.0)

    def get_output_current(self) -> str:
        return str(self._dev.output_current or 0.0)

    def get_output_power(self) -> str:
        return str(self._dev.output_power or 0.0)

    def get_input_voltage(self) -> str:
        return str(self._dev.input_voltage or 0.0)

    def get_temperature(self) -> str:
        return str(self._dev.temperature or 0.0)

    def get_voltage_setpoint(self) -> str:
        return str(self._dev.voltage_setpoint or 0.0)

    def get_current_setpoint(self) -> str:
        return str(self._dev.current_setpoint or 0.0)

    def get_output_state(self) -> str:
        closed = self._dev.output_closed
        return "ON" if closed else "OFF"

    def get_operating_mode(self) -> str:
        return self._dev.mode or "CV"

    def get_protection_state(self) -> str:
        return self._dev.protection_state or "OK"

    def get_output_capacity(self) -> str:
        return str(self._dev.output_capacity or 0.0)

    def get_output_energy(self) -> str:
        return str(self._dev.output_energy or 0.0)

    def get_state(self) -> str:
        return json.dumps(self._dev.state)

    # -- protection ----------------------------------------------------------

    def set_ovp(self, value: float = 0.0) -> str:
        self._dev.set_ovp(float(value))
        return "OK"

    def set_ocp(self, value: float = 0.0) -> str:
        self._dev.set_ocp(float(value))
        return "OK"

    def set_opp(self, value: float = 0.0) -> str:
        self._dev.set_opp(float(value))
        return "OK"

    def set_otp(self, value: float = 0.0) -> str:
        self._dev.set_otp(float(value))
        return "OK"

    def set_lvp(self, value: float = 0.0) -> str:
        self._dev.set_lvp(float(value))
        return "OK"

    # -- metering ------------------------------------------------------------

    def start_metering(self) -> str:
        self._dev.start_metering()
        return "OK"

    def stop_metering(self) -> str:
        self._dev.stop_metering()
        return "OK"

    # -- settings ------------------------------------------------------------

    def set_brightness(self, level: int = 5) -> str:
        self._dev.set_brightness(int(level))
        return "OK"

    def set_volume(self, level: int = 5) -> str:
        self._dev.set_volume(int(level))
        return "OK"

    # -- presets -------------------------------------------------------------

    def set_group_preset(self, group: int = 1, voltage: float = 0.0,
                         current: float = 0.0) -> str:
        self._dev.set_group_preset(int(group), float(voltage), float(current))
        return "OK"

    # -- internal ------------------------------------------------------------

    @property
    def _dev(self):
        if self._device is None:
            raise RuntimeError("DPS-150 not connected")
        return self._device
