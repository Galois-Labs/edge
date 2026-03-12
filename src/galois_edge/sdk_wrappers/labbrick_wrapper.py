"""
SDK wrapper for Vaunix Lab Brick instruments via vendor shared libraries (ctypes).

Supports:
- Lab Brick LMS Signal Synthesizer (``vnx_fsynth``)
- Lab Brick LSG Signal Generator   (``vnx_fsynth``)
- Lab Brick Digital Attenuator      (``vnx_atten``)

These are USB-connected RF/microwave instruments controlled through Vaunix's
vendor shared libraries (.dll on Windows, .so on Linux, .dylib on macOS).

Typical usage via the SDK executor::

    wrapper = LabBrickSynthesizer(serial_number=12345)
    wrapper.connect()
    wrapper.set_frequency(5_000_000_000)   # 5 GHz
    wrapper.set_power(-10.0)               # -10 dBm
    wrapper.set_rf_on(True)
    wrapper.disconnect()
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import platform
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants shared across Lab Brick DLLs
# ---------------------------------------------------------------------------

DEVID_INVALID = 0x8000  # Vaunix invalid device handle sentinel
MAX_DEVICES = 64

# Status codes returned by DLL functions
STATUS_OK = 0

# Frequency / power scaling used by the LMS/LSG DLL
FREQ_SCALE = 10        # DLL uses 10 Hz steps  (value * 10 = Hz)
POWER_SCALE = 4        # DLL uses 0.25 dB steps (value / 4 = dBm)

# Attenuation scaling used by the attenuator DLL
ATTEN_SCALE = 4        # DLL uses 0.25 dB steps (value / 4 = dB)


_PLATFORM_EXT = {
    "Windows": ".dll",
    "Linux": ".so",
    "Darwin": ".dylib",
}


def _load_dll(dll_name: str, dll_path: Optional[str] = None) -> ctypes.CDLL:
    """Load a Vaunix shared library, raising a clear error if unavailable.

    Parameters
    ----------
    dll_name:
        Base name of the library **without** extension (e.g. ``vnx_fsynth``).
        Legacy callers that pass ``vnx_fsynth.dll`` are handled gracefully —
        the ``.dll`` suffix is stripped before appending the platform extension.
    dll_path:
        Optional explicit path to the library file.  When provided, this takes
        priority over all other resolution strategies.
    """
    # If an explicit path was given, use it directly.
    if dll_path is not None:
        try:
            return ctypes.cdll.LoadLibrary(dll_path)
        except OSError as exc:
            raise OSError(
                f"Cannot load Vaunix library at explicit path '{dll_path}'. "
                f"Original error: {exc}"
            ) from exc

    # Determine platform extension.
    system = platform.system()
    ext = _PLATFORM_EXT.get(system)
    if ext is None:
        raise OSError(
            f"Unsupported platform: {system}. "
            f"Vaunix Lab Brick libraries are available for Windows (.dll), "
            f"Linux (.so), and macOS (.dylib). "
            f"See https://vaunix.com/software/"
        )

    # Strip legacy .dll suffix if present, then build the base name.
    base_name = dll_name
    for suffix in (".dll", ".so", ".dylib"):
        if base_name.endswith(suffix):
            base_name = base_name[: -len(suffix)]
            break
    lib_filename = f"{base_name}{ext}"

    paths_tried: list[str] = []

    # 1. Check LABBRICK_LIB_PATH env var.
    env_dir = os.environ.get("LABBRICK_LIB_PATH")
    if env_dir:
        env_path = os.path.join(env_dir, lib_filename)
        paths_tried.append(env_path)
        try:
            return ctypes.cdll.LoadLibrary(env_path)
        except OSError:
            pass

    # 2. Try ctypes.util.find_library (searches system library paths).
    found = ctypes.util.find_library(base_name)
    if found:
        paths_tried.append(found)
        try:
            return ctypes.cdll.LoadLibrary(found)
        except OSError:
            pass

    # 3. Final fallback: bare filename (relies on OS search path).
    paths_tried.append(lib_filename)
    try:
        return ctypes.cdll.LoadLibrary(lib_filename)
    except OSError as exc:
        raise OSError(
            f"Cannot load Vaunix library '{lib_filename}' on {system}. "
            f"Paths tried: {paths_tried}. "
            f"Ensure the library is installed and discoverable. "
            f"Download from https://vaunix.com/software/ — "
            f"Original error: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Lab Brick Synthesizer (LMS / LSG)
# ---------------------------------------------------------------------------

class LabBrickSynthesizer:
    """Wrapper for Vaunix Lab Brick LMS / LSG signal synthesizers.

    Both LMS and LSG models use the same ``vnx_fsynth.dll`` interface.

    Parameters
    ----------
    serial_number:
        Target device serial number.  When *None*, the first available
        device is used.
    dll_path:
        Explicit path to ``vnx_fsynth.dll``.  When *None*, the system
        search path is used.
    """

    DLL_NAME = "vnx_fsynth"

    def __init__(
        self,
        serial_number: Optional[int] = None,
        dll_path: Optional[str] = None,
    ) -> None:
        self._serial_number = serial_number
        self._dll_path = dll_path
        self._dll: Optional[ctypes.CDLL] = None
        self._handle: int = DEVID_INVALID
        self._model_name: str = ""
        self._device_serial: int = 0

    # -- Connection lifecycle -------------------------------------------------

    def connect(self) -> None:
        """Load the DLL, enumerate devices, and open one by serial number."""
        self._dll = _load_dll(self.DLL_NAME, self._dll_path)

        # Enumerate devices
        dev_count = self._dll.fnLMS_GetNumDevices()
        if dev_count <= 0:
            raise RuntimeError("No Vaunix Lab Brick synthesizers found.")

        device_ids = (ctypes.c_uint * MAX_DEVICES)()
        found = self._dll.fnLMS_GetDevInfo(device_ids)

        target_handle = DEVID_INVALID
        for i in range(found):
            dev_id = device_ids[i]
            serial = self._dll.fnLMS_GetSerialNumber(dev_id)
            if self._serial_number is None or serial == self._serial_number:
                target_handle = dev_id
                self._device_serial = serial
                break

        if target_handle == DEVID_INVALID:
            raise RuntimeError(
                f"Lab Brick synthesizer with serial "
                f"{self._serial_number or '(any)'} not found among "
                f"{found} device(s)."
            )

        status = self._dll.fnLMS_InitDevice(target_handle)
        if status != STATUS_OK:
            raise RuntimeError(
                f"fnLMS_InitDevice failed with status {status}"
            )

        self._handle = target_handle

        # Read model name
        buf = ctypes.create_string_buffer(256)
        self._dll.fnLMS_GetModelName(self._handle, buf)
        self._model_name = buf.value.decode("utf-8", errors="replace").strip()

        logger.info(
            "Connected to Lab Brick synthesizer: %s (serial=%d)",
            self._model_name, self._device_serial,
        )

    def disconnect(self) -> None:
        """Close the device handle."""
        if self._dll is not None and self._handle != DEVID_INVALID:
            try:
                self._dll.fnLMS_CloseDevice(self._handle)
            except Exception as exc:
                logger.warning("Error closing Lab Brick synthesizer: %s", exc)
            finally:
                self._handle = DEVID_INVALID
            logger.info("Disconnected Lab Brick synthesizer (serial=%d)", self._device_serial)

    # -- Identity -------------------------------------------------------------

    def get_identity(self) -> str:
        """Return an IDN-style identity string.

        Format: ``Vaunix,<model>,<serial>,1.0``
        """
        self._require_connected()
        return f"Vaunix,{self._model_name},{self._device_serial},1.0"

    # -- Frequency ------------------------------------------------------------

    def set_frequency(self, frequency_hz: float) -> str:
        """Set the output frequency in Hz.

        The DLL internally works in 10 Hz steps.
        """
        self._require_connected()
        raw = int(round(frequency_hz / FREQ_SCALE))
        self._dll.fnLMS_SetFrequency(self._handle, raw)
        logger.debug("Set frequency to %s Hz (raw=%d)", frequency_hz, raw)
        return "OK"

    def get_frequency(self) -> float:
        """Read the current output frequency in Hz."""
        self._require_connected()
        raw = self._dll.fnLMS_GetFrequency(self._handle)
        return float(raw * FREQ_SCALE)

    # -- Power ----------------------------------------------------------------

    def set_power(self, power_dbm: float) -> str:
        """Set the output power in dBm.

        The DLL internally works in 0.25 dB steps.
        """
        self._require_connected()
        raw = int(round(power_dbm * POWER_SCALE))
        self._dll.fnLMS_SetPowerLevel(self._handle, raw)
        logger.debug("Set power to %s dBm (raw=%d)", power_dbm, raw)
        return "OK"

    def get_power(self) -> float:
        """Read the current output power in dBm."""
        self._require_connected()
        raw = self._dll.fnLMS_GetPowerLevel(self._handle)
        return float(raw) / POWER_SCALE

    # -- RF on/off ------------------------------------------------------------

    def set_rf_on(self, on: bool) -> str:
        """Enable or disable the RF output."""
        self._require_connected()
        self._dll.fnLMS_SetRFOn(self._handle, int(bool(on)))
        state = "ON" if on else "OFF"
        logger.debug("Set RF output %s", state)
        return "OK"

    def get_rf_on(self) -> bool:
        """Return True if RF output is enabled."""
        self._require_connected()
        return bool(self._dll.fnLMS_GetRF_On(self._handle))

    # -- Internal helpers -----------------------------------------------------

    def _require_connected(self) -> None:
        """Raise if the device is not connected."""
        if self._dll is None or self._handle == DEVID_INVALID:
            raise RuntimeError(
                "Lab Brick synthesizer is not connected. Call connect() first."
            )


# ---------------------------------------------------------------------------
# Lab Brick Digital Attenuator
# ---------------------------------------------------------------------------

class LabBrickAttenuator:
    """Wrapper for Vaunix Lab Brick Digital Attenuators.

    Uses ``vnx_atten.dll``.

    Parameters
    ----------
    serial_number:
        Target device serial number.  When *None*, the first available
        device is used.
    dll_path:
        Explicit path to ``vnx_atten.dll``.  When *None*, the system
        search path is used.
    """

    DLL_NAME = "vnx_atten"

    def __init__(
        self,
        serial_number: Optional[int] = None,
        dll_path: Optional[str] = None,
    ) -> None:
        self._serial_number = serial_number
        self._dll_path = dll_path
        self._dll: Optional[ctypes.CDLL] = None
        self._handle: int = DEVID_INVALID
        self._model_name: str = ""
        self._device_serial: int = 0

    # -- Connection lifecycle -------------------------------------------------

    def connect(self) -> None:
        """Load the DLL, enumerate devices, and open one by serial number."""
        self._dll = _load_dll(self.DLL_NAME, self._dll_path)

        # Enumerate devices
        dev_count = self._dll.fnLDA_GetNumDevices()
        if dev_count <= 0:
            raise RuntimeError("No Vaunix Lab Brick attenuators found.")

        device_ids = (ctypes.c_uint * MAX_DEVICES)()
        found = self._dll.fnLDA_GetDevInfo(device_ids)

        target_handle = DEVID_INVALID
        for i in range(found):
            dev_id = device_ids[i]
            serial = self._dll.fnLDA_GetSerialNumber(dev_id)
            if self._serial_number is None or serial == self._serial_number:
                target_handle = dev_id
                self._device_serial = serial
                break

        if target_handle == DEVID_INVALID:
            raise RuntimeError(
                f"Lab Brick attenuator with serial "
                f"{self._serial_number or '(any)'} not found among "
                f"{found} device(s)."
            )

        status = self._dll.fnLDA_InitDevice(target_handle)
        if status != STATUS_OK:
            raise RuntimeError(
                f"fnLDA_InitDevice failed with status {status}"
            )

        self._handle = target_handle

        # Read model name
        buf = ctypes.create_string_buffer(256)
        self._dll.fnLDA_GetModelName(self._handle, buf)
        self._model_name = buf.value.decode("utf-8", errors="replace").strip()

        logger.info(
            "Connected to Lab Brick attenuator: %s (serial=%d)",
            self._model_name, self._device_serial,
        )

    def disconnect(self) -> None:
        """Close the device handle."""
        if self._dll is not None and self._handle != DEVID_INVALID:
            try:
                self._dll.fnLDA_CloseDevice(self._handle)
            except Exception as exc:
                logger.warning("Error closing Lab Brick attenuator: %s", exc)
            finally:
                self._handle = DEVID_INVALID
            logger.info("Disconnected Lab Brick attenuator (serial=%d)", self._device_serial)

    # -- Identity -------------------------------------------------------------

    def get_identity(self) -> str:
        """Return an IDN-style identity string.

        Format: ``Vaunix,<model>,<serial>,1.0``
        """
        self._require_connected()
        return f"Vaunix,{self._model_name},{self._device_serial},1.0"

    # -- Attenuation ----------------------------------------------------------

    def set_attenuation(self, attenuation_db: float) -> str:
        """Set the attenuation in dB.

        The DLL internally works in 0.25 dB steps.
        """
        self._require_connected()
        raw = int(round(attenuation_db * ATTEN_SCALE))
        self._dll.fnLDA_SetAttenuation(self._handle, raw)
        logger.debug("Set attenuation to %s dB (raw=%d)", attenuation_db, raw)
        return "OK"

    def get_attenuation(self) -> float:
        """Read the current attenuation in dB."""
        self._require_connected()
        raw = self._dll.fnLDA_GetAttenuation(self._handle)
        return float(raw) / ATTEN_SCALE

    # -- Internal helpers -----------------------------------------------------

    def _require_connected(self) -> None:
        """Raise if the device is not connected."""
        if self._dll is None or self._handle == DEVID_INVALID:
            raise RuntimeError(
                "Lab Brick attenuator is not connected. Call connect() first."
            )
