"""SDK wrapper for Oxford Mercury IPS (Intelligent Power Supply).

The Mercury IPS is a newer-generation superconducting magnet power supply
from Oxford Instruments.  It uses the Oxford ISOBUS protocol over
serial or Ethernet with a hierarchical command syntax similar to the
Oxford Triton system.

Command format:
    ``READ:DEV:GRPX:PSU:SIG:FLD``  — read magnetic field
    ``SET:DEV:GRPX:PSU:SIG:FLD:<value>``  — set target field
    ``READ:DEV:GRPX:PSU:SIG:RFLD``  — read field sweep rate
    ``SET:DEV:GRPX:PSU:SIG:RFLD:<rate>``  — set sweep rate
    ``READ:DEV:GRPX:PSU:ACTN``  — read action/status

Response format: ``STAT:DEV:GRPX:PSU:SIG:FLD:<value>T``
    The value is the last colon-separated segment, optionally with a
    unit suffix (``T`` for tesla, ``T/m`` for rate).
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from .oxford_serial_wrapper import OxfordSerialClient

logger = logging.getLogger(__name__)


class OxfordMercuryIPSClient(OxfordSerialClient):
    """Wrapper for the Oxford Mercury IPS magnet power supply.

    Parameters
    ----------
    address:
        VISA resource string or IP address.
    terminator:
        Line terminator (default ``\\r``).
    grp:
        Device group identifier in the ISOBUS hierarchy (default ``GRPX``).
    """

    def __init__(
        self,
        address: Optional[str] = None,
        terminator: str = "\r",
        grp: str = "GRPX",
    ) -> None:
        super().__init__(address=address, terminator=terminator)
        self._grp = grp

    def get_identity(self) -> str:
        """Return identification string for the Mercury IPS."""
        self._require_connected()
        try:
            resp = self.query("*IDN?")
            return resp
        except Exception:
            try:
                resp = self.query("V")
                return f"Oxford Instruments,Mercury IPS,{resp}"
            except Exception:
                return f"Oxford Instruments,Mercury IPS,{self._address}"

    # -- Field operations -----------------------------------------------------

    def get_field(self) -> float:
        """Read the current magnetic field in Tesla.

        Returns
        -------
        float
            Magnetic field in Tesla.
        """
        self._require_connected()
        response = self.query(
            f"READ:DEV:{self._grp}:PSU:SIG:FLD"
        )
        return self._parse_mercury_value(response)

    def set_field(self, value: float, rate: float = 0.1) -> str:
        """Set the target magnetic field with a sweep rate.

        Parameters
        ----------
        value:
            Target field in Tesla.
        rate:
            Sweep rate in Tesla/minute.

        Returns
        -------
        str
            Acknowledgement string.
        """
        self._require_connected()
        # Set the sweep rate first
        self.query(
            f"SET:DEV:{self._grp}:PSU:SIG:RFLD:{rate}"
        )
        # Set the target field
        response = self.query(
            f"SET:DEV:{self._grp}:PSU:SIG:FLD:{value}"
        )
        # Start the sweep (RTOS = ramp to set)
        self.query(
            f"SET:DEV:{self._grp}:PSU:ACTN:RTOS"
        )
        return response

    def get_field_sweep_rate(self) -> float:
        """Read the current field sweep rate in Tesla/minute.

        Returns
        -------
        float
            Sweep rate in T/min.
        """
        self._require_connected()
        response = self.query(
            f"READ:DEV:{self._grp}:PSU:SIG:RFLD"
        )
        return self._parse_mercury_value(response)

    def set_field_sweep_rate(self, rate: float) -> str:
        """Set the field sweep rate.

        Parameters
        ----------
        rate:
            Sweep rate in Tesla/minute.
        """
        self._require_connected()
        response = self.query(
            f"SET:DEV:{self._grp}:PSU:SIG:RFLD:{rate}"
        )
        return response

    # -- Status ---------------------------------------------------------------

    def get_status(self) -> str:
        """Read the magnet power supply status/action.

        Returns
        -------
        str
            Status string, e.g. ``HOLD``, ``RTOS`` (ramp to set),
            ``RTOZ`` (ramp to zero), ``CLMP`` (clamped).
        """
        self._require_connected()
        response = self.query(
            f"READ:DEV:{self._grp}:PSU:ACTN"
        )
        # Extract the action from the response
        parts = response.split(":")
        return parts[-1].strip() if parts else response.strip()

    def hold(self) -> str:
        """Pause the current sweep (hold at current field)."""
        self._require_connected()
        return self.query(
            f"SET:DEV:{self._grp}:PSU:ACTN:HOLD"
        )

    def ramp_to_zero(self) -> str:
        """Ramp the field to zero."""
        self._require_connected()
        return self.query(
            f"SET:DEV:{self._grp}:PSU:ACTN:RTOZ"
        )

    # -- Persistent switch ----------------------------------------------------

    def get_switch_heater(self) -> str:
        """Read the persistent switch heater status.

        Returns
        -------
        str
            ``ON`` or ``OFF``.
        """
        self._require_connected()
        response = self.query(
            f"READ:DEV:{self._grp}:PSU:SIG:SWHT"
        )
        parts = response.split(":")
        return parts[-1].strip() if parts else response.strip()

    # -- Helpers --------------------------------------------------------------

    @staticmethod
    def _parse_mercury_value(response: str) -> float:
        """Extract a numeric value from a Mercury-style response.

        Mercury responses look like:
            ``STAT:DEV:GRPX:PSU:SIG:FLD:1.234T``

        The value is the last segment, possibly with a unit suffix.
        """
        parts = response.split(":")
        last = parts[-1].strip() if parts else response.strip()
        # Strip common unit suffixes
        match = re.match(r"([+-]?\d+\.?\d*(?:[eE][+-]?\d+)?)", last)
        if match:
            return float(match.group(1))
        raise ValueError(f"Cannot parse Mercury response: {response}")
