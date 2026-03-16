"""NI-DCPower wrapper — thin abstraction over the ``nidcpower`` Python package.

Supports NI PXI source measure units (e.g. PXIe-4141, PXIe-4145).
The ``nidcpower`` package is part of NI's nimi-python project and supports
both local (NI-DCPower driver on Windows) and remote (NI gRPC Device Server)
operation via ``GrpcSessionOptions``.

Typical usage through the SDK executor:
  1. ``connect()`` — open a session (local or gRPC)
  2. ``set_voltage()`` / ``set_current_limit()`` — configure output
  3. ``start()`` — enable output
  4. ``measure_voltage()`` / ``measure_current()`` — take measurements
  5. ``disconnect()`` — close the session
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class NiDCPowerClient:
    """Wraps ``nidcpower`` for NI PXI SMU / power supply operations.

    Parameters
    ----------
    resource : str
        NI-DCPower resource name, e.g. ``"PXI1Slot4"``.
    channels : str
        Channel string, e.g. ``"0"`` or ``"0,1"``.  Empty string for all.
    grpc_address : str or None
        If set, connect via NI gRPC Device Server at this address
        (e.g. ``"192.168.1.100:31763"``).  If ``None``, use local drivers.
    """

    def __init__(
        self,
        resource: str = "PXI1Slot4",
        channels: str = "",
        grpc_address: Optional[str] = None,
    ) -> None:
        self._resource = resource
        self._channels = channels
        self._grpc_address = grpc_address
        self._session: Any = None
        self._connected = False

    # -- lifecycle -----------------------------------------------------------

    def connect(self) -> None:
        """Open a session to the SMU / power supply."""
        try:
            import nidcpower  # type: ignore[import-untyped]
        except (ImportError, OSError):
            raise ImportError(
                "nidcpower package not found. Install with: pip install nidcpower. "
                "Also requires the NI-DCPower runtime driver on Windows, or "
                "NI gRPC Device Server for remote access from Linux. "
                "See: https://github.com/ni/grpc-device"
            )
        if self._grpc_address:
            import grpc
            channel = grpc.insecure_channel(self._grpc_address)
            grpc_options = nidcpower.GrpcSessionOptions(channel, session_name="")
            self._session = nidcpower.Session(
                self._resource, channels=self._channels, grpc_options=grpc_options,
            )
        else:
            self._session = nidcpower.Session(self._resource, channels=self._channels)
        self._connected = True
        logger.info(
            "NI-DCPower connected: %s channels=%s (gRPC: %s)",
            self._resource, self._channels or "all", self._grpc_address or "local",
        )

    def disconnect(self) -> None:
        """Close the NI-DCPower session."""
        if self._session is not None:
            try:
                self._session.close()
            except Exception as exc:
                logger.warning("Error closing NI-DCPower session: %s", exc)
        self._session = None
        self._connected = False
        logger.info("NI-DCPower disconnected: %s", self._resource)

    def get_identity(self) -> str:
        """Return an IDN-style identity string."""
        self._check_connected()
        try:
            model = self._session.instrument_model
            serial = self._session.serial_number
            fw = self._session.instrument_firmware_revision
            return f"NI,{model},{serial},{fw}"
        except Exception:
            return f"NI,DCPower,{self._resource},1.0"

    # -- voltage / current configuration -------------------------------------

    def set_voltage(self, channel: str = "0", voltage: float = 0.0) -> str:
        """Set output voltage level on a channel.

        Parameters
        ----------
        channel : str
            Channel name.
        voltage : float
            Voltage level in volts.
        """
        self._check_connected()
        self._session.channels[channel].voltage_level = voltage
        return f"OK channel={channel} voltage={voltage}V"

    def get_voltage(self, channel: str = "0") -> float:
        """Read configured voltage level for a channel in volts."""
        self._check_connected()
        return float(self._session.channels[channel].voltage_level)

    def set_current_limit(self, channel: str = "0", current: float = 0.01) -> str:
        """Set current limit on a channel.

        Parameters
        ----------
        channel : str
            Channel name.
        current : float
            Current limit in amps.
        """
        self._check_connected()
        self._session.channels[channel].current_limit = current
        return f"OK channel={channel} current_limit={current}A"

    def get_current_limit(self, channel: str = "0") -> float:
        """Read configured current limit for a channel in amps."""
        self._check_connected()
        return float(self._session.channels[channel].current_limit)

    def set_output_function(self, channel: str = "0", function: str = "DC_VOLTAGE") -> str:
        """Set the output function (voltage or current source mode).

        Parameters
        ----------
        channel : str
            Channel name.
        function : str
            Output function: ``"DC_VOLTAGE"`` or ``"DC_CURRENT"``.
        """
        self._check_connected()
        import nidcpower  # type: ignore[import-untyped]
        self._session.channels[channel].output_function = nidcpower.OutputFunction[function]
        return f"OK channel={channel} output_function={function}"

    def set_source_mode(self, mode: str = "SINGLE_POINT") -> str:
        """Set the source mode for the session.

        Parameters
        ----------
        mode : str
            Source mode: ``"SINGLE_POINT"`` or ``"SEQUENCE"``.
        """
        self._check_connected()
        import nidcpower  # type: ignore[import-untyped]
        self._session.source_mode = nidcpower.SourceMode[mode]
        return f"OK source_mode={mode}"

    # -- measurements --------------------------------------------------------

    def measure(self, channel: str = "0", measurement_type: str = "VOLTAGE") -> float:
        """Take a measurement on a channel.

        Parameters
        ----------
        channel : str
            Channel name.
        measurement_type : str
            Measurement type: ``"VOLTAGE"`` or ``"CURRENT"``.

        Returns
        -------
        float
            Measured value.
        """
        self._check_connected()
        import nidcpower  # type: ignore[import-untyped]
        return float(
            self._session.channels[channel].measure(
                nidcpower.MeasurementTypes[measurement_type],
            )
        )

    def measure_voltage(self, channel: str = "0") -> float:
        """Measure voltage on a channel in volts."""
        return self.measure(channel=channel, measurement_type="VOLTAGE")

    def measure_current(self, channel: str = "0") -> float:
        """Measure current on a channel in amps."""
        return self.measure(channel=channel, measurement_type="CURRENT")

    # -- output control ------------------------------------------------------

    def start(self, channel: str = "0") -> str:
        """Initiate output on a channel."""
        self._check_connected()
        self._session.channels[channel].initiate()
        return f"OK channel={channel} output started"

    def abort(self, channel: str = "0") -> str:
        """Abort output on a channel."""
        self._check_connected()
        self._session.channels[channel].abort()
        return f"OK channel={channel} output aborted"

    def commit(self) -> str:
        """Commit session configuration changes."""
        self._check_connected()
        self._session.commit()
        return "OK committed"

    # -- helpers -------------------------------------------------------------

    def _check_connected(self) -> None:
        if not self._connected or self._session is None:
            raise RuntimeError(
                "NI-DCPower is not connected. Call connect() first."
            )
