"""
Unified instrument manager for the galois-edge Python engine.

Aggregates four transport backends behind a single interface:
  1. **PyVISA** — USB-TMC, TCPIP (VXI-11 / HiSLIP), Serial
  2. **GPIB**  — linux-gpib via gpib_ctypes (GPIBManager)
  3. **Raw USB** — pyusb for vendor-specific devices (USBTransport)
  4. **LAN**   — static list + optional mDNS (LANDiscovery)

Key design points:
  - Does NOT import config.py; receives configuration values as
    constructor arguments so it can be tested and used standalone.
  - ``list_resources()`` aggregates all enabled backends.
  - ``connect()`` / ``disconnect()`` route to the correct backend.
  - ``query()`` / ``write()`` / ``identify()`` dispatch by address type.
  - GPIB instruments receive explicit LF termination.
  - TCPIP SOCKET connections receive read/write termination via PyVISA.
  - ``rescan_all()`` is a method, not a timer — the caller (main.py)
    handles scheduling.
"""

import logging
import os
import time
from typing import Optional, Union

logger = logging.getLogger(__name__)

# Guarded PyVISA import — the manager can function in degraded mode
# without it (GPIB-only or USB-only setups).
try:
    import pyvisa
    PYVISA_AVAILABLE = True
except ImportError:
    PYVISA_AVAILABLE = False
    logger.warning("pyvisa not available — VISA transport disabled")

from .gpib_manager import GPIBManager, GPIB_AVAILABLE
from .usb_transport import USBTransport, USB_AVAILABLE
from .lan_discovery import LANDiscovery, is_tcpip_resource, is_tcpip_socket_resource


class _suppress_native_stderr:
    """Redirect fd 2 to /dev/null to silence C library stderr noise.

    libgpib writes 'invalid descriptor' to stderr for every GPIB address
    that does not respond during pyvisa-py enumeration.  This context
    manager suppresses that output at the file-descriptor level so
    Python logging is unaffected.
    """

    def __enter__(self):
        self._old = os.dup(2)
        self._devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(self._devnull, 2)
        return self

    def __exit__(self, *args):
        os.dup2(self._old, 2)
        os.close(self._old)
        os.close(self._devnull)


class InstrumentManager:
    """Unified interface over PyVISA, GPIB, raw USB, and LAN transports.

    Parameters
    ----------
    gpib_enabled:
        Attempt to initialise GPIBManager.
    gpib_default_board:
        Default GPIB board index.
    gpib_scan_on_init:
        Scan GPIB buses during construction.
    usb_raw_enabled:
        Attempt to initialise USBTransport.
    lan_instruments:
        Comma-separated static LAN instrument list
        (``ip[:port[:protocol]],…``).
    lan_default_port:
        Default port for LAN instruments.
    lan_default_protocol:
        Default protocol for LAN instruments (``SOCKET``/``INSTR``/``HISLIP``).
    lan_mdns_enabled:
        Enable Zeroconf/mDNS LAN discovery.
    lan_mdns_timeout:
        Seconds to wait for mDNS responses.
    lan_probe_timeout:
        Seconds for the TCP connect probe.
    visa_backend:
        PyVISA backend selector (default ``"@py"`` for pyvisa-py).
    include_serial_ports:
        Include ``ASRL`` serial resources in VISA listings.
    """

    def __init__(
        self,
        *,
        gpib_enabled: bool = True,
        gpib_default_board: int = 0,
        gpib_scan_on_init: bool = True,
        usb_raw_enabled: bool = True,
        lan_instruments: str = "",
        lan_default_port: int = 5025,
        lan_default_protocol: str = "SOCKET",
        lan_mdns_enabled: bool = False,
        lan_mdns_timeout: float = 3.0,
        lan_probe_timeout: float = 2.0,
        visa_backend: str = "@py",
        include_serial_ports: bool = False,
    ):
        # ----- PyVISA resource manager -----
        self._rm: Optional[object] = None
        self._visa_backend = visa_backend
        self._include_serial_ports = include_serial_ports
        if PYVISA_AVAILABLE:
            try:
                self._rm = pyvisa.ResourceManager(visa_backend)
                logger.info("PyVISA resource manager initialised (backend=%s)", visa_backend)
            except Exception as exc:
                logger.warning("PyVISA initialisation failed: %s", exc)

        self._instruments: dict[str, object] = {}  # visa_address -> pyvisa.Resource

        # ----- GPIB manager -----
        self._gpib: Optional[GPIBManager] = None
        if gpib_enabled and GPIB_AVAILABLE:
            try:
                self._gpib = GPIBManager(
                    default_board=gpib_default_board,
                    scan_on_init=gpib_scan_on_init,
                )
                logger.info("GPIB support enabled via linux-gpib")
            except Exception as exc:
                logger.warning("GPIB initialisation failed: %s", exc)
        elif gpib_enabled and not GPIB_AVAILABLE:
            logger.warning("GPIB enabled but gpib_ctypes not installed")

        # ----- LAN discovery -----
        self._lan: Optional[LANDiscovery] = None
        if lan_instruments or lan_mdns_enabled:
            try:
                self._lan = LANDiscovery(
                    static_list=lan_instruments,
                    default_port=lan_default_port,
                    default_protocol=lan_default_protocol,
                    mdns_enabled=lan_mdns_enabled,
                    mdns_timeout=lan_mdns_timeout,
                    probe_timeout=lan_probe_timeout,
                )
                logger.info("LAN instrument discovery enabled")
            except Exception as exc:
                logger.warning("LAN discovery initialisation failed: %s", exc)

        # ----- Raw USB transport -----
        self._usb: Optional[USBTransport] = None
        if usb_raw_enabled and USB_AVAILABLE:
            try:
                self._usb = USBTransport()
                logger.info("Raw USB transport enabled")
            except Exception as exc:
                logger.warning("USB transport initialisation failed: %s", exc)
        elif usb_raw_enabled and not USB_AVAILABLE:
            logger.warning("USB transport enabled but pyusb not installed")

    # ------------------------------------------------------------------
    # Availability properties
    # ------------------------------------------------------------------

    @property
    def gpib_available(self) -> bool:
        """True when GPIBManager is initialised and has boards."""
        return self._gpib is not None and self._gpib.is_available

    @property
    def usb_available(self) -> bool:
        """True when USBTransport is initialised and pyusb is present."""
        return self._usb is not None and self._usb.is_available

    @property
    def lan_available(self) -> bool:
        """True when LANDiscovery is initialised."""
        return self._lan is not None

    @property
    def visa_available(self) -> bool:
        """True when PyVISA resource manager is initialised."""
        return self._rm is not None

    # ------------------------------------------------------------------
    # Resource listing
    # ------------------------------------------------------------------

    def list_resources(self) -> tuple[str, ...]:
        """Aggregate resources from all enabled backends.

        Returns a deduplicated tuple of VISA resource strings. GPIB
        resources are listed first (most reliable via linux-gpib),
        then VISA, then LAN, then raw USB.
        """
        resources: list[str] = []

        # 1. GPIB resources (preferred for GPIB instruments)
        if self._gpib and self._gpib.is_available:
            gpib_resources = self._gpib.list_resources()
            resources.extend(gpib_resources)
            logger.debug("Found %d GPIB resource(s)", len(gpib_resources))

        # 2. PyVISA resources (excluding GPIB and serial if configured)
        # When USBTransport is active, skip VISA listing entirely to
        # avoid control-transfer side effects on vendor-specific devices.
        if self._rm is not None and self._usb is None:
            try:
                # When GPIBManager handles GPIB, tell PyVISA to skip the
                # GPIB bus entirely.  This prevents pyvisa-py from calling
                # into linux-gpib C library concurrently (not thread-safe).
                query = "(USB|TCPIP|ASRL)?*" if self._gpib else "?*"
                with _suppress_native_stderr():
                    visa_resources = list(self._rm.list_resources(query))
                for res in visa_resources:
                    # Optionally skip serial ports
                    if res.startswith("ASRL") and not self._include_serial_ports:
                        continue
                    resources.append(res)
            except Exception as exc:
                logger.warning("VISA resource listing failed: %s", exc)

        # 3. LAN resources
        if self._lan:
            existing = set(resources)
            for res in self._lan.discover():
                if res not in existing:
                    resources.append(res)
                    existing.add(res)
            logger.debug("After LAN discovery: %d total resource(s)", len(resources))

        # 4. Raw USB resources
        if self._usb and self._usb.is_available:
            existing = set(resources)
            for res in self._usb.discover():
                if res not in existing:
                    resources.append(res)
                    existing.add(res)
            logger.debug("After USB discovery: %d total resource(s)", len(resources))

        return tuple(resources)

    # ------------------------------------------------------------------
    # Discovery with retry
    # ------------------------------------------------------------------

    def discover_resources(
        self,
        max_attempts: int = 5,
        initial_delay: float = 2.0,
        backoff_factor: float = 2.0,
    ) -> tuple[str, ...]:
        """Discover resources with exponential-backoff retry.

        On systemd restarts the previous daemon may still hold USB/GPIB
        locks.  This method retries ``list_resources()`` to wait for
        resource release.
        """
        delay = initial_delay

        for attempt in range(1, max_attempts + 1):
            resources = self.list_resources()
            if resources:
                if attempt > 1:
                    logger.info(
                        "Resource discovery succeeded on attempt %d: "
                        "found %d resource(s)",
                        attempt,
                        len(resources),
                    )
                return resources

            if attempt < max_attempts:
                logger.warning(
                    "No resources found (attempt %d/%d), retrying in %.1fs…",
                    attempt,
                    max_attempts,
                    delay,
                )
                time.sleep(delay)
                delay *= backoff_factor
            else:
                logger.warning(
                    "No resources found after %d attempts. "
                    "Instruments may not be connected or powered on.",
                    max_attempts,
                )

        return ()

    # ------------------------------------------------------------------
    # Rescan
    # ------------------------------------------------------------------

    def rescan_all(self) -> tuple[str, ...]:
        """Rescan all backends, including a fresh GPIB bus scan.

        Unlike ``list_resources()``, this also rescans the GPIB bus
        for newly connected instruments.
        """
        if self._gpib and self._gpib.is_available:
            self._gpib.scan_all_boards()
            logger.info("GPIB buses rescanned")
        return self.list_resources()

    def rescan_gpib(self) -> list[str]:
        """Rescan all GPIB buses and return newly found addresses."""
        if self._gpib and self._gpib.is_available:
            return self._gpib.scan_all_boards()
        return []

    # ------------------------------------------------------------------
    # Type detection helpers
    # ------------------------------------------------------------------

    def _is_gpib(self, visa_address: str) -> bool:
        return (
            self._gpib is not None
            and self._gpib.is_available
            and self._gpib.is_gpib_address(visa_address)
        )

    def _is_usb(self, visa_address: str) -> bool:
        return (
            self._usb is not None
            and self._usb.is_available
            and self._usb.is_usb_resource(visa_address)
        )

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def connect(
        self,
        visa_address: str,
        timeout: int = 5000,
        max_attempts: int = 1,
        retry_delay: float = 2.0,
        serial_config: Optional[object] = None,
    ) -> Optional[str]:
        """Connect to an instrument by VISA address.

        Routes to the appropriate backend based on address format.

        Parameters
        ----------
        visa_address:
            VISA resource string.
        timeout:
            Communication timeout in milliseconds.
        max_attempts:
            Number of connection attempts (for busy VISA resources).
        retry_delay:
            Seconds between retry attempts.
        serial_config:
            Optional ``InterfaceConfig`` with serial settings (baud_rate,
            parity, data_bits, stop_bits) to apply after opening an
            ``ASRL`` resource.

        Returns
        -------
        str or None
            The instrument ID (VISA address) on success, None on failure.
        """
        # --- GPIB ---
        if self._is_gpib(visa_address):
            try:
                if self._gpib.connect(visa_address, timeout_ms=timeout):
                    logger.info("Connected to GPIB instrument: %s", visa_address)
                    return visa_address
                return None
            except Exception as exc:
                logger.error("GPIB connect failed for %s: %s", visa_address, exc)
                return None

        # --- Raw USB ---
        if self._is_usb(visa_address):
            try:
                if self._usb.connect(visa_address, timeout_ms=timeout):
                    logger.info("Connected to USB instrument: %s", visa_address)
                    return visa_address
                return None
            except Exception as exc:
                logger.error("USB connect failed for %s: %s", visa_address, exc)
                return None

        # --- PyVISA (TCPIP, USB-TMC, Serial) ---
        if self._rm is None:
            logger.error("PyVISA not available, cannot connect to %s", visa_address)
            return None

        if visa_address in self._instruments:
            return visa_address

        for attempt in range(1, max_attempts + 1):
            try:
                instrument = self._rm.open_resource(visa_address)
                instrument.timeout = timeout
                # Raw TCPIP SOCKET connections need explicit termination
                if is_tcpip_socket_resource(visa_address):
                    instrument.read_termination = "\n"
                    instrument.write_termination = "\n"
                # Apply serial settings for ASRL resources
                if visa_address.startswith("ASRL") and serial_config is not None:
                    self._apply_serial_settings(instrument, serial_config)
                self._instruments[visa_address] = instrument
                logger.info("Connected to VISA instrument: %s", visa_address)
                return visa_address
            except Exception as exc:
                if attempt < max_attempts:
                    logger.warning(
                        "Connect to %s failed (attempt %d/%d): %s. "
                        "Retrying in %.1fs…",
                        visa_address,
                        attempt,
                        max_attempts,
                        exc,
                        retry_delay,
                    )
                    time.sleep(retry_delay)
                else:
                    logger.error("Failed to connect to %s: %s", visa_address, exc)

        return None

    @staticmethod
    def _apply_serial_settings(resource: object, serial_config: object) -> None:
        """Apply serial communication settings to a VISA resource.

        Parameters
        ----------
        resource:
            An opened ``pyvisa.Resource`` for an ASRL address.
        serial_config:
            An ``InterfaceConfig`` instance with optional serial fields
            (baud_rate, parity, data_bits, stop_bits).
        """
        if getattr(serial_config, "baud_rate", None) is not None:
            resource.baud_rate = serial_config.baud_rate
            logger.debug("Serial baud_rate set to %d", serial_config.baud_rate)

        parity_val = getattr(serial_config, "parity", None)
        if parity_val is not None:
            try:
                import pyvisa.constants as vi_const
                parity_map = {
                    "none": vi_const.Parity.none,
                    "even": vi_const.Parity.even,
                    "odd": vi_const.Parity.odd,
                }
                mapped = parity_map.get(parity_val.lower())
                if mapped is not None:
                    resource.parity = mapped
                    logger.debug("Serial parity set to %s", parity_val)
                else:
                    logger.warning("Unknown parity value: %s", parity_val)
            except ImportError:
                logger.warning("pyvisa.constants not available; cannot set parity")

        if getattr(serial_config, "data_bits", None) is not None:
            resource.data_bits = serial_config.data_bits
            logger.debug("Serial data_bits set to %d", serial_config.data_bits)

        stop_val = getattr(serial_config, "stop_bits", None)
        if stop_val is not None:
            try:
                import pyvisa.constants as vi_const
                stop_map = {
                    1: vi_const.StopBits.one,
                    1.5: vi_const.StopBits.one_and_a_half,
                    2: vi_const.StopBits.two,
                }
                mapped = stop_map.get(stop_val)
                if mapped is not None:
                    resource.stop_bits = mapped
                    logger.debug("Serial stop_bits set to %s", stop_val)
                else:
                    logger.warning("Unknown stop_bits value: %s", stop_val)
            except ImportError:
                logger.warning("pyvisa.constants not available; cannot set stop_bits")

    def disconnect(self, instrument_id: str) -> None:
        """Disconnect from an instrument."""
        # GPIB
        if self._is_gpib(instrument_id):
            self._gpib.disconnect(instrument_id)
            logger.info("Disconnected from GPIB instrument: %s", instrument_id)
            return

        # Raw USB
        if self._is_usb(instrument_id):
            self._usb.disconnect(instrument_id)
            logger.info("Disconnected from USB instrument: %s", instrument_id)
            return

        # PyVISA
        if instrument_id in self._instruments:
            try:
                self._instruments[instrument_id].close()
            except Exception:
                pass
            del self._instruments[instrument_id]
            logger.info("Disconnected from VISA instrument: %s", instrument_id)

    def disconnect_all(self) -> None:
        """Disconnect from every connected instrument across all backends."""
        if self._gpib:
            self._gpib.disconnect_all()
        if self._usb:
            self._usb.disconnect_all()
        for instrument_id in list(self._instruments):
            self.disconnect(instrument_id)

    # ------------------------------------------------------------------
    # Connection state
    # ------------------------------------------------------------------

    def is_connected(self, instrument_id: str) -> bool:
        """Return True if *instrument_id* has an active connection."""
        if self._is_gpib(instrument_id):
            return self._gpib.is_connected(instrument_id)
        if self._is_usb(instrument_id):
            return self._usb.is_connected(instrument_id)
        return instrument_id in self._instruments

    def get_instrument(
        self, instrument_id: str
    ) -> Optional[Union[object, "GPIBManager", "USBTransport"]]:
        """Return the underlying transport or resource for *instrument_id*.

        For GPIB instruments, returns the GPIBManager instance.
        For USB instruments, returns the USBTransport instance.
        For VISA instruments, returns the ``pyvisa.Resource``.
        """
        if self._is_gpib(instrument_id):
            if self._gpib.is_connected(instrument_id):
                return self._gpib
            return None
        if self._is_usb(instrument_id):
            if self._usb.is_connected(instrument_id):
                return self._usb
            return None
        return self._instruments.get(instrument_id)

    def canonical_id(self, instrument_id: str) -> str:
        """Return the canonical resource ID for an instrument.

        Raw USB devices may be discovered with a placeholder serial
        (``"0"``); after connection the real serial is known.
        """
        if self._is_usb(instrument_id):
            key = self._usb._resolve(instrument_id)
            return key if key else instrument_id
        return instrument_id

    # ------------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------------

    def query(self, instrument_id: str, command: str) -> str:
        """Send a query command and return the stripped response.

        Raises
        ------
        ValueError
            If the instrument is not connected.
        IOError / pyvisa.Error
            On communication failure.
        """
        if self._is_gpib(instrument_id):
            if not self._gpib.is_connected(instrument_id):
                raise ValueError(f"Instrument not connected: {instrument_id}")
            return self._gpib.query(instrument_id, command)

        if self._is_usb(instrument_id):
            if not self._usb.is_connected(instrument_id):
                raise ValueError(f"Instrument not connected: {instrument_id}")
            return self._usb.query(instrument_id, command)

        instrument = self._instruments.get(instrument_id)
        if instrument is None:
            raise ValueError(f"Instrument not connected: {instrument_id}")
        return instrument.query(command).strip()

    def write(self, instrument_id: str, command: str) -> None:
        """Send a write command (no response expected).

        Raises
        ------
        ValueError
            If the instrument is not connected.
        IOError / pyvisa.Error
            On communication failure.
        """
        if self._is_gpib(instrument_id):
            if not self._gpib.is_connected(instrument_id):
                raise ValueError(f"Instrument not connected: {instrument_id}")
            self._gpib.write(instrument_id, command)
            return

        if self._is_usb(instrument_id):
            if not self._usb.is_connected(instrument_id):
                raise ValueError(f"Instrument not connected: {instrument_id}")
            self._usb.write(instrument_id, command)
            return

        instrument = self._instruments.get(instrument_id)
        if instrument is None:
            raise ValueError(f"Instrument not connected: {instrument_id}")
        instrument.write(command)

    def read(self, instrument_id: str) -> str:
        """Read a response from a previously written command.

        Raises
        ------
        ValueError
            If the instrument is not connected or backend does not
            support standalone reads.
        """
        if self._is_gpib(instrument_id):
            if not self._gpib.is_connected(instrument_id):
                raise ValueError(f"Instrument not connected: {instrument_id}")
            return self._gpib.read(instrument_id)

        if self._is_usb(instrument_id):
            if not self._usb.is_connected(instrument_id):
                raise ValueError(f"Instrument not connected: {instrument_id}")
            return self._usb.read(instrument_id)

        instrument = self._instruments.get(instrument_id)
        if instrument is None:
            raise ValueError(f"Instrument not connected: {instrument_id}")
        return instrument.read().strip()

    def identify(self, instrument_id: str) -> str:
        """Query instrument identification.

        GPIB and USB transports cache the *IDN?/ID response — this
        method returns the cached value when available.
        """
        if self._is_gpib(instrument_id):
            return self._gpib.identify(instrument_id)
        if self._is_usb(instrument_id):
            return self._usb.identify(instrument_id)
        return self.query(instrument_id, "*IDN?")

    def query_binary_values(
        self,
        instrument_id: str,
        command: str,
        datatype: str = 'd',  # 'd' = float64, 'f' = float32, 'h' = int16
        is_big_endian: bool = False,
        container: type = list,
        timeout_ms: Optional[int] = None,
    ) -> list:
        """Send a query and read the response as IEEE 488.2 binary block data.

        Uses pyvisa's query_binary_values() which handles the #N header parsing.

        Only supported for PyVISA instruments; GPIB and raw USB instruments
        will raise ValueError.

        Parameters
        ----------
        instrument_id:
            VISA resource string identifying the instrument.
        command:
            SCPI query command (e.g. ``":WAV:DATA?"``).
        datatype:
            Format character for ``struct``: ``'d'`` = float64,
            ``'f'`` = float32, ``'h'`` = int16, etc.
        is_big_endian:
            If True, data is big-endian; otherwise little-endian.
        container:
            Container type for the result (default ``list``).
        timeout_ms:
            Optional per-call timeout override in milliseconds.

        Returns
        -------
        list
            The decoded numeric values.

        Raises
        ------
        ValueError
            If the instrument is not connected or the backend does not
            support binary queries.
        """
        if self._is_gpib(instrument_id):
            raise ValueError(
                f"Binary queries not supported for GPIB instrument: {instrument_id}"
            )
        if self._is_usb(instrument_id):
            raise ValueError(
                f"Binary queries not supported for raw USB instrument: {instrument_id}"
            )

        resource = self._instruments.get(instrument_id)
        if resource is None:
            raise ValueError(f"Instrument not connected: {instrument_id}")

        if timeout_ms is not None:
            original_timeout = resource.timeout
            resource.timeout = timeout_ms

        try:
            return resource.query_binary_values(
                command,
                datatype=datatype,
                is_big_endian=is_big_endian,
                container=container,
            )
        finally:
            if timeout_ms is not None:
                resource.timeout = original_timeout

    def read_binary(self, instrument_id: str, num_bytes: int) -> bytes:
        """Read raw binary data from an instrument.

        Only supported for USB transport instruments (e.g. curve buffer
        downloads via ``DCB n``).

        Raises
        ------
        ValueError
            If the instrument is not connected or does not support
            binary reads.
        """
        if self._is_usb(instrument_id):
            if not self._usb.is_connected(instrument_id):
                raise ValueError(f"Instrument not connected: {instrument_id}")
            return self._usb.read_binary(instrument_id, num_bytes)
        raise ValueError(f"Binary reads not supported for {instrument_id}")

    # ------------------------------------------------------------------
    # GPIB identity probes
    # ------------------------------------------------------------------

    def set_gpib_identity_probes(
        self, probes: list[tuple[bytes, str]]
    ) -> None:
        """Forward non-standard identity probes to the GPIB manager.

        Parameters
        ----------
        probes:
            List of ``(command_bytes, label)`` tuples from profile loader.
        """
        if self._gpib is not None:
            self._gpib.set_identity_probes(probes)
