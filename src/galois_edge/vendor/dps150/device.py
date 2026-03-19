"""High-level DPS-150 device interface.

Provides a Pythonic API with context-manager support, automatic session
management, and state tracking via background telemetry.
"""

from __future__ import annotations

import logging
import time
import threading
from dataclasses import dataclass
from typing import Any, Callable, List

from . import protocol as proto
from .discovery import find_dps150_port
from .exceptions import ValueOutOfRange
from .transport import SerialTransport

log = logging.getLogger(__name__)

TelemetryCallback = Callable[[dict[str, Any]], None]

# Absolute hardware limits (from DPS-150 specs / protocol doc).
# Device-reported limits (upper_limit_voltage/current) are dynamic
# and depend on input power — these are the ceilings.
_MAX_VOLTAGE = 30.0
_MAX_CURRENT = 5.5
_MAX_OVP = 30.0
_MAX_OCP = 5.5
_MAX_OPP = 160.0
_MAX_OTP = 100.0
_MAX_LVP = 30.0
_MAX_BRIGHTNESS = 10
_MAX_VOLUME = 10


@dataclass
class SweepPoint:
    """A single measurement taken during a voltage or current sweep."""
    voltage: float
    current: float
    power: float
    temperature: float


class DPS150:
    """Control interface for an FNIRSI DPS-150 power supply.

    Usage::

        with DPS150("/dev/ttyACM0") as dps:
            dps.set_voltage(5.0)
            dps.set_current(1.0)
            dps.enable()
            print(dps.output_voltage)
    """

    def __init__(
        self,
        port: str | None = None,
        callback: TelemetryCallback | None = None,
        *,
        rtscts: bool = True,
        inter_command_delay: float = 0.05,
    ) -> None:
        if port is None:
            port = find_dps150_port()
            if port is None:
                from .exceptions import ConnectionError as ConnErr
                raise ConnErr("No DPS-150 found. Specify a port manually.")
        self._transport = SerialTransport(
            port, rtscts=rtscts, inter_command_delay=inter_command_delay
        )
        self._user_callback = callback
        self._state: dict[str, Any] = {}
        self._init_event = threading.Event()

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> DPS150:
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def open(self, *, timeout: float = 5.0) -> None:
        """Open the connection and run the DPS-150 init sequence."""
        self._transport.open(callback=self._on_packet)
        # Init sequence (matches JS initCommand)
        self._send(proto.SESSION_ENABLE)
        self._send(proto.BAUD_SELECT_115200)
        self._send(proto.build_packet(
            proto.HEADER_OUTPUT, proto.CMD_GET, proto.MODEL_NAME, 0))
        self._send(proto.build_packet(
            proto.HEADER_OUTPUT, proto.CMD_GET, proto.HARDWARE_VERSION, 0))
        self._send(proto.build_packet(
            proto.HEADER_OUTPUT, proto.CMD_GET, proto.FIRMWARE_VERSION, 0))
        self.get_all()
        self._init_event.wait(timeout=timeout)

    def close(self) -> None:
        """Send session-disable and close the serial port."""
        try:
            self._send(proto.SESSION_DISABLE)
        except Exception:
            pass
        self._transport.close()

    # ------------------------------------------------------------------
    # State properties (read-only, updated by background telemetry)
    # ------------------------------------------------------------------

    @property
    def state(self) -> dict[str, Any]:
        """Snapshot of the last-known device state."""
        return dict(self._state)

    @property
    def input_voltage(self) -> float | None:
        return self._state.get("input_voltage")

    @property
    def voltage_setpoint(self) -> float | None:
        """Last-known voltage setpoint from device state."""
        return self._state.get("set_voltage")

    @property
    def current_setpoint(self) -> float | None:
        """Last-known current setpoint from device state."""
        return self._state.get("set_current")

    @property
    def output_voltage(self) -> float | None:
        return self._state.get("output_voltage")

    @property
    def output_current(self) -> float | None:
        return self._state.get("output_current")

    @property
    def output_power(self) -> float | None:
        return self._state.get("output_power")

    @property
    def temperature(self) -> float | None:
        return self._state.get("temperature")

    @property
    def output_closed(self) -> bool | None:
        return self._state.get("output_closed")

    @property
    def protection_state(self) -> str | None:
        return self._state.get("protection_state")

    @property
    def mode(self) -> str | None:
        return self._state.get("mode")

    @property
    def model_name(self) -> str | None:
        return self._state.get("model_name")

    @property
    def hardware_version(self) -> str | None:
        return self._state.get("hardware_version")

    @property
    def firmware_version(self) -> str | None:
        return self._state.get("firmware_version")

    @property
    def upper_limit_voltage(self) -> float | None:
        return self._state.get("upper_limit_voltage")

    @property
    def upper_limit_current(self) -> float | None:
        return self._state.get("upper_limit_current")

    @property
    def output_capacity(self) -> float | None:
        return self._state.get("output_capacity")

    @property
    def output_energy(self) -> float | None:
        return self._state.get("output_energy")

    @property
    def brightness(self) -> int | None:
        return self._state.get("brightness")

    @property
    def volume(self) -> int | None:
        return self._state.get("volume")

    # ------------------------------------------------------------------
    # Output control
    # ------------------------------------------------------------------

    def enable(self) -> None:
        """Turn the output ON."""
        self.set_byte_value(proto.OUTPUT_ENABLE, 1)

    def disable(self) -> None:
        """Turn the output OFF."""
        self.set_byte_value(proto.OUTPUT_ENABLE, 0)

    # ------------------------------------------------------------------
    # Setpoints
    # ------------------------------------------------------------------

    def set_voltage(self, volts: float) -> None:
        """Set the output voltage (V)."""
        self._check_range("voltage", volts, 0, self._voltage_ceiling())
        self.set_float_value(proto.VOLTAGE_SET, volts)

    def set_current(self, amps: float) -> None:
        """Set the output current limit (A)."""
        self._check_range("current", amps, 0, self._current_ceiling())
        self.set_float_value(proto.CURRENT_SET, amps)

    # ------------------------------------------------------------------
    # Protection
    # ------------------------------------------------------------------

    def set_ovp(self, volts: float) -> None:
        self._check_range("OVP", volts, 0, _MAX_OVP)
        self.set_float_value(proto.OVP, volts)

    def set_ocp(self, amps: float) -> None:
        self._check_range("OCP", amps, 0, _MAX_OCP)
        self.set_float_value(proto.OCP, amps)

    def set_opp(self, watts: float) -> None:
        self._check_range("OPP", watts, 0, _MAX_OPP)
        self.set_float_value(proto.OPP, watts)

    def set_otp(self, celsius: float) -> None:
        self._check_range("OTP", celsius, 0, _MAX_OTP)
        self.set_float_value(proto.OTP, celsius)

    def set_lvp(self, volts: float) -> None:
        self._check_range("LVP", volts, 0, _MAX_LVP)
        self.set_float_value(proto.LVP, volts)

    # ------------------------------------------------------------------
    # Metering
    # ------------------------------------------------------------------

    def start_metering(self) -> None:
        self.set_byte_value(proto.METERING_ENABLE, 1)

    def stop_metering(self) -> None:
        self.set_byte_value(proto.METERING_ENABLE, 0)

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def set_brightness(self, level: int) -> None:
        self._check_range("brightness", level, 0, _MAX_BRIGHTNESS)
        self.set_byte_value(proto.BRIGHTNESS, level)

    def set_volume(self, level: int) -> None:
        self._check_range("volume", level, 0, _MAX_VOLUME)
        self.set_byte_value(proto.VOLUME, level)

    # ------------------------------------------------------------------
    # Presets
    # ------------------------------------------------------------------

    def set_group_preset(self, group: int, voltage: float, current: float) -> None:
        """Program a preset memory group (1-6)."""
        if not 1 <= group <= 6:
            raise ValueError(f"group must be 1-6, got {group}")
        self._check_range("voltage", voltage, 0, self._voltage_ceiling())
        self._check_range("current", current, 0, self._current_ceiling())
        v_reg = proto.GROUP1_VOLTAGE_SET + (group - 1) * 2
        i_reg = proto.GROUP1_CURRENT_SET + (group - 1) * 2
        self.set_float_value(v_reg, voltage)
        self.set_float_value(i_reg, current)

    # ------------------------------------------------------------------
    # Sweep functions
    # ------------------------------------------------------------------

    def sweep_voltage(
        self,
        start: float,
        stop: float,
        step: float,
        *,
        dwell: float = 0.5,
    ) -> List[SweepPoint]:
        """Step voltage from *start* to *stop* and measure at each point.

        The output must already be enabled. Current limit is unchanged.
        After the sweep the voltage is left at the last step value.

        Args:
            start: Starting voltage (V).
            stop:  Ending voltage (V), inclusive.
            step:  Voltage increment per step (V). Must be > 0.
            dwell: Seconds to wait at each step before measuring.

        Returns:
            List of :class:`SweepPoint` measurements.
        """
        if step <= 0:
            raise ValueError(f"step must be > 0, got {step}")
        ceiling = self._voltage_ceiling()
        self._check_range("sweep start voltage", start, 0, ceiling)
        self._check_range("sweep stop voltage", stop, 0, ceiling)

        points: List[SweepPoint] = []
        v = start
        ascending = start <= stop
        while (v <= stop + 1e-9) if ascending else (v >= stop - 1e-9):
            self.set_float_value(proto.VOLTAGE_SET, v)
            time.sleep(dwell)
            self.get_all()
            time.sleep(0.15)  # let the response arrive
            points.append(self._snapshot())
            v += step if ascending else -step
        return points

    def sweep_current(
        self,
        start: float,
        stop: float,
        step: float,
        *,
        dwell: float = 0.5,
    ) -> List[SweepPoint]:
        """Step current limit from *start* to *stop* and measure at each point.

        The output must already be enabled. Voltage setpoint is unchanged.
        After the sweep the current limit is left at the last step value.

        Args:
            start: Starting current (A).
            stop:  Ending current (A), inclusive.
            step:  Current increment per step (A). Must be > 0.
            dwell: Seconds to wait at each step before measuring.

        Returns:
            List of :class:`SweepPoint` measurements.
        """
        if step <= 0:
            raise ValueError(f"step must be > 0, got {step}")
        ceiling = self._current_ceiling()
        self._check_range("sweep start current", start, 0, ceiling)
        self._check_range("sweep stop current", stop, 0, ceiling)

        points: List[SweepPoint] = []
        i = start
        ascending = start <= stop
        while (i <= stop + 1e-9) if ascending else (i >= stop - 1e-9):
            self.set_float_value(proto.CURRENT_SET, i)
            time.sleep(dwell)
            self.get_all()
            time.sleep(0.15)
            points.append(self._snapshot())
            i += step if ascending else -step
        return points

    # ------------------------------------------------------------------
    # Generic value setters / query
    # ------------------------------------------------------------------

    def set_float_value(self, register: int, value: float) -> None:
        self._send(proto.build_float_packet(
            proto.HEADER_OUTPUT, proto.CMD_SET, register, value))

    def set_byte_value(self, register: int, value: int) -> None:
        self._send(proto.build_packet(
            proto.HEADER_OUTPUT, proto.CMD_SET, register, value))

    def get_all(self) -> None:
        """Request the full device state dump."""
        self._send(proto.build_packet(
            proto.HEADER_OUTPUT, proto.CMD_GET, proto.ALL, 0))

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _send(self, data: bytes) -> None:
        self._transport.send(data)

    def _on_packet(self, register: int, payload: bytes) -> None:
        parsed = proto.parse_register(register, payload)
        if parsed:
            self._state.update(parsed)
            if register == proto.ALL:
                self._init_event.set()
            if self._user_callback is not None:
                try:
                    self._user_callback(parsed)
                except Exception:
                    log.exception("Error in user telemetry callback")

    def _voltage_ceiling(self) -> float:
        """Best available voltage upper bound — device-reported or hardware max."""
        return self._state.get("upper_limit_voltage") or _MAX_VOLTAGE

    def _current_ceiling(self) -> float:
        """Best available current upper bound — device-reported or hardware max."""
        return self._state.get("upper_limit_current") or _MAX_CURRENT

    @staticmethod
    def _check_range(name: str, value: float, lo: float, hi: float) -> None:
        if value < lo or value > hi:
            raise ValueOutOfRange(
                f"{name} {value} is outside allowed range [{lo}, {hi}]"
            )

    def _snapshot(self) -> SweepPoint:
        """Capture current telemetry as a SweepPoint."""
        return SweepPoint(
            voltage=self._state.get("output_voltage", 0.0),
            current=self._state.get("output_current", 0.0),
            power=self._state.get("output_power", 0.0),
            temperature=self._state.get("temperature", 0.0),
        )
