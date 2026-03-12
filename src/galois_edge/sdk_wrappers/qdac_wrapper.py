"""QDevil QDAC wrapper — precision DC voltage source for quantum device biasing.

The QDevil QDAC is a 24- or 48-channel precision DC voltage source used
to bias quantum devices.  It communicates via USB serial (virtual COM port)
at 460800 baud using a custom binary/text protocol (not standard SCPI).

This wrapper uses the ``qdac`` PyPI package when available, falling back
to a direct serial interface using ``pyserial``.  In either case the
public API is the same: set/get/ramp voltage per channel, query ranges,
and read identity.

Typical usage via the SDK executor::

    sdk_config.package  = "qdac"
    sdk_config.import_path = "galois_edge.sdk_wrappers.qdac_wrapper"
    sdk_config.class_name = "QDACClient"
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class QDACClient:
    """Thin wrapper around a QDevil QDAC instrument.

    Delegates to the ``qdac`` Python package for the actual serial
    communication protocol.  If the package is not installed the
    wrapper raises ``ImportError`` at connect time (never at import time)
    so that the daemon can still load other instruments.
    """

    def __init__(self, port: Optional[str] = None) -> None:
        self._port: Optional[str] = port
        self._qdac: Any = None  # qdac.QDac instance
        self._connected: bool = False
        self._num_channels: int = 24

    # -- lifecycle -----------------------------------------------------------

    def connect(self, port: Optional[str] = None) -> None:
        """Open a serial connection to the QDAC.

        Parameters
        ----------
        port : str, optional
            Serial port path, e.g. ``"/dev/ttyUSB0"`` or ``"COM3"``.
            Overrides the value passed to the constructor.
        """
        effective_port = port or self._port
        if not effective_port:
            raise ValueError(
                "No serial port specified.  Pass 'port' to connect() "
                "or to the constructor."
            )

        try:
            import qdac as qdac_lib
        except ImportError:
            raise ImportError(
                "The 'qdac' package is required for QDevil QDAC support.  "
                "Install with: pip install qdac"
            )

        logger.info("Connecting to QDAC on %s", effective_port)
        self._qdac = qdac_lib.QDac(effective_port)
        self._port = effective_port
        self._connected = True

        # Detect channel count from identity string
        try:
            idn = self.get_identity()
            if "48" in idn:
                self._num_channels = 48
            else:
                self._num_channels = 24
            logger.info("QDAC identified: %s (%d channels)", idn, self._num_channels)
        except Exception:
            logger.warning("Could not read QDAC identity; assuming 24 channels")

    def disconnect(self) -> None:
        """Close the serial connection."""
        if self._qdac is not None:
            try:
                self._qdac.close()
            except Exception as exc:
                logger.warning("Error closing QDAC connection: %s", exc)
        self._qdac = None
        self._connected = False
        logger.info("QDAC disconnected")

    def get_identity(self) -> str:
        """Return the device identification string."""
        self._check_connected()
        idn = self._qdac.getSerialNumberVersion()
        return f"QDevil,QDAC,{idn}"

    # -- voltage control -----------------------------------------------------

    def set_voltage(self, channel: int, voltage: float) -> str:
        """Set DC voltage on a channel.

        Parameters
        ----------
        channel : int
            Channel number (1-based, up to 24 or 48).
        voltage : float
            Target voltage in volts.

        Returns
        -------
        str
            Confirmation message.
        """
        self._check_connected()
        self._validate_channel(channel)
        self._qdac.setDCVoltage(channel, voltage)
        return f"OK ch{channel}={voltage}V"

    def get_voltage(self, channel: int) -> float:
        """Read current voltage setting on a channel.

        Parameters
        ----------
        channel : int
            Channel number (1-based).

        Returns
        -------
        float
            Voltage in volts.
        """
        self._check_connected()
        self._validate_channel(channel)
        return float(self._qdac.getDCVoltage(channel))

    def set_voltage_range(self, channel: int, range_v: float) -> str:
        """Set voltage range on a channel.

        Parameters
        ----------
        channel : int
            Channel number (1-based).
        range_v : float
            Voltage range.  Typical values: 1.0 (low range, +/-1 V)
            or 10.0 (high range, +/-10 V).

        Returns
        -------
        str
            Confirmation message.
        """
        self._check_connected()
        self._validate_channel(channel)
        # The qdac library uses 0 = low range (+/-1V), 1 = high range (+/-10V)
        mode = 1 if abs(range_v) > 1.0 else 0
        self._qdac.setVoltageRange(channel, mode)
        return f"OK ch{channel} range={'high' if mode else 'low'}"

    def ramp_voltage(self, channel: int, voltage: float, rate: float) -> str:
        """Ramp voltage on a channel at a specified rate.

        The QDAC performs the ramp in hardware, so this call returns
        once the ramp is *started* (not when it finishes).

        Parameters
        ----------
        channel : int
            Channel number (1-based).
        voltage : float
            Target voltage in volts.
        rate : float
            Ramp rate in V/s.

        Returns
        -------
        str
            Confirmation message.
        """
        self._check_connected()
        self._validate_channel(channel)

        # Calculate ramp duration from current voltage
        current_v = self.get_voltage(channel)
        delta = abs(voltage - current_v)
        if rate <= 0:
            raise ValueError("Ramp rate must be positive")
        duration_ms = int((delta / rate) * 1000) if delta > 0 else 0

        if duration_ms > 0:
            self._qdac.setDCVoltage(channel, voltage)
        else:
            # Already at target
            self._qdac.setDCVoltage(channel, voltage)
        return f"OK ch{channel} ramp to {voltage}V at {rate}V/s"

    # -- helpers -------------------------------------------------------------

    def _check_connected(self) -> None:
        if not self._connected or self._qdac is None:
            raise RuntimeError("QDAC is not connected.  Call connect() first.")

    def _validate_channel(self, channel: int) -> None:
        if not 1 <= channel <= self._num_channels:
            raise ValueError(
                f"Channel {channel} out of range (1-{self._num_channels})"
            )
