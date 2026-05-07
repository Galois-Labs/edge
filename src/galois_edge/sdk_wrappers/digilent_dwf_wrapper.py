"""Digilent WaveForms (DWF) device wrapper — Analog Discovery, etc.

Wraps the ``dwfpy`` package to expose oscilloscope, waveform generator,
power supply, logic analyzer, pattern generator, digital I/O, and
protocol analyzers (UART, SPI, I2C) through the daemon's SDK executor.

All public methods return strings (SDK executor serialises to gRPC).

Requires: ``pip install dwfpy``  +  OS: digilent.adept.runtime + digilent.waveforms
"""

from __future__ import annotations

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)


# MCP_TOOL_SPECS: agent-callable surface (Phase 3). Subset of the wrapper's
# public methods chosen for clear semantics; raw protocol bus methods
# (uart_*, spi_*, i2c_*) are intentionally omitted to avoid surfacing
# arbitrary-byte-injection paths to agents without explicit operator
# acknowledgement.
MCP_TOOL_SPECS = [
    {
        "name": "enable_positive_supply",
        "description": "Turn on the V+ programmable supply.",
        "params": {
            "instrument_id": {"type": "string", "description": "DWF device id"},
        },
        "is_dangerous": True,
    },
    {
        "name": "disable_positive_supply",
        "description": "Turn off the V+ programmable supply.",
        "params": {
            "instrument_id": {"type": "string", "description": "DWF device id"},
        },
        "is_dangerous": False,
    },
    {
        "name": "set_positive_supply_voltage",
        "description": "Set the V+ programmable supply voltage.",
        "params": {
            "instrument_id": {"type": "string", "description": "DWF device id"},
            "value": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 5.0,
                "unit": "V",
            },
        },
        "is_dangerous": True,
    },
    {
        "name": "scope_measure_dc",
        "description": "One-shot DC voltage measurement on a scope channel.",
        "params": {
            "instrument_id": {"type": "string", "description": "DWF device id"},
            "channel": {"type": "integer", "minimum": 0, "maximum": 1},
            "v_range": {
                "type": "number",
                "minimum": 0.05,
                "maximum": 50.0,
                "unit": "V",
            },
        },
        "is_dangerous": False,
    },
    {
        "name": "scope_measure_peak_to_peak",
        "description": "Measure peak-to-peak voltage on a scope channel.",
        "params": {
            "instrument_id": {"type": "string", "description": "DWF device id"},
            "channel": {"type": "integer", "minimum": 0, "maximum": 1},
            "v_range": {
                "type": "number",
                "minimum": 0.05,
                "maximum": 50.0,
                "unit": "V",
            },
            "sample_rate": {
                "type": "number",
                "minimum": 1000.0,
                "maximum": 100_000_000.0,
                "unit": "Sa/s",
            },
        },
        "is_dangerous": False,
    },
    {
        "name": "scope_measure_frequency",
        "description": "Estimate signal frequency via zero-crossing count.",
        "params": {
            "instrument_id": {"type": "string", "description": "DWF device id"},
            "channel": {"type": "integer", "minimum": 0, "maximum": 1},
            "v_range": {
                "type": "number",
                "minimum": 0.05,
                "maximum": 50.0,
                "unit": "V",
            },
        },
        "is_dangerous": False,
    },
    {
        "name": "wavegen_setup",
        "description": "Configure and start the waveform generator output.",
        "params": {
            "instrument_id": {"type": "string", "description": "DWF device id"},
            "channel": {"type": "integer", "minimum": 0, "maximum": 1},
            "function": {"type": "string", "description": "sine | square | triangle | dc | custom"},
            "frequency": {
                "type": "number",
                "minimum": 0.01,
                "maximum": 25_000_000.0,
                "unit": "Hz",
            },
            "amplitude": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 5.0,
                "unit": "V",
            },
            "offset": {
                "type": "number",
                "minimum": -5.0,
                "maximum": 5.0,
                "unit": "V",
            },
        },
        "is_dangerous": True,
    },
    {
        "name": "wavegen_stop",
        "description": "Stop waveform generator output on a channel.",
        "params": {
            "instrument_id": {"type": "string", "description": "DWF device id"},
            "channel": {"type": "integer", "minimum": 0, "maximum": 1},
        },
        "is_dangerous": False,
    },
]


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
            import dwfpy
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

    def get_device_info(self) -> str:
        """Return device capabilities as JSON."""
        d = self._dev
        ai = d.analog_input
        ao = d.analog_output
        info = {
            "name": getattr(d, "name", "unknown"),
            "serial": getattr(d, "serial_number", "unknown"),
            "scope_channels": len(ai.channels),
            "scope_max_freq": ai.frequency_max,
            "scope_max_buffer": ai.buffer_size_max,
            "wavegen_channels": len(ao.channels),
            "digital_channels": len(d.digital_output.channels),
            "analog_io_channels": len(d.analog_io.channels),
        }
        return json.dumps(info)

    @property
    def _dev(self):
        if self._device is None:
            raise RuntimeError("Digilent device not connected")
        return self._device

    # ========================================================================
    # Power Supply (AnalogIO)
    # ========================================================================

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
    # Oscilloscope (AnalogInput)
    # ========================================================================

    def scope_acquire(
        self,
        channel: int = 0,
        v_range: float = 5.0,
        samples: int = 4096,
        sample_rate: float = 1000000.0,
        trigger_level: float = 0.0,
        trigger_channel: int = -1,
        offset: float = 0.0,
        coupling: str = "dc",
    ) -> str:
        """Single-shot oscilloscope acquisition. Returns JSON array of voltages."""
        ai = self._dev.analog_input
        ch = int(channel)
        ai.setup_channel(ch, range=float(v_range), offset=float(offset),
                         coupling=coupling, enabled=True)
        # Auto-trigger timeout prevents hanging forever
        ai.trigger.auto_timeout = 5.0
        trig_ch = int(trigger_channel)
        if trig_ch >= 0:
            ai.setup_edge_trigger(
                channel=trig_ch,
                slope="rising",
                level=float(trigger_level),
            )
        # single(start=True) blocks until done and auto-transfers data
        ai.single(
            sample_rate=float(sample_rate),
            buffer_size=int(samples),
            start=True,
        )
        data = ai.channels[ch].get_data()
        if data is None:
            return "[]"
        try:
            return json.dumps(data.tolist())
        except AttributeError:
            return json.dumps(list(data))

    def scope_measure_dc(
        self,
        channel: int = 0,
        v_range: float = 5.0,
    ) -> str:
        """Quick DC voltage measurement — returns mean of short acquisition."""
        ai = self._dev.analog_input
        ch = int(channel)
        ai.setup_channel(ch, range=float(v_range), coupling="dc", enabled=True)
        ai.trigger.auto_timeout = 1.0
        ai.single(sample_rate=100000.0, buffer_size=256, start=True)
        data = ai.channels[ch].get_data()
        if data is None:
            return "0.0"
        try:
            return str(float(data.mean()))
        except (AttributeError, ValueError):
            vals = list(data)
            return str(sum(vals) / len(vals)) if vals else "0.0"

    def scope_measure_peak_to_peak(
        self,
        channel: int = 0,
        v_range: float = 5.0,
        sample_rate: float = 1000000.0,
    ) -> str:
        """Measure peak-to-peak voltage."""
        ai = self._dev.analog_input
        ch = int(channel)
        ai.setup_channel(ch, range=float(v_range), coupling="dc", enabled=True)
        ai.trigger.auto_timeout = 2.0
        ai.single(sample_rate=float(sample_rate), buffer_size=4096, start=True)
        data = ai.channels[ch].get_data()
        if data is None:
            return "0.0"
        try:
            return str(float(data.max() - data.min()))
        except (AttributeError, ValueError):
            vals = list(data)
            return str(max(vals) - min(vals)) if vals else "0.0"

    def scope_measure_rms(
        self,
        channel: int = 0,
        v_range: float = 5.0,
        sample_rate: float = 1000000.0,
    ) -> str:
        """Measure RMS voltage."""
        ai = self._dev.analog_input
        ch = int(channel)
        ai.setup_channel(ch, range=float(v_range), coupling="dc", enabled=True)
        ai.trigger.auto_timeout = 2.0
        ai.single(sample_rate=float(sample_rate), buffer_size=4096, start=True)
        data = ai.channels[ch].get_data()
        if data is None:
            return "0.0"
        try:
            import numpy as np
            return str(float(np.sqrt(np.mean(data ** 2))))
        except Exception:
            vals = list(data)
            return str((sum(v * v for v in vals) / len(vals)) ** 0.5) if vals else "0.0"

    def scope_measure_frequency(
        self,
        channel: int = 0,
        v_range: float = 5.0,
        sample_rate: float = 10000000.0,
    ) -> str:
        """Estimate signal frequency via zero-crossing count."""
        ai = self._dev.analog_input
        ch = int(channel)
        ai.setup_channel(ch, range=float(v_range), coupling="ac", enabled=True)
        ai.trigger.auto_timeout = 2.0
        sr = float(sample_rate)
        ai.single(sample_rate=sr, buffer_size=8192, start=True)
        data = ai.channels[ch].get_data()
        if data is None or len(data) < 10:
            return "0.0"
        try:
            crossings = 0
            for i in range(1, len(data)):
                if (data[i - 1] <= 0 and data[i] > 0):
                    crossings += 1
            duration = len(data) / sr
            freq = crossings / duration if duration > 0 else 0.0
            return str(freq)
        except Exception:
            return "0.0"

    # ========================================================================
    # Waveform Generator (AnalogOutput)
    # ========================================================================

    def wavegen_setup(
        self,
        channel: int = 0,
        function: str = "sine",
        frequency: float = 1000.0,
        amplitude: float = 1.0,
        offset: float = 0.0,
        symmetry: float = 50.0,
        phase: float = 0.0,
    ) -> str:
        """Configure and start waveform generator output."""
        ch = self._dev.analog_output[int(channel)]
        ch.setup(
            function=function,
            frequency=float(frequency),
            amplitude=float(amplitude),
            offset=float(offset),
            symmetry=float(symmetry),
            phase=float(phase),
            start=True,
        )
        return "OK"

    def wavegen_dc(self, channel: int = 0, offset: float = 0.0) -> str:
        """Output a DC voltage on the waveform generator channel."""
        ch = self._dev.analog_output[int(channel)]
        ch.setup(function="dc", offset=float(offset), start=True)
        return "OK"

    def wavegen_stop(self, channel: int = 0) -> str:
        """Stop waveform generator output."""
        self._dev.analog_output[int(channel)].reset()
        return "OK"

    def wavegen_custom(
        self,
        channel: int = 0,
        frequency: float = 1000.0,
        amplitude: float = 1.0,
        offset: float = 0.0,
        data: str = "",
    ) -> str:
        """Output a custom (arbitrary) waveform. Data is JSON array of
        normalized floats (-1.0 to 1.0)."""
        import dwfpy
        samples = json.loads(data) if data else []
        if not samples:
            return "ERROR: empty data"
        ch = self._dev.analog_output[int(channel)]
        ch.setup(
            function="custom",
            frequency=float(frequency),
            amplitude=float(amplitude),
            offset=float(offset),
        )
        ch.nodes[dwfpy.AnalogOutputNode.CARRIER].set_data_samples(samples)
        ch.configure(start=True)
        return "OK"

    # ========================================================================
    # Logic Analyzer (DigitalInput)
    # ========================================================================

    def la_acquire(
        self,
        sample_rate: float = 10000000.0,
        samples: int = 4096,
        sample_format: int = 16,
        trigger_channel: int = -1,
        trigger_edge: str = "rising",
    ) -> str:
        """Single-shot logic analyzer acquisition.

        Returns JSON object with 'sample_rate', 'sample_count', and 'data'
        (array of integer bitmasks, one per sample). Extract channel N with
        ``(sample >> N) & 1``.
        """
        di = self._dev.digital_input
        if int(trigger_channel) >= 0:
            di.setup_edge_trigger(
                channel=int(trigger_channel),
                edge=trigger_edge,
            )
        data = di.single(
            sample_rate=float(sample_rate),
            sample_format=int(sample_format),
            buffer_size=int(samples),
            start=True,
        )
        if data is None:
            return json.dumps({"sample_rate": sample_rate, "sample_count": 0, "data": []})
        try:
            sample_list = data.tolist()
        except AttributeError:
            sample_list = list(data)
        return json.dumps({
            "sample_rate": sample_rate,
            "sample_count": len(sample_list),
            "data": sample_list,
        })

    # ========================================================================
    # Pattern Generator (DigitalOutput)
    # ========================================================================

    def patgen_clock(
        self,
        pin: int = 0,
        frequency: float = 1000.0,
        duty_cycle: float = 50.0,
    ) -> str:
        """Output a clock signal on a digital pin."""
        ch = self._dev.digital_output[int(pin)]
        ch.setup_clock(
            frequency=float(frequency),
            duty_cycle=float(duty_cycle),
            enabled=True,
            start=True,
        )
        return "OK"

    def patgen_pulse(
        self,
        pin: int = 0,
        low_time: float = 0.001,
        high_time: float = 0.001,
        count: int = 0,
    ) -> str:
        """Output a pulse pattern on a digital pin. count=0 is infinite."""
        ch = self._dev.digital_output[int(pin)]
        ch.setup_pulse(
            low=float(low_time),
            high=float(high_time),
            repetition=int(count),
            enabled=True,
            start=True,
        )
        return "OK"

    def patgen_stop(self, pin: int = 0) -> str:
        """Stop pattern generator output on a pin."""
        self._dev.digital_output[int(pin)].setup(enabled=False, start=True)
        return "OK"

    def patgen_stop_all(self) -> str:
        """Stop all pattern generator channels."""
        self._dev.digital_output.reset()
        return "OK"

    # ========================================================================
    # Digital I/O (static GPIO)
    # ========================================================================

    def dio_write(self, pin: int = 0, value: int = 0) -> str:
        """Set a digital I/O pin as output and write a value."""
        dio = self._dev.digital_io
        p = int(pin)
        dio.channels[p].setup(enabled=True, state=bool(int(value)))
        dio.configure()
        return "OK"

    def dio_read(self, pin: int = -1) -> str:
        """Read digital I/O pin(s). pin=-1 reads all 16 as hex bitmask."""
        dio = self._dev.digital_io
        dio.read_status()
        p = int(pin)
        if p < 0:
            return f"0x{dio.input_state:04X}"
        return str(int(dio.channels[p].input_state))

    def dio_bus_write(self, mask: int = 0, value: int = 0) -> str:
        """Set multiple digital I/O pins. mask selects which pins to set."""
        dio = self._dev.digital_io
        m = int(mask)
        v = int(value)
        oe = dio.output_enable | m
        dio.output_enable = oe
        out = (dio.output_state & ~m) | (v & m)
        dio.output_state = out
        dio.configure()
        return "OK"

    # ========================================================================
    # UART Protocol
    # ========================================================================

    def uart_setup(
        self,
        pin_rx: int = -1,
        pin_tx: int = -1,
        baud: int = 9600,
        data_bits: int = 8,
        stop_bits: int = 1,
        parity: str = "n",
    ) -> str:
        """Configure UART. Set pin_rx=-1 to skip RX, pin_tx=-1 to skip TX."""
        uart = self._dev.protocols.uart
        kwargs = {
            "rate": int(baud),
            "data_bits": int(data_bits),
            "stop_bits": int(stop_bits),
            "parity": parity,
        }
        rx = int(pin_rx)
        tx = int(pin_tx)
        if rx >= 0:
            kwargs["pin_rx"] = rx
        if tx >= 0:
            kwargs["pin_tx"] = tx
        uart.setup(**kwargs)
        return "OK"

    def uart_write(self, data: str = "") -> str:
        """Transmit data over UART. Data is hex-encoded bytes."""
        raw = bytes.fromhex(data) if data else b""
        self._dev.protocols.uart.write(raw)
        return "OK"

    def uart_read(self, buffer_size: int = 8192) -> str:
        """Read received UART data. Returns JSON {data: hex, parity_errors: int}."""
        rx_data, parity_err = self._dev.protocols.uart.read(
            buffer_size=int(buffer_size)
        )
        return json.dumps({
            "data": rx_data.hex() if rx_data else "",
            "parity_errors": parity_err,
        })

    # ========================================================================
    # SPI Protocol
    # ========================================================================

    def spi_setup(
        self,
        pin_clock: int = 0,
        pin_mosi: int = 1,
        pin_miso: int = 2,
        pin_select: int = 3,
        frequency: float = 1000000.0,
        mode: int = 0,
    ) -> str:
        """Configure SPI master."""
        self._dev.protocols.spi.setup(
            pin_clock=int(pin_clock),
            pin_mosi=int(pin_mosi),
            pin_miso=int(pin_miso),
            pin_select=int(pin_select),
            frequency=float(frequency),
            mode=int(mode),
        )
        return "OK"

    def spi_write_read(self, data: str = "", read_count: int = 0) -> str:
        """Full-duplex SPI transfer. data is hex-encoded.
        Returns JSON {rx: hex_string}."""
        spi = self._dev.protocols.spi
        tx = bytes.fromhex(data) if data else b""
        rc = int(read_count) or len(tx)
        spi.select("low")
        if tx and rc:
            rx = spi.write_read(tx, rc)
        elif tx:
            spi.write(tx)
            rx = b""
        else:
            rx = spi.read(rc)
        spi.select("high")
        if isinstance(rx, bytes):
            return json.dumps({"rx": rx.hex()})
        try:
            return json.dumps({"rx": bytes(rx).hex()})
        except Exception:
            return json.dumps({"rx": str(rx)})

    def spi_select(self, level: str = "low") -> str:
        """Assert or release chip select."""
        self._dev.protocols.spi.select(level)
        return "OK"

    # ========================================================================
    # I2C Protocol
    # ========================================================================

    def i2c_setup(
        self,
        pin_scl: int = 0,
        pin_sda: int = 1,
        rate: float = 100000.0,
    ) -> str:
        """Configure I2C master."""
        self._dev.protocols.i2c.setup(
            pin_scl=int(pin_scl),
            pin_sda=int(pin_sda),
            rate=float(rate),
        )
        return "OK"

    def i2c_write(self, address: int = 0, data: str = "") -> str:
        """Write bytes to I2C device. data is hex-encoded."""
        raw = bytes.fromhex(data) if data else b""
        self._dev.protocols.i2c.write(int(address), raw)
        return "OK"

    def i2c_read(self, address: int = 0, count: int = 1) -> str:
        """Read bytes from I2C device. Returns JSON {data: hex, nak: int}."""
        rx, nak = self._dev.protocols.i2c.read(int(address), int(count))
        return json.dumps({"data": rx.hex() if rx else "", "nak": nak})

    def i2c_write_read(
        self, address: int = 0, data: str = "", count: int = 1
    ) -> str:
        """Write then read from I2C device (combined transaction)."""
        tx = bytes.fromhex(data) if data else b""
        rx, nak = self._dev.protocols.i2c.write_read(
            int(address), tx, int(count)
        )
        return json.dumps({"data": rx.hex() if rx else "", "nak": nak})

    # Keep old method names as aliases for backward compatibility
    read_analog = scope_acquire
    get_analog_voltage = scope_measure_dc
    set_waveform = wavegen_setup
    stop_waveform = wavegen_stop
    set_digital_io = dio_write
    get_digital_io = dio_read
    set_digital_bus = dio_bus_write
