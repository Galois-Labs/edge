"""Digilent WaveForms (DWF) device wrapper — Analog Discovery, etc.

Wraps the ``dwfpy`` package to expose power supply control, oscilloscope
acquisition, waveform generation, and digital I/O through the daemon's
SDK executor interface.  All public methods return strings (the SDK
executor serialises results into gRPC responses).

Requires: ``pip install dwfpy``
"""

from __future__ import annotations

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class DigilentDwfClient:
    """SDK wrapper for Digilent WaveForms devices."""

    def __init__(self, serial_number: Optional[str] = None) -> None:
        self._serial_number: Optional[str] = serial_number
        self._device: object = None  # dwfpy.Device when connected
        self._connected: bool = False

    # -- lifecycle -----------------------------------------------------------

    def connect(self, serial_number: Optional[str] = None) -> None:
        if serial_number is not None:
            self._serial_number = serial_number

        try:
            import dwfpy  # noqa: F811
        except ImportError:
            raise ImportError(
                "dwfpy is required for Digilent WaveForms devices: "
                "pip install dwfpy"
            )

        sn = self._serial_number if self._serial_number else None
        try:
            if sn:
                dev = dwfpy.Device(serial_number=str(sn))
            else:
                dev = dwfpy.Device()
            dev.open()
        except Exception as exc:
            raise ConnectionError(
                f"Failed to open Digilent device (SN={sn}): {exc}"
            ) from exc

        self._device = dev
        self._connected = True
        logger.info(
            "Digilent WaveForms device connected%s",
            f" (SN={sn})" if sn else "",
        )

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
        try:
            name = d.name or "WaveForms"
        except Exception:
            name = "WaveForms"
        try:
            serial = d.serial_number or "unknown"
        except Exception:
            serial = "unknown"
        try:
            import dwfpy
            version = getattr(dwfpy, "__version__", "unknown")
        except Exception:
            version = "unknown"
        return f"Digilent,{name},{serial},{version}"

    # -- internal helper -----------------------------------------------------

    @property
    def _dev(self):
        if self._device is None:
            raise RuntimeError("Digilent device not connected")
        return self._device

    # ========================================================================
    # Power Supply (AnalogIO subsystem)
    # ========================================================================

    # -- Positive Supply (Ch0) -----------------------------------------------

    def enable_positive_supply(self) -> str:
        aio = self._dev.analog_io
        aio[0]["Enable"].value = 1
        aio.configure()
        return "OK"

    def disable_positive_supply(self) -> str:
        aio = self._dev.analog_io
        aio[0]["Enable"].value = 0
        aio.configure()
        return "OK"

    def set_positive_supply_voltage(self, value: float = 5.0) -> str:
        aio = self._dev.analog_io
        aio[0]["Voltage"].value = float(value)
        aio.configure()
        return "OK"

    def get_positive_supply_voltage(self) -> str:
        aio = self._dev.analog_io
        aio.read_status()
        return str(aio[0]["Voltage"].status)

    def get_positive_supply_current(self) -> str:
        aio = self._dev.analog_io
        aio.read_status()
        return str(aio[0]["Current"].status)

    # -- Negative Supply (Ch1) -----------------------------------------------

    def enable_negative_supply(self) -> str:
        aio = self._dev.analog_io
        aio[1]["Enable"].value = 1
        aio.configure()
        return "OK"

    def disable_negative_supply(self) -> str:
        aio = self._dev.analog_io
        aio[1]["Enable"].value = 0
        aio.configure()
        return "OK"

    def set_negative_supply_voltage(self, value: float = -5.0) -> str:
        aio = self._dev.analog_io
        aio[1]["Voltage"].value = float(value)
        aio.configure()
        return "OK"

    def get_negative_supply_voltage(self) -> str:
        aio = self._dev.analog_io
        aio.read_status()
        return str(aio[1]["Voltage"].status)

    def get_negative_supply_current(self) -> str:
        aio = self._dev.analog_io
        aio.read_status()
        return str(aio[1]["Current"].status)

    # -- System Monitor (Ch2) ------------------------------------------------

    def get_usb_voltage(self) -> str:
        aio = self._dev.analog_io
        aio.read_status()
        return str(aio[2]["Voltage"].status)

    def get_usb_current(self) -> str:
        aio = self._dev.analog_io
        aio.read_status()
        return str(aio[2]["Current"].status)

    def get_temperature(self) -> str:
        aio = self._dev.analog_io
        aio.read_status()
        return str(aio[2]["Temperature"].status)

    # -- Supply Monitor (Ch3) — read-only ------------------------------------

    def get_supply_monitor_voltage(self) -> str:
        aio = self._dev.analog_io
        aio.read_status()
        return str(aio[3]["Voltage"].status)

    def get_supply_monitor_current(self) -> str:
        aio = self._dev.analog_io
        aio.read_status()
        return str(aio[3]["Current"].status)

    # -- Master enable -------------------------------------------------------

    def enable_supplies(self) -> str:
        aio = self._dev.analog_io
        aio.master_enable = True
        aio.configure()
        return "OK"

    def disable_supplies(self) -> str:
        aio = self._dev.analog_io
        aio.master_enable = False
        aio.configure()
        return "OK"

    # ========================================================================
    # Oscilloscope (AnalogInput subsystem)
    # ========================================================================

    def read_analog(
        self,
        channel: int = 0,
        v_range: float = 5.0,
        samples: int = 1000,
        frequency: float = 1000000.0,
    ) -> str:
        dev = self._dev
        ai = dev.analog_input
        ai.setup_channel(int(channel), range=float(v_range))
        ai.setup_acquisition(
            sample_rate=float(frequency),
            buffer_size=int(samples),
        )
        ai.single()
        import dwfpy
        ai.wait_for_status(dwfpy.Status.DONE, read_data=True)
        data = ai.channels[int(channel)].get_data()
        if data is None:
            return "[]"
        try:
            samples_list = data.tolist()
        except AttributeError:
            samples_list = list(data)
        return json.dumps(samples_list)

    def get_analog_voltage(
        self,
        channel: int = 0,
        v_range: float = 5.0,
    ) -> str:
        """Quick DC measurement — single short acquisition, return mean."""
        dev = self._dev
        ai = dev.analog_input
        ai.setup_channel(int(channel), range=float(v_range))
        ai.setup_acquisition(sample_rate=100000.0, buffer_size=100)
        ai.single()
        import dwfpy
        ai.wait_for_status(dwfpy.Status.DONE, read_data=True)
        data = ai.channels[int(channel)].get_data()
        if data is None:
            return "0.0"
        try:
            mean_val = float(data.mean())
        except AttributeError:
            mean_val = sum(data) / len(data) if len(data) else 0.0
        return str(mean_val)

    # ========================================================================
    # Waveform Generator (AnalogOutput subsystem)
    # ========================================================================

    def set_waveform(
        self,
        channel: int = 0,
        function: str = "sine",
        frequency: float = 1000.0,
        amplitude: float = 1.0,
        offset: float = 0.0,
    ) -> str:
        dev = self._dev
        dev.analog_output[int(channel)].setup(
            function=function,
            frequency=float(frequency),
            amplitude=float(amplitude),
            offset=float(offset),
            start=True,
        )
        return "OK"

    def stop_waveform(self, channel: int = 0) -> str:
        dev = self._dev
        dev.analog_output[int(channel)].setup(start=False)
        return "OK"

    # ========================================================================
    # Digital I/O
    # ========================================================================

    def set_digital_io(self, pin: int = 0, value: int = 0) -> str:
        dio = self._dev.digital_io
        pin = int(pin)
        value = int(value)
        # Enable pin as output
        oe = dio.output_enable
        oe |= (1 << pin)
        dio.output_enable = oe
        # Set value
        out = dio.output
        if value:
            out |= (1 << pin)
        else:
            out &= ~(1 << pin)
        dio.output = out
        dio.configure()
        return "OK"

    def get_digital_io(self) -> str:
        dio = self._dev.digital_io
        dio.read_status()
        return f"0x{dio.input:04X}"

    def set_digital_bus(self, mask: int = 0, value: int = 0) -> str:
        dio = self._dev.digital_io
        mask = int(mask)
        value = int(value)
        # Enable masked pins as outputs
        oe = dio.output_enable
        oe |= mask
        dio.output_enable = oe
        # Set values for masked pins
        out = dio.output
        out = (out & ~mask) | (value & mask)
        dio.output = out
        dio.configure()
        return "OK"
