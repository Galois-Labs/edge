"""
WS curve-frame timebase tests (work order §4).

Every acquisition-mode `curve` frame must carry the additive timebase
keys t0/dt/x_unit and the y-scaling keys y_scale/y_offset/y_unit, with
dt derived from the STR storage interval actually programmed (readback
when available, else the validated request) and y_scale explicitly 1.0
(never 0) when no counts→physical mapping is known.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import List, Optional
from unittest.mock import MagicMock

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "src")
)

from galois_edge.ws_server import WebSocketServer  # noqa: E402


class MockWebSocket:
    def __init__(self) -> None:
        self._closed = False
        self.sent: List[dict] = []

    @property
    def closed(self) -> bool:
        return self._closed

    async def send_json(self, data: dict) -> None:
        if not self._closed:
            self.sent.append(data)

    def all_of_type(self, type_: str) -> List[dict]:
        return [f for f in self.sent if f.get("type") == type_]


def _make_acq_server(str_readback: Optional[str], length: int = 10) -> WebSocketServer:
    """Server whose instrument completes acquisition immediately.

    ``str_readback``: response to the `STR` readback query (None →
    raise, simulating an instrument without readback support).
    """
    im = MagicMock()
    im.is_connected.return_value = True
    im.connect.return_value = "ok"
    im.write.return_value = None
    im.read_binary.return_value = b"\x00" * (length * 2 + 3)

    def query_fn(instrument_id: str, command: str) -> str:
        if command == "STR":
            if str_readback is None:
                raise RuntimeError("STR readback unsupported")
            return str_readback
        return "0"  # M status: acquisition complete

    im.query.side_effect = query_fn
    ch = MagicMock()
    return WebSocketServer(instrument_manager=im, command_handler=ch, port=8799)


async def _run_acquisition(server: WebSocketServer, config: dict,
                           length: int = 10) -> MockWebSocket:
    ws = MockWebSocket()
    server._active_streams[ws] = {}
    await server._handle_subscribe(ws, {
        "action": "subscribe",
        "stream_id": "acq-1",
        "instrument_id": "GPIB0::ACQ::INSTR",
        "mode": "acquisition",
        "config": config,
    })
    # Acquisition completes on the first M poll; give the task time.
    for _ in range(50):
        await asyncio.sleep(0.01)
        task = server._active_streams.get(ws, {}).get("acq-1")
        if task is None or task.done():
            break
    return ws


class TestCurveTimebase:
    @pytest.mark.asyncio
    async def test_curve_frames_carry_timebase_from_str_readback(self):
        # Request 1000 µs; instrument quantizes to 2500 µs — dt must
        # echo the readback, not the request.
        server = _make_acq_server(str_readback="2500")
        ws = await _run_acquisition(
            server, {"length": 10, "interval": 1000, "channels": 3, "curves": [0]},
        )
        curves = ws.all_of_type("curve")
        assert curves, f"no curve frame; sent={ws.sent}"
        frame = curves[0]
        assert frame["t0"] == 0.0
        assert frame["dt"] == pytest.approx(2500e-6)
        assert frame["x_unit"] == "s"
        assert frame["points"] == 10
        # Explicit-scale rule (§3.0/§4): never 0 for a multiplier.
        assert frame["y_scale"] == 1.0
        assert frame["y_offset"] == 0.0
        assert frame["y_unit"] == ""
        # Pre-existing keys are intact (additive change).
        assert frame["dtype"] == "int16"
        assert frame["format"] == "base64"
        assert "data" in frame

    @pytest.mark.asyncio
    async def test_dt_falls_back_to_validated_request_without_readback(self):
        server = _make_acq_server(str_readback=None)
        ws = await _run_acquisition(
            server, {"length": 10, "interval": 4000, "channels": 3, "curves": [0]},
        )
        curves = ws.all_of_type("curve")
        assert curves
        assert curves[0]["dt"] == pytest.approx(4000e-6)

    @pytest.mark.asyncio
    async def test_unparseable_readback_falls_back_to_request(self):
        server = _make_acq_server(str_readback="garbage")
        ws = await _run_acquisition(
            server, {"length": 10, "interval": 1000, "channels": 3, "curves": [0]},
        )
        curves = ws.all_of_type("curve")
        assert curves
        assert curves[0]["dt"] == pytest.approx(1000e-6)

    @pytest.mark.asyncio
    async def test_zero_readback_falls_back_to_request(self):
        # A "0" reply is not a valid storage interval — never produce
        # dt == 0.
        server = _make_acq_server(str_readback="0")
        ws = await _run_acquisition(
            server, {"length": 10, "interval": 1000, "channels": 3, "curves": [0]},
        )
        curves = ws.all_of_type("curve")
        assert curves
        assert curves[0]["dt"] == pytest.approx(1000e-6)
        assert curves[0]["dt"] > 0

    @pytest.mark.asyncio
    async def test_invalid_requested_interval_defaults_sane(self):
        server = _make_acq_server(str_readback=None)
        ws = await _run_acquisition(
            server,
            {"length": 10, "interval": "bogus", "channels": 3, "curves": [0]},
        )
        curves = ws.all_of_type("curve")
        assert curves
        assert curves[0]["dt"] == pytest.approx(1000e-6)  # 1000 µs default

    @pytest.mark.asyncio
    async def test_every_curve_frame_carries_the_keys(self):
        server = _make_acq_server(str_readback="1000")
        ws = await _run_acquisition(
            server,
            {"length": 10, "interval": 1000, "channels": 3, "curves": [0, 1]},
        )
        curves = ws.all_of_type("curve")
        assert len(curves) == 2
        for frame in curves:
            for key in ("t0", "dt", "x_unit", "y_scale", "y_offset", "y_unit"):
                assert key in frame, f"missing {key} on curve {frame['curve_id']}"
            assert frame["dt"] > 0
            assert frame["y_scale"] != 0
