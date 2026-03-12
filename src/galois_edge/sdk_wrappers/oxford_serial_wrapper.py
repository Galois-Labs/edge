"""Shared base wrapper for Oxford Instruments serial protocol devices.

Oxford cryogenic instruments (ILM, IPS120, PS120, Mercury IPS) use a
custom serial protocol with ``\\r`` line terminators.  Commands are
short ASCII strings (e.g. ``R7`` to read field) and responses echo a
prefix character followed by the numeric value.

This base class handles the connection lifecycle (via PyVISA) and
raw query/write operations.  Instrument-specific subclasses add
typed convenience methods.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


class OxfordSerialClient:
    """Base class for Oxford Instruments serial/GPIB protocol.

    Parameters
    ----------
    address:
        VISA resource string, e.g. ``GPIB0::24::INSTR`` or
        ``ASRL/dev/ttyUSB0::INSTR``.
    terminator:
        Line terminator for commands and responses (default ``\\r``).
    """

    def __init__(
        self,
        address: Optional[str] = None,
        terminator: str = "\r",
    ) -> None:
        self._address = address
        self._terminator = terminator
        self._resource: Optional[object] = None  # pyvisa Resource
        self._rm: Optional[object] = None  # pyvisa ResourceManager

    # -- Connection lifecycle -------------------------------------------------

    def connect(self, address: Optional[str] = None) -> None:
        """Open a VISA connection to the instrument.

        Parameters
        ----------
        address:
            VISA resource string. Overrides the constructor value if
            provided.
        """
        if address is not None:
            self._address = address
        if not self._address:
            raise ValueError("No VISA address provided for Oxford instrument")

        try:
            import pyvisa
        except ImportError as exc:
            raise ImportError(
                "The 'pyvisa' package is required for Oxford serial instruments. "
                "Install with: pip install pyvisa pyvisa-py"
            ) from exc

        self._rm = pyvisa.ResourceManager("@py")
        self._resource = self._rm.open_resource(
            self._address,
            read_termination=self._terminator,
            write_termination=self._terminator,
        )
        logger.info("Connected to Oxford instrument at %s", self._address)

    def disconnect(self) -> None:
        """Close the VISA connection."""
        if self._resource is not None:
            try:
                self._resource.close()  # type: ignore[union-attr]
            except Exception as exc:
                logger.warning("Error closing Oxford instrument: %s", exc)
            finally:
                self._resource = None
        if self._rm is not None:
            try:
                self._rm.close()  # type: ignore[union-attr]
            except Exception:
                pass
            finally:
                self._rm = None
        logger.info("Disconnected Oxford instrument at %s", self._address)

    def get_identity(self) -> str:
        """Return an identification string for the instrument.

        Oxford serial instruments typically do not support ``*IDN?``,
        so this sends a version query (``V``) and falls back to a
        static string if that fails.
        """
        self._require_connected()
        try:
            resp = self.query("V")
            return f"Oxford Instruments,{resp}"
        except Exception:
            return f"Oxford Instruments,Unknown,{self._address}"

    # -- Raw I/O --------------------------------------------------------------

    def query(self, command: str) -> str:
        """Send a command and return the response string.

        Parameters
        ----------
        command:
            Raw command string (without terminator).
        """
        self._require_connected()
        response = self._resource.query(command)  # type: ignore[union-attr]
        return response.strip()

    def write(self, command: str) -> str:
        """Send a command without reading a response.

        Parameters
        ----------
        command:
            Raw command string (without terminator).
        """
        self._require_connected()
        self._resource.write(command)  # type: ignore[union-attr]
        return "OK"

    # -- Helpers --------------------------------------------------------------

    def _require_connected(self) -> None:
        """Raise if the instrument is not connected."""
        if self._resource is None:
            raise RuntimeError(
                "Oxford instrument is not connected. Call connect() first."
            )

    def _strip_prefix(self, response: str, prefix: str = "R") -> float:
        """Strip a leading prefix character and parse as float.

        Oxford instruments return values like ``R1.234`` where ``R``
        is the echo of the read command.
        """
        text = response.strip()
        if text.startswith(prefix):
            text = text[len(prefix):]
        return float(text)
