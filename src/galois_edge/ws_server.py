"""
WebSocket data streaming server for the edge daemon.

Provides real-time instrument data to frontends and local clients over
WebSocket. Runs on 127.0.0.1:WS_PORT -- the Go supervisor's TCP proxy
handles external access via Tailscale.

Two streaming modes:
  - **Poll**: periodically query the instrument and push JSON results.
  - **Acquisition**: manage curve buffer transfers and push binary data.

JSON protocol (client <-> server):
  Subscribe:   {"action": "subscribe", "stream_id": "<id>",
                 "instrument_id": "...",
                 "mode": "poll", "interval_ms": 100}
  Unsubscribe: {"action": "unsubscribe", "stream_id": "<id>"}
  Command:     {"action": "command", "instrument_id": "...",
                 "scpi": "*IDN?"}
  Data push:   {"type": "data", "stream_id": "<id>",
                 "timestamp": 1234.5, "values": {"VOLT": 1.23}}
  Error:       {"type": "error", "message": "...",
                 "stream_id": "<id>"}  # stream_id omitted for parse errors
  Status:      {"type": "status", "stream_id": "<id>",
                 "state": "subscribed"}

Each socket supports up to **32 concurrent named streams**, each identified
by a caller-supplied ``stream_id``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Maximum concurrent streams per WebSocket connection.
_MAX_STREAMS_PER_SOCKET = 32

# Guard aiohttp import -- the daemon should still load without it for
# environments that only need gRPC.
try:
    from aiohttp import web
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    logger.warning("aiohttp not available -- WebSocket server disabled")


def _validate_stream_id(stream_id: Any) -> Optional[str]:
    """Validate stream_id; return the string on success or None on failure."""
    if not isinstance(stream_id, str) or not stream_id:
        return None
    if len(stream_id) > 64:
        return None
    if not all(32 <= ord(c) <= 126 for c in stream_id):
        return None
    return stream_id


class WebSocketServer:
    """WebSocket server for streaming instrument data from the edge.

    Each connected client may have up to 32 concurrent named streams,
    identified by caller-supplied ``stream_id`` strings. Subscriptions are
    tracked per-WebSocket connection and automatically cancelled when the
    client disconnects.
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

        # Two-level dict: ws -> {stream_id -> asyncio.Task}
        self._active_streams: Dict[Any, Dict[str, asyncio.Task]] = {}

        # Global acquisition exclusion: instrument_id -> True while acquiring
        self._acquiring_instruments: set[str] = set()

        # Per-instrument asyncio.Lock to serialise poll ticks and commands
        self._instrument_locks: Dict[str, asyncio.Lock] = {}

    @property
    def port(self) -> int:
        return self._port

    def _get_instrument_lock(self, instrument_id: str) -> asyncio.Lock:
        """Return the per-instrument Lock, creating it if necessary."""
        if instrument_id not in self._instrument_locks:
            self._instrument_locks[instrument_id] = asyncio.Lock()
        return self._instrument_locks[instrument_id]

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
        for ws, streams in list(self._active_streams.items()):
            for task in list(streams.values()):
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
        total = sum(len(s) for s in self._active_streams.values())
        return web.json_response({
            "status": "ok",
            "active_streams": total,
        })

    async def _handle_ws(self, request: Any) -> Any:
        """Upgrade HTTP connection to WebSocket and handle messages."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        logger.info("WebSocket client connected: %s", request.remote)

        # Initialise per-socket stream registry
        self._active_streams[ws] = {}

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
            await self._cancel_all_streams(ws)
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
            await self._handle_unsubscribe(ws, msg)
        elif action == "command":
            await self._handle_command(ws, msg)
        else:
            await self._send_error(ws, f"Unknown action: {action}")

    # ------------------------------------------------------------------
    # Subscribe / streaming
    # ------------------------------------------------------------------

    async def _handle_subscribe(self, ws: Any, msg: dict) -> None:
        """Start a streaming subscription for a client."""
        # --- Validate stream_id (required, hard break) ---
        raw_sid = msg.get("stream_id")
        stream_id = _validate_stream_id(raw_sid)
        if stream_id is None:
            await self._send_error(
                ws, "stream_id must be a non-empty string"
            )
            return

        # --- Validate instrument_id ---
        instrument_id = msg.get("instrument_id")
        if not instrument_id:
            await self._send_error(
                ws, "Missing instrument_id", stream_id=stream_id
            )
            return

        # --- Stream cap check ---
        streams = self._active_streams.get(ws, {})
        # If this stream_id is new and we're already at the cap, reject
        if stream_id not in streams and len(streams) >= _MAX_STREAMS_PER_SOCKET:
            await self._send_error(
                ws,
                "Stream limit reached (max 32 per connection)",
                stream_id=stream_id,
            )
            return

        # --- Ensure connected ---
        if not self._instruments.is_connected(instrument_id):
            connected = self._instruments.connect(instrument_id)
            if not connected:
                await self._send_error(
                    ws,
                    f"Cannot connect to {instrument_id}",
                    stream_id=stream_id,
                )
                return

        # --- Cancel duplicate stream_id (cancel-and-replace) ---
        existing = streams.get(stream_id)
        if existing is not None and not existing.done():
            existing.cancel()
            try:
                await asyncio.wait_for(existing, timeout=1.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                # CancelledError is the expected outcome; TimeoutError shields
                # against a misbehaving task that swallows cancellation; any
                # other exception thrown during teardown is logged-and-dropped
                # by the original task's own error handler.
                pass

        # --- Create new task ---
        mode = msg.get("mode", "poll")
        if mode == "poll":
            task = asyncio.create_task(
                self._poll_loop(ws, instrument_id, stream_id, msg)
            )
        elif mode == "acquisition":
            # Acquisition exclusion check
            if instrument_id in self._acquiring_instruments:
                await self._send_error(
                    ws,
                    "Instrument already in acquisition mode",
                    stream_id=stream_id,
                )
                return
            task = asyncio.create_task(
                self._acquisition_loop(ws, instrument_id, stream_id, msg)
            )
        else:
            await self._send_error(
                ws, f"Unknown mode: {mode}", stream_id=stream_id
            )
            return

        # Register and ack
        if ws not in self._active_streams:
            self._active_streams[ws] = {}
        self._active_streams[ws][stream_id] = task
        await self._send_status(ws, "subscribed", stream_id=stream_id, mode=mode)

    async def _handle_unsubscribe(self, ws: Any, msg: dict) -> None:
        """Cancel a specific stream by stream_id."""
        raw_sid = msg.get("stream_id")
        stream_id = _validate_stream_id(raw_sid)
        if stream_id is None:
            await self._send_error(
                ws, "stream_id must be a non-empty string"
            )
            return

        streams = self._active_streams.get(ws, {})
        task = streams.get(stream_id)
        if task is None:
            await self._send_error(
                ws, "Unknown stream_id", stream_id=stream_id
            )
            return

        if not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        streams.pop(stream_id, None)
        await self._send_status(ws, "unsubscribed", stream_id=stream_id)

    async def _poll_loop(
        self,
        ws: Any,
        instrument_id: str,
        stream_id: str,
        config: dict,
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
        lock = self._get_instrument_lock(instrument_id)

        try:
            while not ws.closed:
                ts = time.time()

                try:
                    async with lock:
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
                        "stream_id": stream_id,
                        "timestamp": ts,
                        "values": values,
                    })

                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "Poll tick error for stream %s: %s", stream_id, exc
                    )
                    await self._send_error(ws, str(exc), stream_id=stream_id)
                    # Check for unrecoverable errors (instrument gone)
                    if _is_unrecoverable(exc):
                        # Self-cleanup: pop from inner dict
                        streams = self._active_streams.get(ws)
                        if streams is not None:
                            streams.pop(stream_id, None)
                        return

                # Sleep for the remainder of the interval
                elapsed = time.time() - ts
                remaining = max(0, interval - elapsed)
                await asyncio.sleep(remaining)

        except asyncio.CancelledError:
            pass

    async def _acquisition_loop(
        self,
        ws: Any,
        instrument_id: str,
        stream_id: str,
        config: dict,
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

        # Register acquisition exclusion
        self._acquiring_instruments.add(instrument_id)
        try:
            # Configure the curve buffer
            await self._send_status(ws, "configuring", stream_id=stream_id)
            im.write(instrument_id, "HC")                    # halt
            im.write(instrument_id, f"LEN {length}")
            im.write(instrument_id, f"STR {interval_us}")
            # dt for the curve frames must reflect the storage interval
            # actually programmed (work order §4): prefer the STR
            # readback (the instrument may quantize the request), fall
            # back to the validated request value.
            actual_interval_us = self._read_back_storage_interval(
                instrument_id, interval_us
            )
            im.write(instrument_id, f"CBD {channels}")
            im.write(instrument_id, "NC")                    # new curve
            im.write(instrument_id, "TD")                    # trigger

            await self._send_status(ws, "acquiring", stream_id=stream_id)

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

                await self._send_status(ws, "acquiring", stream_id=stream_id)
                await asyncio.sleep(0.1)

            if ws.closed:
                return

            await self._send_status(ws, "downloading", stream_id=stream_id)

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
                        "stream_id": stream_id,
                        "curve_id": curve_idx,
                        "format": "base64",
                        "dtype": "int16",
                        "points": length,
                        "data": encoded,
                        # Timebase (work order §4) so clients can plot
                        # y[] against time: x(i) = t0 + i*dt seconds.
                        # Curve buffers start at acquisition start, so
                        # t0 = 0.0; dt is the STR storage interval
                        # actually programmed (readback when available,
                        # else the validated request), µs → s. Additive
                        # keys — old clients ignore them.
                        "t0": 0.0,
                        "dt": actual_interval_us * 1e-6,
                        "x_unit": "s",
                        # Counts → physical-units mapping is unknown for
                        # this device path: explicit 1.0 / 0.0 / "" —
                        # never 0 for a multiplier (§3.0 rule).
                        "y_scale": 1.0,
                        "y_offset": 0.0,
                        "y_unit": "",
                    })
                except Exception as exc:
                    await self._send_error(
                        ws,
                        f"Curve {curve_idx} download failed: {exc}",
                        stream_id=stream_id,
                    )

            await self._send_status(ws, "complete", stream_id=stream_id)

        except asyncio.CancelledError:
            # Best-effort halt on cancellation
            try:
                im.write(instrument_id, "HC")
            except Exception:
                pass
        finally:
            # Always release the acquisition exclusion lock
            self._acquiring_instruments.discard(instrument_id)

    def _read_back_storage_interval(
        self, instrument_id: str, requested_us: Any,
    ) -> float:
        """Return the storage interval actually programmed, in µs.

        Work order §4: the curve frame's ``dt`` MUST come from the value
        actually programmed into the instrument — the ``STR`` readback
        when available (instruments may quantize the request), otherwise
        the validated request value. Never returns <= 0.
        """
        try:
            requested = float(requested_us)
        except (TypeError, ValueError):
            requested = 1000.0
        if requested <= 0:
            requested = 1000.0

        try:
            raw = self._instruments.query(instrument_id, "STR")
            readback = float(str(raw).strip().split(",")[0])
            if readback > 0:
                return readback
        except Exception as exc:
            logger.debug(
                "STR readback failed for %s (%s); using requested interval",
                instrument_id, exc,
            )
        return requested

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

        lock = self._get_instrument_lock(instrument_id)
        async with lock:
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

    async def _cancel_all_streams(self, ws: Any) -> None:
        """Cancel every stream task for a WebSocket (called on disconnect)."""
        streams = self._active_streams.pop(ws, {})
        for task in streams.values():
            if not task.done():
                task.cancel()
        # Let tasks complete their CancelledError handling
        if streams:
            await asyncio.gather(*streams.values(), return_exceptions=True)

    async def _send_error(
        self,
        ws: Any,
        message: str,
        stream_id: Optional[str] = None,
    ) -> None:
        """Send a JSON error to the client."""
        if not ws.closed:
            payload: Dict[str, Any] = {"type": "error", "message": message}
            if stream_id is not None:
                payload["stream_id"] = stream_id
            await ws.send_json(payload)

    async def _send_status(
        self, ws: Any, state: str, stream_id: Optional[str] = None, **kwargs: Any,
    ) -> None:
        """Send a JSON status to the client."""
        if not ws.closed:
            payload: Dict[str, Any] = {"type": "status", "state": state}
            if stream_id is not None:
                payload["stream_id"] = stream_id
            payload.update(kwargs)
            await ws.send_json(payload)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _is_unrecoverable(exc: Exception) -> bool:
    """Return True for exceptions that indicate the instrument has gone away."""
    msg = str(exc).lower()
    unrecoverable_keywords = (
        "disconnected",
        "no longer available",
        "connection refused",
        "timeout",
        "resource not found",
        "vi_error_rsrc_nfound",
        "visaioerror",
    )
    return any(kw in msg for kw in unrecoverable_keywords)


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
