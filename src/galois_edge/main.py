"""
Edge Daemon main lifecycle module.

Provides the ``EdgeDaemon`` class with async start/stop lifecycle,
background instrument scanning, stdin EOF detection (for Go supervisor
shutdown signal), and signal handling.

Usage::

    from galois_edge.main import main
    main()
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import signal
import socket
import sys
import uuid
from typing import Any, Optional

from .config import Config, load_config
from .command_handler import CommandHandler
from .grpc_server import GRPCServer
from .instrument_manager import InstrumentManager
from .capability_manager import CapabilityManager
from .sdk_executor import SDKExecutor
from .ws_server import WebSocketServer

try:
    from .mcp import MCPServer
except ImportError:
    MCPServer = None  # type: ignore[assignment,misc]

# Protocol driver registry (Modbus, etc.)
try:
    from .drivers.registry import DriverRegistry
except ImportError:
    DriverRegistry = None  # type: ignore[assignment,misc]

# Profile loader uses yaml, which is optional
try:
    from .profile_loader import ProfileLoader
except ImportError:
    ProfileLoader = None  # type: ignore[assignment,misc]

# Trickle scanner (always available -- no external deps)
from .trickle_scanner import TrickleScanScheduler

# USB hotplug monitor (optional -- requires pyudev, Linux only)
try:
    from .usb_monitor import USBMonitor, PYUDEV_AVAILABLE
except ImportError:
    USBMonitor = None  # type: ignore[assignment,misc]
    PYUDEV_AVAILABLE = False

logger = logging.getLogger(__name__)


class _DemoInstrumentManagerProxy:
    """Composite proxy that delegates to the sim manager for virtual
    addresses and to the real InstrumentManager for everything else.

    Implements the same duck-typed interface that CommandHandler and
    GRPCServer rely on, so neither needs any changes.
    """

    def __init__(self, real: "InstrumentManager", sim: object) -> None:
        self._real = real
        self._sim = sim
        self._sim_addrs: set[str] = set(sim.list_resources())

    def _manager_for(self, instrument_id: str) -> object:
        return self._sim if instrument_id in self._sim_addrs else self._real

    # -- Resource listing (merge both) --
    def list_resources(self) -> tuple[str, ...]:
        return tuple(list(self._real.list_resources()) + list(self._sim.list_resources()))

    def discover_resources(self, *a, **kw) -> tuple[str, ...]:
        return self.list_resources()

    def rescan_all(self) -> tuple[str, ...]:
        real = self._real.rescan_all()
        return tuple(list(real) + list(self._sim.list_resources()))

    def rescan_gpib(self) -> list[str]:
        return self._real.rescan_gpib()

    # -- Properties --
    @property
    def gpib_available(self) -> bool:
        return self._real.gpib_available

    @property
    def usb_available(self) -> bool:
        return self._real.usb_available

    @property
    def lan_available(self) -> bool:
        return True

    @property
    def visa_available(self) -> bool:
        return True

    # -- Connection --
    def connect(self, visa_address: str, **kw):
        return self._manager_for(visa_address).connect(visa_address, **kw)

    def disconnect(self, instrument_id: str) -> None:
        self._manager_for(instrument_id).disconnect(instrument_id)

    def disconnect_all(self) -> None:
        self._real.disconnect_all()
        self._sim.disconnect_all()

    def is_connected(self, instrument_id: str) -> bool:
        return self._manager_for(instrument_id).is_connected(instrument_id)

    def canonical_id(self, instrument_id: str) -> str:
        return self._manager_for(instrument_id).canonical_id(instrument_id)

    def mark_absent(self, visa_address: str) -> None:
        self._manager_for(visa_address).mark_absent(visa_address)

    def get_instrument(self, instrument_id: str):
        return self._manager_for(instrument_id).get_instrument(instrument_id)

    # -- I/O --
    def query(self, instrument_id: str, command: str) -> str:
        return self._manager_for(instrument_id).query(instrument_id, command)

    def write(self, instrument_id: str, command: str) -> None:
        self._manager_for(instrument_id).write(instrument_id, command)

    def read(self, instrument_id: str) -> str:
        return self._manager_for(instrument_id).read(instrument_id)

    def identify(self, instrument_id: str) -> str:
        return self._manager_for(instrument_id).identify(instrument_id)

    def query_binary_values(self, instrument_id: str, command: str, **kw):
        return self._manager_for(instrument_id).query_binary_values(instrument_id, command, **kw)

    def set_gpib_identity_probes(self, probes: list) -> None:
        self._real.set_gpib_identity_probes(probes)


class EdgeDaemon:
    """Main daemon process orchestrating all subsystems.

    Lifecycle::

        daemon = EdgeDaemon()
        await daemon.start()   # blocks until shutdown
        # ... or call daemon.stop() from a signal handler / stdin watcher

    The daemon manages:
      - InstrumentManager (PyVISA, GPIB, USB, LAN)
      - ProfileLoader + CapabilityManager (YAML profiles)
      - CommandHandler (SCPI dispatch)
      - SDKExecutor (vendor SDK dispatch)
      - GRPCServer (async gRPC service)
      - WebSocketServer (aiohttp streaming)
      - Background rescan task (periodic instrument rediscovery)
      - Stdin watcher (EOF triggers graceful shutdown)
    """

    def __init__(self, cfg: Optional[Config] = None) -> None:
        self._cfg = cfg or load_config()
        self._running = False

        # Edge identity
        self._edge_id = socket.gethostname() + "-" + uuid.uuid4().hex[:8]

        # Single-thread executor for all instrument I/O.
        # linux-gpib is NOT thread-safe — all PyVISA/GPIB calls must be
        # serialized through one thread to prevent SIGABRT.
        self._io_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="instrument-io"
        )

        # Subsystem references (populated during start)
        self._instrument_manager: Optional[InstrumentManager] = None
        self._profile_loader: Optional[object] = None
        self._capability_manager: Optional[CapabilityManager] = None
        self._command_handler: Optional[CommandHandler] = None
        self._sdk_executor: Optional[SDKExecutor] = None
        self._grpc_server: Optional[GRPCServer] = None
        self._ws_server: Optional[WebSocketServer] = None
        self._mcp_server: Optional[Any] = None
        self._driver_registry: Optional[object] = None  # DriverRegistry or None

        # Background tasks
        self._rescan_task: Optional[asyncio.Task] = None
        self._stdin_task: Optional[asyncio.Task] = None
        self._trickle_task: Optional[asyncio.Task] = None

        # Demo mode (virtual instruments)
        self._sim_manager: Optional[object] = None

        # Discovery subsystems
        self._trickle_scanner: Optional[TrickleScanScheduler] = None
        self._usb_monitor: Optional[object] = None  # USBMonitor or None

    @property
    def running(self) -> bool:
        return self._running

    @property
    def edge_id(self) -> str:
        return self._edge_id

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialise all subsystems and start servers.

        This method blocks until shutdown is triggered (via signal,
        stdin EOF, or explicit ``stop()`` call).
        """
        self._running = True
        _configure_logging(self._cfg.log_level)

        logger.info("Edge daemon starting (edge_id=%s)", self._edge_id)
        if self._cfg.demo:
            logger.info("DEMO MODE enabled — virtual instruments will be registered")
        logger.info(
            "Config: grpc_port=%d, ws_port=%d, scan_interval=%ds",
            self._cfg.grpc_port,
            self._cfg.ws_port,
            self._cfg.scan_interval_s,
        )

        # 0. Surface Pi-specific UART issues before anything else touches /dev/serial0.
        #    No-op on non-Pi hosts.
        try:
            from galois_edge.pi_diagnostics import log_diagnostics
            log_diagnostics()
        except Exception as exc:  # diagnostics must never block startup
            logger.debug("Pi diagnostics skipped: %s", exc)

        # 1. Initialise instrument manager (no scanning yet)
        real_manager = InstrumentManager(
            gpib_enabled=self._cfg.gpib_enabled,
            gpib_scan_on_init=False,  # defer GPIB scan to background
            lan_instruments=self._cfg.lan_instruments,
            include_serial_ports=self._cfg.include_serial_ports,
        )

        # 1b. In demo mode, wrap with a proxy that also routes to virtual instruments
        if self._cfg.demo:
            try:
                from contrib.simulation.engine import SimulatedInstrumentManager
                self._sim_manager = SimulatedInstrumentManager()
                self._instrument_manager = _DemoInstrumentManagerProxy(real_manager, self._sim_manager)
                logger.info("Demo mode: SimulatedInstrumentManager active with %d virtual instruments",
                            len(self._sim_manager.list_resources()))
            except ImportError:
                logger.warning("Demo mode requested but contrib.simulation not available — running without virtual instruments")
                self._instrument_manager = real_manager
        else:
            self._instrument_manager = real_manager

        # 2. Initialise SDK executor, command handler, capability manager
        #    (all empty until background discovery runs)
        self._sdk_executor = SDKExecutor(self._instrument_manager)
        self._capability_manager = CapabilityManager()
        self._command_handler = CommandHandler(self._instrument_manager)

        # 2b. Initialise protocol driver registry (Modbus, etc.)
        if DriverRegistry is not None:
            self._driver_registry = DriverRegistry(self._cfg.driver_profile_dir)
            profile_count = self._driver_registry.discover()
            if profile_count:
                logger.info("Discovered %d protocol driver profile(s)", profile_count)
        else:
            self._driver_registry = None

        # 3. Start gRPC server FIRST (so Go supervisor health check passes immediately)
        #    Instrument discovery + profile matching happen in background AFTER this.
        self._grpc_server = GRPCServer(
            instrument_manager=self._instrument_manager,
            command_handler=self._command_handler,
            edge_id=self._edge_id,
            port=self._cfg.grpc_port,
            max_workers=self._cfg.grpc_max_workers,
            capability_manager=self._capability_manager,
            sdk_executor=self._sdk_executor,
            io_executor=self._io_executor,
            driver_registry=self._driver_registry,
            inbound_auth_token=self._cfg.inbound_auth_token,
        )
        if not await self._grpc_server.start():
            logger.error("Failed to start gRPC server -- aborting")
            return

        # 5. Start WebSocket server
        self._ws_server = WebSocketServer(
            instrument_manager=self._instrument_manager,
            command_handler=self._command_handler,
            port=self._cfg.ws_port,
        )
        try:
            await self._ws_server.start()
        except Exception as exc:
            logger.warning("WebSocket server failed to start: %s", exc)

        # 5b. Start MCP server (Phase 1: tailnet-direct, no relay).
        #     Phase 3: pass the SDK executor so per-SDK typed tools are
        #     emitted alongside the per-instrument dynamic tools.
        if self._cfg.mcp_enabled and MCPServer is not None:
            try:
                self._mcp_server = MCPServer(
                    capability_manager=self._capability_manager,
                    command_handler=self._command_handler,
                    instrument_manager=self._instrument_manager,
                    port=self._cfg.mcp_port,
                    path=self._cfg.mcp_path,
                    edge_id=self._edge_id,
                    edge_name=socket.gethostname(),
                    sdk_executor=self._sdk_executor,
                )
                await self._mcp_server.start()
            except Exception as exc:
                logger.warning("MCP server failed to start: %s", exc)
                self._mcp_server = None

        # 4. Start USB hotplug monitor (if available and enabled)
        if (
            self._cfg.usb_monitor_enabled
            and PYUDEV_AVAILABLE
            and USBMonitor is not None
        ):
            try:
                self._usb_monitor = USBMonitor()
                self._usb_monitor.on_gpib_adapter_added = self._on_gpib_adapter_added
                self._usb_monitor.on_gpib_adapter_removed = self._on_gpib_adapter_removed
                self._usb_monitor.on_usbtmc_added = self._on_usbtmc_added
                self._usb_monitor.on_usbtmc_removed = self._on_usbtmc_removed
                self._usb_monitor.start(asyncio.get_running_loop())
                logger.info("USB hotplug monitor started")
            except Exception as exc:
                logger.warning("USB hotplug monitor failed to start: %s", exc)
                self._usb_monitor = None
        else:
            if not PYUDEV_AVAILABLE:
                logger.info(
                    "USB hotplug monitor not available (pyudev not installed)"
                )

        # 6. Load profiles + match instruments in background (slow -- YAML I/O + instrument connect)
        asyncio.create_task(self._background_profile_match())

        # 7. Run initial GPIB scan + start trickle scanner
        asyncio.create_task(self._initial_gpib_scan_then_trickle())

        # 9. Start periodic reconciliation task
        self._rescan_task = asyncio.create_task(
            self._periodic_reconcile()
        )

        # 10. Start stdin watcher (Go supervisor shutdown)
        self._stdin_task = asyncio.create_task(self._watch_stdin())

        logger.info("Edge daemon is running. Waiting for shutdown signal.")

        # Block until the gRPC server terminates
        if self._grpc_server is not None:
            await self._grpc_server.wait_for_termination()

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def stop(self) -> None:
        """Graceful shutdown in reverse startup order."""
        if not self._running:
            return

        self._running = False
        logger.info("Edge daemon shutting down...")

        # 1. Stop trickle scanner
        if self._trickle_scanner is not None:
            await self._trickle_scanner.stop()

        # 1b. Stop USB hotplug monitor
        if self._usb_monitor is not None:
            try:
                self._usb_monitor.stop()
            except Exception:
                pass

        # 1c. Cancel background tasks
        for task in (self._rescan_task, self._stdin_task, self._trickle_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # 2. Stop WebSocket server
        if self._ws_server is not None:
            await self._ws_server.stop()

        # 2b. Stop MCP server
        if self._mcp_server is not None:
            try:
                await self._mcp_server.stop()
            except Exception as exc:
                logger.warning("MCP server stop error: %s", exc)

        # 3. Stop gRPC server
        if self._grpc_server is not None:
            await self._grpc_server.stop()

        # 4. Send cleanup commands for all registered instruments
        if self._capability_manager and self._command_handler:
            for inst_id, caps in self._capability_manager.all_instruments.items():
                if caps.has_profile and caps.profile.settings.cleanup_commands:
                    for cmd in caps.profile.settings.cleanup_commands:
                        try:
                            self._command_handler.execute_command(
                                cmd, inst_id,
                                timeout_ms=caps.profile.settings.timeout_ms,
                            )
                        except Exception:
                            pass  # Best-effort on shutdown

        # 5. Disconnect SDK clients
        if self._sdk_executor is not None:
            self._sdk_executor.disconnect_all()

        # 6. Disconnect all instruments
        if self._instrument_manager is not None:
            self._instrument_manager.disconnect_all()

        # 7. Shut down the instrument I/O executor
        self._io_executor.shutdown(wait=False)

        logger.info("Edge daemon stopped.")

    # ------------------------------------------------------------------
    # Profile loading
    # ------------------------------------------------------------------

    def _load_profiles(self) -> None:
        """Load YAML profiles and match to currently connected instruments."""
        if ProfileLoader is None:
            logger.info(
                "Profile loader not available (pyyaml not installed)"
            )
            return

        try:
            loader = ProfileLoader(self._cfg.profile_dir)
            count = loader.load_all()
            logger.info("Loaded %d instrument profile(s)", count)
            self._profile_loader = loader
        except Exception as exc:
            logger.warning("Profile loading failed: %s", exc)
            return

        if self._instrument_manager is None or self._capability_manager is None:
            return

        # Inject identity probes for GPIB instruments
        probes = loader.get_identity_probes()
        if probes:
            self._instrument_manager.set_gpib_identity_probes(probes)
            logger.info(
                "Configured %d non-standard identity probe(s)", len(probes)
            )

        # Profile matching deferred to _background_profile_match (after gRPC starts)

    def _discover_serial_sdk_instruments(self) -> set[str]:
        """Discover SDK instruments on USB-serial ports via VID/PID matching.

        Scans system serial ports with pyserial and matches against profiles
        that declare usb_vid/usb_pid in their interface config. Instruments
        matched this way bypass VISA entirely — they connect through the
        SDKExecutor using the raw serial port path.

        Returns the set of claimed serial port paths (e.g. ``/dev/ttyACM0``)
        so the VISA discovery loop can skip them.
        """
        claimed: set[str] = set()

        if self._profile_loader is None or self._capability_manager is None:
            return claimed
        if self._sdk_executor is None:
            return claimed

        try:
            from serial.tools import list_ports
        except ImportError:
            logger.debug("pyserial not available — skipping serial SDK discovery")
            return claimed

        # Build a lookup: (vid, pid) → (profile, interface_config)
        vid_pid_profiles: list[tuple[int, int, object, object]] = []
        profiles = getattr(self._profile_loader, "profiles", {})
        for profile in profiles.values():
            if not profile.sdk or not profile.sdk.import_path:
                continue
            for iface in profile.interfaces:
                if iface.usb_vid and iface.usb_pid:
                    try:
                        vid = int(iface.usb_vid, 16)
                        pid = int(iface.usb_pid, 16)
                        vid_pid_profiles.append((vid, pid, profile, iface))
                    except ValueError:
                        pass

        if not vid_pid_profiles:
            return claimed

        # Scan serial ports
        for port_info in list_ports.comports():
            if port_info.vid is None or port_info.pid is None:
                continue
            for vid, pid, profile, iface in vid_pid_profiles:
                if port_info.vid == vid and port_info.pid == pid:
                    port_path = port_info.device
                    claimed.add(port_path)

                    # Skip if already registered
                    if self._capability_manager.get_instrument_caps(port_path):
                        continue

                    logger.info(
                        "Serial SDK discovery: %s matched profile %s (VID=%04X PID=%04X)",
                        port_path, profile.profile_key, vid, pid,
                    )

                    # Connect via SDK
                    runtime_args = {"address": port_path}
                    ok = self._sdk_executor.connect(port_path, profile.sdk, runtime_args)
                    if not ok:
                        logger.warning("SDK connect failed for %s", port_path)
                        continue

                    # Get identity from SDK
                    idn = self._sdk_executor.identify(port_path, profile.sdk)

                    # Register the instrument
                    self._capability_manager.register_instrument(
                        instrument_id=port_path,
                        visa_address=port_path,
                        idn_response=idn or "",
                        profile=profile,
                    )
                    logger.info(
                        "Registered SDK instrument: %s -> %s (IDN: %s)",
                        port_path, profile.profile_key, idn,
                    )

        return claimed

    def _discover_dwf_instruments(self) -> None:
        """Discover Digilent WaveForms instruments via DWF device enumeration.

        Unlike serial SDK discovery (VID/PID matching via pyserial), Digilent
        devices use FTDI D2XX (not serial) and are enumerated through libdwf.
        The ftdi_sio kernel driver is auto-unbound by the Adept Runtime's
        udev rules, so these devices don't appear as serial ports.
        """
        if (
            self._profile_loader is None
            or self._capability_manager is None
            or self._sdk_executor is None
        ):
            return

        # Find a profile whose SDK import_path references the DWF wrapper
        dwf_profile = None
        profiles = getattr(self._profile_loader, "profiles", {})
        for profile in profiles.values():
            if (
                profile.sdk
                and profile.sdk.import_path
                and "digilent_dwf_wrapper" in profile.sdk.import_path
            ):
                dwf_profile = profile
                break

        if dwf_profile is None:
            return  # No Digilent profile loaded — nothing to scan

        # Enumerate devices via libdwf ctypes (lightweight, no full dwfpy open)
        try:
            import ctypes

            try:
                libdwf = ctypes.cdll.LoadLibrary("libdwf.so")
            except OSError:
                # Try macOS / alternative names
                try:
                    libdwf = ctypes.cdll.LoadLibrary("libdwf.dylib")
                except OSError:
                    logger.debug(
                        "libdwf not installed — skipping DWF discovery"
                    )
                    return

            count = ctypes.c_int()
            libdwf.FDwfEnum(0, ctypes.byref(count))

            if count.value == 0:
                return

            logger.info(
                "DWF enumeration found %d device(s)", count.value
            )

            for i in range(count.value):
                try:
                    serial_buf = ctypes.create_string_buffer(64)
                    name_buf = ctypes.create_string_buffer(64)
                    libdwf.FDwfEnumSN(i, serial_buf)
                    libdwf.FDwfEnumDeviceName(i, name_buf)

                    sn = serial_buf.value.decode().replace("SN:", "")
                    dev_name = name_buf.value.decode()

                    if not sn:
                        logger.debug("DWF device %d has no serial number, skipping", i)
                        continue

                    inst_id = f"DWF:{sn}"

                    # Skip if already registered
                    if self._capability_manager.get_instrument_caps(inst_id):
                        continue

                    logger.info(
                        "DWF discovery: %s (%s) SN=%s",
                        dev_name, inst_id, sn,
                    )

                    # Connect via SDK executor
                    runtime_args = {"serial_number": sn}
                    ok = self._sdk_executor.connect(
                        inst_id, dwf_profile.sdk, runtime_args
                    )
                    if not ok:
                        logger.warning("SDK connect failed for DWF %s", inst_id)
                        continue

                    # Get identity from SDK
                    idn = self._sdk_executor.identify(inst_id, dwf_profile.sdk)

                    # Register the instrument
                    self._capability_manager.register_instrument(
                        instrument_id=inst_id,
                        visa_address=inst_id,
                        idn_response=idn or "",
                        profile=dwf_profile,
                    )
                    logger.info(
                        "Registered DWF instrument: %s -> %s (IDN: %s)",
                        inst_id, dwf_profile.profile_key, idn,
                    )
                except Exception as exc:
                    logger.warning(
                        "DWF discovery failed for device index %d: %s", i, exc
                    )

        except Exception as exc:
            logger.warning("DWF instrument discovery failed: %s", exc)

    def _find_serial_config(self, visa_addr: str):
        """Find a serial InterfaceConfig for an ASRL address from loaded profiles.

        Scans all loaded profiles for a serial interface whose baud_rate
        or other serial fields are set.  Returns the first matching
        InterfaceConfig, or None.
        """
        if not visa_addr.startswith("ASRL"):
            return None
        if self._profile_loader is None:
            return None
        profiles = getattr(self._profile_loader, "profiles", {})
        for profile in profiles.values():
            for iface in profile.interfaces:
                if iface.type == "serial" and iface.baud_rate is not None:
                    return iface
        return None

    def _try_match_profile(self, visa_addr: str) -> None:
        """Attempt to connect, identify, and match a profile."""
        if self._instrument_manager is None:
            return
        if self._capability_manager is None:
            return
        if self._profile_loader is None:
            return

        # Skip resources already registered (e.g. by serial SDK discovery)
        if self._capability_manager.get_instrument_caps(visa_addr):
            return

        try:
            # For ASRL addresses, look up a serial config to apply
            serial_config = self._find_serial_config(visa_addr)
            connected = self._instrument_manager.connect(
                visa_addr, max_attempts=3, retry_delay=2.0,
                serial_config=serial_config,
            )
            if not connected:
                return

            canon = self._instrument_manager.canonical_id(visa_addr)
            idn = self._instrument_manager.identify(canon)

            # Find matching profile
            profile = self._profile_loader.match_instrument(idn) if idn else None

            # Re-apply serial settings from the *matched* profile if it
            # differs from the initial guess (e.g. different baud rate).
            if profile and visa_addr.startswith("ASRL"):
                matched_serial = None
                for iface in profile.interfaces:
                    if iface.type == "serial":
                        matched_serial = iface
                        break
                if matched_serial and matched_serial is not serial_config:
                    resource = self._instrument_manager.get_instrument(canon)
                    if resource is not None:
                        InstrumentManager._apply_serial_settings(
                            resource, matched_serial,
                        )

            self._capability_manager.register_instrument(
                instrument_id=canon,
                visa_address=canon,
                idn_response=idn or "",
                profile=profile,
            )

            # Connect SDK if profile has SDK config
            if profile and profile.sdk and profile.sdk.import_path and self._sdk_executor:
                try:
                    runtime_args = {"address": canon}
                    self._sdk_executor.connect(canon, profile.sdk, runtime_args)
                except Exception as exc:
                    logger.warning("SDK connect failed for %s: %s", canon, exc)

            # Send init commands if profile defines them
            if profile and profile.settings.init_commands and self._command_handler:
                for cmd in profile.settings.init_commands:
                    try:
                        self._command_handler.execute_command(
                            cmd, canon, timeout_ms=profile.settings.timeout_ms
                        )
                    except Exception as exc:
                        logger.warning(
                            "Init command '%s' failed for %s: %s",
                            cmd, canon, exc,
                        )

            if profile is not None:
                logger.info(
                    "Matched %s -> profile %s",
                    canon, profile.profile_key,
                )

        except Exception as exc:
            logger.warning(
                "Could not match profile for %s: %s", visa_addr, exc,
            )

    # ------------------------------------------------------------------
    # Background tasks
    # ------------------------------------------------------------------

    async def _background_profile_match(self) -> None:
        """Load profiles and match to instruments in a background thread.

        Deferred from start() so the gRPC server can listen immediately
        (passing the Go supervisor health check).  Instruments and
        capabilities populate asynchronously.
        """
        if self._instrument_manager is None:
            return

        loop = asyncio.get_running_loop()

        def _discover_and_match():
            # Discover instruments (LAN/USB/serial scan — can be slow)
            resources = self._instrument_manager.list_resources()
            logger.info("Resource discovery: %d instrument(s)", len(resources))
            # Load YAML profiles (slow on SD card — 129 files)
            self._load_profiles()
            if self._profile_loader is not None:
                # Discover serial SDK instruments FIRST by USB VID/PID.
                # These use custom protocols (not SCPI) and must be claimed
                # before the VISA loop tries to send *IDN? to them.
                sdk_claimed_ports = self._discover_serial_sdk_instruments()

                # Discover Digilent WaveForms instruments (FTDI D2XX,
                # not serial — enumerated via libdwf ctypes).
                self._discover_dwf_instruments()

                # Match remaining instruments via VISA / *IDN?
                logger.info("Matching %d resource(s) to profiles...", len(resources))
                for visa_addr in resources:
                    # Skip ASRL resources whose underlying port was claimed
                    # by serial SDK discovery (e.g. ASRL/dev/ttyACM0::INSTR
                    # when /dev/ttyACM0 is a DPS-150)
                    if sdk_claimed_ports and visa_addr.startswith("ASRL"):
                        port = visa_addr.split("::")[0].replace("ASRL", "", 1)
                        if port in sdk_claimed_ports:
                            logger.debug(
                                "Skipping %s — claimed by serial SDK discovery",
                                visa_addr,
                            )
                            continue
                    self._try_match_profile(visa_addr)

        try:
            await loop.run_in_executor(self._io_executor, _discover_and_match)
            logger.info("Background discovery and profile matching complete")
        except Exception as exc:
            logger.warning("Background discovery/matching failed: %s", exc)

        # Register virtual demo instruments (if demo mode enabled)
        if self._cfg.demo:
            await self._register_demo_instruments()

        # Connect configured Modbus / protocol driver instruments
        await self._connect_protocol_drivers()

    async def _register_demo_instruments(self) -> None:
        """Connect virtual instruments and register them with profiles.

        The SimulatedInstrumentManager was already created in start()
        and wrapped in the proxy. This method connects each virtual
        instrument and matches it to a Quantifi profile.
        """
        if self._sim_manager is None:
            return
        if self._capability_manager is None or self._profile_loader is None:
            logger.warning("Demo mode: profile system not ready, skipping virtual instruments")
            return

        PROFILE_OVERRIDES = {
            "TCPIP::192.168.1.10::5025::SOCKET": "quantifi_photonics_laser_1000",
            "TCPIP::192.168.1.11::5025::SOCKET": "quantifi_photonics_switch",
            "TCPIP::192.168.1.12::5025::SOCKET": "quantifi_photonics_voa",
            "TCPIP::192.168.1.13::5025::SOCKET": "quantifi_photonics_power_1400",
            "TCPIP::192.168.1.14::5025::SOCKET": "quantifi_photonics_osa_1000",
        }

        sim = self._sim_manager
        registered = 0
        for addr in sim.list_resources():
            sim.connect(addr)
            idn = sim.identify(addr)

            profile_key = PROFILE_OVERRIDES.get(addr)
            if profile_key:
                # Override key is authoritative — don't fall through to match_instrument
                profile = self._profile_loader.get_profile(profile_key)
                if not profile:
                    logger.error(
                        "Demo: profile override '%s' for %s not found in loader "
                        "(loaded %d profiles). Check profile YAML and cache.",
                        profile_key, addr, self._profile_loader.profile_count,
                    )
            else:
                profile = self._profile_loader.match_instrument(idn) if idn else None

            self._capability_manager.register_instrument(
                instrument_id=addr,
                visa_address=addr,
                idn_response=idn or "",
                profile=profile,
            )
            registered += 1
            if profile:
                logger.info(
                    "Demo: %s -> %s (profile: %s)",
                    addr, idn, profile.profile_key,
                )
            else:
                logger.warning("Demo: %s -> no profile matched for IDN: %s", addr, idn)

        logger.info("Demo mode: registered %d virtual instrument(s)", registered)

    async def _connect_protocol_drivers(self) -> None:
        """Connect Modbus and generic-serial instruments declared in config."""
        if self._driver_registry is None:
            return

        modbus_configs = self._cfg.modbus_instrument_list
        serial_configs = self._cfg.serial_instrument_list
        if not modbus_configs and not serial_configs:
            return

        if modbus_configs:
            logger.info("Connecting %d Modbus instrument(s)...", len(modbus_configs))
        if serial_configs:
            logger.info("Connecting %d generic-serial instrument(s)...", len(serial_configs))

        loop = asyncio.get_running_loop()

        def _connect_all():
            for cfg_entry in modbus_configs:
                profile_name = cfg_entry.get("profile", "")
                inst_id = cfg_entry.get("id", "")
                uri = cfg_entry.get("uri", "")
                slave_id = cfg_entry.get("slave_id", 1)
                if not profile_name or not inst_id or not uri:
                    logger.warning("Skipping incomplete Modbus config: %s", cfg_entry)
                    continue
                try:
                    driver = self._driver_registry.instantiate(
                        profile_name=profile_name,
                        instrument_id=inst_id,
                        transport_uri=uri,
                        slave_id=slave_id,
                    )
                    driver.connect()
                    if self._capability_manager is not None:
                        self._capability_manager.register_protocol_driver(inst_id, driver)
                    logger.info(
                        "Connected Modbus instrument: %s (%s @ %s, slave %d)",
                        inst_id, profile_name, uri, slave_id,
                    )
                except Exception as exc:
                    logger.warning("Failed to connect Modbus instrument %s: %s", inst_id, exc)

            for cfg_entry in serial_configs:
                profile_name = cfg_entry.get("profile", "")
                inst_id = cfg_entry.get("id", "")
                uri = cfg_entry.get("uri", "")
                if not profile_name or not inst_id or not uri:
                    logger.warning("Skipping incomplete serial config: %s", cfg_entry)
                    continue
                try:
                    driver = self._driver_registry.instantiate(
                        profile_name=profile_name,
                        instrument_id=inst_id,
                        transport_uri=uri,
                    )
                    driver.connect()
                    if self._capability_manager is not None:
                        self._capability_manager.register_protocol_driver(inst_id, driver)
                    logger.info(
                        "Connected serial instrument: %s (%s @ %s)",
                        inst_id, profile_name, uri,
                    )
                except Exception as exc:
                    logger.warning("Failed to connect serial instrument %s: %s", inst_id, exc)

        try:
            await loop.run_in_executor(self._io_executor, _connect_all)
        except Exception as exc:
            logger.warning("Protocol driver connection failed: %s", exc)

    async def _initial_gpib_scan_then_trickle(self) -> None:
        """Run a one-time full GPIB scan, then start the trickle scanner.

        The initial scan finds instruments already powered on at boot
        (fast -- most addresses have no listener). After it completes,
        the trickle scanner maintains continuous background coverage
        for newly powered-on instruments.
        """
        if self._instrument_manager is None:
            return
        if not self._instrument_manager.gpib_available:
            return

        logger.info("Starting initial GPIB bus scan...")
        loop = asyncio.get_running_loop()

        def _scan_and_match():
            gpib_resources = self._instrument_manager.rescan_gpib()
            if gpib_resources:
                logger.info(
                    "Initial GPIB scan found %d instrument(s)",
                    len(gpib_resources),
                )
                for visa_addr in gpib_resources:
                    self._try_match_profile(visa_addr)
            else:
                logger.info("Initial GPIB scan: no instruments found")

        try:
            await loop.run_in_executor(self._io_executor, _scan_and_match)
        except Exception as exc:
            logger.warning("Initial GPIB scan failed: %s", exc)

        # Start trickle scanner for ongoing discovery
        trickle_interval = self._cfg.gpib_trickle_interval_s
        if trickle_interval <= 0:
            logger.info(
                "GPIB trickle scanning disabled (interval <= 0)"
            )
            return

        if self._instrument_manager._gpib is not None:
            self._trickle_scanner = TrickleScanScheduler(
                gpib_manager=self._instrument_manager._gpib,
                io_executor=self._io_executor,
                interval_s=trickle_interval,
                on_instrument_found=self._on_gpib_instrument_found,
            )
            self._trickle_task = asyncio.create_task(
                self._trickle_scanner.run()
            )
            logger.info(
                "GPIB trickle scanner started (interval=%.1fs)",
                trickle_interval,
            )

    def _on_gpib_instrument_found(self, visa_addr: str) -> None:
        """Callback from trickle scanner when a new GPIB instrument is found.

        Runs profile matching in the I/O executor to identify and
        register the instrument.
        """
        logger.info("Trickle scanner discovered: %s", visa_addr)

        loop = asyncio.get_running_loop()

        async def _match():
            try:
                await loop.run_in_executor(
                    self._io_executor,
                    lambda: self._try_match_profile(visa_addr),
                )
            except Exception as exc:
                logger.warning(
                    "Profile matching failed for trickle-discovered %s: %s",
                    visa_addr, exc,
                )

        asyncio.ensure_future(_match())

    # ------------------------------------------------------------------
    # USB hotplug callbacks
    # ------------------------------------------------------------------

    async def _on_gpib_adapter_added(self, sysfs_path: str) -> None:
        """Handle USB-GPIB adapter plug-in."""
        logger.info("USB-GPIB adapter plugged in: %s", sysfs_path)
        if self._instrument_manager is None:
            return

        loop = asyncio.get_running_loop()

        def _reinit():
            gpib_mgr = self._instrument_manager._gpib
            if gpib_mgr is None:
                return
            # Try to re-init all possible boards (the adapter may claim
            # any board index depending on plug order)
            for idx in range(16):
                if idx not in gpib_mgr.boards:
                    gpib_mgr.reinit_board(idx)

        try:
            await loop.run_in_executor(self._io_executor, _reinit)
        except Exception as exc:
            logger.warning("GPIB board re-init failed: %s", exc)

        # Reset trickle scanner to scan from the beginning
        if self._trickle_scanner is not None:
            self._trickle_scanner.reset()

    async def _on_gpib_adapter_removed(self, sysfs_path: str) -> None:
        """Handle USB-GPIB adapter removal."""
        logger.warning("USB-GPIB adapter unplugged: %s", sysfs_path)
        if self._instrument_manager is None:
            return

        gpib_mgr = self._instrument_manager._gpib
        if gpib_mgr is None:
            return

        # Remove all instruments on all boards that are no longer
        # accessible. We don't know which specific board was unplugged
        # from the sysfs path alone, so we check all boards.
        for board_idx in list(gpib_mgr.boards.keys()):
            removed = gpib_mgr.remove_devices_on_board(board_idx)
            for visa_addr in removed:
                if self._capability_manager:
                    self._capability_manager.unregister_instrument(visa_addr)
                logger.warning(
                    "Instrument removed (adapter unplug): %s", visa_addr
                )

    async def _on_usbtmc_added(self, sysfs_path: str) -> None:
        """Handle USB-TMC device plug-in."""
        logger.info("USB-TMC device plugged in: %s", sysfs_path)
        if self._instrument_manager is None:
            return

        loop = asyncio.get_running_loop()

        def _discover_and_match():
            resources = self._instrument_manager.list_resources()
            for visa_addr in resources:
                if visa_addr.startswith("USB") and not visa_addr.endswith("::RAW"):
                    if (
                        self._capability_manager
                        and not self._capability_manager.get_instrument_caps(visa_addr)
                    ):
                        self._try_match_profile(visa_addr)

        try:
            await loop.run_in_executor(self._io_executor, _discover_and_match)
        except Exception as exc:
            logger.warning("USB-TMC discovery failed: %s", exc)

    async def _on_usbtmc_removed(self, sysfs_path: str) -> None:
        """Handle USB-TMC device removal."""
        logger.warning("USB-TMC device unplugged: %s", sysfs_path)
        # Reconciliation will catch this in the next periodic cycle

    # ------------------------------------------------------------------
    # Periodic reconciliation (Tier 3)
    # ------------------------------------------------------------------

    async def _periodic_reconcile(self) -> None:
        """Periodic reconciliation pass (Tier 3 backstop).

        Every scan_interval_s seconds:
          1. LAN: TCP-probe known LAN endpoints
          2. USB: Verify USB device nodes still exist
          3. State: Reconcile capability_manager with instrument_manager
          4. Diff: Detect new non-GPIB instruments and removed instruments
        """
        interval = self._cfg.scan_interval_s
        if interval <= 0:
            logger.info("Periodic reconciliation disabled (interval <= 0)")
            return

        logger.info("Periodic reconciliation enabled (every %ds)", interval)

        if self._instrument_manager is None:
            return

        loop = asyncio.get_running_loop()
        known: set[str] = set()

        while self._running:
            await asyncio.sleep(interval)

            if not self._running:
                break

            try:
                def _reconcile():
                    nonlocal known
                    current = set(self._instrument_manager.list_resources())

                    new_resources = current - known
                    lost_resources = known - current

                    # Handle new non-GPIB instruments
                    # (GPIB discovery is handled by trickle scanner)
                    for visa_addr in new_resources:
                        if not self._is_gpib_address(visa_addr):
                            logger.info(
                                "Reconciler: new instrument detected: %s",
                                visa_addr,
                            )
                            self._try_match_profile(visa_addr)

                    # Handle removed instruments
                    for visa_addr in lost_resources:
                        logger.warning(
                            "Reconciler: instrument removed: %s", visa_addr
                        )
                        self._instrument_manager.disconnect(visa_addr)
                        if self._capability_manager:
                            self._capability_manager.unregister_instrument(
                                visa_addr
                            )

                    # Re-run serial SDK discovery (catches hot-plugged USB-serial devices)
                    self._discover_serial_sdk_instruments()

                    # Re-run DWF discovery (catches hot-plugged Digilent devices)
                    self._discover_dwf_instruments()

                    # State reconciliation: check capability_manager
                    # entries against instrument_manager
                    if self._capability_manager:
                        for inst_id in list(
                            self._capability_manager.all_instruments.keys()
                        ):
                            if self._is_gpib_address(inst_id):
                                continue  # Trickle scanner handles GPIB
                            # Skip SDK instruments — they aren't in the VISA
                            # resource list.  Serial SDK discovery handles
                            # their lifecycle separately.
                            if self._sdk_executor and self._sdk_executor.is_connected(inst_id):
                                continue
                            if inst_id not in current:
                                logger.warning(
                                    "Reconciler: removing stale instrument %s",
                                    inst_id,
                                )
                                self._capability_manager.unregister_instrument(
                                    inst_id
                                )
                                self._instrument_manager.disconnect(inst_id)

                    known = current

                await loop.run_in_executor(self._io_executor, _reconcile)

            except Exception as exc:
                logger.warning("Periodic reconciliation error: %s", exc)

    @staticmethod
    def _is_gpib_address(visa_addr: str) -> bool:
        """Return True if visa_addr looks like a GPIB address."""
        return visa_addr.upper().startswith("GPIB")

    async def _watch_stdin(self) -> None:
        """Watch stdin for EOF.

        When Go supervisor closes the write end of the stdin pipe,
        Python detects EOF and triggers graceful shutdown.

        If stdin is a TTY (interactive mode), this method returns
        immediately and does nothing.
        """
        if sys.stdin is None:
            return

        if sys.stdin.isatty():
            logger.debug("Stdin is a TTY -- stdin watcher not active")
            return

        logger.info("Stdin is a pipe -- will shut down on EOF")
        loop = asyncio.get_running_loop()

        try:
            reader = asyncio.StreamReader()
            protocol = asyncio.StreamReaderProtocol(reader)
            try:
                await loop.connect_read_pipe(lambda: protocol, sys.stdin)
            except OSError:
                # On macOS with Python 3.9's kqueue-based selector,
                # connect_read_pipe raises OSError on fd 0 under certain
                # pipe conditions.  Fall back to a threaded blocking read.
                logger.warning(
                    "asyncio stdin reader failed (macOS kqueue); "
                    "falling back to threaded stdin watcher"
                )
                await loop.run_in_executor(None, self._blocking_stdin_read)
                logger.info("Stdin closed (EOF) -- initiating shutdown")
                await self.stop()
                return

            # Block until EOF
            await reader.read()
            logger.info("Stdin closed (EOF) -- initiating shutdown")
            await self.stop()

        except Exception as exc:
            logger.warning("Stdin watcher error: %s", exc)
            await self.stop()

    @staticmethod
    def _blocking_stdin_read() -> None:
        """Blocking stdin read for threaded fallback.

        Reads one byte at a time until EOF (empty bytes).
        """
        while True:
            data = sys.stdin.buffer.read(1)
            if not data:
                return


# ---------------------------------------------------------------------------
# Logging configuration
# ---------------------------------------------------------------------------


def _configure_logging(level_name: str) -> None:
    """Set up root logger with a consistent format."""
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        force=True,
    )


# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------


def _install_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    daemon: EdgeDaemon,
) -> None:
    """Install SIGINT/SIGTERM handlers to trigger graceful shutdown."""

    def _shutdown_handler() -> None:
        logger.info("Received shutdown signal")
        asyncio.ensure_future(daemon.stop())

    if sys.platform == "win32":
        # Windows does not support loop.add_signal_handler
        signal.signal(signal.SIGINT, lambda s, f: _shutdown_handler())
    else:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _shutdown_handler)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Entry point for the galois-edge Python engine."""
    cfg = load_config()
    daemon = EdgeDaemon(cfg)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    _install_signal_handlers(loop, daemon)

    try:
        loop.run_until_complete(daemon.start())
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
    finally:
        loop.run_until_complete(daemon.stop())
        loop.close()
