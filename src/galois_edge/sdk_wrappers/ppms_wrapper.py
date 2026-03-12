"""Quantum Design PPMS DynaCool wrapper — cryostat control via MultiPyVu.

The PPMS DynaCool is controlled over the network using the ``MultiPyVu``
Python package.  The MultiPyVu server application must be running on a
Windows machine that has the MultiVu application installed and connected
to the PPMS hardware.

Capabilities:
    - Temperature control (1.8 K – 400 K)
    - Magnetic field control (up to ±14 T)
    - Chamber state management (seal, purge, vent)
    - Wait-for-stability blocking calls
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class PPMSClient:
    """Wraps the MultiPyVu Client for Quantum Design PPMS DynaCool."""

    def __init__(self, host: str = "localhost", port: int = 5000) -> None:
        self._host = host
        self._port = port
        self._client: Any = None
        self._connected = False

    # -- lifecycle -----------------------------------------------------------

    def connect(self) -> None:
        """Import MultiPyVu and connect to the PPMS server."""
        try:
            import MultiPyVu  # type: ignore[import-untyped]
        except (ImportError, OSError):
            raise ImportError(
                "MultiPyVu package not found. Install with: pip install MultiPyVu. "
                "Note: The MultiPyVu server must be running on a Windows machine "
                "with the MultiVu application."
            )

        self._client = MultiPyVu.Client(host=self._host, port=self._port)
        self._client.open()
        self._connected = True
        logger.info("PPMS connected: %s:%d", self._host, self._port)

    def disconnect(self) -> None:
        """Close the connection to the PPMS server."""
        if self._client is not None:
            try:
                self._client.close()
            except Exception as exc:
                logger.warning("Error closing PPMS connection: %s", exc)
        self._client = None
        self._connected = False
        logger.info("PPMS disconnected")

    def get_identity(self) -> str:
        """Return instrument identity string."""
        return f"Quantum Design,PPMS DynaCool,{self._host}:{self._port},1.0"

    # -- internal helpers ----------------------------------------------------

    def _check_connected(self) -> None:
        """Raise RuntimeError if the client is not connected."""
        if not self._connected or self._client is None:
            raise RuntimeError(
                "PPMS client is not connected. Call connect() first."
            )

    # -- temperature ---------------------------------------------------------

    def get_temperature(self) -> float:
        """Read the current sample temperature in Kelvin."""
        self._check_connected()
        try:
            temperature = self._client.get_temperature()
            logger.debug("PPMS temperature: %.4f K", temperature)
            return temperature
        except Exception as exc:
            logger.error("Failed to read PPMS temperature: %s", exc)
            raise

    def set_temperature(self, temperature: float, rate: float = 10.0) -> str:
        """Set the target sample temperature.

        Parameters
        ----------
        temperature : float
            Target temperature in Kelvin (1.8 – 400 K).
        rate : float
            Ramp rate in K/min (default 10.0).
        """
        self._check_connected()
        try:
            self._client.set_temperature(temperature, rate)
            logger.info(
                "PPMS set temperature: %.2f K at %.1f K/min",
                temperature, rate,
            )
            return "OK"
        except Exception as exc:
            logger.error("Failed to set PPMS temperature: %s", exc)
            raise

    # -- magnetic field ------------------------------------------------------

    def get_field(self) -> float:
        """Read the current magnetic field in Oersted."""
        self._check_connected()
        try:
            field = self._client.get_field()
            logger.debug("PPMS field: %.1f Oe", field)
            return field
        except Exception as exc:
            logger.error("Failed to read PPMS field: %s", exc)
            raise

    def set_field(self, field: float, rate: float = 100.0) -> str:
        """Set the target magnetic field.

        Parameters
        ----------
        field : float
            Target field in Oersted (±140000 Oe for 14 T system).
        rate : float
            Ramp rate in Oe/s (default 100.0).
        """
        self._check_connected()
        try:
            self._client.set_field(field, rate)
            logger.info(
                "PPMS set field: %.1f Oe at %.1f Oe/s", field, rate,
            )
            return "OK"
        except Exception as exc:
            logger.error("Failed to set PPMS field: %s", exc)
            raise

    # -- chamber -------------------------------------------------------------

    def get_chamber(self) -> str:
        """Read the current chamber status.

        Returns a string such as 'sealed', 'purged', 'vented', etc.
        """
        self._check_connected()
        try:
            status = self._client.get_chamber()
            logger.debug("PPMS chamber status: %s", status)
            return str(status)
        except Exception as exc:
            logger.error("Failed to read PPMS chamber status: %s", exc)
            raise

    def set_chamber(self, state: str) -> str:
        """Set the chamber state.

        Parameters
        ----------
        state : str
            One of: seal, purge_seal, vent_seal, pump_continuous,
            vent_continuous.
        """
        self._check_connected()
        try:
            self._client.set_chamber(state)
            logger.info("PPMS set chamber: %s", state)
            return "OK"
        except Exception as exc:
            logger.error("Failed to set PPMS chamber state: %s", exc)
            raise

    # -- stability -----------------------------------------------------------

    def wait_for(
        self,
        temperature: bool = True,
        field: bool = True,
        delay: int = 0,
    ) -> str:
        """Wait for temperature and/or field to stabilize.

        Parameters
        ----------
        temperature : bool
            Wait for temperature stability (default True).
        field : bool
            Wait for field stability (default True).
        delay : int
            Additional delay in seconds after stability (default 0).
        """
        self._check_connected()
        try:
            self._client.wait_for(
                temperature=temperature, field=field, delay=delay,
            )
            logger.info(
                "PPMS stability reached (temp=%s, field=%s)", temperature, field,
            )
            return "OK"
        except Exception as exc:
            logger.error("PPMS wait_for failed: %s", exc)
            raise
