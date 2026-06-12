"""
ScalarChunker + hardware-source tests (work order §7).

Pure-Python modules — no generated stubs or instrument I/O required.
"""

from __future__ import annotations

import os
import struct
import sys

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "src")
)

from galois_edge.scalar_chunker import (  # noqa: E402
    CHUNK_DTYPES,
    CHUNK_TRIGGER_MS,
    CHUNK_WINDOW_MS,
    DEFAULT_CHUNK_DTYPE,
    HW_PERIOD_FLOOR_MS,
    MAX_CHUNK_WINDOW_MS,
    ScalarChunker,
    clamp_sample_period,
    should_chunk,
)
from galois_edge.hw_stream import (  # noqa: E402
    HardwareSampleBlock,
    SyntheticDAQDriver,
    SyntheticDAQSource,
)


def unpack(data: bytes, dtype: str) -> list:
    fmt = CHUNK_DTYPES[dtype]
    size = struct.calcsize(fmt)
    return list(struct.unpack("<%d%s" % (len(data) // size, fmt), data))


# ---------------------------------------------------------------------------
# §7.2 — the negotiation-free trigger rule
# ---------------------------------------------------------------------------


class TestTriggerRule:
    def test_boundary_is_100ms(self):
        assert CHUNK_TRIGGER_MS == 100

    def test_chunks_only_below_100_on_hw_clocked(self):
        assert should_chunk(1, hardware_clocked=True) is True
        assert should_chunk(50, hardware_clocked=True) is True
        assert should_chunk(99, hardware_clocked=True) is True

    def test_never_chunks_at_or_above_100(self):
        # Old clouds reset sub-100 requests to 1000 ms — >= 100 MUST stay
        # per-point so pre-chunk clouds keep rendering.
        assert should_chunk(100, hardware_clocked=True) is False
        assert should_chunk(1000, hardware_clocked=True) is False

    def test_polled_commands_never_chunk(self):
        # Doc §7.2: ordinary polled commands keep per-point emission and
        # the 10 ms poll floor regardless of the requested interval.
        assert should_chunk(1, hardware_clocked=False) is False
        assert should_chunk(50, hardware_clocked=False) is False

    def test_zero_or_negative_interval_never_chunks(self):
        assert should_chunk(0, hardware_clocked=True) is False
        assert should_chunk(-5, hardware_clocked=True) is False


class TestSamplePeriodReinterpretation:
    def test_one_ms_floor(self):
        assert HW_PERIOD_FLOOR_MS == 1.0
        assert clamp_sample_period(0.2) == 1.0
        assert clamp_sample_period(0) == 1.0
        assert clamp_sample_period(-3) == 1.0

    def test_above_floor_passes_through(self):
        assert clamp_sample_period(1) == 1.0
        assert clamp_sample_period(5) == 5.0
        assert clamp_sample_period(99) == 99.0


# ---------------------------------------------------------------------------
# §7.3 — windowing
# ---------------------------------------------------------------------------


class TestWindowing:
    def test_50_samples_at_1khz(self):
        c = ScalarChunker(dt_ms=1.0, t0_ms=0.0)
        assert c.target_n == 50

    def test_block_duration_in_doc_band_for_every_legal_period(self):
        # 20–100 ms blocks (§7.3): never beyond 100 ms, never empty.
        for period in [1.0, 2.5, 5.0, 10.0, 20.0, 49.0, 50.0, 60.0, 99.0]:
            c = ScalarChunker(dt_ms=period, t0_ms=0.0)
            assert c.target_n >= 1
            assert c.target_n * period <= MAX_CHUNK_WINDOW_MS + 1e-9

    def test_add_signals_full_exactly_at_target(self):
        c = ScalarChunker(dt_ms=1.0, t0_ms=0.0)
        for i in range(c.target_n - 1):
            assert c.add(float(i)) is False
        assert c.add(99.0) is True

    def test_rejects_non_positive_period(self):
        with pytest.raises(ValueError):
            ScalarChunker(dt_ms=0.0, t0_ms=0.0)
        with pytest.raises(ValueError):
            ScalarChunker(dt_ms=-1.0, t0_ms=0.0)


# ---------------------------------------------------------------------------
# §7.3 — population
# ---------------------------------------------------------------------------


class TestPopulation:
    def test_float32_default_and_explicit_scale(self):
        assert DEFAULT_CHUNK_DTYPE == "float32"
        c = ScalarChunker(dt_ms=1.0, t0_ms=1000.0)
        ys = [0.5, -1.25, 2.0]
        for y in ys:
            c.add(y)
        chunks = c.take()
        assert len(chunks) == 1
        ch = chunks[0]
        assert ch["field"] == ""           # routes to the primary channel
        assert ch["y_dtype"] == "float32"
        assert ch["y_scale"] == 1.0        # explicit — never an unset zero
        assert ch["y_offset"] == 0.0
        assert ch["n"] == 3
        assert len(ch["y_data"]) == ch["n"] * 4   # n * sizeof(float32)
        assert unpack(ch["y_data"], "float32") == pytest.approx(ys)
        assert ch["t_data"] == b""         # uniform timebase: no t_data

    def test_t0_maps_hardware_ticks_onto_anchor(self):
        anchor = 1_750_000_000_000.0
        c = ScalarChunker(dt_ms=2.0, t0_ms=anchor)
        for i in range(c.target_n):
            c.add(float(i))
        first = c.take()[0]
        assert first["t0_ms"] == anchor
        assert first["dt_ms"] == 2.0
        for i in range(c.target_n):
            c.add(float(i))
        second = c.take()[0]
        # Second window starts exactly one window after the first.
        assert second["t0_ms"] == anchor + first["n"] * first["dt_ms"]

    def test_dtype_constraint_rejects_off_list_dtypes(self):
        # §2.4 five-dtype constraint: uint16/int8 decode as garbage
        # client-side and must be rejected at the producer.
        for bad in ("uint16", "int8", "float16", ""):
            with pytest.raises(ValueError):
                ScalarChunker(dt_ms=1.0, t0_ms=0.0, y_dtype=bad)

    def test_integer_dtype_packs_rounded_counts(self):
        c = ScalarChunker(dt_ms=1.0, t0_ms=0.0, y_dtype="int16")
        c.add(1.4)
        c.add(-2.6)
        ch = c.take()[0]
        assert unpack(ch["y_data"], "int16") == [1, -3]
        assert len(ch["y_data"]) == ch["n"] * 2

    def test_named_fields_get_their_own_chunks(self):
        c = ScalarChunker(dt_ms=1.0, t0_ms=500.0)
        c.add(1.0, {"X": 10.0, "phase": 0.5})
        c.add(2.0, {"X": 20.0, "phase": 0.6})
        chunks = {ch["field"]: ch for ch in c.take()}
        assert set(chunks) == {"", "X", "phase"}
        assert unpack(chunks[""]["y_data"], "float32") == pytest.approx([1.0, 2.0])
        assert unpack(chunks["X"]["y_data"], "float32") == pytest.approx([10.0, 20.0])
        assert unpack(chunks["phase"]["y_data"], "float32") == pytest.approx([0.5, 0.6])
        # All channels share the hardware tick clock here.
        assert chunks["X"]["t0_ms"] == 500.0

    def test_field_missing_ticks_falls_back_to_explicit_t_data(self):
        c = ScalarChunker(dt_ms=1.0, t0_ms=100.0)
        c.add(1.0, {"X": 10.0})
        c.add(2.0)                  # X skips this tick
        c.add(3.0, {"X": 30.0})
        chunks = {ch["field"]: ch for ch in c.take()}
        assert chunks[""]["t_data"] == b""   # primary stayed contiguous
        x = chunks["X"]
        assert x["n"] == 2
        ts = unpack(x["t_data"], "float64")
        assert ts == [100.0, 102.0]
        assert len(x["t_data"]) == x["n"] * 8

    def test_empty_take_yields_nothing(self):
        c = ScalarChunker(dt_ms=1.0, t0_ms=0.0)
        assert c.take() == []
        assert not c.pending


# ---------------------------------------------------------------------------
# §7.3 / §7.5 — monotonic non-overlapping windows over a long run
# ---------------------------------------------------------------------------


class TestMonotonicity:
    def test_long_synthetic_run_is_contiguous_per_channel(self):
        # 20k hardware ticks at 1 kHz with a named channel: assert the
        # §7.5 check — t0 strictly increasing and
        # |t0(k+1) - (t0(k) + n*dt)| < dt for consecutive chunks.
        anchor = 1_700_000_000_000.0
        c = ScalarChunker(dt_ms=1.0, t0_ms=anchor)
        emitted: dict = {"": [], "Q": []}
        for i in range(20_000):
            if c.add(float(i % 7), {"Q": float(i % 5)}):
                for ch in c.take():
                    emitted[ch["field"]].append(ch)
        if c.pending:
            for ch in c.take():
                emitted[ch["field"]].append(ch)

        for field, chunks in emitted.items():
            assert len(chunks) == 400  # 20000 ticks / 50-sample windows
            total_n = sum(ch["n"] for ch in chunks)
            assert total_n == 20_000
            for prev, nxt in zip(chunks, chunks[1:]):
                end = prev["t0_ms"] + prev["n"] * prev["dt_ms"]
                assert nxt["t0_ms"] > prev["t0_ms"]
                assert nxt["t0_ms"] >= end - 1e-9          # no overlap
                assert abs(nxt["t0_ms"] - end) < prev["dt_ms"]  # no stretch

    def test_rebase_starts_fresh_t0_without_stretching_dt(self):
        c = ScalarChunker(dt_ms=1.0, t0_ms=1000.0)
        for i in range(c.target_n):
            c.add(float(i))
        first = c.take()[0]
        # Overflow: fresh timebase well after the gap.
        c.rebase(5000.0)
        for i in range(c.target_n):
            c.add(float(i))
        second = c.take()[0]
        assert second["t0_ms"] == 5000.0
        assert second["dt_ms"] == first["dt_ms"] == 1.0  # never stretched
        assert second["t0_ms"] >= first["t0_ms"] + first["n"] * first["dt_ms"]

    def test_rebase_clamps_wall_clock_regressions(self):
        c = ScalarChunker(dt_ms=1.0, t0_ms=1000.0)
        for i in range(c.target_n):
            c.add(float(i))
        first = c.take()[0]
        end = first["t0_ms"] + first["n"] * first["dt_ms"]
        # NTP stepped the wall clock backwards across the overflow.
        c.rebase(900.0)
        c.add(1.0)
        ch = c.take()[0]
        assert ch["t0_ms"] >= end  # timeline never regresses

    def test_rebase_requires_flush_first(self):
        c = ScalarChunker(dt_ms=1.0, t0_ms=0.0)
        c.add(1.0)
        with pytest.raises(RuntimeError):
            c.rebase(100.0)


# ---------------------------------------------------------------------------
# hw_stream — HardwareSampleBlock / synthetic source & driver
# ---------------------------------------------------------------------------


class TestHardwareSampleBlock:
    def test_rows_pairs_primary_with_named_channels(self):
        b = HardwareSampleBlock(
            samples=[1.0, 2.0], values={"Q": [10.0, 20.0]}
        )
        assert list(b.rows()) == [(1.0, {"Q": 10.0}), (2.0, {"Q": 20.0})]
        assert len(b) == 2

    def test_rows_tolerates_short_named_channels(self):
        b = HardwareSampleBlock(samples=[1.0, 2.0], values={"Q": [10.0]})
        assert list(b.rows()) == [(1.0, {"Q": 10.0}), (2.0, None)]


class TestSyntheticDAQSource:
    def test_start_returns_quantized_readback_not_request(self):
        src = SyntheticDAQSource(quantum_ms=2.0)
        actual = src.start(3.0)
        assert actual == 4.0  # divider readback, != request

    def test_start_applies_one_ms_floor(self):
        src = SyntheticDAQSource(quantum_ms=0.5)
        assert src.start(0.1) == 1.0

    def test_read_drains_the_hardware_clock(self):
        src = SyntheticDAQSource(frequency_hz=10.0)
        src.start(1.0)
        import time as _t
        _t.sleep(0.03)
        block = src.read()
        assert not block.overflow
        assert len(block) >= 20  # ~30 ms at 1 kHz, minus scheduling slop
        # Second immediate read returns only the few new samples.
        assert len(src.read()) <= 5

    def test_fifo_overflow_reported_and_backlog_dropped(self):
        src = SyntheticDAQSource(fifo_depth=5)
        src.start(1.0)
        import time as _t
        _t.sleep(0.03)  # ~30 samples > depth 5
        block = src.read()
        assert block.overflow
        assert len(block) == 0
        nxt = src.read()
        assert not nxt.overflow  # recovered

    def test_read_before_start_is_empty(self):
        assert len(SyntheticDAQSource().read()) == 0


class TestSyntheticDAQDriver:
    def test_open_hw_stream_only_for_its_command(self):
        drv = SyntheticDAQDriver()
        assert drv.open_hw_stream("read_sine") is not None
        assert drv.open_hw_stream("other_cmd") is None

    def test_execute_command_returns_scalar(self):
        drv = SyntheticDAQDriver()
        v = drv.execute_command("read_sine", {"amplitude": 2.0})
        assert isinstance(v, float)
        assert -2.0 <= v <= 2.0
        with pytest.raises(ValueError):
            drv.execute_command("nope")

    def test_driver_shape_for_capability_registration(self):
        drv = SyntheticDAQDriver()
        assert isinstance(drv.identify(), str)
        caps = drv.get_capabilities()
        assert caps["commands"][0]["name"] == "read_sine"
        assert drv.transport_uri.startswith("synthetic://")
