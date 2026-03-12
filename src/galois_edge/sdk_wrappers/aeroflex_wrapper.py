"""Aeroflex PXI wrapper — thin abstraction for Aeroflex 302x/303x PXI modules.

Supports Aeroflex PXI instruments:
  - 302x: PXI signal generators (3020, 3025, 3026)
  - 303x: PXI digitizers (3030, 3035, 3036)

These modules are typically controlled via NI-RFSG (signal generation) and
NI-RFSA (signal acquisition) drivers, or via the Aeroflex proprietary SDK.
This wrapper provides a unified interface for both module families.

Typical usage via the SDK executor::

    sdk_config.package    = "nirfsg"   # or "aeroflex"
    sdk_config.import_path = "galois_edge.sdk_wrappers.aeroflex_wrapper"
    sdk_config.class_name  = "Aeroflex302xClient"  # or Aeroflex303xClient
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 302x Signal Generator wrapper
# ---------------------------------------------------------------------------

class Aeroflex302xClient:
    """Wraps NI-RFSG / Aeroflex SDK for 302x PXI signal generators.

    Parameters
    ----------
    resource : str
        NI-RFSG resource name, e.g. ``"PXI1Slot2"`` or an IVI resource string.
    """

    def __init__(self, resource: str = "PXI1Slot2") -> None:
        self._resource = resource
        self._session: Any = None
        self._connected = False

    # -- lifecycle -----------------------------------------------------------

    def connect(self) -> None:
        """Open a session to the signal generator module."""
        try:
            import nirfsg  # type: ignore[import-untyped]
            self._session = nirfsg.Session(self._resource)
        except (ImportError, OSError):
            raise ImportError(
                "NI-RFSG Python bindings not found. Install with: pip install nirfsg. "
                "Also requires the NI-RFSG runtime driver. "
                "See: https://www.ni.com/en/support/downloads/drivers/download.ni-rfsg.html"
            )
        self._connected = True
        logger.info("Aeroflex 302x connected: %s", self._resource)

    def disconnect(self) -> None:
        """Close the RFSG session."""
        if self._session is not None:
            try:
                self._session.close()
            except Exception as exc:
                logger.warning("Error closing Aeroflex 302x session: %s", exc)
        self._session = None
        self._connected = False
        logger.info("Aeroflex 302x disconnected: %s", self._resource)

    def get_identity(self) -> str:
        """Return an IDN-style identity string."""
        self._check_connected()
        try:
            name = self._session.instrument_model
            serial = self._session.serial_number
            return f"Aeroflex,{name},{serial},1.0"
        except Exception:
            return f"Aeroflex,302x Signal Generator,{self._resource},1.0"

    # -- signal generation ---------------------------------------------------

    def set_frequency(self, frequency: float) -> str:
        """Set RF output frequency.

        Parameters
        ----------
        frequency : float
            Output frequency in Hz.
        """
        self._check_connected()
        self._session.frequency = frequency
        return f"OK freq={frequency}Hz"

    def get_frequency(self) -> float:
        """Read current RF output frequency in Hz."""
        self._check_connected()
        return float(self._session.frequency)

    def set_power(self, power: float) -> str:
        """Set RF output power level.

        Parameters
        ----------
        power : float
            Output power in dBm.
        """
        self._check_connected()
        self._session.power_level = power
        return f"OK power={power}dBm"

    def get_power(self) -> float:
        """Read current RF output power in dBm."""
        self._check_connected()
        return float(self._session.power_level)

    def set_output_enabled(self, enabled: bool = True) -> str:
        """Enable or disable RF output.

        Parameters
        ----------
        enabled : bool
            True to enable output, False to disable.
        """
        self._check_connected()
        self._session.output_enabled = enabled
        return f"OK output={'enabled' if enabled else 'disabled'}"

    def start(self) -> str:
        """Initiate signal generation."""
        self._check_connected()
        self._session.initiate()
        return "OK generation started"

    def stop(self) -> str:
        """Abort signal generation."""
        self._check_connected()
        self._session.abort()
        return "OK generation stopped"

    # -- helpers -------------------------------------------------------------

    def _check_connected(self) -> None:
        if not self._connected or self._session is None:
            raise RuntimeError(
                "Aeroflex 302x is not connected. Call connect() first."
            )


# ---------------------------------------------------------------------------
# 303x Digitizer wrapper
# ---------------------------------------------------------------------------

class Aeroflex303xClient:
    """Wraps NI-RFSA / Aeroflex SDK for 303x PXI digitizers.

    Parameters
    ----------
    resource : str
        NI-RFSA resource name, e.g. ``"PXI1Slot3"`` or an IVI resource string.
    """

    def __init__(self, resource: str = "PXI1Slot3") -> None:
        self._resource = resource
        self._session: Any = None
        self._connected = False

    # -- lifecycle -----------------------------------------------------------

    def connect(self) -> None:
        """Open a session to the digitizer module."""
        try:
            import nirfsa  # type: ignore[import-untyped]
            self._session = nirfsa.Session(self._resource)
        except (ImportError, OSError):
            raise ImportError(
                "NI-RFSA Python bindings not found. The 'nirfsa' package is not yet "
                "available on PyPI. Options:\n"
                "  1. Install NI-RFSA from the NI Modular Instruments bundle\n"
                "  2. Use the NI gRPC Device Server for remote instrument access\n"
                "     (see: https://github.com/ni/grpc-device)\n"
                "  3. Contact NI for nirfsa Python package availability"
            )
        self._connected = True
        logger.info("Aeroflex 303x connected: %s", self._resource)

    def disconnect(self) -> None:
        """Close the RFSA session."""
        if self._session is not None:
            try:
                self._session.close()
            except Exception as exc:
                logger.warning("Error closing Aeroflex 303x session: %s", exc)
        self._session = None
        self._connected = False
        logger.info("Aeroflex 303x disconnected: %s", self._resource)

    def get_identity(self) -> str:
        """Return an IDN-style identity string."""
        self._check_connected()
        try:
            name = self._session.instrument_model
            serial = self._session.serial_number
            return f"Aeroflex,{name},{serial},1.0"
        except Exception:
            return f"Aeroflex,303x Digitizer,{self._resource},1.0"

    # -- acquisition ---------------------------------------------------------

    def set_frequency(self, frequency: float) -> str:
        """Set center frequency for acquisition.

        Parameters
        ----------
        frequency : float
            Center frequency in Hz.
        """
        self._check_connected()
        self._session.frequency = frequency
        return f"OK center_freq={frequency}Hz"

    def get_frequency(self) -> float:
        """Read current center frequency in Hz."""
        self._check_connected()
        return float(self._session.frequency)

    def set_reference_level(self, level: float) -> str:
        """Set reference level for acquisition.

        Parameters
        ----------
        level : float
            Reference level in dBm.
        """
        self._check_connected()
        self._session.reference_level = level
        return f"OK ref_level={level}dBm"

    def get_reference_level(self) -> float:
        """Read current reference level in dBm."""
        self._check_connected()
        return float(self._session.reference_level)

    def read_iq(self, samples: int = 1000) -> str:
        """Acquire IQ data samples.

        Parameters
        ----------
        samples : int
            Number of IQ samples to read.

        Returns
        -------
        str
            JSON-encoded dict with ``"i"`` and ``"q"`` arrays.
        """
        self._check_connected()
        data = self._session.read(samples)
        # NI-RFSA returns complex IQ data
        try:
            i_data = [float(x.real) for x in data]
            q_data = [float(x.imag) for x in data]
        except (TypeError, AttributeError):
            # If data is not complex, return as-is
            i_data = list(data)
            q_data = []
        return json.dumps({"i": i_data, "q": q_data})

    def read_power_spectrum(self, samples: int = 1000) -> str:
        """Acquire a power spectrum.

        Parameters
        ----------
        samples : int
            Number of frequency bins.

        Returns
        -------
        str
            JSON-encoded list of power values in dBm.
        """
        self._check_connected()
        data = self._session.read(samples)
        try:
            values = [float(x) for x in data]
        except (TypeError, ValueError):
            values = []
        return json.dumps(values)

    def start_acquisition(self) -> str:
        """Start continuous acquisition."""
        self._check_connected()
        self._session.initiate()
        return "OK acquisition started"

    def stop_acquisition(self) -> str:
        """Stop acquisition."""
        self._check_connected()
        self._session.abort()
        return "OK acquisition stopped"

    # -- helpers -------------------------------------------------------------

    def _check_connected(self) -> None:
        if not self._connected or self._session is None:
            raise RuntimeError(
                "Aeroflex 303x is not connected. Call connect() first."
            )
