"""AlazarTech Digitizer wrapper — PCIe high-speed digitizers.

Supports ATS9870, ATS9373, ATS9360 and similar boards that use the
AlazarTech ``atsapi`` C SDK with its Python ctypes binding.

API overview::

    board = ats.Board(systemId, boardId)
    board.setCaptureClock(...)
    board.inputControlEx(...)
    board.setTriggerOperation(...)
    board.startCapture()
    board.read(...)

The wrapper presents a simplified interface suitable for the galois-edge
SDK executor: connect / disconnect / identity / configure / acquire / get_data.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class AlazarTechClient:
    """Wraps the AlazarTech atsapi Python bindings."""

    def __init__(self, board_id: int = 1, system_id: int = 1) -> None:
        self._board_id = board_id
        self._system_id = system_id
        self._board: Any = None
        self._ats: Any = None  # atsapi module reference
        self._last_data: Optional[Dict[str, List[float]]] = None

    # -- lifecycle -----------------------------------------------------------

    def connect(self) -> None:
        """Initialise the board handle via atsapi."""
        try:
            from galois_edge.vendor import atsapi as ats
        except (ImportError, OSError) as exc:
            raise RuntimeError(
                f"AlazarTech SDK not available: {exc}. "
                "Install the ATS-SDK from https://www.alazartech.com/Support/Download%%20Files/ "
                "to get the ATSApi shared library (ATSApi.dll / libATSApi.so)."
            ) from exc

        self._ats = ats
        self._board = ats.Board(self._system_id, self._board_id)
        logger.info(
            "AlazarTech board connected: system=%d board=%d",
            self._system_id, self._board_id,
        )

    def disconnect(self) -> None:
        """Release the board handle."""
        self._board = None
        self._last_data = None
        logger.info("AlazarTech board disconnected")

    def get_identity(self) -> str:
        """Return board model and serial number."""
        if self._board is None:
            return "AlazarTech,Unknown,N/A,N/A"
        try:
            ats = self._ats
            board_kind = self._board.getBoardKind()
            # Map board kind enum to a human-readable name
            name_map = {
                getattr(ats, "ATS9870", None): "ATS9870",
                getattr(ats, "ATS9373", None): "ATS9373",
                getattr(ats, "ATS9360", None): "ATS9360",
            }
            model = name_map.get(board_kind, f"ATS-{board_kind}")
            serial = str(self._board.getBoardSerialNumber() if hasattr(self._board, "getBoardSerialNumber") else "N/A")
            return f"AlazarTech,{model},{serial},1.0"
        except Exception as exc:
            logger.warning("Identity query failed: %s", exc)
            return "AlazarTech,Unknown,N/A,N/A"

    # -- configuration -------------------------------------------------------

    def configure(
        self,
        sample_rate: int = 1000000000,
        channel_mask: int = 3,
        trigger_source: str = "external",
    ) -> str:
        """Configure clock, input channels, and trigger.

        Parameters
        ----------
        sample_rate : int
            Samples per second (e.g. 1_000_000_000 for 1 GS/s).
        channel_mask : int
            Bitmask of enabled channels (1=ChA, 2=ChB, 3=both).
        trigger_source : str
            "external" or "channel_a".
        """
        if self._board is None:
            raise RuntimeError("Board not connected")

        ats = self._ats

        # Clock configuration — internal clock, sample rate
        rate_map = {
            1000000000: getattr(ats, "SAMPLE_RATE_1GSPS", 0x0000001A),
            500000000: getattr(ats, "SAMPLE_RATE_500MSPS", 0x00000019),
            250000000: getattr(ats, "SAMPLE_RATE_250MSPS", 0x00000018),
            100000000: getattr(ats, "SAMPLE_RATE_100MSPS", 0x00000017),
        }
        rate_id = rate_map.get(sample_rate, getattr(ats, "SAMPLE_RATE_1GSPS", 0x0000001A))

        self._board.setCaptureClock(
            getattr(ats, "INTERNAL_CLOCK", 1),
            rate_id,
            getattr(ats, "CLOCK_EDGE_RISING", 0),
            0,  # decimation = 0
        )

        # Input range for each enabled channel
        input_range = getattr(ats, "INPUT_RANGE_PM_400_MV", 0x07)
        coupling = getattr(ats, "DC_COUPLING", 2)
        impedance = getattr(ats, "IMPEDANCE_50_OHM", 2)

        if channel_mask & 1:
            self._board.inputControlEx(
                getattr(ats, "CHANNEL_A", 1),
                coupling, input_range, impedance,
            )
        if channel_mask & 2:
            self._board.inputControlEx(
                getattr(ats, "CHANNEL_B", 2),
                coupling, input_range, impedance,
            )

        # Trigger
        if trigger_source == "external":
            trig_src = getattr(ats, "TRIG_EXTERNAL", 2)
        else:
            trig_src = getattr(ats, "TRIG_CHAN_A", 0)

        self._board.setTriggerOperation(
            getattr(ats, "TRIG_ENGINE_OP_J", 0),
            getattr(ats, "TRIG_ENGINE_J", 0),
            trig_src,
            getattr(ats, "TRIGGER_SLOPE_POSITIVE", 1),
            128,  # trigger level (midpoint for 8-bit)
            getattr(ats, "TRIG_ENGINE_K", 1),
            getattr(ats, "TRIG_DISABLE", 5),
            getattr(ats, "TRIGGER_SLOPE_POSITIVE", 1),
            128,
        )

        logger.info(
            "Board configured: rate=%d, channels=0x%x, trigger=%s",
            sample_rate, channel_mask, trigger_source,
        )
        return "OK"

    # -- acquisition ---------------------------------------------------------

    def acquire(self, samples_per_record: int = 1024, records: int = 1) -> str:
        """Start acquisition and wait for completion.

        Parameters
        ----------
        samples_per_record : int
            Number of samples per record (must be a multiple of 64).
        records : int
            Number of records to acquire.
        """
        if self._board is None:
            raise RuntimeError("Board not connected")

        ats = self._ats

        # Ensure sample count is aligned (AlazarTech requires multiples of 64)
        aligned = ((samples_per_record + 63) // 64) * 64

        self._board.setRecordSize(0, aligned)
        self._board.setRecordCount(records)
        self._board.startCapture()

        # Poll until acquisition completes
        import time
        timeout_s = 10.0
        start = time.monotonic()
        while not self._board.busy() == 0:
            if time.monotonic() - start > timeout_s:
                self._board.abortCapture()
                raise TimeoutError("Acquisition timed out")
            time.sleep(0.001)

        logger.info(
            "Acquisition complete: %d samples x %d records",
            aligned, records,
        )
        # Store metadata for get_data
        self._last_acquire_samples = aligned
        self._last_acquire_records = records
        return "OK"

    def get_data(self, channel: str = "A") -> str:
        """Read acquired data from the specified channel.

        Returns JSON-encoded list of float values (normalised to volts).
        """
        if self._board is None:
            raise RuntimeError("Board not connected")

        ats = self._ats
        import numpy as np  # type: ignore[import-untyped]

        ch_map = {
            "A": getattr(ats, "CHANNEL_A", 1),
            "B": getattr(ats, "CHANNEL_B", 2),
        }
        ch_id = ch_map.get(channel.upper(), getattr(ats, "CHANNEL_A", 1))

        samples = getattr(self, "_last_acquire_samples", 1024)
        records = getattr(self, "_last_acquire_records", 1)

        # Allocate buffer and read
        buf = np.zeros(samples * records, dtype=np.uint8)
        self._board.read(
            ch_id,
            buf.ctypes.data,
            len(buf),
            1,  # first record
            0,  # start sample
        )

        # Convert 8-bit unsigned to volts (assuming +/- 400 mV range)
        volts = (buf.astype(float) - 128.0) / 128.0 * 0.4
        return json.dumps(volts.tolist())

    # -- status --------------------------------------------------------------

    def get_status(self) -> str:
        """Return board busy/idle status."""
        if self._board is None:
            return "disconnected"
        try:
            return "idle" if self._board.busy() == 0 else "busy"
        except Exception:
            return "unknown"
