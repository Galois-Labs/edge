"""SignalHound SA124B wrapper — USB spectrum analyser.

The SA124B is a USB-powered spectrum analyser covering 100 kHz to 12.4 GHz.
It uses the vendored ctypes binding in ``galois_edge.vendor.sa_api`` which
wraps the vendor C shared library (sa_api.dll / libsa_api.so).

API overview::

    sa = sa_api.sa_open_device()
    sa_api.sa_config_center_span(sa, center, span)
    sa_api.sa_config_level(sa, ref)
    sa_api.sa_config_sweep_coupling(sa, rbw, vbw, reject)
    sa_api.sa_initiate(sa, SA_SWEEPING, 0)
    sa_api.sa_get_sweep_64f(sa)
    sa_api.sa_close_device(sa)

The wrapper provides a simplified high-level interface.
"""

from __future__ import annotations

import json
import logging
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


class SignalHoundClient:
    """Wraps the SignalHound SA124B spectrum analyser SDK."""

    def __init__(self) -> None:
        self._device: Any = None
        self._module: Any = None
        self._center_freq: float = 1e9
        self._span: float = 1e6
        self._rbw: float = 1e3
        self._vbw: float = 1e3
        self._ref_level: float = 0.0

    # -- lifecycle -----------------------------------------------------------

    def connect(self) -> None:
        """Open connection to the SA124B."""
        try:
            from galois_edge.vendor import sa_api
        except (ImportError, OSError) as exc:
            raise RuntimeError(
                f"SignalHound SDK not available: {exc}. "
                "Install the SA-Series SDK from https://signalhound.com/software/ "
                "to get the shared library (sa_api.dll / libsa_api.so)."
            ) from exc

        self._module = sa_api
        handle = sa_api.sa_open_device()
        if isinstance(handle, tuple):
            # Some API versions return (status, handle)
            self._device = handle[1] if handle[0] == 0 else None
            if self._device is None:
                raise RuntimeError(f"Failed to open SA124B: status={handle[0]}")
        else:
            self._device = handle

        logger.info("SignalHound SA124B connected")

    def disconnect(self) -> None:
        """Close the SA124B device handle."""
        if self._device is not None and self._module is not None:
            try:
                self._module.sa_close_device(self._device)
            except Exception as exc:
                logger.warning("Error closing SignalHound device: %s", exc)
        self._device = None
        logger.info("SignalHound SA124B disconnected")

    def get_identity(self) -> str:
        """Return device identity string."""
        if self._device is None:
            return "SignalHound,SA124B,N/A,N/A"
        try:
            serial = self._module.sa_get_serial_number(self._device)
            fw_ver = self._module.sa_get_firmware_version(self._device) if hasattr(self._module, "sa_get_firmware_version") else "N/A"
            return f"SignalHound,SA124B,{serial},{fw_ver}"
        except Exception:
            return "SignalHound,SA124B,N/A,N/A"

    # -- configuration -------------------------------------------------------

    def set_center_freq(self, hz: float) -> str:
        """Set center frequency in Hz."""
        if self._device is None:
            raise RuntimeError("Device not connected")
        self._center_freq = float(hz)
        self._apply_freq_config()
        return "OK"

    def set_span(self, hz: float) -> str:
        """Set frequency span in Hz."""
        if self._device is None:
            raise RuntimeError("Device not connected")
        self._span = float(hz)
        self._apply_freq_config()
        return "OK"

    def set_rbw(self, hz: float) -> str:
        """Set resolution bandwidth in Hz."""
        if self._device is None:
            raise RuntimeError("Device not connected")
        self._rbw = float(hz)
        self._apply_sweep_coupling()
        return "OK"

    def set_ref_level(self, dbm: float) -> str:
        """Set reference level in dBm."""
        if self._device is None:
            raise RuntimeError("Device not connected")
        self._ref_level = float(dbm)
        self._module.sa_config_level(self._device, self._ref_level)
        return "OK"

    def _apply_freq_config(self) -> None:
        """Push center/span to the device."""
        self._module.sa_config_center_span(
            self._device, self._center_freq, self._span,
        )

    def _apply_sweep_coupling(self) -> None:
        """Push RBW/VBW to the device."""
        reject = getattr(self._module, "SA_FALSE", 0)
        self._module.sa_config_sweep_coupling(
            self._device, self._rbw, self._vbw, reject,
        )

    # -- measurement ---------------------------------------------------------

    def sweep(self, center_freq: Optional[float] = None, span: Optional[float] = None) -> str:
        """Configure and perform a sweep, returning the trace as JSON.

        Parameters
        ----------
        center_freq : float, optional
            Center frequency in Hz. Uses current value if omitted.
        span : float, optional
            Span in Hz. Uses current value if omitted.

        Returns
        -------
        str
            JSON-encoded dict with 'frequencies' and 'amplitudes' arrays.
        """
        if self._device is None:
            raise RuntimeError("Device not connected")

        if center_freq is not None:
            self._center_freq = float(center_freq)
        if span is not None:
            self._span = float(span)

        self._apply_freq_config()
        self._apply_sweep_coupling()
        self._module.sa_config_level(self._device, self._ref_level)

        # Initiate sweep
        sweep_mode = getattr(self._module, "SA_SWEEPING", 0)
        self._module.sa_initiate(self._device, sweep_mode, 0)

        # Get sweep data
        result = self._module.sa_get_sweep_64f(self._device)
        if isinstance(result, tuple) and len(result) >= 2:
            freqs, amps = result[0], result[1]
        else:
            freqs, amps = [], result

        # Convert numpy arrays to lists if needed
        if hasattr(freqs, "tolist"):
            freqs = freqs.tolist()
        if hasattr(amps, "tolist"):
            amps = amps.tolist()

        return json.dumps({"frequencies": freqs, "amplitudes": amps})

    def get_trace(self) -> str:
        """Get the most recent sweep trace (re-sweeps if needed).

        Returns JSON-encoded dict with 'frequencies' and 'amplitudes'.
        """
        return self.sweep()

    # -- status queries ------------------------------------------------------

    def get_center_freq(self) -> str:
        """Return current center frequency in Hz."""
        return str(self._center_freq)

    def get_span(self) -> str:
        """Return current span in Hz."""
        return str(self._span)

    def get_rbw(self) -> str:
        """Return current resolution bandwidth in Hz."""
        return str(self._rbw)

    def get_status(self) -> str:
        """Return device connection status."""
        return "connected" if self._device is not None else "disconnected"
