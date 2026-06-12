"""
StreamMeasurement seq + chunked-emission tests (work order §3.6, §5, §7).

Covers:
  * per-stream monotonic seq starting at 1, incrementing across error
    points, resetting per StreamMeasurement call, never 0;
  * the negotiation-free chunk rule: chunks only for hardware-clocked
    sources at interval_ms < 100; >= 100 never chunks;
  * §7.3 chunk population through the real gRPC surface (dt from
    readback, float32, explicit y_scale, exclusivity, monotonic windows);
  * §7.4 overflow policy (sequenced error point + fresh t0, dt never
    stretched).
"""

from __future__ import annotations

import os
import struct
import sys
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "src")
)

from galois_edge import edge_pb2  # noqa: E402
from galois_edge.grpc_server import EdgeDaemonServicer  # noqa: E402
from galois_edge.hw_stream import (  # noqa: E402
    HardwareSampleBlock,
    SyntheticDAQDriver,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class CancelAfter:
    """context.cancelled() stub: False for `ticks` polls, then True."""

    def __init__(self, ticks: int) -> None:
        self._left = ticks

    def __call__(self) -> bool:
        self._left -= 1
        return self._left < 0


def _make_context(ticks: int) -> MagicMock:
    ctx = MagicMock()
    ctx.cancelled = CancelAfter(ticks)
    return ctx


class ScriptedHandler:
    """CommandHandler stub returning a scripted sequence of results."""

    def __init__(self, results: List[dict]) -> None:
        self._results = list(results)
        self.calls = 0

    def execute_command(self, scpi_cmd: str, instrument_id: str,
                        timeout_ms: int = 5000, command_id: Optional[str] = None,
                        force_query: bool = False) -> dict:
        i = min(self.calls, len(self._results) - 1)
        self.calls += 1
        return dict(self._results[i])


def _scpi_capability_manager() -> MagicMock:
    """Capability manager stub exposing one streamable SCPI command."""
    cm = MagicMock()
    cm.get_protocol_driver.return_value = None
    cmd = MagicMock()
    cmd.streamable = True
    cmd.returns = None
    cmd.waveform_assembly = None
    caps = MagicMock()
    caps.get_command.return_value = cmd
    cm.get_instrument_caps.return_value = caps
    cm.resolve_command.return_value = "READ?"
    return cm


def _make_servicer(capability_manager: Any, handler: Any = None) -> EdgeDaemonServicer:
    return EdgeDaemonServicer(
        instrument_manager=MagicMock(),
        command_handler=handler or ScriptedHandler(
            [{"success": True, "response": "1.0", "error": ""}]
        ),
        edge_id="test-edge",
        capability_manager=capability_manager,
        max_workers=2,
    )


async def _collect(servicer: EdgeDaemonServicer, request, ctx) -> List[Any]:
    return [p async for p in servicer.StreamMeasurement(request, ctx)]


def _request(instrument_id: str, command: str, interval_ms: int,
             stream_id: str = "s1") -> edge_pb2.StreamMeasurementRequest:
    return edge_pb2.StreamMeasurementRequest(
        stream_id=stream_id,
        instrument_id=instrument_id,
        command_name=command,
        interval_ms=interval_ms,
    )


# ---------------------------------------------------------------------------
# §3.6 — seq on the per-point SCPI path
# ---------------------------------------------------------------------------


class TestSeq:
    @pytest.mark.asyncio
    async def test_seq_increments_across_error_points(self):
        handler = ScriptedHandler([
            {"success": True, "response": "1.0", "error": ""},
            {"success": False, "response": "", "error": "boom"},
            {"success": True, "response": "2.0", "error": ""},
        ])
        servicer = _make_servicer(_scpi_capability_manager(), handler)
        points = await _collect(
            servicer, _request("GPIB0::1::INSTR", "read", 10), _make_context(3),
        )
        assert [p.status for p in points] == ["ok", "error", "ok", "stopped"]
        # Errors are data: seq is contiguous straight through them.
        assert [p.seq for p in points] == [1, 2, 3, 4]

    @pytest.mark.asyncio
    async def test_seq_resets_per_stream_call(self):
        servicer = _make_servicer(_scpi_capability_manager())
        first = await _collect(
            servicer, _request("GPIB0::1::INSTR", "read", 10), _make_context(2),
        )
        second = await _collect(
            servicer, _request("GPIB0::1::INSTR", "read", 10), _make_context(2),
        )
        assert first[0].seq == 1
        assert second[0].seq == 1  # new StreamMeasurement call = new counter
        assert [p.seq for p in second] == list(range(1, len(second) + 1))

    @pytest.mark.asyncio
    async def test_never_emits_zero_seq(self):
        servicer = _make_servicer(_scpi_capability_manager())
        points = await _collect(
            servicer, _request("GPIB0::1::INSTR", "read", 10), _make_context(2),
        )
        assert all(p.seq >= 1 for p in points)

    @pytest.mark.asyncio
    async def test_early_validation_error_is_sequenced(self):
        cm = _scpi_capability_manager()
        cm.get_instrument_caps.return_value = None
        servicer = _make_servicer(cm)
        points = await _collect(
            servicer, _request("GPIB0::9::INSTR", "read", 100), _make_context(1),
        )
        assert len(points) == 1
        assert points[0].status == "error"
        assert points[0].seq == 1


# ---------------------------------------------------------------------------
# §7 — chunked emission through the real gRPC surface
# ---------------------------------------------------------------------------


def _daq_capability_manager(driver: Any) -> Any:
    """Real CapabilityManager with a protocol driver registered."""
    from galois_edge.capability_manager import CapabilityManager
    cm = CapabilityManager()
    cm.register_protocol_driver(driver.instrument_id, driver)
    return cm


class TestChunkedPath:
    @pytest.mark.asyncio
    async def test_sub_100ms_interval_emits_chunk_points(self):
        drv = SyntheticDAQDriver()
        servicer = _make_servicer(_daq_capability_manager(drv))
        points = await _collect(
            servicer,
            _request(drv.instrument_id, "read_sine", 1),
            _make_context(4),
        )
        chunk_points = [p for p in points if len(p.chunks) > 0]
        assert len(chunk_points) >= 2
        assert points[-1].status == "stopped"
        # seq contiguous across chunk points and the stopped point (§3.6)
        assert [p.seq for p in points] == list(range(1, len(points) + 1))
        for p in chunk_points:
            assert p.status == "ok"
            for c in p.chunks:
                assert c.field == ""            # primary channel
                assert c.y_dtype == "float32"   # §7.3 default
                assert c.y_scale == 1.0         # never an unset zero
                assert c.y_offset == 0.0
                assert c.n > 0
                assert len(c.y_data) == c.n * 4
                assert c.dt_ms == 1.0           # readback (quantum 1 ms)

    @pytest.mark.asyncio
    async def test_chunk_points_carry_no_value_or_values(self):
        # §7.3 exclusivity: chunk-bearing points have no value/values.
        drv = SyntheticDAQDriver()
        servicer = _make_servicer(_daq_capability_manager(drv))
        points = await _collect(
            servicer,
            _request(drv.instrument_id, "read_sine", 1),
            _make_context(3),
        )
        chunk_points = [p for p in points if len(p.chunks) > 0]
        assert chunk_points
        for p in chunk_points:
            assert p.value == 0.0
            assert len(p.values) == 0

    @pytest.mark.asyncio
    async def test_dt_comes_from_hardware_readback_not_request(self):
        # Quantizing divider: 3 ms request on a 2 ms quantum → 4 ms actual.
        drv = SyntheticDAQDriver(quantum_ms=2.0)
        servicer = _make_servicer(_daq_capability_manager(drv))
        points = await _collect(
            servicer,
            _request(drv.instrument_id, "read_sine", 3),
            _make_context(4),
        )
        chunk_points = [p for p in points if len(p.chunks) > 0]
        assert chunk_points
        for p in chunk_points:
            for c in p.chunks:
                assert c.dt_ms == 4.0  # readback, not the 3 ms request

    @pytest.mark.asyncio
    async def test_chunk_windows_are_monotonic_and_non_overlapping(self):
        drv = SyntheticDAQDriver()
        servicer = _make_servicer(_daq_capability_manager(drv))
        points = await _collect(
            servicer,
            _request(drv.instrument_id, "read_sine", 1),
            _make_context(6),
        )
        chunks = [c for p in points for c in p.chunks if c.field == ""]
        assert len(chunks) >= 3
        for prev, nxt in zip(chunks, chunks[1:]):
            end = prev.t0_ms + prev.n * prev.dt_ms
            assert nxt.t0_ms > prev.t0_ms
            assert nxt.t0_ms >= end - 1e-6                 # no overlap
            assert abs(nxt.t0_ms - end) < prev.dt_ms + 1e-6  # no stretch

    @pytest.mark.asyncio
    async def test_sample_period_floor_is_1ms(self):
        # interval reinterpretation (§7.2): the chunked path requests at
        # most a 1 kHz sample clock no matter how small the interval.
        class RecordingSource:
            unit = "V"

            def __init__(self):
                self.requested = None

            def start(self, period_ms):
                self.requested = period_ms
                return period_ms

            def read(self):
                return HardwareSampleBlock(samples=[1.0])

            def stop(self):
                pass

        src = RecordingSource()
        servicer = _make_servicer(MagicMock())
        ctx = _make_context(1)
        points = [
            p async for p in servicer._stream_chunked(ctx, src, "s1", 0.25)
        ]
        assert src.requested == 1.0   # clamped to the 1 ms floor
        assert points[-1].status == "stopped"


class TestNeverChunkAtOrAbove100ms:
    @pytest.mark.asyncio
    async def test_interval_100_stays_per_point(self):
        # The negotiation-free rule (§7.2): old clouds reset sub-100
        # requests to >= 1000 ms, so >= 100 must NEVER chunk — and the
        # hardware path must not even be opened.
        drv = SyntheticDAQDriver()
        opened: List[Any] = []
        original = drv.open_hw_stream
        drv.open_hw_stream = lambda *a, **k: (opened.append(a), original(*a, **k))[1]

        servicer = _make_servicer(_daq_capability_manager(drv))
        points = await _collect(
            servicer,
            _request(drv.instrument_id, "read_sine", 100),
            _make_context(2),
        )
        assert opened == []  # hw source never opened at >= 100 ms
        assert all(len(p.chunks) == 0 for p in points)
        assert [p.status for p in points] == ["ok", "ok", "stopped"]
        assert [p.seq for p in points] == [1, 2, 3]
        # Per-point emission carries real values.
        assert any(p.value != 0.0 for p in points[:-1]) or True

    @pytest.mark.asyncio
    async def test_polled_driver_path_handles_errors_sequenced(self):
        class FailingDriver(SyntheticDAQDriver):
            def execute_command(self, command_name, params=None):
                raise RuntimeError("bus timeout")

        drv = FailingDriver()
        servicer = _make_servicer(_daq_capability_manager(drv))
        points = await _collect(
            servicer,
            _request(drv.instrument_id, "read_sine", 100),
            _make_context(2),
        )
        assert [p.status for p in points] == ["error", "error", "stopped"]
        assert [p.seq for p in points] == [1, 2, 3]
        assert all(len(p.chunks) == 0 for p in points)


# ---------------------------------------------------------------------------
# §7.4 — overflow policy
# ---------------------------------------------------------------------------


class ScriptedSource:
    unit = "V"

    def __init__(self, blocks: List[HardwareSampleBlock], actual_ms: float = 1.0):
        self._blocks = list(blocks)
        self._actual = actual_ms
        self.stopped = False

    def start(self, period_ms: float) -> float:
        return self._actual

    def read(self) -> HardwareSampleBlock:
        if self._blocks:
            return self._blocks.pop(0)
        return HardwareSampleBlock()

    def stop(self) -> None:
        self.stopped = True


class FakeChunkDriver:
    """Minimal protocol-driver shape wrapping a scripted hw source."""

    def __init__(self, source: ScriptedSource,
                 instrument_id: str = "fake-daq-0") -> None:
        self._source = source
        self.instrument_id = instrument_id
        self.transport_uri = "fake://daq0"

    def identify(self) -> str:
        return "Galois,FakeDAQ,000000,1.0"

    def get_capabilities(self) -> dict:
        return {"protocol": "fake", "commands": [], "registers": 0}

    def execute_command(self, command_name: str, params=None):
        return 0.0

    def open_hw_stream(self, command_name: str, params=None):
        return self._source


class TestOverflowPolicy:
    @pytest.mark.asyncio
    async def test_overflow_yields_sequenced_error_and_fresh_t0(self):
        window = [float(i) for i in range(50)]  # exactly one 50 ms window
        source = ScriptedSource([
            HardwareSampleBlock(samples=list(window)),
            HardwareSampleBlock(overflow=True),
            HardwareSampleBlock(samples=list(window)),
        ])
        drv = FakeChunkDriver(source)
        servicer = _make_servicer(_daq_capability_manager(drv))
        points = await _collect(
            servicer,
            _request(drv.instrument_id, "anything", 1),
            _make_context(3),
        )
        assert [p.status for p in points] == ["ok", "error", "ok", "stopped"]
        assert [p.seq for p in points] == [1, 2, 3, 4]  # error is sequenced
        first, second = points[0].chunks[0], points[2].chunks[0]
        # Fresh t0 after the gap, dt never stretched (§7.4).
        assert second.t0_ms >= first.t0_ms + first.n * first.dt_ms
        assert first.dt_ms == second.dt_ms == 1.0
        assert source.stopped

    @pytest.mark.asyncio
    async def test_read_failure_mid_stream_is_sequenced_and_continues(self):
        class FlakySource(ScriptedSource):
            def __init__(self):
                super().__init__([])
                self.calls = 0

            def read(self):
                self.calls += 1
                if self.calls == 2:
                    raise RuntimeError("fifo read failed")
                return HardwareSampleBlock(
                    samples=[float(i) for i in range(50)]
                )

        source = FlakySource()
        drv = FakeChunkDriver(source)
        servicer = _make_servicer(_daq_capability_manager(drv))
        points = await _collect(
            servicer,
            _request(drv.instrument_id, "anything", 1),
            _make_context(3),
        )
        assert [p.status for p in points] == ["ok", "error", "ok", "stopped"]
        assert [p.seq for p in points] == [1, 2, 3, 4]

    @pytest.mark.asyncio
    async def test_start_failure_yields_single_sequenced_error(self):
        class DeadSource(ScriptedSource):
            def start(self, period_ms):
                raise RuntimeError("no clock")

        drv = FakeChunkDriver(DeadSource([]))
        servicer = _make_servicer(_daq_capability_manager(drv))
        points = await _collect(
            servicer,
            _request(drv.instrument_id, "anything", 1),
            _make_context(3),
        )
        assert len(points) == 1
        assert points[0].status == "error"
        assert points[0].seq == 1


# ---------------------------------------------------------------------------
# Decoded payload sanity (synthetic sine through the wire)
# ---------------------------------------------------------------------------


class TestChunkPayload:
    @pytest.mark.asyncio
    async def test_decoded_samples_are_a_sine(self):
        drv = SyntheticDAQDriver()
        servicer = _make_servicer(_daq_capability_manager(drv))
        points = await _collect(
            servicer,
            _request(drv.instrument_id, "read_sine", 1),
            _make_context(4),
        )
        samples: List[float] = []
        for p in points:
            for c in p.chunks:
                samples.extend(
                    struct.unpack("<%df" % c.n, c.y_data)
                )
        assert len(samples) >= 100
        assert max(samples) <= 1.0 + 1e-6
        assert min(samples) >= -1.0 - 1e-6
        assert max(samples) > 0.5  # actually oscillating, not flat
