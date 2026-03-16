"""NI-FGEN wrapper — thin abstraction over the ``nifgen`` Python package.

Supports NI PXI function generators (e.g. PXIe-5433, PXIe-5442).
The ``nifgen`` package is part of NI's nimi-python project and supports
both local (NI-FGEN driver on Windows) and remote (NI gRPC Device Server)
operation via ``GrpcSessionOptions``.

Typical usage through the SDK executor:
  1. ``connect()`` — open a session (local or gRPC)
  2. ``configure_standard_waveform()`` — set up output waveform
  3. ``start()`` — begin generation
  4. ``disconnect()`` — close the session
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class NiFgenClient:
    """Wraps ``nifgen`` for NI PXI function generator operations.

    Parameters
    ----------
    resource : str
        NI-FGEN resource name, e.g. ``"PXI1Slot3"``.
    grpc_address : str or None
        If set, connect via NI gRPC Device Server at this address
        (e.g. ``"192.168.1.100:31763"``).  If ``None``, use local drivers.
    """

    def __init__(self, resource: str = "PXI1Slot3", grpc_address: Optional[str] = None) -> None:
        self._resource = resource
        self._grpc_address = grpc_address
        self._session: Any = None
        self._connected = False

    # -- lifecycle -----------------------------------------------------------

    def connect(self) -> None:
        """Open a session to the function generator."""
        try:
            import nifgen  # type: ignore[import-untyped]
        except (ImportError, OSError):
            raise ImportError(
                "nifgen package not found. Install with: pip install nifgen. "
                "Also requires the NI-FGEN runtime driver on Windows, or "
                "NI gRPC Device Server for remote access from Linux. "
                "See: https://github.com/ni/grpc-device"
            )
        if self._grpc_address:
            import grpc
            channel = grpc.insecure_channel(self._grpc_address)
            grpc_options = nifgen.GrpcSessionOptions(channel, session_name="")
            self._session = nifgen.Session(self._resource, grpc_options=grpc_options)
        else:
            self._session = nifgen.Session(self._resource)
        self._connected = True
        logger.info("NI-FGEN connected: %s (gRPC: %s)", self._resource, self._grpc_address or "local")

    def disconnect(self) -> None:
        """Close the NI-FGEN session."""
        if self._session is not None:
            try:
                self._session.close()
            except Exception as exc:
                logger.warning("Error closing NI-FGEN session: %s", exc)
        self._session = None
        self._connected = False
        logger.info("NI-FGEN disconnected: %s", self._resource)

    def get_identity(self) -> str:
        """Return an IDN-style identity string."""
        self._check_connected()
        try:
            model = self._session.instrument_model
            serial = self._session.serial_number
            fw = self._session.instrument_firmware_revision
            return f"NI,{model},{serial},{fw}"
        except Exception:
            return f"NI,FGen,{self._resource},1.0"

    # -- waveform configuration ----------------------------------------------

    def configure_standard_waveform(
        self,
        channel: str = "0",
        waveform: str = "SINE",
        amplitude: float = 1.0,
        frequency: float = 1e6,
        dc_offset: float = 0.0,
    ) -> str:
        """Configure a standard waveform on a channel.

        Parameters
        ----------
        channel : str
            Output channel name.
        waveform : str
            Waveform type: ``"SINE"``, ``"SQUARE"``, ``"TRIANGLE"``,
            ``"RAMP_UP"``, ``"RAMP_DOWN"``, ``"DC"``, ``"NOISE"``, etc.
        amplitude : float
            Peak-to-peak amplitude in volts.
        frequency : float
            Waveform frequency in Hz.
        dc_offset : float
            DC offset in volts.
        """
        self._check_connected()
        import nifgen  # type: ignore[import-untyped]
        self._session.channels[channel].configure_standard_waveform(
            waveform=nifgen.Waveform[waveform],
            amplitude=amplitude,
            frequency=frequency,
            dc_offset=dc_offset,
        )
        return f"OK channel={channel} waveform={waveform} amp={amplitude}V freq={frequency}Hz"

    def set_amplitude(self, channel: str = "0", amplitude: float = 1.0) -> str:
        """Set output amplitude for a channel.

        Parameters
        ----------
        channel : str
            Output channel name.
        amplitude : float
            Peak-to-peak amplitude in volts.
        """
        self._check_connected()
        self._session.channels[channel].func_amplitude = amplitude
        return f"OK channel={channel} amplitude={amplitude}V"

    def get_amplitude(self, channel: str = "0") -> float:
        """Read output amplitude for a channel in volts."""
        self._check_connected()
        return float(self._session.channels[channel].func_amplitude)

    def set_frequency(self, channel: str = "0", frequency: float = 1e6) -> str:
        """Set output frequency for a channel.

        Parameters
        ----------
        channel : str
            Output channel name.
        frequency : float
            Output frequency in Hz.
        """
        self._check_connected()
        self._session.channels[channel].func_frequency = frequency
        return f"OK channel={channel} frequency={frequency}Hz"

    def get_frequency(self, channel: str = "0") -> float:
        """Read output frequency for a channel in Hz."""
        self._check_connected()
        return float(self._session.channels[channel].func_frequency)

    def set_dc_offset(self, channel: str = "0", offset: float = 0.0) -> str:
        """Set DC offset for a channel.

        Parameters
        ----------
        channel : str
            Output channel name.
        offset : float
            DC offset in volts.
        """
        self._check_connected()
        self._session.channels[channel].func_dc_offset = offset
        return f"OK channel={channel} dc_offset={offset}V"

    def set_waveform_type(self, channel: str = "0", waveform: str = "SINE") -> str:
        """Set waveform type for a channel.

        Parameters
        ----------
        channel : str
            Output channel name.
        waveform : str
            Waveform type: ``"SINE"``, ``"SQUARE"``, ``"TRIANGLE"``, etc.
        """
        self._check_connected()
        import nifgen  # type: ignore[import-untyped]
        self._session.channels[channel].func_waveform = nifgen.Waveform[waveform]
        return f"OK channel={channel} waveform={waveform}"

    # -- output control ------------------------------------------------------

    def set_output_enabled(self, channel: str = "0", enabled: bool = True) -> str:
        """Enable or disable output on a channel.

        Parameters
        ----------
        channel : str
            Output channel name.
        enabled : bool
            True to enable, False to disable.
        """
        self._check_connected()
        self._session.channels[channel].output_enabled = enabled
        return f"OK channel={channel} output={'enabled' if enabled else 'disabled'}"

    def start(self) -> str:
        """Initiate waveform generation."""
        self._check_connected()
        self._session.initiate()
        return "OK generation started"

    def abort(self) -> str:
        """Abort waveform generation."""
        self._check_connected()
        self._session.abort()
        return "OK generation aborted"

    # -- helpers -------------------------------------------------------------

    def _check_connected(self) -> None:
        if not self._connected or self._session is None:
            raise RuntimeError(
                "NI-FGEN is not connected. Call connect() first."
            )
