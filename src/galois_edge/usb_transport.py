"""
Raw USB bulk transport for vendor-specific instruments.

Some instruments (e.g. Signal Recovery 7270 DSP Lock-In Amplifier) expose
a USB Vendor Specific class (0xFF) interface rather than USB-TMC.  PyVISA-py
cannot discover or communicate with these devices.  This module provides a
custom transport layer using pyusb that plugs into InstrumentManager with the
same interface pattern as GPIBManager.

Wire protocol (e.g. SR 7270):
    TX: command_bytes + 0x00 terminator
    RX: response_bytes + 0x00 + status_byte + overload_byte
    Binary (DCB n): num_points * 2 + 3 bytes (int16 samples + trailer)

Key design points:
    - pyusb import is GUARDED — module works (returns empty) without it
    - Synthetic VISA address: USB0::0xVID::0xPID::SERIAL::RAW
    - Discovery avoids control transfers before interface is claimed
    - Serial number is read only after set_configuration + claim_interface
    - Placeholder serial "0" is used at discovery; canonical ID updated on connect
"""

import logging
import re
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# Guarded import — raw USB transport is optional
try:
    import usb.core
    import usb.util
    USB_AVAILABLE = True
except ImportError:
    USB_AVAILABLE = False
    logger.info("pyusb not available — raw USB transport disabled")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class USBDevice:
    """Cached state for a connected raw-USB instrument."""

    device: object       # usb.core.Device
    ep_out: object       # bulk OUT endpoint
    ep_in: object        # bulk IN endpoint
    serial: str = ""
    idn: str = ""
    vid: int = 0
    pid: int = 0
    timeout_ms: int = 5000


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Known vendor IDs for devices that require raw USB rather than USB-TMC
VID_SIGNAL_RECOVERY = 0x0A2D
PID_SR_7270 = 0x001B

# Synthetic VISA address pattern
_USB_RAW_RE = re.compile(
    r"^USB\d*::0x([0-9A-Fa-f]+)::0x([0-9A-Fa-f]+)::(.+?)::RAW$",
    re.IGNORECASE,
)

# Default read buffer
_READ_BUF = 4096


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

class USBTransport:
    """Raw USB bulk transport for vendor-specific instruments.

    Mirrors GPIBManager's public interface so that InstrumentManager can
    route to either backend using the same connect / disconnect / query /
    write pattern.

    Parameters
    ----------
    known_vids:
        Vendor IDs to scan during :meth:`discover`.  Defaults to
        ``[VID_SIGNAL_RECOVERY]``.
    """

    def __init__(
        self,
        known_vids: Optional[list[int]] = None,
    ):
        self._devices: dict[str, USBDevice] = {}
        self._known_vids = known_vids or [VID_SIGNAL_RECOVERY]

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_available(self) -> bool:
        """True when pyusb was successfully imported."""
        return USB_AVAILABLE

    # ------------------------------------------------------------------
    # Address helpers
    # ------------------------------------------------------------------

    @staticmethod
    def make_resource_id(vid: int, pid: int, serial: str) -> str:
        """Build a synthetic VISA-style resource string."""
        return f"USB0::0x{vid:04X}::0x{pid:04X}::{serial}::RAW"

    @staticmethod
    def parse_usb_address(resource_id: str) -> Optional[tuple[int, int, str]]:
        """Parse a raw-USB resource string into ``(vid, pid, serial)``.

        Returns None if *resource_id* does not match the pattern.
        """
        m = _USB_RAW_RE.match(resource_id)
        if m is None:
            return None
        vid = int(m.group(1), 16)
        pid = int(m.group(2), 16)
        serial = m.group(3)
        return vid, pid, serial

    @staticmethod
    def is_usb_resource(resource_id: str) -> bool:
        """Return True when *resource_id* is a raw-USB address."""
        return _USB_RAW_RE.match(resource_id) is not None

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover(self) -> list[str]:
        """Scan the USB bus for known vendor-specific instruments.

        Returns a list of synthetic VISA resource strings.

        **Important:** This method must NOT issue USB control transfers
        (e.g. ``get_string``) because doing so before
        ``set_configuration`` + ``claim_interface`` can poison the
        bulk-endpoint state on certain devices.  The serial number field
        is set to ``"0"`` as a placeholder; the real serial is resolved
        during :meth:`connect`.
        """
        if not USB_AVAILABLE:
            return []

        found: list[str] = []
        for vid in self._known_vids:
            try:
                devices = usb.core.find(find_all=True, idVendor=vid)
                for dev in devices:
                    # Use a placeholder serial; connect() reads the real one.
                    resource_id = self.make_resource_id(
                        dev.idVendor, dev.idProduct, "0"
                    )
                    # If already connected, return the canonical resource ID.
                    canonical = self._find_connected(dev.idVendor, dev.idProduct)
                    if canonical:
                        resource_id = canonical
                    found.append(resource_id)
                    logger.debug(
                        "USB discover: VID=0x%04X PID=0x%04X",
                        dev.idVendor,
                        dev.idProduct,
                    )
            except usb.core.USBError as exc:
                logger.warning("USB bus scan error for VID 0x%04X: %s", vid, exc)

        logger.info("USB discover: found %d device(s)", len(found))
        return found

    def _find_connected(self, vid: int, pid: int) -> Optional[str]:
        """Return the resource ID of an already-connected device by VID/PID."""
        for res_id, usb_dev in self._devices.items():
            if usb_dev.vid == vid and usb_dev.pid == pid:
                return res_id
        return None

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def connect(self, resource_id: str, timeout_ms: int = 5000) -> bool:
        """Open a raw USB connection to an instrument.

        The connection sequence is carefully ordered to avoid poisoning
        bulk-endpoint state on sensitive devices:

        1. ``usb.core.find()`` — locate the device
        2. Detach kernel driver if active
        3. ``set_configuration()``
        4. ``claim_interface()``
        5. Flush stale IN data
        6. Identify the device (``ID`` command)
        7. Read real serial number via ``get_string()``

        Returns True on success.
        """
        if not USB_AVAILABLE:
            return False

        # Already connected under this exact resource ID?
        if resource_id in self._devices:
            return True

        parsed = self.parse_usb_address(resource_id)
        if parsed is None:
            logger.error("Invalid USB resource string: %s", resource_id)
            return False

        vid, pid, serial = parsed

        # Already connected under a different serial (placeholder vs real)?
        existing = self._find_connected(vid, pid)
        if existing:
            return True

        try:
            dev = usb.core.find(idVendor=vid, idProduct=pid)
            if dev is None:
                logger.error(
                    "USB device not found: VID=0x%04X PID=0x%04X", vid, pid
                )
                return False

            # Detach kernel driver if active (Linux only, no-op elsewhere)
            try:
                if dev.is_kernel_driver_active(0):
                    dev.detach_kernel_driver(0)
            except (usb.core.USBError, NotImplementedError):
                pass

            # Set configuration before any I/O
            dev.set_configuration()

            # Claim interface before bulk or control transfers
            usb.util.claim_interface(dev, 0)

            # Locate bulk endpoints
            cfg = dev.get_active_configuration()
            intf = cfg[(0, 0)]

            ep_out = usb.util.find_descriptor(
                intf,
                custom_match=lambda e: (
                    usb.util.endpoint_direction(e.bEndpointAddress)
                    == usb.util.ENDPOINT_OUT
                ),
            )
            ep_in = usb.util.find_descriptor(
                intf,
                custom_match=lambda e: (
                    usb.util.endpoint_direction(e.bEndpointAddress)
                    == usb.util.ENDPOINT_IN
                ),
            )

            if ep_out is None or ep_in is None:
                logger.error(
                    "Could not find bulk endpoints for %s", resource_id
                )
                return False

            usb_dev = USBDevice(
                device=dev,
                ep_out=ep_out,
                ep_in=ep_in,
                serial=serial,
                vid=vid,
                pid=pid,
                timeout_ms=timeout_ms,
            )

            # Flush stale data from the IN endpoint
            try:
                dev.read(ep_in.bEndpointAddress, 64, timeout=200)
            except usb.core.USBTimeoutError:
                pass  # no stale data — expected

            # Attempt identification (retry once for busy devices)
            for attempt in range(2):
                try:
                    idn = self._raw_query(usb_dev, "ID")
                    usb_dev.idn = idn
                    break
                except Exception as exc:
                    if attempt == 0:
                        logger.debug("ID attempt 1 failed, retrying: %s", exc)
                        time.sleep(0.5)
                    else:
                        logger.warning(
                            "Could not identify USB device %s: %s",
                            resource_id,
                            exc,
                        )

            # Now safe to read the real serial number
            real_serial = serial
            try:
                if dev.iSerialNumber:
                    real_serial = usb.util.get_string(dev, dev.iSerialNumber)
                    usb_dev.serial = real_serial
            except (usb.core.USBError, ValueError):
                pass

            canonical_id = self.make_resource_id(vid, pid, real_serial)
            self._devices[canonical_id] = usb_dev
            logger.info(
                "Connected to USB instrument: %s (%s)",
                canonical_id,
                usb_dev.idn,
            )
            return True

        except usb.core.USBError as exc:
            logger.error("USB connect failed for %s: %s", resource_id, exc)
            return False

    def disconnect(self, resource_id: str) -> None:
        """Release a USB instrument."""
        key = self._resolve(resource_id)
        if key is None:
            return

        usb_dev = self._devices.pop(key)
        try:
            usb.util.release_interface(usb_dev.device, 0)
        except Exception:
            pass
        try:
            usb.util.dispose_resources(usb_dev.device)
        except Exception:
            pass
        logger.info("Disconnected from USB instrument: %s", resource_id)

    def disconnect_all(self) -> None:
        """Release all connected USB instruments."""
        for resource_id in list(self._devices):
            self.disconnect(resource_id)

    def _resolve(self, resource_id: str) -> Optional[str]:
        """Map *resource_id* to the canonical key in ``_devices``.

        Discovery may use placeholder serial ``"0"`` while the device
        is stored under its real serial.  This helper checks both the
        literal key and a VID/PID lookup.
        """
        if resource_id in self._devices:
            return resource_id
        parsed = self.parse_usb_address(resource_id)
        if parsed is None:
            return None
        vid, pid, _ = parsed
        return self._find_connected(vid, pid)

    def is_connected(self, resource_id: str) -> bool:
        """Return True if a device matching *resource_id* is connected."""
        return self._resolve(resource_id) is not None

    # ------------------------------------------------------------------
    # Low-level I/O (null-terminated protocol)
    # ------------------------------------------------------------------

    @staticmethod
    def _raw_write(usb_dev: USBDevice, command: str) -> None:
        """Send a null-terminated command over the bulk OUT endpoint."""
        payload = command.encode("ascii") + b"\x00"
        usb_dev.ep_out.write(payload, timeout=usb_dev.timeout_ms)

    @staticmethod
    def _raw_read(usb_dev: USBDevice, size: int = _READ_BUF) -> bytes:
        """Read from the bulk IN endpoint until the null terminator.

        The device returns: ``response_bytes + 0x00 + status + overload``.
        Everything before the first 0x00 is the response payload.
        """
        data = usb_dev.ep_in.read(size, timeout=usb_dev.timeout_ms)
        raw = bytes(data)
        null_pos = raw.find(b"\x00")
        if null_pos >= 0:
            return raw[:null_pos]
        return raw

    @staticmethod
    def _raw_query(usb_dev: USBDevice, command: str) -> str:
        """Write then read (text protocol)."""
        USBTransport._raw_write(usb_dev, command)
        return (
            USBTransport._raw_read(usb_dev)
            .decode("ascii", errors="replace")
            .strip()
        )

    # ------------------------------------------------------------------
    # Public I/O (keyed by resource_id)
    # ------------------------------------------------------------------

    def write(self, resource_id: str, command: str) -> None:
        """Send a command (no response expected).

        Raises
        ------
        ValueError
            If not connected.
        IOError
            On USB communication failure.
        """
        key = self._resolve(resource_id)
        if key is None:
            raise ValueError(f"Not connected to {resource_id}")

        try:
            self._raw_write(self._devices[key], command)
        except usb.core.USBError as exc:
            raise IOError(f"USB write error: {exc}") from exc

    def read(self, resource_id: str, size: int = _READ_BUF) -> str:
        """Read a text response from the instrument.

        Raises
        ------
        ValueError
            If not connected.
        IOError
            On USB communication failure.
        """
        key = self._resolve(resource_id)
        if key is None:
            raise ValueError(f"Not connected to {resource_id}")

        try:
            return (
                self._raw_read(self._devices[key], size)
                .decode("ascii", errors="replace")
                .strip()
            )
        except usb.core.USBError as exc:
            raise IOError(f"USB read error: {exc}") from exc

    def query(self, resource_id: str, command: str) -> str:
        """Send a query and return the text response.

        Raises
        ------
        ValueError
            If not connected.
        IOError
            On USB communication failure.
        """
        key = self._resolve(resource_id)
        if key is None:
            raise ValueError(f"Not connected to {resource_id}")

        try:
            return self._raw_query(self._devices[key], command)
        except usb.core.USBError as exc:
            raise IOError(f"USB query error: {exc}") from exc

    def identify(self, resource_id: str) -> str:
        """Return the cached identification, or query ``ID`` for it."""
        key = self._resolve(resource_id)
        if key and self._devices[key].idn:
            return self._devices[key].idn
        return self.query(resource_id, "ID")

    def read_binary(self, resource_id: str, num_bytes: int) -> bytes:
        """Read exactly *num_bytes* of raw binary data.

        Used for curve buffer downloads (``DCB n`` command).  The caller
        must send the appropriate write command first via :meth:`write`.

        Raises
        ------
        ValueError
            If not connected.
        IOError
            On USB communication failure.
        """
        key = self._resolve(resource_id)
        if key is None:
            raise ValueError(f"Not connected to {resource_id}")

        usb_dev = self._devices[key]
        buf = bytearray()

        try:
            while len(buf) < num_bytes:
                remaining = num_bytes - len(buf)
                chunk = usb_dev.ep_in.read(
                    min(remaining, _READ_BUF),
                    timeout=usb_dev.timeout_ms,
                )
                buf.extend(bytes(chunk))
        except usb.core.USBError as exc:
            raise IOError(
                f"USB binary read error (got {len(buf)}/{num_bytes} bytes): {exc}"
            ) from exc

        return bytes(buf[:num_bytes])

    # ------------------------------------------------------------------
    # Info
    # ------------------------------------------------------------------

    def list_resources(self) -> list[str]:
        """Return resource IDs for all currently connected devices."""
        return list(self._devices.keys())

    def get_device_info(self, resource_id: str) -> Optional[USBDevice]:
        """Return the USBDevice dataclass for an open connection."""
        key = self._resolve(resource_id)
        return self._devices.get(key) if key else None
