"""SDK wrapper for Oxford ILM (Intelligent Level Meter).

The Oxford ILM measures liquid cryogen levels (helium, nitrogen) in
cryostats.  It uses the Oxford serial protocol with ``\\r`` terminators.

Commands:
    - ``R{channel}`` — read level for channel (1=He, 2=N2)
    - ``T{channel}`` — read channel status
    - ``V`` — version/identity query

Response format: ``R{value}`` — strip the ``R`` prefix to get the
float percentage.
"""

from __future__ import annotations

import logging
from typing import Optional

from .oxford_serial_wrapper import OxfordSerialClient

logger = logging.getLogger(__name__)


class OxfordILMClient(OxfordSerialClient):
    """Wrapper for the Oxford ILM cryogen level meter.

    Channels:
        1 — Liquid helium
        2 — Liquid nitrogen
    """

    def __init__(
        self,
        address: Optional[str] = None,
        terminator: str = "\r",
    ) -> None:
        super().__init__(address=address, terminator=terminator)

    def get_identity(self) -> str:
        """Return identification string for the ILM."""
        self._require_connected()
        try:
            resp = self.query("V")
            return f"Oxford Instruments,ILM,{resp}"
        except Exception:
            return f"Oxford Instruments,ILM,{self._address}"

    # -- Level readings -------------------------------------------------------

    def get_helium_level(self) -> float:
        """Read the liquid helium level (channel 1).

        Returns
        -------
        float
            Helium level as a percentage (0–100).
        """
        self._require_connected()
        response = self.query("R1")
        return self._strip_prefix(response, "R")

    def get_nitrogen_level(self) -> float:
        """Read the liquid nitrogen level (channel 2).

        Returns
        -------
        float
            Nitrogen level as a percentage (0–100).
        """
        self._require_connected()
        response = self.query("R2")
        return self._strip_prefix(response, "R")

    def get_level(self, channel: int = 1) -> float:
        """Read the cryogen level for a given channel.

        Parameters
        ----------
        channel:
            1 for helium, 2 for nitrogen.

        Returns
        -------
        float
            Level as a percentage (0–100).
        """
        if channel not in (1, 2):
            raise ValueError(f"Invalid ILM channel: {channel}. Use 1 (He) or 2 (N2).")
        self._require_connected()
        response = self.query(f"R{channel}")
        return self._strip_prefix(response, "R")
