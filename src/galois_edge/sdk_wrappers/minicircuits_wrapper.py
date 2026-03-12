"""MiniCircuits RF Switch wrapper — controls switches via HTTP API.

MiniCircuits programmable switches (RC-xSPxT series, USB-8SPDT, etc.)
expose an HTTP API on newer models. The API is accessed at::

    http://{address}/SCPI

With commands sent as query parameters, e.g.::

    http://{address}/SCPI?SETA=1
    http://{address}/SCPI?GETSWITCH
    http://{address}/SCPI?MN?  (model number)
    http://{address}/SCPI?SN?  (serial number)

Older USB-only models use a vendor DLL (``mcl_RF_Switch_Controller64.dll``),
which is not covered here. This wrapper targets the HTTP/Ethernet models
that are most commonly deployed in modern labs.
"""

from __future__ import annotations

import json
import logging
from typing import Dict, Optional
from urllib.request import urlopen, Request
from urllib.error import URLError

logger = logging.getLogger(__name__)

_TIMEOUT_S = 5


class MiniCircuitsClient:
    """HTTP API client for MiniCircuits programmable RF switches."""

    def __init__(self, address: Optional[str] = None) -> None:
        self._address: Optional[str] = address
        self._connected: bool = False

    # -- lifecycle -----------------------------------------------------------

    def connect(self, address: Optional[str] = None) -> None:
        """Verify HTTP connectivity to the switch.

        Parameters
        ----------
        address : str, optional
            IP address or hostname. Overrides constructor value if given.
        """
        if address is not None:
            self._address = address
        if not self._address:
            raise ValueError("No address provided for MiniCircuits switch")

        # Verify connectivity by querying the model number
        try:
            self._http_get("MN?")
            self._connected = True
            logger.info("MiniCircuits switch connected at %s", self._address)
        except Exception as exc:
            raise ConnectionError(
                f"Cannot reach MiniCircuits switch at {self._address}: {exc}"
            ) from exc

    def disconnect(self) -> None:
        """No-op — HTTP is stateless."""
        self._connected = False

    def get_identity(self) -> str:
        """Query model and serial number, return IDN-style string."""
        model = self._http_get("MN?")
        serial = self._http_get("SN?")
        return f"MiniCircuits,{model},{serial},1.0"

    # -- switch control ------------------------------------------------------

    def set_switch(self, switch_id: str = "A", state: int = 0) -> str:
        """Set a single switch to the given position.

        Parameters
        ----------
        switch_id : str
            Switch identifier, e.g. ``"A"``, ``"B"``, ``"C"``, ``"D"``.
        state : int
            Switch position (0 = COM, 1-6 = port number depending on model).

        Returns
        -------
        str
            ``"1"`` on success, ``"0"`` on failure.
        """
        return self._http_get(f"SET{switch_id}={state}")

    def get_switch(self, switch_id: str = "A") -> str:
        """Read the current position of a single switch.

        Returns the port number as a string (e.g. ``"0"``, ``"1"``).
        """
        return self._http_get(f"GETSWITCH{switch_id}")

    def set_all_switches(self, state: int = 0) -> str:
        """Set all switches to the same position.

        Returns ``"1"`` on success.
        """
        return self._http_get(f"SETP1={state}")

    def get_all_switches(self) -> str:
        """Read all switch positions.

        Returns a JSON-encoded dict mapping switch IDs to positions.
        """
        result: Dict[str, str] = {}
        for switch_id in ("A", "B", "C", "D"):
            try:
                pos = self._http_get(f"GETSWITCH{switch_id}")
                if pos and pos.strip() != "":
                    result[switch_id] = pos.strip()
            except Exception:
                # Switch ID doesn't exist on this model — skip
                pass
        return json.dumps(result)

    # -- matrix control (USB-8SPDT style) ------------------------------------

    def set_matrix_switch(self, switch_number: int = 1, state: int = 0) -> str:
        """Set a numbered switch in a switch matrix.

        Parameters
        ----------
        switch_number : int
            Switch number (1-8 for USB-8SPDT-A18).
        state : int
            0 or 1 for SPDT switches.
        """
        return self._http_get(f"SET{switch_number}={state}")

    def get_matrix_switch(self, switch_number: int = 1) -> str:
        """Read the state of a numbered switch in a switch matrix."""
        return self._http_get(f"GETSWITCH{switch_number}")

    # -- internal ------------------------------------------------------------

    def _http_get(self, command: str) -> str:
        """Send a command via HTTP GET and return the response body."""
        if not self._address:
            raise RuntimeError("MiniCircuits switch address not set")

        url = f"http://{self._address}/SCPI?{command}"
        try:
            req = Request(url)
            with urlopen(req, timeout=_TIMEOUT_S) as resp:
                body = resp.read().decode("utf-8", errors="replace").strip()
                return body
        except URLError as exc:
            raise ConnectionError(
                f"HTTP request to {self._address} failed: {exc}"
            ) from exc
