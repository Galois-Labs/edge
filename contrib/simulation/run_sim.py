"""
Standalone entry point for running the edge daemon with simulated instruments.

Usage:
    python -m contrib.simulation.run_sim

This starts the full EdgeDaemon but swaps InstrumentManager for
SimulatedInstrumentManager before startup. No env vars needed,
no production code modified.

The daemon will:
1. Create a SimulatedInstrumentManager with 5 virtual Quantifi instruments
2. Start the gRPC server on the configured port
3. Start the WebSocket server
4. Respond to SCPI commands with physics-based simulated values

All downstream subsystems (CapabilityManager, CommandHandler, GRPCServer)
work identically — they can't tell the difference.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import os

# Ensure the source tree is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
# Ensure contrib is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from galois_edge.main import EdgeDaemon
from contrib.simulation.engine import SimulatedInstrumentManager


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def _run() -> None:
    logger.info("=" * 60)
    logger.info("QUANTIFI PHOTONICS SIMULATION MODE")
    logger.info("5 virtual instruments on 192.168.1.10-14:5025")
    logger.info("=" * 60)

    daemon = EdgeDaemon()

    # Swap InstrumentManager BEFORE start() initializes subsystems
    sim_manager = SimulatedInstrumentManager()

    # Override the _instrument_manager that start() would create.
    # We monkey-patch the start method to skip real InstrumentManager init.
    original_start = daemon.start

    async def patched_start() -> None:
        """Start daemon with simulated instruments."""
        daemon._running = True

        # Configure logging
        from galois_edge.main import _configure_logging
        _configure_logging(daemon._cfg.log_level)

        logger.info("Edge daemon starting in SIMULATION MODE (edge_id=%s)", daemon._edge_id)

        # 1. Use simulated instrument manager
        daemon._instrument_manager = sim_manager

        # 2. Initialize subsystems with simulated manager
        from galois_edge.sdk_executor import SDKExecutor
        from galois_edge.command_handler import CommandHandler
        from galois_edge.capability_manager import CapabilityManager
        from galois_edge.grpc_server import GRPCServer
        from galois_edge.ws_server import WebSocketServer

        daemon._sdk_executor = SDKExecutor(sim_manager)
        daemon._capability_manager = CapabilityManager()
        daemon._command_handler = CommandHandler(sim_manager)

        # 3. Auto-discover and register simulated instruments
        try:
            from galois_edge.profile_loader import ProfileLoader
            profile_loader = ProfileLoader()
            loaded = profile_loader.load_all()
            logger.info("Loaded %d instrument profiles", loaded)

            for addr in sim_manager.list_resources():
                sim_manager.connect(addr)
                idn = sim_manager.identify(addr)
                profile = profile_loader.match_instrument(idn)
                daemon._capability_manager.add_instrument(
                    instrument_id=addr,
                    visa_address=addr,
                    idn_response=idn,
                    profile=profile,
                )
                if profile:
                    logger.info(
                        "  %s -> %s %s (profile: %s)",
                        addr, profile.instrument.manufacturer,
                        profile.instrument.model, profile.profile_key,
                    )
                else:
                    logger.warning("  %s -> no profile matched for IDN: %s", addr, idn)
        except ImportError:
            logger.warning("ProfileLoader not available — instruments registered without profiles")
            for addr in sim_manager.list_resources():
                sim_manager.connect(addr)
                idn = sim_manager.identify(addr)
                daemon._capability_manager.add_instrument(
                    instrument_id=addr,
                    visa_address=addr,
                    idn_response=idn,
                )

        # 4. Start gRPC server
        daemon._grpc_server = GRPCServer(
            instrument_manager=sim_manager,
            command_handler=daemon._command_handler,
            edge_id=daemon._edge_id,
            port=daemon._cfg.grpc_port,
            max_workers=daemon._cfg.grpc_max_workers,
            capability_manager=daemon._capability_manager,
            sdk_executor=daemon._sdk_executor,
            io_executor=daemon._io_executor,
        )
        if not await daemon._grpc_server.start():
            logger.error("Failed to start gRPC server")
            return

        logger.info("gRPC server listening on port %d", daemon._cfg.grpc_port)

        # 5. Start WebSocket server
        daemon._ws_server = WebSocketServer(
            instrument_manager=sim_manager,
            command_handler=daemon._command_handler,
            port=daemon._cfg.ws_port,
        )
        try:
            await daemon._ws_server.start()
            logger.info("WebSocket server listening on port %d", daemon._cfg.ws_port)
        except Exception as exc:
            logger.warning("WebSocket server failed: %s", exc)

        logger.info("Simulation daemon ready — waiting for connections...")

        # Block until shutdown
        stop_event = asyncio.Event()

        def _handle_signal() -> None:
            logger.info("Shutdown signal received")
            stop_event.set()

        loop = asyncio.get_running_loop()
        for sig in (asyncio.signal.SIGINT, asyncio.signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _handle_signal)
            except NotImplementedError:
                pass  # Windows

        await stop_event.wait()
        await daemon.stop()

    await patched_start()


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("Simulation daemon stopped")


if __name__ == "__main__":
    main()
