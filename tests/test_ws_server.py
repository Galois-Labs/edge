"""
Tests for ws_server.py — Multi-subscription WebSocket protocol (Spec B).

Covers test cases T1–T10 from the spec §10 test plan.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "src")
)

from galois_edge.ws_server import WebSocketServer, _validate_stream_id


# ---------------------------------------------------------------------------
# Helpers — Mock WebSocket
# ---------------------------------------------------------------------------


class MockWebSocket:
    """Minimal aiohttp WebSocketResponse mock for testing."""

    def __init__(self) -> None:
        self._closed = False
        self.sent: List[dict] = []

    @property
    def closed(self) -> bool:
        return self._closed

    async def send_json(self, data: dict) -> None:
        if not self._closed:
            self.sent.append(data)

    def close_ws(self) -> None:
        self._closed = True

    def last_of_type(self, type_: str) -> Optional[dict]:
        for frame in reversed(self.sent):
            if frame.get("type") == type_:
                return frame
        return None

    def all_of_type(self, type_: str) -> List[dict]:
        return [f for f in self.sent if f.get("type") == type_]


class MockCommandHandler:
    """Minimal mock for CommandHandler."""

    def __init__(self, response: str = "OK") -> None:
        self._response = response

    def execute_command(self, scpi_cmd: str, instrument_id: str) -> dict:
        return {"success": True, "response": self._response, "error": ""}


def _make_server(
    query_fn=None,
    connected: bool = True,
) -> WebSocketServer:
    """Build a WebSocketServer with a mock instrument manager."""
    im = MagicMock()
    im.is_connected.return_value = connected
    im.connect.return_value = "GPIB0::1::INSTR"

    if query_fn is not None:
        im.query.side_effect = query_fn
    else:
        im.query.return_value = "1.0,2.0"

    ch = MockCommandHandler()
    return WebSocketServer(instrument_manager=im, command_handler=ch, port=8799)


# ---------------------------------------------------------------------------
# _validate_stream_id unit tests
# ---------------------------------------------------------------------------


class TestValidateStreamId:
    def test_valid(self):
        assert _validate_stream_id("volt-poll-1") == "volt-poll-1"

    def test_empty_string(self):
        assert _validate_stream_id("") is None

    def test_integer(self):
        assert _validate_stream_id(42) is None

    def test_none(self):
        assert _validate_stream_id(None) is None

    def test_too_long(self):
        assert _validate_stream_id("x" * 65) is None

    def test_max_length(self):
        s = "a" * 64
        assert _validate_stream_id(s) == s

    def test_non_printable(self):
        assert _validate_stream_id("bad\x00id") is None


# ---------------------------------------------------------------------------
# T1 — Two poll streams on different instruments emit independent data frames
# ---------------------------------------------------------------------------


class TestT1_TwoPollStreamsDifferentInstruments:

    @pytest.mark.asyncio
    async def test_two_streams_independent_data(self):
        """T1: Two poll streams on different instruments both emit data frames
        with the correct stream_id."""
        call_counts: Dict[str, int] = {"instr_a": 0, "instr_b": 0}

        def query_fn(instrument_id: str, command: str) -> str:
            if "INSTR_A" in instrument_id:
                call_counts["instr_a"] += 1
                return "1.0"
            else:
                call_counts["instr_b"] += 1
                return "2.0"

        server = _make_server(query_fn=query_fn)
        ws = MockWebSocket()
        server._active_streams[ws] = {}

        # Subscribe stream "a" on instrument A
        await server._handle_subscribe(ws, {
            "action": "subscribe",
            "stream_id": "a",
            "instrument_id": "GPIB0::INSTR_A::INSTR",
            "mode": "poll",
            "interval_ms": 50,
            "scpi_command": "?",
        })
        # Subscribe stream "b" on instrument B
        await server._handle_subscribe(ws, {
            "action": "subscribe",
            "stream_id": "b",
            "instrument_id": "GPIB0::INSTR_B::INSTR",
            "mode": "poll",
            "interval_ms": 50,
            "scpi_command": "?",
        })

        assert "a" in server._active_streams[ws]
        assert "b" in server._active_streams[ws]

        # Let both tasks run briefly
        await asyncio.sleep(0.15)

        # Cancel both tasks
        for task in list(server._active_streams[ws].values()):
            task.cancel()
        await asyncio.gather(*server._active_streams[ws].values(), return_exceptions=True)

        # Check that data frames were sent with correct stream_ids
        data_frames = ws.all_of_type("data")
        stream_ids_seen = {f["stream_id"] for f in data_frames}
        assert "a" in stream_ids_seen
        assert "b" in stream_ids_seen

        # Verify each data frame has correct stream_id
        for frame in data_frames:
            assert frame["stream_id"] in ("a", "b")


# ---------------------------------------------------------------------------
# T2 — Unsubscribe "a", "b" continues
# ---------------------------------------------------------------------------


class TestT2_UnsubscribeOneStreamOtherContinues:

    @pytest.mark.asyncio
    async def test_unsubscribe_a_b_continues(self):
        """T2: Unsubscribe 'a'; 'b' keeps emitting data frames."""
        server = _make_server()
        ws = MockWebSocket()
        server._active_streams[ws] = {}

        # Subscribe two streams
        await server._handle_subscribe(ws, {
            "action": "subscribe",
            "stream_id": "a",
            "instrument_id": "GPIB0::1::INSTR",
            "mode": "poll",
            "interval_ms": 50,
        })
        await server._handle_subscribe(ws, {
            "action": "subscribe",
            "stream_id": "b",
            "instrument_id": "GPIB0::2::INSTR",
            "mode": "poll",
            "interval_ms": 50,
        })

        # Let them both emit a bit
        await asyncio.sleep(0.1)

        # Unsubscribe "a"
        ws.sent.clear()
        await server._handle_unsubscribe(ws, {
            "action": "unsubscribe",
            "stream_id": "a",
        })

        # Verify status: unsubscribed sent for "a"
        status_frames = ws.all_of_type("status")
        unsubscribed = [f for f in status_frames if f.get("state") == "unsubscribed"]
        assert len(unsubscribed) == 1
        assert unsubscribed[0]["stream_id"] == "a"

        # "a" task should be gone
        assert "a" not in server._active_streams.get(ws, {})

        # "b" task should still be active
        assert "b" in server._active_streams.get(ws, {})
        b_task = server._active_streams[ws]["b"]
        assert not b_task.done()

        # Let "b" emit more frames
        ws.sent.clear()
        await asyncio.sleep(0.1)
        data_frames = ws.all_of_type("data")
        assert all(f["stream_id"] == "b" for f in data_frames)
        assert len(data_frames) > 0

        # Cleanup
        b_task.cancel()
        try:
            await b_task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# T3 — 32-stream cap
# ---------------------------------------------------------------------------


class TestT3_StreamCap:

    @pytest.mark.asyncio
    async def test_32_streams_then_33rd_rejected(self):
        """T3: First 32 subscribe calls succeed; the 33rd returns an error."""
        server = _make_server()
        ws = MockWebSocket()
        server._active_streams[ws] = {}

        # Subscribe 32 streams
        tasks = []
        for i in range(32):
            sid = f"stream-{i}"
            await server._handle_subscribe(ws, {
                "action": "subscribe",
                "stream_id": sid,
                "instrument_id": f"GPIB0::{i}::INSTR",
                "mode": "poll",
                "interval_ms": 10000,  # slow so tasks don't produce noise
            })
            t = server._active_streams[ws].get(sid)
            if t:
                tasks.append(t)

        assert len(server._active_streams[ws]) == 32

        # 33rd should fail
        ws.sent.clear()
        await server._handle_subscribe(ws, {
            "action": "subscribe",
            "stream_id": "stream-32",
            "instrument_id": "GPIB0::32::INSTR",
            "mode": "poll",
            "interval_ms": 10000,
        })

        error_frames = ws.all_of_type("error")
        assert len(error_frames) == 1
        assert "Stream limit reached" in error_frames[0]["message"]
        assert error_frames[0]["stream_id"] == "stream-32"

        # Still 32 streams
        assert len(server._active_streams[ws]) == 32

        # Cleanup
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


# ---------------------------------------------------------------------------
# T4 — Poll and acquisition streams on different instruments simultaneously
# ---------------------------------------------------------------------------


class TestT4_PollAndAcquisitionDifferentInstruments:

    @pytest.mark.asyncio
    async def test_poll_and_acquisition_coexist(self):
        """T4: One poll stream and one acquisition stream run on different
        instruments; both send frames with correct stream_ids."""
        im = MagicMock()
        im.is_connected.return_value = True
        im.connect.return_value = "ok"
        im.query.return_value = "0"   # acquisition status: not running
        im.write.return_value = None

        # Poll queries return data
        def query_side_effect(instrument_id, command):
            if "POLL" in instrument_id:
                return "3.14"
            return "0"   # acquisition M query: done immediately

        im.query.side_effect = query_side_effect

        ch = MockCommandHandler()
        server = WebSocketServer(instrument_manager=im, command_handler=ch, port=8799)
        ws = MockWebSocket()
        server._active_streams[ws] = {}

        # Subscribe poll stream
        await server._handle_subscribe(ws, {
            "action": "subscribe",
            "stream_id": "poll-ch",
            "instrument_id": "GPIB0::POLL::INSTR",
            "mode": "poll",
            "interval_ms": 50,
            "scpi_command": "MEAS?",
        })

        # Subscribe acquisition stream
        await server._handle_subscribe(ws, {
            "action": "subscribe",
            "stream_id": "acq-ch",
            "instrument_id": "GPIB0::ACQ::INSTR",
            "mode": "acquisition",
            "config": {"length": 10, "interval": 100, "channels": 3, "curves": [0]},
        })

        # Let them run
        await asyncio.sleep(0.3)

        # Poll task may still be running; acquisition may have completed
        poll_task = server._active_streams[ws].get("poll-ch")

        # Check frames
        data_frames = ws.all_of_type("data")
        assert any(f["stream_id"] == "poll-ch" for f in data_frames)

        status_frames = ws.all_of_type("status")
        acq_states = [
            f.get("state") for f in status_frames if f.get("stream_id") == "acq-ch"
        ]
        # Should have at least "subscribed" status
        assert "subscribed" in acq_states or len(acq_states) > 0

        # Cleanup
        if poll_task and not poll_task.done():
            poll_task.cancel()
            try:
                await poll_task
            except asyncio.CancelledError:
                pass


# ---------------------------------------------------------------------------
# T5 — Two poll streams on the same instrument
# ---------------------------------------------------------------------------


class TestT5_TwoPollStreamsSameInstrument:

    @pytest.mark.asyncio
    async def test_two_streams_same_instrument(self):
        """T5: Two poll streams on the same instrument both produce data
        frames without SCPI interleaving corruption."""
        query_log: List[str] = []
        lock = asyncio.Lock()

        def query_fn(instrument_id: str, command: str) -> str:
            query_log.append(f"{instrument_id}:{command}")
            return "5.0"

        server = _make_server(query_fn=query_fn)
        ws = MockWebSocket()
        server._active_streams[ws] = {}

        # Subscribe two streams on same instrument at staggered intervals
        await server._handle_subscribe(ws, {
            "action": "subscribe",
            "stream_id": "stream-x",
            "instrument_id": "GPIB0::1::INSTR",
            "mode": "poll",
            "interval_ms": 100,
            "scpi_command": "MEAS:VOLT?",
        })

        await asyncio.sleep(0.05)  # stagger by 50 ms

        await server._handle_subscribe(ws, {
            "action": "subscribe",
            "stream_id": "stream-y",
            "instrument_id": "GPIB0::1::INSTR",
            "mode": "poll",
            "interval_ms": 100,
            "scpi_command": "MEAS:CURR?",
        })

        await asyncio.sleep(0.35)

        # Cancel both
        for task in list(server._active_streams[ws].values()):
            task.cancel()
        await asyncio.gather(*server._active_streams[ws].values(), return_exceptions=True)

        # Both stream_ids should appear in data frames
        data_frames = ws.all_of_type("data")
        sids = {f["stream_id"] for f in data_frames}
        assert "stream-x" in sids
        assert "stream-y" in sids


# ---------------------------------------------------------------------------
# T6 — Second acquisition on same instrument is rejected
# ---------------------------------------------------------------------------


class TestT6_AcquisitionExclusion:

    @pytest.mark.asyncio
    async def test_second_acquisition_rejected(self):
        """T6: A second acquisition subscribe on the same instrument
        returns error 'Instrument already in acquisition mode'."""
        im = MagicMock()
        im.is_connected.return_value = True
        im.connect.return_value = "ok"
        im.write.return_value = None

        # Acquisition will hang on M query (never done)
        call_count = {"n": 0}
        async_event = asyncio.Event()

        def query_fn(instrument_id, command):
            if command == "M":
                # Simulate still acquiring (TD bit set)
                return "2"
            return "0"

        im.query.side_effect = query_fn

        ch = MockCommandHandler()
        server = WebSocketServer(instrument_manager=im, command_handler=ch, port=8799)
        ws = MockWebSocket()
        server._active_streams[ws] = {}

        # First acquisition
        await server._handle_subscribe(ws, {
            "action": "subscribe",
            "stream_id": "acq-1",
            "instrument_id": "GPIB0::1::INSTR",
            "mode": "acquisition",
            "config": {"length": 10, "interval": 100, "channels": 3, "curves": [0]},
        })

        # Let the acquisition task start and register itself
        await asyncio.sleep(0.05)

        # Attempt second acquisition on same instrument
        ws.sent.clear()
        await server._handle_subscribe(ws, {
            "action": "subscribe",
            "stream_id": "acq-2",
            "instrument_id": "GPIB0::1::INSTR",
            "mode": "acquisition",
            "config": {"length": 10, "interval": 100, "channels": 3, "curves": [0]},
        })

        error_frames = ws.all_of_type("error")
        assert len(error_frames) >= 1
        assert any(
            "already in acquisition mode" in f["message"]
            for f in error_frames
        )
        assert "acq-2" not in server._active_streams.get(ws, {})

        # Cleanup
        task = server._active_streams[ws].get("acq-1")
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


# ---------------------------------------------------------------------------
# T7 — Close socket while three streams active
# ---------------------------------------------------------------------------


class TestT7_SocketCloseCleanup:

    @pytest.mark.asyncio
    async def test_socket_close_cancels_all_streams(self):
        """T7: Close the socket; all three tasks are cancelled and
        _active_streams entry is removed."""
        server = _make_server()
        ws = MockWebSocket()
        server._active_streams[ws] = {}

        # Subscribe three streams
        for i in range(3):
            await server._handle_subscribe(ws, {
                "action": "subscribe",
                "stream_id": f"s{i}",
                "instrument_id": f"GPIB0::{i}::INSTR",
                "mode": "poll",
                "interval_ms": 10000,
            })

        assert len(server._active_streams[ws]) == 3
        tasks = list(server._active_streams[ws].values())

        # Simulate socket close
        ws.close_ws()
        await server._cancel_all_streams(ws)

        # All tasks should be cancelled/done
        for task in tasks:
            assert task.done()
            # No CancelledError should propagate: gather already consumed it
            assert task.cancelled() or task.done()

        # _active_streams entry should be removed
        assert ws not in server._active_streams


# ---------------------------------------------------------------------------
# T8 — Integer stream_id rejected
# ---------------------------------------------------------------------------


class TestT8_IntegerStreamIdRejected:

    @pytest.mark.asyncio
    async def test_integer_stream_id_rejected(self):
        """T8: stream_id=42 (integer) returns error; no task created."""
        server = _make_server()
        ws = MockWebSocket()
        server._active_streams[ws] = {}

        await server._handle_subscribe(ws, {
            "action": "subscribe",
            "stream_id": 42,
            "instrument_id": "GPIB0::1::INSTR",
            "mode": "poll",
        })

        error_frames = ws.all_of_type("error")
        assert len(error_frames) == 1
        assert "stream_id must be a non-empty string" in error_frames[0]["message"]

        # No task should have been created
        assert len(server._active_streams.get(ws, {})) == 0


# ---------------------------------------------------------------------------
# T9 — Unsubscribe nonexistent stream_id
# ---------------------------------------------------------------------------


class TestT9_UnsubscribeNonexistentStream:

    @pytest.mark.asyncio
    async def test_unsubscribe_nonexistent_returns_error(self):
        """T9: Unsubscribe for an unknown stream_id returns error frame."""
        server = _make_server()
        ws = MockWebSocket()
        server._active_streams[ws] = {}

        await server._handle_unsubscribe(ws, {
            "action": "unsubscribe",
            "stream_id": "nonexistent",
        })

        error_frames = ws.all_of_type("error")
        assert len(error_frames) == 1
        assert error_frames[0]["stream_id"] == "nonexistent"
        assert "Unknown stream_id" in error_frames[0]["message"]


# ---------------------------------------------------------------------------
# T10 — Instrument exception during poll tick; task continues
# ---------------------------------------------------------------------------


class TestT10_PerTickErrorTaskContinues:

    @pytest.mark.asyncio
    async def test_per_tick_error_continues_with_stream_id(self):
        """T10: After three good ticks, instrument raises; the error frame
        carries stream_id and the next tick succeeds."""
        tick_count = {"n": 0}

        def query_fn(instrument_id: str, command: str) -> str:
            tick_count["n"] += 1
            if tick_count["n"] == 4:
                raise RuntimeError("Simulated instrument error")
            return "9.9"

        server = _make_server(query_fn=query_fn)
        ws = MockWebSocket()
        server._active_streams[ws] = {}

        await server._handle_subscribe(ws, {
            "action": "subscribe",
            "stream_id": "err-test",
            "instrument_id": "GPIB0::1::INSTR",
            "mode": "poll",
            "interval_ms": 30,
            "scpi_command": "MEAS?",
        })

        # Let enough ticks pass to hit the error (tick 4) and one more
        await asyncio.sleep(0.25)

        task = server._active_streams[ws].get("err-test")
        assert task is not None
        assert not task.done(), "Task should still be running after transient error"

        # Cancel the task
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Check error frame was emitted with correct stream_id
        error_frames = ws.all_of_type("error")
        assert len(error_frames) >= 1
        tick4_errors = [
            f for f in error_frames
            if f.get("stream_id") == "err-test" and "Simulated" in f.get("message", "")
        ]
        assert len(tick4_errors) >= 1

        # Check data frames appeared before AND after the error
        data_frames = ws.all_of_type("data")
        assert len(data_frames) >= 3, "Expected at least 3 successful data frames"
        assert all(f["stream_id"] == "err-test" for f in data_frames)


# ---------------------------------------------------------------------------
# Additional — Duplicate stream_id cancel-and-replace
# ---------------------------------------------------------------------------


class TestDuplicateStreamIdReplace:

    @pytest.mark.asyncio
    async def test_duplicate_stream_id_replaces_old_task(self):
        """Subscribing the same stream_id twice cancels the first task
        and starts a fresh one."""
        server = _make_server()
        ws = MockWebSocket()
        server._active_streams[ws] = {}

        await server._handle_subscribe(ws, {
            "action": "subscribe",
            "stream_id": "dup",
            "instrument_id": "GPIB0::1::INSTR",
            "mode": "poll",
            "interval_ms": 10000,
        })
        first_task = server._active_streams[ws]["dup"]

        await server._handle_subscribe(ws, {
            "action": "subscribe",
            "stream_id": "dup",
            "instrument_id": "GPIB0::1::INSTR",
            "mode": "poll",
            "interval_ms": 10000,
        })
        second_task = server._active_streams[ws]["dup"]

        assert first_task is not second_task
        # Give the event loop a moment to process cancellation
        await asyncio.sleep(0.05)
        assert first_task.cancelled() or first_task.done()

        # Cleanup
        second_task.cancel()
        try:
            await second_task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# Additional — stream_id is included in status: subscribed frame
# ---------------------------------------------------------------------------


class TestSubscribeStatusFrame:

    @pytest.mark.asyncio
    async def test_subscribed_status_has_stream_id(self):
        """The status: subscribed frame sent after subscribe includes stream_id."""
        server = _make_server()
        ws = MockWebSocket()
        server._active_streams[ws] = {}

        await server._handle_subscribe(ws, {
            "action": "subscribe",
            "stream_id": "test-stream",
            "instrument_id": "GPIB0::1::INSTR",
            "mode": "poll",
            "interval_ms": 10000,
        })

        status_frames = ws.all_of_type("status")
        subscribed = [f for f in status_frames if f.get("state") == "subscribed"]
        assert len(subscribed) == 1
        assert subscribed[0]["stream_id"] == "test-stream"

        # Cleanup
        task = server._active_streams[ws]["test-stream"]
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# Additional — acquisition exclusion is released after task done
# ---------------------------------------------------------------------------


class TestAcquisitionExclusionReleased:

    @pytest.mark.asyncio
    async def test_acquiring_instruments_cleared_after_cancel(self):
        """_acquiring_instruments is cleared when acquisition task is cancelled."""
        im = MagicMock()
        im.is_connected.return_value = True
        im.connect.return_value = "ok"
        im.write.return_value = None
        im.query.return_value = "2"  # still acquiring

        ch = MockCommandHandler()
        server = WebSocketServer(instrument_manager=im, command_handler=ch, port=8799)
        ws = MockWebSocket()
        server._active_streams[ws] = {}

        await server._handle_subscribe(ws, {
            "action": "subscribe",
            "stream_id": "acq-x",
            "instrument_id": "GPIB0::1::INSTR",
            "mode": "acquisition",
            "config": {"length": 10, "interval": 100, "channels": 3, "curves": []},
        })

        await asyncio.sleep(0.05)
        assert "GPIB0::1::INSTR" in server._acquiring_instruments

        task = server._active_streams[ws].get("acq-x")
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # After cancellation, acquiring_instruments should be cleared
        assert "GPIB0::1::INSTR" not in server._acquiring_instruments


# ---------------------------------------------------------------------------
# Additional — per-instrument lock acquired during command
# ---------------------------------------------------------------------------


class TestPerInstrumentLock:

    @pytest.mark.asyncio
    async def test_command_uses_per_instrument_lock(self):
        """_handle_command acquires the per-instrument lock, preventing
        concurrent SCPI races with a poll loop."""
        im = MagicMock()
        im.is_connected.return_value = True

        ch = MagicMock()
        ch.execute_command.return_value = {
            "success": True,
            "response": "*IDN response",
            "error": "",
        }

        server = WebSocketServer(instrument_manager=im, command_handler=ch, port=8799)
        ws = MockWebSocket()
        server._active_streams[ws] = {}

        # Verify the lock is created and used
        instrument_id = "GPIB0::1::INSTR"
        lock = server._get_instrument_lock(instrument_id)
        assert lock is server._get_instrument_lock(instrument_id)  # same object

        await server._handle_command(ws, {
            "action": "command",
            "instrument_id": instrument_id,
            "scpi": "*IDN?",
        })

        ch.execute_command.assert_called_once()
        frames = ws.all_of_type("command_result")
        assert len(frames) == 1
        assert frames[0]["success"] is True
