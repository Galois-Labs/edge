"""NI-SCOPE wrapper — thin abstraction over the ``niscope`` Python package.

Supports NI PXI oscilloscopes/digitizers (e.g. PXIe-5162, PXIe-5164).
The ``niscope`` package is part of NI's nimi-python project and supports
both local (NI-SCOPE driver on Windows) and remote (NI gRPC Device Server)
operation via ``GrpcSessionOptions``.

Typical usage through the SDK executor:
  1. ``connect()`` — open a session (local or gRPC)
  2. ``configure_horizontal()`` / ``configure_vertical()`` — set up acquisition
  3. ``read()`` / ``fetch()`` — acquire waveform data
  4. ``disconnect()`` — close the session
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class NiScopeClient:
    """Wraps ``niscope`` for NI PXI oscilloscope/digitizer operations.

    Parameters
    ----------
    resource : str
        NI-SCOPE resource name, e.g. ``"PXI1Slot2"``.
    grpc_address : str or None
        If set, connect via NI gRPC Device Server at this address
        (e.g. ``"192.168.1.100:31763"``).  If ``None``, use local drivers.
    """

    def __init__(self, resource: str = "PXI1Slot2", grpc_address: Optional[str] = None) -> None:
        self._resource = resource
        self._grpc_address = grpc_address
        self._session: Any = None
        self._connected = False

    # -- lifecycle -----------------------------------------------------------

    def connect(self) -> None:
        """Open a session to the oscilloscope/digitizer."""
        try:
            import niscope  # type: ignore[import-untyped]
        except (ImportError, OSError):
            raise ImportError(
                "niscope package not found. Install with: pip install niscope. "
                "Also requires the NI-SCOPE runtime driver on Windows, or "
                "NI gRPC Device Server for remote access from Linux. "
                "See: https://github.com/ni/grpc-device"
            )
        if self._grpc_address:
            import grpc
            channel = grpc.insecure_channel(self._grpc_address)
            grpc_options = niscope.GrpcSessionOptions(channel, session_name="")
            self._session = niscope.Session(self._resource, grpc_options=grpc_options)
        else:
            self._session = niscope.Session(self._resource)
        self._connected = True
        logger.info("NI-SCOPE connected: %s (gRPC: %s)", self._resource, self._grpc_address or "local")

    def disconnect(self) -> None:
        """Close the NI-SCOPE session."""
        if self._session is not None:
            try:
                self._session.close()
            except Exception as exc:
                logger.warning("Error closing NI-SCOPE session: %s", exc)
        self._session = None
        self._connected = False
        logger.info("NI-SCOPE disconnected: %s", self._resource)

    def get_identity(self) -> str:
        """Return an IDN-style identity string."""
        self._check_connected()
        try:
            model = self._session.instrument_model
            serial = self._session.serial_number
            fw = self._session.instrument_firmware_revision
            return f"NI,{model},{serial},{fw}"
        except Exception:
            return f"NI,Scope,{self._resource},1.0"

    # -- horizontal configuration -------------------------------------------

    def configure_horizontal(
        self,
        sample_rate: float = 1e9,
        record_length: int = 1000,
        num_records: int = 1,
    ) -> str:
        """Configure horizontal timing (sample rate, record length).

        Parameters
        ----------
        sample_rate : float
            Sample rate in Sa/s.
        record_length : int
            Number of samples per record.
        num_records : int
            Number of records to acquire.
        """
        self._check_connected()
        self._session.configure_horizontal_timing(
            min_sample_rate=sample_rate,
            min_num_pts=record_length,
            ref_position=50.0,
            num_records=num_records,
            enforce_realtime=True,
        )
        return f"OK sample_rate={sample_rate} record_length={record_length} num_records={num_records}"

    # -- vertical configuration ---------------------------------------------

    def configure_vertical(
        self,
        channel: str = "0",
        range_v: float = 10.0,
        coupling: str = "DC",
        offset: float = 0.0,
        enabled: bool = True,
    ) -> str:
        """Configure vertical settings for a channel.

        Parameters
        ----------
        channel : str
            Channel name, e.g. ``"0"`` or ``"0,1"``.
        range_v : float
            Vertical range in volts (peak-to-peak).
        coupling : str
            Coupling mode: ``"DC"``, ``"AC"``, or ``"GND"``.
        offset : float
            Vertical offset in volts.
        enabled : bool
            Whether the channel is enabled.
        """
        self._check_connected()
        import niscope  # type: ignore[import-untyped]
        self._session.channels[channel].configure_vertical(
            range=range_v,
            coupling=niscope.VerticalCoupling[coupling],
            offset=offset,
            enabled=enabled,
        )
        return f"OK channel={channel} range={range_v}V coupling={coupling}"

    # -- acquisition ---------------------------------------------------------

    def read(self, channel: str = "0", num_samples: int = 1000, timeout: float = 5.0) -> str:
        """Initiate acquisition and read waveform data.

        Parameters
        ----------
        channel : str
            Channel to read from.
        num_samples : int
            Number of samples to read.
        timeout : float
            Timeout in seconds.

        Returns
        -------
        str
            JSON-encoded list of voltage samples.
        """
        self._check_connected()
        waveforms = self._session.channels[channel].read(
            num_samples=num_samples,
            timeout=timeout,
        )
        # niscope returns list of WaveformInfo; extract samples from first
        if isinstance(waveforms, list) and len(waveforms) > 0:
            samples = [float(x) for x in waveforms[0].samples]
        else:
            samples = [float(x) for x in waveforms.samples]
        return json.dumps(samples)

    def fetch(self, channel: str = "0", num_samples: int = 1000, timeout: float = 5.0) -> str:
        """Fetch waveform data from a previously initiated acquisition.

        Parameters
        ----------
        channel : str
            Channel to fetch from.
        num_samples : int
            Number of samples to fetch.
        timeout : float
            Timeout in seconds.

        Returns
        -------
        str
            JSON-encoded list of voltage samples.
        """
        self._check_connected()
        waveforms = self._session.channels[channel].fetch(
            num_samples=num_samples,
            timeout=timeout,
        )
        if isinstance(waveforms, list) and len(waveforms) > 0:
            samples = [float(x) for x in waveforms[0].samples]
        else:
            samples = [float(x) for x in waveforms.samples]
        return json.dumps(samples)

    def get_sample_rate(self) -> float:
        """Read the actual horizontal sample rate in Sa/s."""
        self._check_connected()
        return float(self._session.horz_sample_rate)

    def get_record_length(self) -> int:
        """Read the actual record length in samples."""
        self._check_connected()
        return int(self._session.horz_record_length)

    # -- trigger -------------------------------------------------------------

    def set_trigger(self, source: str = "0", level: float = 0.0, slope: str = "POSITIVE") -> str:
        """Configure edge trigger.

        Parameters
        ----------
        source : str
            Trigger source channel.
        level : float
            Trigger level in volts.
        slope : str
            Trigger slope: ``"POSITIVE"`` or ``"NEGATIVE"``.
        """
        self._check_connected()
        import niscope  # type: ignore[import-untyped]
        self._session.configure_trigger_edge(
            trigger_source=source,
            level=level,
            slope=niscope.TriggerSlope[slope],
        )
        return f"OK trigger source={source} level={level}V slope={slope}"

    # -- control -------------------------------------------------------------

    def start(self) -> str:
        """Initiate acquisition."""
        self._check_connected()
        self._session.initiate()
        return "OK acquisition started"

    def abort(self) -> str:
        """Abort acquisition."""
        self._check_connected()
        self._session.abort()
        return "OK acquisition aborted"

    # -- helpers -------------------------------------------------------------

    def _check_connected(self) -> None:
        if not self._connected or self._session is None:
            raise RuntimeError(
                "NI-SCOPE is not connected. Call connect() first."
            )
