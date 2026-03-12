"""NI DAQ wrapper — thin abstraction over the ``nidaqmx`` Python package.

Supports National Instruments Data Acquisition devices (e.g. USB-6218).
The ``nidaqmx`` package wraps the NI-DAQmx C driver and provides a
Pythonic API for analog input/output and digital I/O.

Typical usage through the SDK executor:
  1. ``connect()`` — verify the device exists on the system
  2. ``read_analog()`` / ``write_analog()`` — analog I/O
  3. ``read_digital()`` / ``write_digital()`` — digital I/O
  4. ``disconnect()`` — clean up open tasks
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class NiDaqClient:
    """Wraps ``nidaqmx`` for NI DAQ device operations.

    Parameters
    ----------
    device_name : str
        NI-DAQmx device name, e.g. ``"Dev1"``.  This corresponds to the
        name shown in NI MAX (Measurement & Automation Explorer).
    """

    def __init__(self, device_name: str = "Dev1") -> None:
        self.device_name = device_name
        self._device: Any = None
        self._connected = False

    # -- lifecycle -----------------------------------------------------------

    def connect(self) -> None:
        """Verify the device exists on the local NI-DAQmx system."""
        try:
            import nidaqmx.system  # type: ignore[import-untyped]

            system = nidaqmx.system.System.local()
            device_names = [d.name for d in system.devices]
            if self.device_name not in device_names:
                raise RuntimeError(
                    f"NI-DAQmx device '{self.device_name}' not found. "
                    f"Available devices: {device_names}"
                )
            self._device = system.devices[self.device_name]
            self._connected = True
            logger.info("NI DAQ connected: %s", self.device_name)
        except (ImportError, OSError):
            raise ImportError(
                "nidaqmx package not found. Install with: pip install nidaqmx. "
                "Also requires NI-DAQmx runtime driver installed on the host OS."
            )
        except Exception as exc:
            import platform
            msg = str(exc)
            if platform.system() == "Linux":
                msg += (
                    " (Note: NI-DAQmx does not support USB DAQ devices on Linux. "
                    "PCI/PCIe/PXI devices are supported.)"
                )
            raise RuntimeError(msg) from exc

    def disconnect(self) -> None:
        """Clean up any references."""
        self._device = None
        self._connected = False
        logger.info("NI DAQ disconnected: %s", self.device_name)

    def get_identity(self) -> str:
        """Return an IDN-style identity string for the device."""
        if self._device is not None:
            try:
                product_type = self._device.product_type
                serial_number = self._device.dev_serial_num
                return f"NI,{product_type},{serial_number},1.0"
            except Exception:
                pass
        return f"NI,DAQ,{self.device_name},1.0"

    # -- analog input --------------------------------------------------------

    def read_analog(self, channel: str = "ai0", samples: int = 1) -> float:
        """Read analog input voltage from a channel.

        Parameters
        ----------
        channel : str
            Channel name, e.g. ``"ai0"``, ``"ai1"``.  The full physical
            channel path ``<device>/<channel>`` is constructed automatically.
        samples : int
            Number of samples to read.  If 1, returns a single float.
            If > 1, returns the mean of the samples.

        Returns
        -------
        float
            Voltage reading in volts.
        """
        import nidaqmx  # type: ignore[import-untyped]

        physical_channel = f"{self.device_name}/{channel}"
        with nidaqmx.Task() as task:
            task.ai_channels.add_ai_voltage_chan(physical_channel)
            if samples == 1:
                value = task.read()
            else:
                values = task.read(number_of_samples_per_channel=samples)
                value = sum(values) / len(values)
        return float(value)

    def get_ai_voltage(self, channel: str = "ai0") -> float:
        """Simplified single-sample analog read (convenience alias)."""
        return self.read_analog(channel=channel, samples=1)

    # -- analog output -------------------------------------------------------

    def write_analog(self, channel: str = "ao0", voltage: float = 0.0) -> str:
        """Write a voltage to an analog output channel.

        Parameters
        ----------
        channel : str
            Channel name, e.g. ``"ao0"``, ``"ao1"``.
        voltage : float
            Voltage to output in volts.

        Returns
        -------
        str
            ``"OK"`` on success.
        """
        import nidaqmx  # type: ignore[import-untyped]

        physical_channel = f"{self.device_name}/{channel}"
        with nidaqmx.Task() as task:
            task.ao_channels.add_ao_voltage_chan(physical_channel)
            task.write(voltage)
        return "OK"

    # -- digital I/O ---------------------------------------------------------

    def read_digital(self, port: str = "port0", line: str = "line0") -> bool:
        """Read a digital input line.

        Parameters
        ----------
        port : str
            Digital port, e.g. ``"port0"``.
        line : str
            Digital line, e.g. ``"line0"``.

        Returns
        -------
        bool
            Logic state of the line.
        """
        import nidaqmx  # type: ignore[import-untyped]

        physical_line = f"{self.device_name}/{port}/{line}"
        with nidaqmx.Task() as task:
            task.di_channels.add_di_chan(physical_line)
            value = task.read()
        return bool(value)

    def write_digital(
        self, port: str = "port0", line: str = "line0", value: bool = False,
    ) -> str:
        """Write a digital output line.

        Parameters
        ----------
        port : str
            Digital port, e.g. ``"port0"``.
        line : str
            Digital line, e.g. ``"line0"``.
        value : bool
            Logic state to write.

        Returns
        -------
        str
            ``"OK"`` on success.
        """
        import nidaqmx  # type: ignore[import-untyped]

        physical_line = f"{self.device_name}/{port}/{line}"
        with nidaqmx.Task() as task:
            task.do_channels.add_do_chan(physical_line)
            task.write(value)
        return "OK"

    # -- bulk reads ----------------------------------------------------------

    def get_ai_all_voltages(self, num_channels: int = 8) -> str:
        """Read voltage from multiple analog input channels.

        Reads channels ai0 through ai<num_channels-1> and returns a
        JSON-encoded dict, e.g. ``{"ai0": 1.23, "ai1": -0.45, ...}``.
        Channels that fail to read are silently skipped.
        """
        import json

        result: Dict[str, float] = {}
        for i in range(num_channels):
            ch = f"ai{i}"
            try:
                result[ch] = self.read_analog(channel=ch)
            except Exception:
                pass
        return json.dumps(result)
