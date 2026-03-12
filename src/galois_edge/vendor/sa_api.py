"""Minimal ctypes binding for the SignalHound SA124B spectrum analyser SDK.

Requires ``sa_api.dll`` / ``bb_api.dll`` (Windows) or ``libsa_api.so`` /
``libbb_api.so`` (Linux) installed via the vendor SDK.  This module vendors
just enough of the sa_api interface for the galois-edge SignalHound wrapper
to function.  The C shared library is loaded **lazily** -- on first use, not
at import time -- so importing this module always succeeds even when the SDK
is not installed.

Environment variable ``SIGNALHOUND_LIB_PATH`` can be set to the directory
(or full path) of the shared library to override the default search.

Download the SDK from: https://signalhound.com/software/
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import platform
from typing import Optional

# ---------------------------------------------------------------------------
# Constants (values match the official SA-Series API headers)
# ---------------------------------------------------------------------------

# Sweep / acquisition modes
SA_SWEEPING: int = 0x0
SA_REAL_TIME: int = 0x1

# Boolean flags
SA_FALSE: int = 0
SA_TRUE: int = 1

# Status codes
SA_OK: int = 0

# ---------------------------------------------------------------------------
# Lazy library loader
# ---------------------------------------------------------------------------

_lib: Optional[ctypes.CDLL] = None


def _load_lib() -> ctypes.CDLL:
    """Load the SignalHound SA-Series C shared library for the current platform.

    Raises ``OSError`` with a descriptive message (including a download link)
    if the library cannot be found.
    """
    global _lib
    if _lib is not None:
        return _lib

    # Allow explicit override via environment variable
    env_path = os.environ.get("SIGNALHOUND_LIB_PATH")
    if env_path:
        # If env_path points to a file, load it directly; if a directory,
        # we'll look for the platform-appropriate name inside it.
        if os.path.isfile(env_path):
            try:
                _lib = ctypes.CDLL(env_path)
                return _lib
            except OSError:
                pass  # fall through
        # treat as directory -- resolved below with lib_names

    system = platform.system()
    if system == "Windows":
        lib_names = ["sa_api.dll", "bb_api.dll"]
    elif system == "Linux":
        lib_names = ["libsa_api.so", "libbb_api.so"]
    elif system == "Darwin":
        # Not officially supported, but allow for development
        lib_names = ["libsa_api.dylib", "libbb_api.dylib"]
    else:
        raise OSError(
            f"Unsupported platform '{system}' for SignalHound SA-Series SDK."
        )

    # If env_path is a directory, prepend it to each candidate name
    if env_path and os.path.isdir(env_path):
        lib_names = [os.path.join(env_path, n) for n in lib_names]

    # Try ctypes.util.find_library first (honours LD_LIBRARY_PATH etc.)
    for search_name in ("sa_api", "bb_api"):
        found = ctypes.util.find_library(search_name)
        if found:
            try:
                _lib = ctypes.CDLL(found)
                return _lib
            except OSError:
                pass

    # Direct load attempts
    for name in lib_names:
        try:
            _lib = ctypes.CDLL(name)
            return _lib
        except OSError:
            continue

    raise OSError(
        f"SignalHound SA-Series shared library not found "
        f"(tried {', '.join(lib_names)}). "
        f"Install the SDK from https://signalhound.com/software/ and ensure "
        f"the library is on the system library search path, or set the "
        f"SIGNALHOUND_LIB_PATH environment variable to its location."
    )


# ---------------------------------------------------------------------------
# Return-code checker
# ---------------------------------------------------------------------------


def _check(status: int, func_name: str) -> None:
    """Raise ``RuntimeError`` on non-success status codes from the SA API."""
    if status != SA_OK:
        raise RuntimeError(
            f"SignalHound API call {func_name} failed with status code {status}"
        )


# ---------------------------------------------------------------------------
# Wrapped API functions
# ---------------------------------------------------------------------------


def sa_open_device() -> int:
    """Open the first available SA-Series device.

    Returns the device handle (an integer).  Raises ``RuntimeError`` if no
    device can be opened.
    """
    lib = _load_lib()

    lib.saOpenDevice.restype = ctypes.c_int  # saStatus
    lib.saOpenDevice.argtypes = [ctypes.POINTER(ctypes.c_int)]

    handle = ctypes.c_int(-1)
    status = lib.saOpenDevice(ctypes.byref(handle))
    _check(status, "saOpenDevice")
    return handle.value


def sa_close_device(handle: int) -> None:
    """Close a previously opened SA device."""
    lib = _load_lib()

    lib.saCloseDevice.restype = ctypes.c_int
    lib.saCloseDevice.argtypes = [ctypes.c_int]

    status = lib.saCloseDevice(handle)
    _check(status, "saCloseDevice")


def sa_config_center_span(handle: int, center: float, span: float) -> None:
    """Configure the center frequency and span for sweep mode."""
    lib = _load_lib()

    lib.saConfigCenterSpan.restype = ctypes.c_int
    lib.saConfigCenterSpan.argtypes = [
        ctypes.c_int, ctypes.c_double, ctypes.c_double,
    ]

    status = lib.saConfigCenterSpan(handle, center, span)
    _check(status, "saConfigCenterSpan")


def sa_config_level(handle: int, ref_level: float) -> None:
    """Configure the reference level (dBm)."""
    lib = _load_lib()

    lib.saConfigLevel.restype = ctypes.c_int
    lib.saConfigLevel.argtypes = [ctypes.c_int, ctypes.c_double]

    status = lib.saConfigLevel(handle, ref_level)
    _check(status, "saConfigLevel")


def sa_config_sweep_coupling(
    handle: int, rbw: float, vbw: float, reject: int
) -> None:
    """Configure RBW, VBW, and image rejection for sweep mode."""
    lib = _load_lib()

    lib.saConfigSweepCoupling.restype = ctypes.c_int
    lib.saConfigSweepCoupling.argtypes = [
        ctypes.c_int, ctypes.c_double, ctypes.c_double, ctypes.c_int,
    ]

    status = lib.saConfigSweepCoupling(handle, rbw, vbw, reject)
    _check(status, "saConfigSweepCoupling")


def sa_initiate(handle: int, mode: int, flag: int) -> None:
    """Initiate the device in the specified mode."""
    lib = _load_lib()

    lib.saInitiate.restype = ctypes.c_int
    lib.saInitiate.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]

    status = lib.saInitiate(handle, mode, flag)
    _check(status, "saInitiate")


def sa_get_sweep_64f(handle: int) -> tuple:
    """Retrieve a sweep as 64-bit float arrays.

    Returns a ``(frequencies, amplitudes)`` tuple of numpy arrays.
    """
    lib = _load_lib()

    # First query the sweep length
    lib.saQuerySweepInfo.restype = ctypes.c_int
    lib.saQuerySweepInfo.argtypes = [
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_int),     # sweep_length
        ctypes.POINTER(ctypes.c_double),  # start_freq
        ctypes.POINTER(ctypes.c_double),  # bin_size
    ]

    sweep_len = ctypes.c_int(0)
    start_freq = ctypes.c_double(0.0)
    bin_size = ctypes.c_double(0.0)

    status = lib.saQuerySweepInfo(
        handle, ctypes.byref(sweep_len),
        ctypes.byref(start_freq), ctypes.byref(bin_size),
    )
    _check(status, "saQuerySweepInfo")

    n = sweep_len.value

    # Allocate output arrays
    min_buf = (ctypes.c_double * n)()
    max_buf = (ctypes.c_double * n)()

    lib.saGetSweep_64f.restype = ctypes.c_int
    lib.saGetSweep_64f.argtypes = [
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_double),  # min (or single sweep)
        ctypes.POINTER(ctypes.c_double),  # max
    ]

    status = lib.saGetSweep_64f(handle, min_buf, max_buf)
    _check(status, "saGetSweep_64f")

    import numpy as np  # deferred — not needed until sweep data is read

    # Build frequency array from start + bin_size
    frequencies = np.array(
        [start_freq.value + i * bin_size.value for i in range(n)]
    )
    # Return max amplitudes (typical for spectrum display)
    amplitudes = np.ctypeslib.as_array(max_buf)

    return (frequencies, amplitudes)


def sa_get_serial_number(handle: int) -> int:
    """Return the serial number of the device."""
    lib = _load_lib()

    lib.saGetSerialNumber.restype = ctypes.c_int
    lib.saGetSerialNumber.argtypes = [
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_int),
    ]

    serial = ctypes.c_int(0)
    status = lib.saGetSerialNumber(handle, ctypes.byref(serial))
    _check(status, "saGetSerialNumber")
    return serial.value


def sa_get_firmware_version(handle: int) -> str:
    """Return the firmware version string of the device."""
    lib = _load_lib()

    lib.saGetFirmwareString.restype = ctypes.c_int
    lib.saGetFirmwareString.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
    ]

    buf = ctypes.create_string_buffer(256)
    status = lib.saGetFirmwareString(handle, buf)
    _check(status, "saGetFirmwareString")
    return buf.value.decode("ascii", errors="replace")
