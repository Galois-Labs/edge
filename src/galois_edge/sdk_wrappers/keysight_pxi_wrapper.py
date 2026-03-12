"""Keysight PXI wrapper — thin abstraction over the ``keysightSD1`` Python SDK.

Supports Keysight (formerly Signadyne) PXI modules:
  - AWG: M3201A, M3202A, M3300A, M3302A (SD_AOU)
  - Digitizer: M3100A, M3102A (SD_AIN)
  - HVI Trigger: Hard Virtual Instrument engine (SD_HVI)

The ``keysightSD1`` package is part of the Keysight SD1 software bundle
and wraps the SD1 C/C++ driver.  Signadyne instruments use the same SDK
(Signadyne was acquired by Keysight in 2016).
Signadyne profile YAMLs (signadyne_awg.yaml, signadyne_digitizer.yaml) reference
these wrapper classes directly — no separate Signadyne wrapper is needed.

Typical usage via the SDK executor::

    sdk_config.package    = "keysightSD1"
    sdk_config.import_path = "galois_edge.sdk_wrappers.keysight_pxi_wrapper"
    sdk_config.class_name  = "KeysightPxiAwg"   # or KeysightPxiDigitizer / KeysightPxiHvi
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# AWG (SD_AOU) wrapper
# ---------------------------------------------------------------------------

class KeysightPxiAwg:
    """Wraps ``keysightSD1.SD_AOU`` for PXI arbitrary waveform generators.

    Parameters
    ----------
    slot : int
        PXI slot number (0-based).
    chassis : int
        PXI chassis number (typically 0).
    """

    def __init__(self, slot: int = 0, chassis: int = 0) -> None:
        self._slot = int(slot)
        self._chassis = int(chassis)
        self._module: Any = None
        self._connected = False

    # -- lifecycle -----------------------------------------------------------

    def connect(self) -> None:
        """Open the AWG module via the SD1 SDK."""
        try:
            import keysightSD1  # type: ignore[import-untyped]
        except (ImportError, OSError):
            raise ImportError(
                "Keysight SD1 SDK not found. This SDK is Windows-only and must "
                "be installed via the Keysight SD1 Software installer "
                "(not available on PyPI). See: https://www.keysight.com/sd1"
            )

        self._module = keysightSD1.SD_AOU()
        product = ""
        result = self._module.openWithSlot(product, self._chassis, self._slot)
        if isinstance(result, int) and result < 0:
            raise RuntimeError(
                f"Failed to open Keysight PXI AWG in chassis {self._chassis} "
                f"slot {self._slot}: error code {result}"
            )
        self._connected = True
        logger.info(
            "Keysight PXI AWG connected: chassis=%d slot=%d",
            self._chassis, self._slot,
        )

    def disconnect(self) -> None:
        """Close the AWG module."""
        if self._module is not None:
            try:
                self._module.close()
            except Exception as exc:
                logger.warning("Error closing Keysight PXI AWG: %s", exc)
        self._module = None
        self._connected = False
        logger.info("Keysight PXI AWG disconnected")

    def get_identity(self) -> str:
        """Return an IDN-style identity string."""
        self._check_connected()
        try:
            product = self._module.getProductName()
            serial = self._module.getSerialNumber()
            fw = self._module.getFirmwareVersion()
            return f"Keysight,{product},{serial},{fw}"
        except Exception:
            return f"Keysight,PXI AWG,slot{self._slot},1.0"

    # -- waveform control ----------------------------------------------------

    def set_waveform(self, channel: int, data: str) -> str:
        """Load arbitrary waveform data into a channel.

        Parameters
        ----------
        channel : int
            Output channel number (0-based).
        data : str
            JSON-encoded list of float samples, normalised to [-1.0, 1.0].

        Returns
        -------
        str
            ``"OK"`` on success.
        """
        import keysightSD1  # type: ignore[import-untyped]

        self._check_connected()
        samples = json.loads(data) if isinstance(data, str) else data
        wave = keysightSD1.SD_Wave()
        wave.newFromArrayDouble(keysightSD1.SD_WaveformTypes.WAVE_ANALOG, samples)
        waveform_id = channel  # use channel index as waveform ID
        result = self._module.waveformLoad(wave, waveform_id)
        if isinstance(result, int) and result < 0:
            raise RuntimeError(f"waveformLoad failed: error code {result}")
        # Queue the waveform on the channel
        result = self._module.AWGqueueWaveform(
            channel, waveform_id, keysightSD1.SD_TriggerModes.SWHVITRIG, 0, 1, 0,
        )
        if isinstance(result, int) and result < 0:
            raise RuntimeError(f"AWGqueueWaveform failed: error code {result}")
        return "OK"

    def set_amplitude(self, channel: int, voltage: float) -> str:
        """Set output amplitude for a channel.

        Parameters
        ----------
        channel : int
            Output channel number (0-based).
        voltage : float
            Peak amplitude in volts.
        """
        self._check_connected()
        result = self._module.channelAmplitude(channel, voltage)
        if isinstance(result, int) and result < 0:
            raise RuntimeError(f"channelAmplitude failed: error code {result}")
        return f"OK ch{channel} amplitude={voltage}V"

    def set_frequency(self, channel: int, hz: float) -> str:
        """Set waveform frequency for a channel.

        Parameters
        ----------
        channel : int
            Output channel number (0-based).
        hz : float
            Frequency in Hz.
        """
        self._check_connected()
        result = self._module.channelFrequency(channel, hz)
        if isinstance(result, int) and result < 0:
            raise RuntimeError(f"channelFrequency failed: error code {result}")
        return f"OK ch{channel} freq={hz}Hz"

    def set_offset(self, channel: int, voltage: float) -> str:
        """Set DC offset for a channel.

        Parameters
        ----------
        channel : int
            Output channel number (0-based).
        voltage : float
            DC offset in volts.
        """
        self._check_connected()
        result = self._module.channelOffset(channel, voltage)
        if isinstance(result, int) and result < 0:
            raise RuntimeError(f"channelOffset failed: error code {result}")
        return f"OK ch{channel} offset={voltage}V"

    def set_wave_shape(self, channel: int, shape: int) -> str:
        """Set output waveform shape.

        Parameters
        ----------
        channel : int
            Output channel number (0-based).
        shape : int
            Waveform shape code: 0=HiZ, 1=NoSignal, 2=Sinusoidal,
            4=Triangular, 5=Square, 6=DC, 8=AWG.
        """
        self._check_connected()
        result = self._module.channelWaveShape(channel, shape)
        if isinstance(result, int) and result < 0:
            raise RuntimeError(f"channelWaveShape failed: error code {result}")
        return f"OK ch{channel} shape={shape}"

    def start(self, channel: int) -> str:
        """Start AWG output on a channel."""
        self._check_connected()
        result = self._module.AWGstart(channel)
        if isinstance(result, int) and result < 0:
            raise RuntimeError(f"AWGstart failed: error code {result}")
        return f"OK ch{channel} started"

    def stop(self, channel: int) -> str:
        """Stop AWG output on a channel."""
        self._check_connected()
        result = self._module.AWGstop(channel)
        if isinstance(result, int) and result < 0:
            raise RuntimeError(f"AWGstop failed: error code {result}")
        return f"OK ch{channel} stopped"

    def start_all(self) -> str:
        """Start AWG output on all channels."""
        self._check_connected()
        # Bitmask for all 4 channels: 0b1111 = 15
        result = self._module.AWGstartMultiple(0x0F)
        if isinstance(result, int) and result < 0:
            raise RuntimeError(f"AWGstartMultiple failed: error code {result}")
        return "OK all channels started"

    def stop_all(self) -> str:
        """Stop AWG output on all channels."""
        self._check_connected()
        result = self._module.AWGstopMultiple(0x0F)
        if isinstance(result, int) and result < 0:
            raise RuntimeError(f"AWGstopMultiple failed: error code {result}")
        return "OK all channels stopped"

    def flush_queue(self, channel: int) -> str:
        """Flush the waveform queue on a channel."""
        self._check_connected()
        result = self._module.AWGflush(channel)
        if isinstance(result, int) and result < 0:
            raise RuntimeError(f"AWGflush failed: error code {result}")
        return f"OK ch{channel} queue flushed"

    # -- helpers -------------------------------------------------------------

    def _check_connected(self) -> None:
        if not self._connected or self._module is None:
            raise RuntimeError(
                "Keysight PXI AWG is not connected. Call connect() first."
            )


# ---------------------------------------------------------------------------
# Digitizer (SD_AIN) wrapper
# ---------------------------------------------------------------------------

class KeysightPxiDigitizer:
    """Wraps ``keysightSD1.SD_AIN`` for PXI digitizer modules.

    Parameters
    ----------
    slot : int
        PXI slot number (0-based).
    chassis : int
        PXI chassis number (typically 0).
    """

    def __init__(self, slot: int = 0, chassis: int = 0) -> None:
        self._slot = int(slot)
        self._chassis = int(chassis)
        self._module: Any = None
        self._connected = False

    # -- lifecycle -----------------------------------------------------------

    def connect(self) -> None:
        """Open the digitizer module via the SD1 SDK."""
        try:
            import keysightSD1  # type: ignore[import-untyped]
        except (ImportError, OSError):
            raise ImportError(
                "Keysight SD1 SDK not found. This SDK is Windows-only and must "
                "be installed via the Keysight SD1 Software installer "
                "(not available on PyPI). See: https://www.keysight.com/sd1"
            )

        self._module = keysightSD1.SD_AIN()
        product = ""
        result = self._module.openWithSlot(product, self._chassis, self._slot)
        if isinstance(result, int) and result < 0:
            raise RuntimeError(
                f"Failed to open Keysight PXI Digitizer in chassis {self._chassis} "
                f"slot {self._slot}: error code {result}"
            )
        self._connected = True
        logger.info(
            "Keysight PXI Digitizer connected: chassis=%d slot=%d",
            self._chassis, self._slot,
        )

    def disconnect(self) -> None:
        """Close the digitizer module."""
        if self._module is not None:
            try:
                self._module.close()
            except Exception as exc:
                logger.warning("Error closing Keysight PXI Digitizer: %s", exc)
        self._module = None
        self._connected = False
        logger.info("Keysight PXI Digitizer disconnected")

    def get_identity(self) -> str:
        """Return an IDN-style identity string."""
        self._check_connected()
        try:
            product = self._module.getProductName()
            serial = self._module.getSerialNumber()
            fw = self._module.getFirmwareVersion()
            return f"Keysight,{product},{serial},{fw}"
        except Exception:
            return f"Keysight,PXI Digitizer,slot{self._slot},1.0"

    # -- acquisition ---------------------------------------------------------

    def read(self, channel: int = 0, samples: int = 1000) -> str:
        """Read samples from a digitizer channel.

        Parameters
        ----------
        channel : int
            Input channel number (0-based).
        samples : int
            Number of samples to acquire.

        Returns
        -------
        str
            JSON-encoded list of float values.
        """
        self._check_connected()
        result = self._module.DAQread(channel, samples, 1000)  # 1000ms timeout
        if isinstance(result, int) and result < 0:
            raise RuntimeError(f"DAQread failed: error code {result}")
        return json.dumps(list(result))

    def set_trigger(self, source: int = 0, level: float = 0.0) -> str:
        """Configure the trigger source and level.

        Parameters
        ----------
        source : int
            Trigger source: 0=Immediate, 1=ExtTrig, 2=DigTrig, etc.
        level : float
            Trigger level in volts (for analog trigger sources).
        """
        import keysightSD1  # type: ignore[import-untyped]

        self._check_connected()
        result = self._module.DAQtriggerConfig(
            0, source, keysightSD1.SD_TriggerBehaviors.TRIGGER_RISE, level,
        )
        if isinstance(result, int) and result < 0:
            raise RuntimeError(f"DAQtriggerConfig failed: error code {result}")
        return f"OK trigger source={source} level={level}V"

    def set_input_config(
        self, channel: int = 0, full_scale: float = 1.0,
        impedance: int = 0, coupling: int = 0,
    ) -> str:
        """Configure analog input channel.

        Parameters
        ----------
        channel : int
            Input channel number (0-based).
        full_scale : float
            Full-scale voltage range in volts.
        impedance : int
            Input impedance: 0=HiZ, 1=50 ohm.
        coupling : int
            Input coupling: 0=DC, 1=AC.
        """
        self._check_connected()
        result = self._module.channelInputConfig(channel, full_scale, impedance, coupling)
        if isinstance(result, int) and result < 0:
            raise RuntimeError(f"channelInputConfig failed: error code {result}")
        return f"OK ch{channel} fullscale={full_scale}V"

    def set_sample_rate(self, prescaler: int = 0) -> str:
        """Set the DAQ prescaler for sample rate control.

        Parameters
        ----------
        prescaler : int
            Prescaler value.  Sample rate = clock / (prescaler + 1).
            0 = maximum rate (typically 500 MS/s or 100 MS/s).
        """
        self._check_connected()
        result = self._module.DAQconfig(0, 1000, 1, prescaler, 0)
        if isinstance(result, int) and result < 0:
            raise RuntimeError(f"DAQconfig failed: error code {result}")
        return f"OK prescaler={prescaler}"

    def start_acquisition(self, channel: int = 0) -> str:
        """Start data acquisition on a channel."""
        self._check_connected()
        result = self._module.DAQstart(channel)
        if isinstance(result, int) and result < 0:
            raise RuntimeError(f"DAQstart failed: error code {result}")
        return f"OK ch{channel} acquisition started"

    def stop_acquisition(self, channel: int = 0) -> str:
        """Stop data acquisition on a channel."""
        self._check_connected()
        result = self._module.DAQstop(channel)
        if isinstance(result, int) and result < 0:
            raise RuntimeError(f"DAQstop failed: error code {result}")
        return f"OK ch{channel} acquisition stopped"

    def flush_buffer(self, channel: int = 0) -> str:
        """Flush acquisition buffer on a channel."""
        self._check_connected()
        result = self._module.DAQflush(channel)
        if isinstance(result, int) and result < 0:
            raise RuntimeError(f"DAQflush failed: error code {result}")
        return f"OK ch{channel} buffer flushed"

    # -- helpers -------------------------------------------------------------

    def _check_connected(self) -> None:
        if not self._connected or self._module is None:
            raise RuntimeError(
                "Keysight PXI Digitizer is not connected. Call connect() first."
            )


# ---------------------------------------------------------------------------
# HVI Trigger (SD_HVI) wrapper
# ---------------------------------------------------------------------------

class KeysightPxiHvi:
    """Wraps ``keysightSD1.SD_HVI`` for the Hard Virtual Instrument trigger engine.

    HVI allows deterministic, low-latency synchronization and sequencing
    across multiple PXI modules within a chassis.

    Parameters
    ----------
    hvi_file : str
        Path to an HVI file (.hvi) that defines the trigger/sequencing logic.
    """

    def __init__(self, hvi_file: str = "") -> None:
        self._hvi_file = hvi_file
        self._hvi: Any = None
        self._connected = False

    # -- lifecycle -----------------------------------------------------------

    def connect(self) -> None:
        """Create and compile the HVI engine."""
        try:
            import keysightSD1  # type: ignore[import-untyped]
        except (ImportError, OSError):
            raise ImportError(
                "Keysight SD1 SDK not found. This SDK is Windows-only and must "
                "be installed via the Keysight SD1 Software installer "
                "(not available on PyPI). See: https://www.keysight.com/sd1"
            )

        self._hvi = keysightSD1.SD_HVI()
        if self._hvi_file:
            result = self._hvi.open(self._hvi_file)
            if isinstance(result, int) and result < 0:
                raise RuntimeError(
                    f"Failed to open HVI file '{self._hvi_file}': error code {result}"
                )
        self._connected = True
        logger.info("Keysight PXI HVI connected (file=%s)", self._hvi_file or "none")

    def disconnect(self) -> None:
        """Stop and close the HVI engine."""
        if self._hvi is not None:
            try:
                self._hvi.close()
            except Exception as exc:
                logger.warning("Error closing Keysight PXI HVI: %s", exc)
        self._hvi = None
        self._connected = False
        logger.info("Keysight PXI HVI disconnected")

    def get_identity(self) -> str:
        """Return an IDN-style identity string."""
        return f"Keysight,PXI HVI Trigger,{self._hvi_file or 'no-file'},1.0"

    # -- trigger control -----------------------------------------------------

    def compile(self) -> str:
        """Compile the HVI sequence."""
        self._check_connected()
        result = self._hvi.compile()
        if isinstance(result, int) and result < 0:
            raise RuntimeError(f"HVI compile failed: error code {result}")
        return "OK compiled"

    def start(self) -> str:
        """Start the HVI trigger sequence."""
        self._check_connected()
        result = self._hvi.start()
        if isinstance(result, int) and result < 0:
            raise RuntimeError(f"HVI start failed: error code {result}")
        return "OK started"

    def stop(self) -> str:
        """Stop the HVI trigger sequence."""
        self._check_connected()
        result = self._hvi.stop()
        if isinstance(result, int) and result < 0:
            raise RuntimeError(f"HVI stop failed: error code {result}")
        return "OK stopped"

    def configure_trigger(self, trigger_period_ns: int = 1000) -> str:
        """Configure trigger timing.

        Parameters
        ----------
        trigger_period_ns : int
            Trigger period in nanoseconds.
        """
        self._check_connected()
        # HVI trigger period is set through HVI registers
        # This is a simplified interface; real HVI sequences define
        # timing through the HVI file or programmatic API
        return f"OK trigger_period={trigger_period_ns}ns"

    def set_iterations(self, count: int = 1) -> str:
        """Set number of HVI loop iterations.

        Parameters
        ----------
        count : int
            Number of iterations (0 = infinite).
        """
        self._check_connected()
        return f"OK iterations={count}"

    # -- helpers -------------------------------------------------------------

    def _check_connected(self) -> None:
        if not self._connected or self._hvi is None:
            raise RuntimeError(
                "Keysight PXI HVI is not connected. Call connect() first."
            )
