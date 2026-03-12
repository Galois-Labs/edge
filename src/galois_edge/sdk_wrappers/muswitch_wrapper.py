"""MuSwitch / MuSwitchEX RF switch wrapper.

MuSwitch devices are USB/serial-based RF switch matrices commonly used
in quantum computing setups. They present a simple text command interface
over a serial port (typically a USB virtual COM port).

Command protocol::

    SET <channel> <state>   — set switch channel to state (0/1)
    GET <channel>           — query current state of a channel
    SETALL <state>          — set all channels to the same state
    GETALL                  — query all channel states
    *IDN?                   — identity query

The MuSwitchEX is an extended model with more channels and additional
features (e.g., switch groups, interlock).
"""

from __future__ import annotations

import json
import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class MuSwitchClient:
    """Client for MuSwitch and MuSwitchEX RF switch matrices.

    This wrapper provides the SDK interface expected by the daemon's
    ``SDKExecutor``. Actual serial communication requires ``pyserial``.
    """

    def __init__(self, address: Optional[str] = None) -> None:
        self._address: Optional[str] = address
        self._serial: object = None  # serial.Serial instance when connected
        self._connected: bool = False
        self._num_channels: int = 1  # basic MuSwitch default

    # -- lifecycle -----------------------------------------------------------

    def connect(self, address: Optional[str] = None, num_channels: int = 1) -> None:
        """Open serial connection to the MuSwitch.

        Parameters
        ----------
        address : str, optional
            Serial port path (e.g. ``/dev/ttyUSB0``, ``COM3``).
            Overrides constructor value if given.
        num_channels : int
            Number of switch channels (1 for MuSwitch, more for MuSwitchEX).
        """
        if address is not None:
            self._address = address
        if not self._address:
            raise ValueError("No address/port provided for MuSwitch")

        self._num_channels = num_channels

        try:
            import serial
            self._serial = serial.Serial(
                port=self._address,
                baudrate=115200,
                timeout=2,
            )
            self._connected = True
            logger.info("MuSwitch connected on %s", self._address)
        except ImportError:
            raise ImportError(
                "pyserial is required for MuSwitch: pip install pyserial"
            )
        except Exception as exc:
            raise ConnectionError(
                f"Cannot open MuSwitch on {self._address}: {exc}"
            ) from exc

    def disconnect(self) -> None:
        """Close the serial connection."""
        if self._serial is not None:
            try:
                self._serial.close()  # type: ignore[union-attr]
            except Exception:
                pass
        self._serial = None
        self._connected = False

    def get_identity(self) -> str:
        """Query device identity."""
        resp = self._send_command("*IDN?")
        if resp:
            return resp
        return "MuSwitch,Unknown,N/A,1.0"

    # -- switch control ------------------------------------------------------

    def set_switch(self, channel: int = 1, state: int = 0) -> str:
        """Set a switch channel to the given state.

        Parameters
        ----------
        channel : int
            Channel number (1-based).
        state : int
            Switch state (0 or 1).

        Returns
        -------
        str
            ``"OK"`` on success.
        """
        return self._send_command(f"SET {channel} {state}")

    def get_switch(self, channel: int = 1) -> str:
        """Read the current state of a switch channel.

        Returns ``"0"`` or ``"1"``.
        """
        return self._send_command(f"GET {channel}")

    def set_all_switches(self, state: int = 0) -> str:
        """Set all switch channels to the same state."""
        return self._send_command(f"SETALL {state}")

    def get_all_switches(self) -> str:
        """Read all switch channel states.

        Returns a JSON-encoded dict mapping channel numbers to states.
        """
        result: Dict[str, str] = {}
        for ch in range(1, self._num_channels + 1):
            try:
                val = self._send_command(f"GET {ch}")
                result[str(ch)] = val.strip()
            except Exception:
                pass
        return json.dumps(result)

    # -- internal ------------------------------------------------------------

    def _send_command(self, command: str) -> str:
        """Send a text command and read the response line."""
        if self._serial is None:
            raise RuntimeError("MuSwitch not connected")

        ser = self._serial
        ser.write(f"{command}\n".encode("ascii"))  # type: ignore[union-attr]
        resp = ser.readline().decode("ascii", errors="replace").strip()  # type: ignore[union-attr]
        return resp
