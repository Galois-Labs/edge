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
from typing import Optional

from .config import Config, load_config
from .command_handler import CommandHandler
from .grpc_server import GRPCServer
from .instrument_manager import InstrumentManager
from .capability_manager import CapabilityManager
from .sdk_executor import SDKExecutor
from .ws_server import WebSocketServer

# Profile loader uses yaml, which is optional
try:
    from .profile_loader import ProfileLoader
except ImportError:
    ProfileLoader = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)


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

        # Background tasks
        self._rescan_task: Optional[asyncio.Task] = None
        self._stdin_task: Optional[asyncio.Task] = None

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
        logger.info(
            "Config: grpc_port=%d, ws_port=%d, scan_interval=%ds",
            self._cfg.grpc_port,
            self._cfg.ws_port,
            self._cfg.scan_interval_s,
        )

        # 1. Initialise instrument manager (no scanning yet)
        self._instrument_manager = InstrumentManager(
            gpib_enabled=self._cfg.gpib_enabled,
            gpib_scan_on_init=False,  # defer GPIB scan to background
            lan_instruments=self._cfg.lan_instruments,
            include_serial_ports=self._cfg.include_serial_ports,
        )

        # 2. Initialise SDK executor, command handler, capability manager
        #    (all empty until background discovery runs)
        self._sdk_executor = SDKExecutor(self._instrument_manager)
        self._capability_manager = CapabilityManager()
        self._command_handler = CommandHandler(self._instrument_manager)

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

        # 6. Load profiles + match instruments in background (slow — YAML I/O + instrument connect)
        asyncio.create_task(self._background_profile_match())

        # 8. Start background GPIB scan (slow -- runs in thread)
        asyncio.create_task(self._background_gpib_scan())

        # 9. Start periodic rescan task
        self._rescan_task = asyncio.create_task(
            self._periodic_rescan()
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

        # 1. Cancel background tasks
        for task in (self._rescan_task, self._stdin_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # 2. Stop WebSocket server
        if self._ws_server is not None:
            await self._ws_server.stop()

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
            # Match connected instruments to profiles
            if self._profile_loader is not None:
                logger.info("Matching %d resource(s) to profiles...", len(resources))
                for visa_addr in resources:
                    self._try_match_profile(visa_addr)

        try:
            await loop.run_in_executor(self._io_executor, _discover_and_match)
            logger.info("Background discovery and profile matching complete")
        except Exception as exc:
            logger.warning("Background discovery/matching failed: %s", exc)

    async def _background_gpib_scan(self) -> None:
        """Run the slow GPIB bus scan in a thread pool.

        This lets the daemon start serving immediately while the scan
        probes GPIB addresses (can take 60+ seconds).
        """
        if self._instrument_manager is None:
            return
        if not self._instrument_manager.gpib_available:
            return

        logger.info("Starting background GPIB bus scan...")
        loop = asyncio.get_running_loop()

        def _scan_and_match():
            gpib_resources = self._instrument_manager.rescan_gpib()
            if gpib_resources:
                logger.info(
                    "Background GPIB scan found %d instrument(s)",
                    len(gpib_resources),
                )
                for visa_addr in gpib_resources:
                    self._try_match_profile(visa_addr)
            else:
                logger.info("Background GPIB scan: no instruments found")

        try:
            await loop.run_in_executor(self._io_executor, _scan_and_match)
        except Exception as exc:
            logger.warning("Background GPIB scan failed: %s", exc)

    async def _periodic_rescan(self) -> None:
        """Periodically check for new / removed instruments.

        Lightweight diff -- does NOT trigger a GPIB bus scan, just
        compares list_resources() against the last known set.
        """
        interval = self._cfg.scan_interval_s
        if interval <= 0:
            logger.info("Periodic rescan disabled (interval <= 0)")
            return

        logger.info("Periodic rescan enabled (every %ds)", interval)

        if self._instrument_manager is None:
            return

        loop = asyncio.get_running_loop()
        known: set[str] = set()

        while self._running:
            await asyncio.sleep(interval)

            if not self._running:
                break

            try:
                def _rescan_diff():
                    nonlocal known
                    current = set(self._instrument_manager.list_resources())

                    new_resources = current - known
                    lost_resources = known - current

                    if not new_resources and not lost_resources:
                        return

                    for visa_addr in new_resources:
                        logger.info("New instrument detected: %s", visa_addr)
                        self._try_match_profile(visa_addr)

                    for visa_addr in lost_resources:
                        logger.warning("Instrument removed: %s", visa_addr)
                        self._instrument_manager.disconnect(visa_addr)
                        if self._capability_manager:
                            self._capability_manager.unregister_instrument(
                                visa_addr
                            )

                    known = current

                await loop.run_in_executor(self._io_executor, _rescan_diff)

            except Exception as exc:
                logger.warning("Periodic rescan error: %s", exc)

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
