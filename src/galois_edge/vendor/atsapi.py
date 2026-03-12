"""Minimal ctypes binding for AlazarTech ATS-SDK.

Requires ATSApi.dll (Windows) or libATSApi.so (Linux) installed via the
vendor SDK.  This module vendors just enough of the atsapi interface for
the galois-edge AlazarTech wrapper to function.  The C shared library is
loaded **lazily** -- on the first ``Board()`` instantiation, not at import
time -- so importing this module always succeeds even when the SDK is not
installed.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import platform
import sys
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Constants (values match the official ATS-SDK headers)
# ---------------------------------------------------------------------------

# Channel identifiers
CHANNEL_A: int = 1
CHANNEL_B: int = 2

# Clock sources
INTERNAL_CLOCK: int = 1

# Clock edges
CLOCK_EDGE_RISING: int = 0

# Sample rates
SAMPLE_RATE_1GSPS: int = 0x0000001A
SAMPLE_RATE_500MSPS: int = 0x00000019
SAMPLE_RATE_250MSPS: int = 0x00000018
SAMPLE_RATE_100MSPS: int = 0x00000017

# Input ranges
INPUT_RANGE_PM_400_MV: int = 0x07

# Coupling
DC_COUPLING: int = 2

# Impedance
IMPEDANCE_50_OHM: int = 2

# Trigger sources
TRIG_EXTERNAL: int = 2
TRIG_CHAN_A: int = 0
TRIG_DISABLE: int = 5

# Trigger engine
TRIG_ENGINE_OP_J: int = 0
TRIG_ENGINE_J: int = 0
TRIG_ENGINE_K: int = 1

# Trigger slope
TRIGGER_SLOPE_POSITIVE: int = 1

# Board model identifiers
ATS9870: int = 13
ATS9373: int = 25
ATS9360: int = 24

# ---------------------------------------------------------------------------
# Lazy library loader
# ---------------------------------------------------------------------------

_lib: Optional[ctypes.CDLL] = None


def _load_lib() -> ctypes.CDLL:
    """Load the AlazarTech C shared library for the current platform.

    Raises ``OSError`` with a descriptive message if the library cannot be
    found.
    """
    global _lib
    if _lib is not None:
        return _lib

    system = platform.system()
    if system == "Windows":
        lib_name = "ATSApi.dll"
    elif system == "Linux":
        lib_name = "libATSApi.so"
    elif system == "Darwin":
        # Not officially supported, but allow for development
        lib_name = "libATSApi.dylib"
    else:
        raise OSError(
            f"Unsupported platform '{system}' for AlazarTech ATS-SDK."
        )

    # Try ctypes.util.find_library first (honours LD_LIBRARY_PATH etc.)
    found = ctypes.util.find_library("ATSApi")
    if found:
        try:
            _lib = ctypes.CDLL(found)
            return _lib
        except OSError:
            pass  # fall through to direct attempt

    # Direct load attempt
    try:
        _lib = ctypes.CDLL(lib_name)
        return _lib
    except OSError:
        raise OSError(
            f"AlazarTech ATS-SDK shared library '{lib_name}' not found. "
            f"Install the ATS-SDK from https://www.alazartech.com/ and "
            f"ensure {lib_name} is on the library search path."
        )


# ---------------------------------------------------------------------------
# Return-code checker
# ---------------------------------------------------------------------------

_API_SUCCESS = 512  # ApiSuccess in the SDK


def _check(return_code: int, func_name: str) -> None:
    """Raise on non-success return codes from ATS API functions."""
    if return_code != _API_SUCCESS:
        raise RuntimeError(
            f"AlazarTech API call {func_name} failed with code {return_code}"
        )


# ---------------------------------------------------------------------------
# Board class
# ---------------------------------------------------------------------------

class Board:
    """Handle to a single AlazarTech digitizer board."""

    def __init__(self, systemId: int = 1, boardId: int = 1) -> None:
        lib = _load_lib()

        lib.AlazarGetBoardBySystemID.restype = ctypes.c_void_p
        lib.AlazarGetBoardBySystemID.argtypes = [ctypes.c_uint32, ctypes.c_uint32]

        self._handle = lib.AlazarGetBoardBySystemID(
            ctypes.c_uint32(systemId),
            ctypes.c_uint32(boardId),
        )
        if not self._handle:
            raise RuntimeError(
                f"AlazarGetBoardBySystemID({systemId}, {boardId}) returned NULL. "
                "Check that the board is installed and powered on."
            )
        self._lib = lib

    # -- Clock configuration ------------------------------------------------

    def setCaptureClock(
        self, source: int, rate: int, edge: int, decimation: int
    ) -> None:
        fn = self._lib.AlazarSetCaptureClock
        fn.restype = ctypes.c_uint32
        fn.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
        ]
        rc = fn(self._handle, source, rate, edge, decimation)
        _check(rc, "AlazarSetCaptureClock")

    # -- Input configuration ------------------------------------------------

    def inputControlEx(
        self, channel: int, coupling: int, inputRange: int, impedance: int
    ) -> None:
        fn = self._lib.AlazarInputControlEx
        fn.restype = ctypes.c_uint32
        fn.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
        ]
        rc = fn(self._handle, channel, coupling, inputRange, impedance)
        _check(rc, "AlazarInputControlEx")

    # -- Trigger configuration ----------------------------------------------

    def setTriggerOperation(
        self,
        operation: int,
        engine1: int,
        source1: int,
        slope1: int,
        level1: int,
        engine2: int,
        source2: int,
        slope2: int,
        level2: int,
    ) -> None:
        fn = self._lib.AlazarSetTriggerOperation
        fn.restype = ctypes.c_uint32
        fn.argtypes = [
            ctypes.c_void_p,
        ] + [ctypes.c_uint32] * 9
        rc = fn(
            self._handle,
            operation, engine1, source1, slope1, level1,
            engine2, source2, slope2, level2,
        )
        _check(rc, "AlazarSetTriggerOperation")

    # -- Record configuration -----------------------------------------------

    def setRecordSize(self, preTriggerSamples: int, postTriggerSamples: int) -> None:
        fn = self._lib.AlazarSetRecordSize
        fn.restype = ctypes.c_uint32
        fn.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32]
        rc = fn(self._handle, preTriggerSamples, postTriggerSamples)
        _check(rc, "AlazarSetRecordSize")

    def setRecordCount(self, count: int) -> None:
        fn = self._lib.AlazarSetRecordCount
        fn.restype = ctypes.c_uint32
        fn.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        rc = fn(self._handle, count)
        _check(rc, "AlazarSetRecordCount")

    # -- Capture control ----------------------------------------------------

    def startCapture(self) -> None:
        fn = self._lib.AlazarStartCapture
        fn.restype = ctypes.c_uint32
        fn.argtypes = [ctypes.c_void_p]
        rc = fn(self._handle)
        _check(rc, "AlazarStartCapture")

    def abortCapture(self) -> None:
        fn = self._lib.AlazarAbortCapture
        fn.restype = ctypes.c_uint32
        fn.argtypes = [ctypes.c_void_p]
        rc = fn(self._handle)
        _check(rc, "AlazarAbortCapture")

    def busy(self) -> int:
        """Return 0 if the board is idle, non-zero if busy."""
        fn = self._lib.AlazarBusy
        fn.restype = ctypes.c_uint32
        fn.argtypes = [ctypes.c_void_p]
        return fn(self._handle)

    # -- Data readout -------------------------------------------------------

    def read(
        self,
        channel: int,
        buffer: Any,
        bytesToCopy: int,
        record: int,
        transferOffset: int,
    ) -> None:
        fn = self._lib.AlazarRead
        fn.restype = ctypes.c_uint32
        fn.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_long,
            ctypes.c_int64,
        ]
        rc = fn(self._handle, channel, buffer, bytesToCopy, record, transferOffset)
        _check(rc, "AlazarRead")

    # -- Board info ---------------------------------------------------------

    def getBoardKind(self) -> int:
        """Return the board kind identifier (e.g. ATS9870)."""
        fn = self._lib.AlazarGetBoardKind
        fn.restype = ctypes.c_uint32
        fn.argtypes = [ctypes.c_void_p]
        return fn(self._handle)

    def getBoardSerialNumber(self) -> int:
        """Return the board serial number."""
        # AlazarQueryCapability with capability = 0x10000024 (GET_SERIAL_NUMBER)
        fn = self._lib.AlazarQueryCapability
        fn.restype = ctypes.c_uint32
        fn.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
        value = ctypes.c_uint32(0)
        _GET_SERIAL_NUMBER = 0x10000024
        rc = fn(self._handle, _GET_SERIAL_NUMBER, 0, ctypes.byref(value))
        _check(rc, "AlazarQueryCapability(GET_SERIAL_NUMBER)")
        return value.value
