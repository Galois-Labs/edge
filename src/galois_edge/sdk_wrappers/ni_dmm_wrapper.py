"""NI-DMM wrapper — thin abstraction over the ``nidmm`` Python package.

Supports NI PXI digital multimeters (e.g. PXIe-4081, PXIe-4082).
The ``nidmm`` package is part of NI's nimi-python project and supports
both local (NI-DMM driver on Windows) and remote (NI gRPC Device Server)
operation via ``GrpcSessionOptions``.

Typical usage through the SDK executor:
  1. ``connect()`` — open a session (local or gRPC)
  2. ``configure_measurement()`` — set measurement function / range
  3. ``read()`` — take a single reading
  4. ``disconnect()`` — close the session
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class NiDmmClient:
    """Wraps ``nidmm`` for NI PXI digital multimeter operations.

    Parameters
    ----------
    resource : str
        NI-DMM resource name, e.g. ``"PXI1Slot5"``.
    grpc_address : str or None
        If set, connect via NI gRPC Device Server at this address
        (e.g. ``"192.168.1.100:31763"``).  If ``None``, use local drivers.
    """

    def __init__(self, resource: str = "PXI1Slot5", grpc_address: Optional[str] = None) -> None:
        self._resource = resource
        self._grpc_address = grpc_address
        self._session: Any = None
        self._connected = False

    # -- lifecycle -----------------------------------------------------------

    def connect(self) -> None:
        """Open a session to the digital multimeter."""
        try:
            import nidmm  # type: ignore[import-untyped]
        except (ImportError, OSError):
            raise ImportError(
                "nidmm package not found. Install with: pip install nidmm. "
                "Also requires the NI-DMM runtime driver on Windows, or "
                "NI gRPC Device Server for remote access from Linux. "
                "See: https://github.com/ni/grpc-device"
            )
        if self._grpc_address:
            import grpc
            channel = grpc.insecure_channel(self._grpc_address)
            grpc_options = nidmm.GrpcSessionOptions(channel, session_name="")
            self._session = nidmm.Session(self._resource, grpc_options=grpc_options)
        else:
            self._session = nidmm.Session(self._resource)
        self._connected = True
        logger.info("NI-DMM connected: %s (gRPC: %s)", self._resource, self._grpc_address or "local")

    def disconnect(self) -> None:
        """Close the NI-DMM session."""
        if self._session is not None:
            try:
                self._session.close()
            except Exception as exc:
                logger.warning("Error closing NI-DMM session: %s", exc)
        self._session = None
        self._connected = False
        logger.info("NI-DMM disconnected: %s", self._resource)

    def get_identity(self) -> str:
        """Return an IDN-style identity string."""
        self._check_connected()
        try:
            model = self._session.instrument_model
            serial = self._session.serial_number
            fw = self._session.instrument_firmware_revision
            return f"NI,{model},{serial},{fw}"
        except Exception:
            return f"NI,DMM,{self._resource},1.0"

    # -- measurement configuration -------------------------------------------

    def configure_measurement(
        self,
        function: str = "DC_VOLTS",
        range_val: float = 10.0,
        resolution: float = 6.5,
    ) -> str:
        """Configure measurement function, range, and resolution.

        Parameters
        ----------
        function : str
            Measurement function: ``"DC_VOLTS"``, ``"AC_VOLTS"``,
            ``"DC_CURRENT"``, ``"AC_CURRENT"``, ``"RESISTANCE"``,
            ``"TWO_WIRE_RES"``, ``"FOUR_WIRE_RES"``, ``"FREQ"``,
            ``"PERIOD"``, ``"TEMPERATURE"``, ``"DIODE"``, etc.
        range_val : float
            Measurement range (e.g. 10.0 for 10V range).  Use -1 for auto-range.
        resolution : float
            Resolution in digits (e.g. 6.5).
        """
        self._check_connected()
        import nidmm  # type: ignore[import-untyped]
        self._session.configure_measurement_digits(
            measurement_function=nidmm.Function[function],
            range=range_val,
            resolution_digits=resolution,
        )
        return f"OK function={function} range={range_val} resolution={resolution}"

    def set_function(self, function: str = "DC_VOLTS") -> str:
        """Set the measurement function.

        Parameters
        ----------
        function : str
            Measurement function (see ``configure_measurement``).
        """
        self._check_connected()
        import nidmm  # type: ignore[import-untyped]
        self._session.function = nidmm.Function[function]
        return f"OK function={function}"

    def get_function(self) -> str:
        """Read the current measurement function name."""
        self._check_connected()
        return str(self._session.function.name)

    def set_range(self, range_val: float = 10.0) -> str:
        """Set the measurement range.

        Parameters
        ----------
        range_val : float
            Measurement range.  Use -1 for auto-range.
        """
        self._check_connected()
        self._session.range = range_val
        return f"OK range={range_val}"

    def get_range(self) -> float:
        """Read the current measurement range."""
        self._check_connected()
        return float(self._session.range)

    def set_resolution(self, digits: float = 6.5) -> str:
        """Set the resolution in digits.

        Parameters
        ----------
        digits : float
            Resolution in digits (e.g. 4.5, 5.5, 6.5).
        """
        self._check_connected()
        self._session.resolution_digits = digits
        return f"OK resolution={digits}"

    def get_resolution(self) -> float:
        """Read the current resolution in digits."""
        self._check_connected()
        return float(self._session.resolution_digits)

    # -- acquisition ---------------------------------------------------------

    def read(self) -> float:
        """Initiate measurement and read a single value.

        Returns
        -------
        float
            Measured value in the unit of the current function.
        """
        self._check_connected()
        return float(self._session.read())

    def fetch(self, max_time: float = 5.0) -> float:
        """Fetch a measurement from a previously initiated acquisition.

        Parameters
        ----------
        max_time : float
            Maximum time to wait in seconds.

        Returns
        -------
        float
            Measured value.
        """
        self._check_connected()
        return float(self._session.fetch(max_time=max_time))

    def start(self) -> str:
        """Initiate a measurement acquisition."""
        self._check_connected()
        self._session.initiate()
        return "OK measurement started"

    def abort(self) -> str:
        """Abort the current measurement."""
        self._check_connected()
        self._session.abort()
        return "OK measurement aborted"

    # -- helpers -------------------------------------------------------------

    def _check_connected(self) -> None:
        if not self._connected or self._session is None:
            raise RuntimeError(
                "NI-DMM is not connected. Call connect() first."
            )
