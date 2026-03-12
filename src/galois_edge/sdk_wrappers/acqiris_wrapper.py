"""Acqiris U1084A Digitizer wrapper — high-speed digitizer/oscilloscope.

The Acqiris U1084A is a Keysight (formerly Agilent) high-speed digitizer
that can be controlled via the Agilent/Keysight IVI driver or the native
``acqiris`` SDK.

The wrapper provides a simplified interface:
connect / disconnect / identity / configure / acquire / get_data.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class AcqirisClient:
    """Wraps the Acqiris digitizer SDK."""

    def __init__(self, resource_name: str = "PCI::INSTR0") -> None:
        self._resource_name = resource_name
        self._session: Any = None
        self._module: Any = None
        self._last_acquire_samples: int = 1024
        self._last_acquire_segments: int = 1

    # -- lifecycle -----------------------------------------------------------

    def connect(self) -> None:
        """Initialise connection to the Acqiris digitizer."""
        try:
            import acqiris  # type: ignore[import-untyped]
        except (ImportError, OSError):
            raise ImportError(
                "Acqiris digitizer SDK not found. This instrument requires the "
                "Keysight/Acqiris IVI-C driver and Python bindings. "
                "Note: The U1084A is discontinued — driver updates may be limited. "
                "See: https://www.keysight.com/us/en/support/U1084A.html"
            )

        self._module = acqiris
        self._session = acqiris.Digitizer(self._resource_name)
        logger.info("Acqiris digitizer connected: %s", self._resource_name)

    def disconnect(self) -> None:
        """Close the digitizer session."""
        if self._session is not None:
            try:
                self._session.close()
            except Exception as exc:
                logger.warning("Error closing Acqiris session: %s", exc)
        self._session = None
        logger.info("Acqiris digitizer disconnected")

    def get_identity(self) -> str:
        """Return instrument identity string."""
        if self._session is None:
            return "Acqiris,U1084A,N/A,N/A"
        try:
            name = getattr(self._session, "name", "U1084A")
            serial = getattr(self._session, "serial_number", "N/A")
            return f"Acqiris,{name},{serial},1.0"
        except Exception:
            return "Acqiris,U1084A,N/A,N/A"

    # -- configuration -------------------------------------------------------

    def configure(
        self,
        sample_rate: float = 1e9,
        num_samples: int = 1024,
        num_segments: int = 1,
        full_scale: float = 1.0,
        trigger_source: str = "external",
        trigger_level: float = 0.0,
    ) -> str:
        """Configure the digitizer for acquisition.

        Parameters
        ----------
        sample_rate : float
            Sample rate in Hz.
        num_samples : int
            Number of samples per segment.
        num_segments : int
            Number of segments (multi-record mode).
        full_scale : float
            Input range full scale in volts.
        trigger_source : str
            "external", "channel_1", or "channel_2".
        trigger_level : float
            Trigger level in volts.
        """
        if self._session is None:
            raise RuntimeError("Digitizer not connected")

        self._session.configure_timebase(sample_rate, num_samples)

        # Channel 1 configuration
        self._session.configure_channel(1, full_scale, offset=0.0, coupling="DC")

        # Trigger
        trig_map = {
            "external": -1,
            "channel_1": 1,
            "channel_2": 2,
        }
        trig_src = trig_map.get(trigger_source, -1)
        self._session.configure_trigger(trig_src, trigger_level)

        if num_segments > 1:
            self._session.configure_multisegment(num_segments)

        self._last_acquire_samples = num_samples
        self._last_acquire_segments = num_segments

        logger.info(
            "Acqiris configured: rate=%.0f, samples=%d, segments=%d",
            sample_rate, num_samples, num_segments,
        )
        return "OK"

    # -- acquisition ---------------------------------------------------------

    def acquire(self) -> str:
        """Start acquisition and wait for completion."""
        if self._session is None:
            raise RuntimeError("Digitizer not connected")

        self._session.acquire()
        self._session.wait_for_end_of_acquisition(timeout=10.0)

        logger.info("Acqiris acquisition complete")
        return "OK"

    def get_data(self, channel: int = 1) -> str:
        """Read acquired waveform data from the specified channel.

        Returns JSON-encoded list of float voltage values.
        """
        if self._session is None:
            raise RuntimeError("Digitizer not connected")

        data = self._session.read_channel(channel)

        # data may be a numpy array or list
        if hasattr(data, "tolist"):
            data = data.tolist()

        return json.dumps(data)

    # -- status --------------------------------------------------------------

    def get_status(self) -> str:
        """Return digitizer status."""
        if self._session is None:
            return "disconnected"
        try:
            if hasattr(self._session, "is_busy"):
                return "busy" if self._session.is_busy() else "idle"
            return "idle"
        except Exception:
            return "unknown"

    def get_sample_rate(self) -> str:
        """Return current configured sample rate."""
        if self._session is None:
            raise RuntimeError("Digitizer not connected")
        try:
            rate = self._session.get_timebase_sample_rate()
            return str(rate)
        except Exception:
            return "unknown"
