"""
WebSocket data streaming server for the edge daemon.

Provides real-time instrument data to frontends and local clients over
WebSocket. Runs on 127.0.0.1:WS_PORT -- the Go supervisor's TCP proxy
handles external access via Tailscale.

Two streaming modes:
  - **Poll**: periodically query the instrument and push JSON results.
  - **Acquisition**: manage curve buffer transfers and push binary data.

JSON protocol (client <-> server):
  Subscribe:   {"action": "subscribe", "instrument_id": "...",
                 "mode": "poll", "interval_ms": 100}
  Unsubscribe: {"action": "unsubscribe", "instrument_id": "..."}
  Command:     {"action": "command", "instrument_id": "...",
                 "scpi": "*IDN?"}
  Data push:   {"type": "data", "timestamp": 1234.5,
                 "values": {"VOLT": 1.23}}
  Error:       {"type": "error", "message": "..."}
  Status:      {"type": "status", "state": "subscribed"}
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Guard aiohttp import -- the daemon should still load without it for
# environments that only need gRPC.
try:
    from aiohttp import web
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    logger.warning("aiohttp not available -- WebSocket server disabled")


class WebSocketServer:
    """WebSocket server for streaming instrument data from the edge.

    Each connected client may have at most one active subscription.
    Subscriptions are tracked per-WebSocket connection and automatically
    cancelled when the client disconnects.
    """

    def __init__(
        self,
        instrument_manager: Any,
        command_handler: Any,
        port: int = 8766,
    ) -> None:
        """Initialise the WebSocket server.

        Args:
            instrument_manager: InstrumentManager instance (query/write/
                is_connected/connect methods).
            command_handler: CommandHandler instance (execute_command).
            port: Bind port (default 8766).
        """
        self._instruments = instrument_manager
        self._handler = command_handler
        self._port = port

        self._app: Optional[Any] = None      # web.Application
        self._runner: Optional[Any] = None    # web.AppRunner
        self._active_streams: Dict[Any, asyncio.Task] = {}

    @property
    def port(self) -> int:
        return self._port

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the WebSocket server."""
        if not AIOHTTP_AVAILABLE:
            logger.error("Cannot start WebSocket server: aiohttp missing")
            return

        self._app = web.Application()
        self._app.router.add_get("/ws", self._handle_ws)
        self._app.router.add_get("/health", self._handle_health)

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", self._port)
        await site.start()
        logger.info(
            "WebSocket server listening on 127.0.0.1:%d", self._port
        )

    async def stop(self) -> None:
        """Stop the server and cancel all active streaming tasks."""
        for ws, task in list(self._active_streams.items()):
            task.cancel()
            try:
                if not ws.closed:
                    await ws.close()
            except Exception:
                pass
        self._active_streams.clear()

        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        logger.info("WebSocket server stopped")

    # ------------------------------------------------------------------
    # HTTP handlers
    # ------------------------------------------------------------------

    async def _handle_health(self, request: Any) -> Any:
        """Simple health endpoint."""
        return web.json_response({
            "status": "ok",
            "active_streams": len(self._active_streams),
        })

    async def _handle_ws(self, request: Any) -> Any:
        """Upgrade HTTP connection to WebSocket and handle messages."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        logger.info("WebSocket client connected: %s", request.remote)

        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    await self._on_message(ws, msg.data)
                elif msg.type == web.WSMsgType.ERROR:
                    logger.warning(
                        "WebSocket error: %s", ws.exception()
                    )
        except asyncio.CancelledError:
            pass
        finally:
            self._cancel_stream(ws)
            logger.info(
                "WebSocket client disconnected: %s", request.remote
            )

        return ws

    # ------------------------------------------------------------------
    # Message dispatch
    # ------------------------------------------------------------------

    async def _on_message(self, ws: Any, raw: str) -> None:
        """Parse and dispatch a JSON message from the client."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            await self._send_error(ws, "Invalid JSON")
            return

        action = msg.get("action")

        if action == "subscribe":
            await self._handle_subscribe(ws, msg)
        elif action == "unsubscribe":
            self._cancel_stream(ws)
            await self._send_status(ws, "unsubscribed")
        elif action == "command":
            await self._handle_command(ws, msg)
        else:
            await self._send_error(ws, f"Unknown action: {action}")

    # ------------------------------------------------------------------
    # Subscribe / streaming
    # ------------------------------------------------------------------

    async def _handle_subscribe(self, ws: Any, msg: dict) -> None:
        """Start a streaming subscription for a client."""
        instrument_id = msg.get("instrument_id")
        if not instrument_id:
            await self._send_error(ws, "Missing instrument_id")
            return

        # Ensure connected
        if not self._instruments.is_connected(instrument_id):
            connected = self._instruments.connect(instrument_id)
            if not connected:
                await self._send_error(
                    ws, f"Cannot connect to {instrument_id}"
                )
                return

        # Cancel any existing subscription for this WebSocket
        self._cancel_stream(ws)

        mode = msg.get("mode", "poll")
        if mode == "poll":
            task = asyncio.create_task(
                self._poll_loop(ws, instrument_id, msg)
            )
        elif mode == "acquisition":
            task = asyncio.create_task(
                self._acquisition_loop(ws, instrument_id, msg)
            )
        else:
            await self._send_error(ws, f"Unknown mode: {mode}")
            return

        self._active_streams[ws] = task
        await self._send_status(ws, "subscribed", mode=mode)

    async def _poll_loop(
        self, ws: Any, instrument_id: str, config: dict,
    ) -> None:
        """Periodically query the instrument and push JSON data.

        Config keys:
            interval_ms (int): polling interval, default 100
            scpi_command (str): optional custom SCPI query
            signals (list[str]): signal names to query individually
        """
        interval = config.get("interval_ms", 100) / 1000.0
        signals = config.get("signals", [])
        scpi_command = config.get("scpi_command", "")

        try:
            while not ws.closed:
                ts = time.time()

                try:
                    if scpi_command:
                        # Custom SCPI query
                        raw = self._instruments.query(
                            instrument_id, scpi_command
                        )
                        values = _parse_csv_data(raw)
                    elif signals:
                        # Query each signal individually
                        values: Dict[str, Any] = {}
                        for sig in signals:
                            resp = self._instruments.query(
                                instrument_id, sig
                            )
                            try:
                                values[sig] = float(resp)
                            except ValueError:
                                values[sig] = resp
                    else:
                        # Fast snapshot query
                        raw = self._instruments.query(instrument_id, "?")
                        values = _parse_csv_data(raw)

                    await ws.send_json({
                        "type": "data",
                        "timestamp": ts,
                        "values": values,
                    })

                except Exception as exc:
                    await self._send_error(ws, str(exc))

                # Sleep for the remainder of the interval
                elapsed = time.time() - ts
                remaining = max(0, interval - elapsed)
                await asyncio.sleep(remaining)

        except asyncio.CancelledError:
            pass

    async def _acquisition_loop(
        self, ws: Any, instrument_id: str, config: dict,
    ) -> None:
        """Manage a curve buffer acquisition and stream binary data.

        Config keys (nested under 'config'):
            length (int): number of points, default 1000
            interval (int): storage interval in microseconds, default 1000
            channels (int): channel bitmask, default 3 (X+Y)
            curves (list[int]): curve indices to download, default [0, 1]
        """
        acq = config.get("config", {})
        length = acq.get("length", 1000)
        interval_us = acq.get("interval", 1000)
        channels = acq.get("channels", 3)
        curves = acq.get("curves", [0, 1])

        im = self._instruments

        try:
            # Configure the curve buffer
            await self._send_status(ws, "configuring")
            im.write(instrument_id, "HC")                    # halt
            im.write(instrument_id, f"LEN {length}")
            im.write(instrument_id, f"STR {interval_us}")
            im.write(instrument_id, f"CBD {channels}")
            im.write(instrument_id, "NC")                    # new curve
            im.write(instrument_id, "TD")                    # trigger

            await self._send_status(ws, "acquiring")

            # Poll acquisition status
            while not ws.closed:
                status_raw = im.query(instrument_id, "M")
                try:
                    status_val = int(status_raw)
                except ValueError:
                    status_val = 0

                td_running = bool(status_val & 0x02)
                tdc_running = bool(status_val & 0x04)

                if not td_running and not tdc_running:
                    break

                await self._send_status(ws, "acquiring")
                await asyncio.sleep(0.1)

            if ws.closed:
                return

            await self._send_status(ws, "downloading")

            # Download each curve as base64-encoded binary
            for curve_idx in curves:
                if ws.closed:
                    return

                try:
                    binary_data = self._read_curve_binary(
                        instrument_id, curve_idx, length,
                    )
                    encoded = base64.b64encode(binary_data).decode("ascii")

                    await ws.send_json({
                        "type": "curve",
                        "curve_id": curve_idx,
                        "format": "base64",
                        "dtype": "int16",
                        "points": length,
                        "data": encoded,
                    })
                except Exception as exc:
                    await self._send_error(
                        ws,
                        f"Curve {curve_idx} download failed: {exc}",
                    )

            await self._send_status(ws, "complete")

        except asyncio.CancelledError:
            # Best-effort halt on cancellation
            try:
                im.write(instrument_id, "HC")
            except Exception:
                pass

    def _read_curve_binary(
        self, instrument_id: str, curve_idx: int, num_points: int,
    ) -> bytes:
        """Read binary curve data via DCB command.

        Returns raw bytes (int16 samples). Falls back to text if
        read_binary is not available.
        """
        expected_bytes = num_points * 2 + 3  # int16 + trailer

        self._instruments.write(instrument_id, f"DCB {curve_idx}")

        if hasattr(self._instruments, "read_binary"):
            raw = self._instruments.read_binary(
                instrument_id, expected_bytes
            )
        else:
            raw = self._instruments.query(
                instrument_id, f"DC. {curve_idx}"
            ).encode()

        if len(raw) >= expected_bytes:
            return raw[:num_points * 2]
        return raw

    # ------------------------------------------------------------------
    # Pass-through command
    # ------------------------------------------------------------------

    async def _handle_command(self, ws: Any, msg: dict) -> None:
        """Execute a single SCPI command and return the result."""
        scpi = msg.get("scpi", "")
        instrument_id = msg.get("instrument_id", "")

        if not scpi or not instrument_id:
            await self._send_error(ws, "Missing scpi or instrument_id")
            return

        result = self._handler.execute_command(
            scpi_cmd=scpi,
            instrument_id=instrument_id,
        )

        await ws.send_json({
            "type": "command_result",
            "success": result["success"],
            "response": result.get("response", ""),
            "error": result.get("error", ""),
        })

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _cancel_stream(self, ws: Any) -> None:
        """Cancel an active streaming task for a WebSocket."""
        task = self._active_streams.pop(ws, None)
        if task is not None and not task.done():
            task.cancel()

    async def _send_error(self, ws: Any, message: str) -> None:
        """Send a JSON error to the client."""
        if not ws.closed:
            await ws.send_json({"type": "error", "message": message})

    async def _send_status(
        self, ws: Any, state: str, **kwargs: Any,
    ) -> None:
        """Send a JSON status to the client."""
        if not ws.closed:
            payload: Dict[str, Any] = {"type": "status", "state": state}
            payload.update(kwargs)
            await ws.send_json(payload)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _parse_csv_data(raw: str) -> Dict[str, Any]:
    """Parse a comma-separated instrument response into named values.

    Labels are generic channel names. If the response has more fields
    than labels, extra fields get numbered names.
    """
    default_labels = [
        "X", "Y", "MAG", "PHA", "sensitivity", "noise",
        "frequency", "ref_phase", "harmonic", "overloads",
    ]
    parts = raw.split(",")
    values: Dict[str, Any] = {}
    for i, part in enumerate(parts):
        key = default_labels[i] if i < len(default_labels) else f"ch{i}"
        part = part.strip()
        try:
            values[key] = float(part)
        except ValueError:
            values[key] = part
    return values
