"""
SDK wrapper for Ocean Optics spectrometers via the ``seabreeze`` library.

This thin wrapper adapts the seabreeze ``Spectrometer`` API to the generic
calling convention expected by :class:`~galois_edge.sdk_executor.SDKExecutor`.
Install the driver with ``pip install seabreeze``.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


class OceanOpticsSpectrometer:
    """Wrapper around :pypi:`seabreeze` for Ocean Optics USB spectrometers.

    Supported models include USB2000, USB4000, HR2000, HR4000, QE65000,
    and others supported by the SeaBreeze library.

    Parameters
    ----------
    serial_number:
        Serial number of the target spectrometer. When *None*, the first
        available device is used.
    """

    def __init__(self, serial_number: Optional[str] = None) -> None:
        self._serial_number = serial_number
        self._spec: Optional[object] = None  # seabreeze Spectrometer instance

    # -- Connection lifecycle -------------------------------------------------

    def connect(self) -> None:
        """Open a connection to the spectrometer.

        Uses ``Spectrometer.from_serial_number()`` if a serial was provided,
        otherwise falls back to ``Spectrometer.from_first_available()``.
        """
        try:
            from seabreeze.spectrometers import Spectrometer
        except (ImportError, OSError) as exc:
            raise ImportError(
                "The 'seabreeze' package is required for Ocean Optics instruments. "
                "Install with: pip install seabreeze"
            ) from exc

        if self._serial_number:
            self._spec = Spectrometer.from_serial_number(self._serial_number)
            logger.info(
                "Connected to Ocean Optics spectrometer serial=%s", self._serial_number
            )
        else:
            self._spec = Spectrometer.from_first_available()
            logger.info("Connected to first available Ocean Optics spectrometer")

    def disconnect(self) -> None:
        """Close the spectrometer connection and release the USB handle."""
        if self._spec is not None:
            try:
                self._spec.close()  # type: ignore[union-attr]
            except Exception as exc:
                logger.warning("Error closing spectrometer: %s", exc)
            finally:
                self._spec = None
            logger.info("Disconnected Ocean Optics spectrometer")

    # -- Identity -------------------------------------------------------------

    def get_identity(self) -> str:
        """Return an IDN-style identity string.

        Format: ``Ocean Optics,<model>,<serial>,1.0``
        """
        self._require_connected()
        model = self._spec.model  # type: ignore[union-attr]
        serial = self._spec.serial_number  # type: ignore[union-attr]
        return f"Ocean Optics,{model},{serial},1.0"

    # -- Spectrum acquisition -------------------------------------------------

    def get_wavelengths(self) -> str:
        """Return the wavelength array as a comma-separated string (nm)."""
        self._require_connected()
        wavelengths = self._spec.wavelengths()  # type: ignore[union-attr]
        return ",".join(f"{w:.4f}" for w in wavelengths)

    def get_intensities(self) -> str:
        """Return the intensity array as a comma-separated string (counts)."""
        self._require_connected()
        intensities = self._spec.intensities()  # type: ignore[union-attr]
        return ",".join(f"{i:.4f}" for i in intensities)

    def get_spectrum(self) -> str:
        """Return wavelengths and intensities as two semicolon-separated CSV rows.

        Format: ``<wavelengths>;<intensities>``
        """
        self._require_connected()
        wavelengths = self._spec.wavelengths()  # type: ignore[union-attr]
        intensities = self._spec.intensities()  # type: ignore[union-attr]
        wl_str = ",".join(f"{w:.4f}" for w in wavelengths)
        int_str = ",".join(f"{i:.4f}" for i in intensities)
        return f"{wl_str};{int_str}"

    # -- Integration time -----------------------------------------------------

    def set_integration_time(self, time_us: int) -> str:
        """Set the integration time in microseconds.

        Parameters
        ----------
        time_us:
            Integration time in microseconds. Must be within the device's
            supported range (see :meth:`get_integration_time_limits`).
        """
        self._require_connected()
        self._spec.integration_time_micros(int(time_us))  # type: ignore[union-attr]
        return "OK"

    def get_integration_time_limits(self) -> str:
        """Return the min and max integration time in microseconds.

        Format: ``<min_us>,<max_us>``
        """
        self._require_connected()
        limits = self._spec.integration_time_micros_limits  # type: ignore[union-attr]
        return f"{limits[0]},{limits[1]}"

    # -- Internal helpers -----------------------------------------------------

    def _require_connected(self) -> None:
        """Raise if the spectrometer is not connected."""
        if self._spec is None:
            raise RuntimeError(
                "Spectrometer is not connected. Call connect() first."
            )
