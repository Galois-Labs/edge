"""Leiden Cryogenics pressure monitoring wrapper.

Leiden Cryogenics dilution refrigerators often include a pressure
monitoring system accessible over a serial or Ethernet interface.
The communication protocol is typically a simple ASCII command/response
over a TCP socket or serial port.

Common commands::

    *IDN?             -> identity string
    PRES? <channel>   -> pressure reading for channel N
    STAT?             -> system status
    TEMP? <channel>   -> temperature reading

The wrapper wraps this into a clean SDK-style interface.
"""

from __future__ import annotations

import json
import logging
import socket
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Default communication settings
DEFAULT_HOST = "192.168.0.100"
DEFAULT_PORT = 9001
DEFAULT_TIMEOUT = 5.0


class LeidenClient:
    """Wraps communication with a Leiden Cryogenics pressure monitor."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
    ) -> None:
        self._host = host
        self._port = port
        self._sock: Optional[socket.socket] = None
        self._timeout = DEFAULT_TIMEOUT

    # -- lifecycle -----------------------------------------------------------

    def connect(self) -> None:
        """Open a TCP socket to the Leiden pressure monitor."""
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.settimeout(self._timeout)
        try:
            self._sock.connect((self._host, self._port))
        except (socket.error, OSError) as exc:
            self._sock = None
            raise ConnectionError(
                f"Cannot connect to Leiden pressure monitor at "
                f"{self._host}:{self._port}: {exc}"
            )
        logger.info(
            "Leiden pressure monitor connected: %s:%d",
            self._host, self._port,
        )

    def disconnect(self) -> None:
        """Close the socket connection."""
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception as exc:
                logger.warning("Error closing Leiden socket: %s", exc)
        self._sock = None
        logger.info("Leiden pressure monitor disconnected")

    def get_identity(self) -> str:
        """Query instrument identity."""
        try:
            resp = self._query("*IDN?")
            return resp.strip()
        except Exception:
            return "Leiden,Pressure,N/A,1.0"

    # -- communication -------------------------------------------------------

    def _send(self, command: str) -> None:
        """Send a command string terminated with newline."""
        if self._sock is None:
            raise RuntimeError("Not connected to Leiden pressure monitor")
        self._sock.sendall((command + "\n").encode("ascii"))

    def _recv(self, bufsize: int = 4096) -> str:
        """Receive a response string."""
        if self._sock is None:
            raise RuntimeError("Not connected to Leiden pressure monitor")
        data = self._sock.recv(bufsize)
        return data.decode("ascii", errors="replace").strip()

    def _query(self, command: str) -> str:
        """Send a command and return the response."""
        self._send(command)
        return self._recv()

    # -- pressure readings ---------------------------------------------------

    def get_pressure(self, channel: int = 1) -> str:
        """Read pressure from the specified channel.

        Parameters
        ----------
        channel : int
            Pressure gauge channel number (typically 1-6).

        Returns
        -------
        str
            Pressure value in mbar.
        """
        resp = self._query(f"PRES? {channel}")
        return resp

    def get_all_pressures(self) -> str:
        """Read pressure from all channels (1-6).

        Returns JSON-encoded dict mapping channel number to pressure.
        """
        result: Dict[str, str] = {}
        for ch in range(1, 7):
            try:
                result[str(ch)] = self.get_pressure(ch)
            except Exception as exc:
                logger.debug("Channel %d read failed: %s", ch, exc)
        return json.dumps(result)

    # -- temperature readings ------------------------------------------------

    def get_temperature(self, channel: int = 1) -> str:
        """Read temperature from the specified channel.

        Parameters
        ----------
        channel : int
            Temperature sensor channel.

        Returns
        -------
        str
            Temperature value in K.
        """
        resp = self._query(f"TEMP? {channel}")
        return resp

    # -- status --------------------------------------------------------------

    def get_status(self) -> str:
        """Query system status."""
        if self._sock is None:
            return "disconnected"
        try:
            return self._query("STAT?")
        except Exception:
            return "unknown"

    def get_valve_state(self, valve: int = 1) -> str:
        """Read valve open/closed state.

        Parameters
        ----------
        valve : int
            Valve number.

        Returns
        -------
        str
            "open" or "closed".
        """
        resp = self._query(f"VALVE? {valve}")
        return resp
