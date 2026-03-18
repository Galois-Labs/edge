"""
GPIB instrument manager using linux-gpib via gpib_ctypes.

Provides direct GPIB communication for instruments connected via
GPIB adapters (e.g. NI GPIB-USB-HS) using the linux-gpib driver stack.

linux-gpib is preferred over NI-VISA for GPIB on Linux because it offers
native kernel driver support, no proprietary library conflicts, and better
reliability with USB-GPIB adapters.

Key design points:
  - gpib_ctypes import is GUARDED — module is usable (returns empty) without it
  - Scans boards 0-15, addresses 1-30
  - Explicit LF (0x0A) line termination for SCPI commands
  - Device descriptors are cached in GPIBDevice dataclasses
  - Thread-safe via a per-manager lock
"""

import logging
import re
import threading
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# Guarded import — GPIB support is optional
try:
    from gpib_ctypes import gpib
    GPIB_AVAILABLE = True
except ImportError:
    GPIB_AVAILABLE = False
    logger.info("gpib_ctypes not available — GPIB support disabled")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class GPIBDevice:
    """Cached state for a connected GPIB device."""

    board: int
    address: int
    descriptor: int
    idn: str = ""
    timeout_code: int = 13  # T10s by default


@dataclass
class GPIBBoard:
    """Cached state for an initialised GPIB board."""

    index: int
    descriptor: int


# ---------------------------------------------------------------------------
# Timeout codes (linux-gpib constants)
# ---------------------------------------------------------------------------

class _Timeout:
    """Named constants for linux-gpib timeout values."""

    NONE = 0
    T10us = 1
    T30us = 2
    T100us = 3
    T300us = 4
    T1ms = 5
    T3ms = 6
    T10ms = 7
    T30ms = 8
    T100ms = 9
    T300ms = 10
    T1s = 11
    T3s = 12
    T10s = 13
    T30s = 14
    T100s = 15
    T300s = 16
    T1000s = 17


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

# VISA address pattern: GPIB0::25::INSTR or GPIB::25::INSTR
_GPIB_RE = re.compile(r"^GPIB(\d*)::(\d+)::INSTR$", re.IGNORECASE)

# Maximum number of boards to probe
_MAX_BOARDS = 16

# GPIB primary addresses to scan
_ADDR_START = 1
_ADDR_END = 30

# Default read buffer size
_READ_BUF = 4096


class GPIBManager:
    """Manage GPIB instrument discovery, connections, and I/O.

    Handles:
      * Multiple board initialisation and Controller-In-Charge (CIC) assertion
      * Bus scanning across all detected boards
      * Connection lifecycle with proper EOS/EOI configuration
      * Thread-safe read / write / query with automatic LF termination
      * Optional non-SCPI identity probes injected from instrument profiles

    Parameters
    ----------
    default_board:
        Board index used when a VISA address omits the board number
        (e.g. ``GPIB::25::INSTR``).
    scan_on_init:
        If True, scan all detected boards for devices during ``__init__``.
    """

    def __init__(self, default_board: int = 0, scan_on_init: bool = True):
        self._default_board = default_board
        self._devices: dict[str, GPIBDevice] = {}
        self._boards: dict[int, GPIBBoard] = {}
        self._lock = threading.Lock()
        self._identity_probes: list[tuple[bytes, str]] = []

        if not GPIB_AVAILABLE:
            logger.error("GPIB support not available — gpib_ctypes not installed")
            return

        self._discover_boards()

        if scan_on_init and self._boards:
            self.scan_all_boards()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_available(self) -> bool:
        """True when gpib_ctypes is importable and at least one board exists."""
        return GPIB_AVAILABLE and bool(self._boards)

    # ------------------------------------------------------------------
    # Board discovery
    # ------------------------------------------------------------------

    def _discover_boards(self) -> None:
        """Probe board indices 0..15 and initialise each as CIC."""
        if not GPIB_AVAILABLE:
            return

        for idx in range(_MAX_BOARDS):
            try:
                bd = gpib.find(f"gpib{idx}")
                if bd < 0:
                    continue
                # Become system controller and assert IFC
                gpib.config(bd, gpib.IbcSC, 1)
                gpib.interface_clear(bd)
                # Remote-enable so instruments accept bus commands
                gpib.remote_enable(bd, 1)

                self._boards[idx] = GPIBBoard(index=idx, descriptor=bd)
                logger.info(
                    "GPIB board %d initialised as CIC (descriptor %d)", idx, bd
                )
            except gpib.GpibError:
                # Board does not exist at this index — skip
                continue

        if self._boards:
            logger.info(
                "Discovered %d GPIB board(s): %s",
                len(self._boards),
                sorted(self._boards.keys()),
            )
        else:
            logger.warning("No GPIB boards found")

    def _assert_cic(self, board: int) -> None:
        """Re-assert Controller-In-Charge on *board* after a bus fault."""
        if board not in self._boards:
            return
        try:
            gpib.interface_clear(self._boards[board].descriptor)
        except gpib.GpibError as exc:
            logger.error("Failed to assert CIC on board %d: %s", board, exc)

    # ------------------------------------------------------------------
    # Identity probes
    # ------------------------------------------------------------------

    def set_identity_probes(self, probes: list[tuple[bytes, str]]) -> None:
        """Register additional identity probes from instrument profiles.

        Parameters
        ----------
        probes:
            List of ``(command_bytes, label)`` tuples. During bus scanning
            these are tried after the standard ``*IDN?`` probe.
        """
        self._identity_probes = list(probes)
        logger.info("Set %d additional identity probe(s)", len(probes))

    # ------------------------------------------------------------------
    # Address parsing
    # ------------------------------------------------------------------

    def parse_gpib_address(self, visa_address: str) -> Optional[tuple[int, int]]:
        """Parse a GPIB VISA address into ``(board, primary_address)``.

        Returns None if *visa_address* does not match the GPIB pattern.
        """
        m = _GPIB_RE.match(visa_address)
        if m is None:
            return None
        board = int(m.group(1)) if m.group(1) else self._default_board
        address = int(m.group(2))
        return board, address

    def is_gpib_address(self, visa_address: str) -> bool:
        """Return True when *visa_address* is a GPIB VISA string."""
        return _GPIB_RE.match(visa_address) is not None

    # ------------------------------------------------------------------
    # Bus scanning
    # ------------------------------------------------------------------

    def scan_all_boards(self) -> list[str]:
        """Scan every initialised board and return found VISA addresses."""
        results: list[str] = []
        for board_idx in sorted(self._boards):
            results.extend(self.scan_bus(board=board_idx))
        return results

    def scan_bus(
        self,
        board: Optional[int] = None,
        start_addr: int = _ADDR_START,
        end_addr: int = _ADDR_END,
    ) -> list[str]:
        """Scan a single GPIB bus for listeners and identify them.

        Uses ``gpib.listener()`` for a fast presence check, then sends
        ``*IDN?`` (and any registered identity probes) to get an
        identification string.

        Parameters
        ----------
        board:
            Board index to scan.  Defaults to ``default_board``.
        start_addr / end_addr:
            Primary-address range to probe (inclusive).

        Returns
        -------
        list[str]
            VISA resource strings for instruments that responded.
        """
        if not self.is_available:
            return []

        if board is None:
            board = self._default_board
        if board not in self._boards:
            logger.warning("GPIB board %d not initialised, skipping scan", board)
            return []

        with self._lock:
            self._assert_cic(board)
            bd = self._boards[board].descriptor

            # Phase 1 — fast listener detection
            listeners: list[int] = []
            for addr in range(start_addr, end_addr + 1):
                try:
                    if gpib.listener(bd, addr):
                        listeners.append(addr)
                except gpib.GpibError:
                    pass

            logger.info(
                "GPIB%d listener scan: %d device(s) at %s",
                board,
                len(listeners),
                listeners or "none",
            )

            # Phase 2 — identify each listener
            # Build probe list: standard *IDN? first, then profile-driven
            probes = [(b"*IDN?\n", "SCPI")] + self._identity_probes

            found: list[str] = []
            for addr in listeners:
                try:
                    # Open a device descriptor with LF-based EOS
                    # eos = 0x140A ⇒ LF char (0x0A) + REOS flag
                    dev = gpib.dev(
                        board, addr, 0, _Timeout.T3s, 1, 0x140A
                    )

                    response: Optional[str] = None
                    for cmd, label in probes:
                        try:
                            gpib.write(dev, cmd)
                            raw = gpib.read(dev, _READ_BUF).decode().strip()
                            if not raw:
                                continue
                            # For standard SCPI, reject pure-numeric garbage
                            if label == "SCPI" and not any(c.isalpha() for c in raw):
                                logger.debug(
                                    "GPIB%d::%d SCPI response looks non-IDN, "
                                    "trying remaining probes",
                                    board,
                                    addr,
                                )
                                continue
                            response = raw
                            break
                        except gpib.GpibError:
                            continue

                    visa_addr = f"GPIB{board}::{addr}::INSTR"

                    if response:
                        logger.info(
                            "Found instrument at %s: %s", visa_addr, response
                        )
                        self._devices[visa_addr] = GPIBDevice(
                            board=board,
                            address=addr,
                            descriptor=dev,
                            idn=response,
                        )
                        found.append(visa_addr)
                    else:
                        logger.warning(
                            "GPIB%d::%d listener present but no ID response",
                            board,
                            addr,
                        )
                        gpib.close(dev)

                except gpib.GpibError as exc:
                    logger.warning(
                        "GPIB%d::%d listener but identification failed: %s",
                        board,
                        addr,
                        exc,
                    )

            logger.info(
                "GPIB%d scan complete: found %d instrument(s)",
                board,
                len(found),
            )
            return found

    def scan_single_address(
        self,
        board: int,
        addr: int,
        timeout_code: int = _Timeout.T1s,
    ) -> Optional[str]:
        """Probe a single GPIB address and return VISA string if found.

        Unlike scan_bus(), this acquires the lock for only ONE address probe
        (~500ms worst case), making it suitable for trickle scanning between
        command jobs.

        Parameters
        ----------
        board:
            GPIB board index to probe.
        addr:
            Primary address to probe (1-30).
        timeout_code:
            linux-gpib timeout code for the probe. Default T1s (shorter
            than scan_bus's T3s) to limit worst-case blocking to ~1s.

        Returns
        -------
        str or None
            The VISA address string if a new instrument was found,
            or None if no listener, already known, or probe failed.
        """
        if not self.is_available:
            return None
        if board not in self._boards:
            return None

        visa_addr = f"GPIB{board}::{addr}::INSTR"

        # Already known -- skip
        if visa_addr in self._devices:
            return None

        with self._lock:
            try:
                bd = self._boards[board].descriptor

                # Fast listener check
                if not gpib.listener(bd, addr):
                    return None

                logger.debug(
                    "Trickle: listener at GPIB%d::%d, probing identity",
                    board, addr,
                )

                # Open device with shorter timeout
                dev = gpib.dev(board, addr, 0, timeout_code, 1, 0x140A)

                # Build probe list: standard *IDN? first, then profile-driven
                probes = [(b"*IDN?\n", "SCPI")] + self._identity_probes

                response: Optional[str] = None
                for cmd, label in probes:
                    try:
                        gpib.write(dev, cmd)
                        raw = gpib.read(dev, _READ_BUF).decode().strip()
                        if not raw:
                            continue
                        # For standard SCPI, reject pure-numeric garbage
                        if label == "SCPI" and not any(c.isalpha() for c in raw):
                            continue
                        response = raw
                        break
                    except gpib.GpibError:
                        continue

                if response:
                    self._devices[visa_addr] = GPIBDevice(
                        board=board,
                        address=addr,
                        descriptor=dev,
                        idn=response,
                    )
                    logger.info(
                        "Trickle scan found instrument at %s: %s",
                        visa_addr, response,
                    )
                    return visa_addr
                else:
                    logger.debug(
                        "Trickle: GPIB%d::%d listener but no ID response",
                        board, addr,
                    )
                    gpib.close(dev)
                    return None

            except gpib.GpibError as exc:
                logger.debug(
                    "Trickle: GPIB%d::%d probe error: %s", board, addr, exc
                )
                return None

    def remove_devices_on_board(self, board: int) -> list[str]:
        """Remove all devices associated with a board (adapter unplugged).

        Does NOT attempt gpib.close() on device descriptors (the USB
        device is already gone; calling close() would hang or SIGABRT).

        Returns list of VISA addresses that were removed.
        """
        removed: list[str] = []
        with self._lock:
            to_remove = [
                addr for addr, dev in self._devices.items()
                if dev.board == board
            ]
            for addr in to_remove:
                del self._devices[addr]
                removed.append(addr)
                logger.info(
                    "Removed device %s (board %d adapter unplugged)", addr, board
                )

            # Remove the board entry itself
            if board in self._boards:
                del self._boards[board]
                logger.info("Removed GPIB board %d (adapter unplugged)", board)

        return removed

    def reinit_board(self, board_index: int) -> bool:
        """Re-initialise a single GPIB board after adapter re-plug.

        Calls gpib.find(), gpib.config(IbcSC), gpib.interface_clear(),
        gpib.remote_enable() for the specified board only.

        Returns True if the board was successfully initialised.
        """
        if not GPIB_AVAILABLE:
            return False

        with self._lock:
            try:
                bd = gpib.find(f"gpib{board_index}")
                if bd < 0:
                    logger.warning(
                        "reinit_board: gpib%d not found", board_index
                    )
                    return False

                gpib.config(bd, gpib.IbcSC, 1)
                gpib.interface_clear(bd)
                gpib.remote_enable(bd, 1)

                self._boards[board_index] = GPIBBoard(
                    index=board_index, descriptor=bd
                )
                logger.info(
                    "GPIB board %d re-initialised as CIC (descriptor %d)",
                    board_index, bd,
                )
                return True

            except gpib.GpibError as exc:
                logger.error(
                    "Failed to re-initialise GPIB board %d: %s",
                    board_index, exc,
                )
                return False

    # ------------------------------------------------------------------
    # Resource listing
    # ------------------------------------------------------------------

    def list_resources(self) -> list[str]:
        """Return VISA addresses for all discovered GPIB devices."""
        return list(self._devices.keys())

    @property
    def boards(self) -> dict[int, GPIBBoard]:
        """Return the dict of initialised boards (read-only access)."""
        return self._boards

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def connect(self, visa_address: str, timeout_ms: int = 10000) -> bool:
        """Open a connection to a GPIB instrument.

        If the device was already found during scanning its cached
        descriptor is reused.  Otherwise a new device descriptor is
        opened and an identification probe is attempted.

        Returns True on success.
        """
        if not self.is_available:
            return False

        # Already connected?
        if visa_address in self._devices:
            return True

        parsed = self.parse_gpib_address(visa_address)
        if parsed is None:
            logger.error("Invalid GPIB address: %s", visa_address)
            return False

        board, addr = parsed
        if board not in self._boards:
            logger.error("GPIB board %d not initialised", board)
            return False

        with self._lock:
            try:
                self._assert_cic(board)
                tc = self._ms_to_timeout_code(timeout_ms)

                dev = gpib.dev(board, addr, 0, tc, 1, 0x140A)

                # Attempt identification
                probes = [(b"*IDN?\n", "SCPI")] + self._identity_probes
                idn = ""
                for cmd, label in probes:
                    try:
                        gpib.write(dev, cmd)
                        raw = gpib.read(dev, _READ_BUF).decode().strip()
                        if not raw:
                            continue
                        if label == "SCPI" and not any(c.isalpha() for c in raw):
                            continue
                        idn = raw
                        break
                    except gpib.GpibError:
                        continue

                self._devices[visa_address] = GPIBDevice(
                    board=board,
                    address=addr,
                    descriptor=dev,
                    idn=idn,
                    timeout_code=tc,
                )
                logger.info(
                    "Connected to GPIB instrument: %s (%s)", visa_address, idn
                )
                return True

            except gpib.GpibError as exc:
                logger.error(
                    "Failed to connect to %s: %s", visa_address, exc
                )
                return False

    def disconnect(self, visa_address: str) -> None:
        """Close a GPIB instrument connection."""
        if visa_address not in self._devices:
            return

        with self._lock:
            try:
                gpib.close(self._devices[visa_address].descriptor)
            except gpib.GpibError:
                pass

        del self._devices[visa_address]
        logger.info("Disconnected from %s", visa_address)

    def disconnect_all(self) -> None:
        """Close all GPIB instrument connections."""
        for addr in list(self._devices):
            self.disconnect(addr)

    def is_connected(self, visa_address: str) -> bool:
        """Return True if *visa_address* has an open descriptor."""
        return visa_address in self._devices

    # ------------------------------------------------------------------
    # Low-level I/O (lock must be held by caller)
    # ------------------------------------------------------------------

    def _write_locked(
        self, device: GPIBDevice, visa_address: str, data: bytes
    ) -> None:
        """Write *data* to *device*.  Retries once after re-asserting CIC."""
        try:
            gpib.write(device.descriptor, data)
        except gpib.GpibError as exc:
            # ENOL (error 2) means no listener — retry after CIC
            if "Iberr 2" in str(exc):
                logger.debug(
                    "%s no-listener on write, re-asserting CIC", visa_address
                )
                self._assert_cic(device.board)
                try:
                    gpib.write(device.descriptor, data)
                    return
                except gpib.GpibError as exc2:
                    raise IOError(
                        f"GPIB write error (after CIC retry): {exc2}"
                    ) from exc2
            raise IOError(f"GPIB write error: {exc}") from exc

    def _read_locked(self, device: GPIBDevice, bufsize: int = _READ_BUF) -> str:
        """Read from *device* and return stripped text."""
        try:
            raw = gpib.read(device.descriptor, bufsize)
            return raw.decode().strip()
        except gpib.GpibError as exc:
            raise IOError(f"GPIB read error: {exc}") from exc

    # ------------------------------------------------------------------
    # Public I/O
    # ------------------------------------------------------------------

    def write(self, visa_address: str, command: str) -> None:
        """Send a write command to an instrument.

        A trailing LF is appended automatically if not already present.

        Raises
        ------
        ValueError
            If not connected to *visa_address*.
        IOError
            On communication failure.
        """
        if visa_address not in self._devices:
            raise ValueError(f"Not connected to {visa_address}")

        device = self._devices[visa_address]
        if not command.endswith("\n"):
            command += "\n"

        with self._lock:
            self._write_locked(device, visa_address, command.encode())

    def read(self, visa_address: str, bufsize: int = _READ_BUF) -> str:
        """Read a response from an instrument.

        Returns
        -------
        str
            Stripped response text.

        Raises
        ------
        ValueError
            If not connected.
        IOError
            On communication failure.
        """
        if visa_address not in self._devices:
            raise ValueError(f"Not connected to {visa_address}")

        device = self._devices[visa_address]

        with self._lock:
            return self._read_locked(device, bufsize)

    def query(self, visa_address: str, command: str) -> str:
        """Write a query command and read the response atomically.

        The lock is held for the full write-then-read sequence to prevent
        interleaving on the shared GPIB bus.

        Raises
        ------
        ValueError
            If not connected.
        IOError
            On communication failure.
        """
        if visa_address not in self._devices:
            raise ValueError(f"Not connected to {visa_address}")

        device = self._devices[visa_address]
        if not command.endswith("\n"):
            command += "\n"

        with self._lock:
            self._write_locked(device, visa_address, command.encode())
            return self._read_locked(device)

    def identify(self, visa_address: str) -> str:
        """Return the cached *IDN? response, or query it live."""
        if visa_address in self._devices and self._devices[visa_address].idn:
            return self._devices[visa_address].idn
        return self.query(visa_address, "*IDN?")

    def clear(self, visa_address: str) -> None:
        """Send ``*CLS`` (SCPI clear status) to an instrument.

        Uses a SCPI write rather than ``gpib.clear()`` to avoid ECIC
        errors on linux-gpib.
        """
        if visa_address not in self._devices:
            raise ValueError(f"Not connected to {visa_address}")

        device = self._devices[visa_address]
        with self._lock:
            try:
                gpib.write(device.descriptor, b"*CLS\n")
            except gpib.GpibError as exc:
                raise IOError(f"GPIB clear error: {exc}") from exc

    # ------------------------------------------------------------------
    # Device info
    # ------------------------------------------------------------------

    def get_device_info(self, visa_address: str) -> Optional[GPIBDevice]:
        """Return the GPIBDevice dataclass for *visa_address*, or None."""
        return self._devices.get(visa_address)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _ms_to_timeout_code(timeout_ms: int) -> int:
        """Convert milliseconds to the nearest linux-gpib timeout constant."""
        if timeout_ms <= 0:
            return _Timeout.NONE
        if timeout_ms <= 1:
            return _Timeout.T1ms
        if timeout_ms <= 10:
            return _Timeout.T10ms
        if timeout_ms <= 30:
            return _Timeout.T30ms
        if timeout_ms <= 100:
            return _Timeout.T100ms
        if timeout_ms <= 300:
            return _Timeout.T300ms
        if timeout_ms <= 1000:
            return _Timeout.T1s
        if timeout_ms <= 3000:
            return _Timeout.T3s
        if timeout_ms <= 10000:
            return _Timeout.T10s
        if timeout_ms <= 30000:
            return _Timeout.T30s
        if timeout_ms <= 100000:
            return _Timeout.T100s
        return _Timeout.T300s
