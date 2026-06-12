"""
ScalarChunk assembly for hardware-clocked kHz scalar streams.

Implements the packing/windowing layer of the stream-transport addendum
(daemon work order `daemon-clean-required-changes.md` §7): when the cloud
requests ``interval_ms < 100`` on a chunk-capable (hardware-clocked / DAQ)
command, the daemon reinterprets ``interval_ms`` as the *sample period*
(1 ms daemon floor) and coalesces samples into ``ScalarChunk`` blocks
instead of emitting one gRPC message per sample.

The negotiation-free back-compat rule (§7.2) is load-bearing:

* Chunks are emitted ONLY when the requested ``interval_ms < 100``. Old
  clouds reset every request below 100 to 1000 ms before it reaches the
  daemon, so an old cloud can never trigger chunking — the request
  interval IS the negotiation. No capability field exists or is needed.
* Requests at ``interval_ms >= 100`` MUST stay per-point so pre-chunk
  clouds and frontends keep rendering.
* Ordinary polled commands keep per-point emission and the 10 ms poll
  floor regardless of the requested interval. Only hardware-clocked
  sources (see ``hw_stream.py``) feed this chunker.

Population rules implemented here (§7.3):

* 20–100 ms blocks (~50 samples at 1 kHz; never beyond 100 ms — the
  cloud's display cadence is built on 50 ms server flush ticks).
* ``t0_ms`` maps the hardware sample clock onto daemon wall-clock
  captured at acquisition start; ``dt_ms`` is the ACTUAL configured
  period (instrument readback when quantized), never the request.
* Chunk windows on one channel are monotonic and non-overlapping:
  ``next.t0_ms >= prev.t0_ms + prev.n * prev.dt_ms``.
* ``y_scale`` is explicitly 1.0 / ``y_offset`` 0.0 for pre-scaled data —
  never an unset zero multiplier.
* dtype is one of the five the cloud decodes
  (``float64|float32|int32|int16|uint8``); float32 is the recommended
  default for kHz scalars.
* ``field`` ``""`` routes to the stream's primary value channel; any
  other string routes to the ``values{}``-style named channel.

This module is dependency-free (no edge_pb2 import) so the packing and
windowing logic stays unit-testable without generated stubs —
``grpc_server.py`` converts the dicts produced here into proto messages.
"""

from __future__ import annotations

import struct
from typing import Dict, List, Optional, Tuple

# Requested intervals below this enable chunking on chunk-capable sources
# (§7.2 — the negotiation rule; >= 100 ms MUST stay per-point).
CHUNK_TRIGGER_MS = 100

# Daemon-side floor for the hardware sample period (1 kHz). The 10 ms
# POLL floor for ordinary polled commands is unchanged and lives in the
# poll loops, not here.
HW_PERIOD_FLOOR_MS = 1.0

# Target block duration. §7.3 allows 20–100 ms; the cloud's display
# cadence is built on 50 ms server flush ticks.
CHUNK_WINDOW_MS = 50.0

# Hard upper bound on block duration (§7.3: "Do not exceed 100 ms").
MAX_CHUNK_WINDOW_MS = 100.0

# The five dtypes the cloud client decodes (§2.4 constraint) -> struct
# format characters. Anything else silently falls through to a float64
# reinterpretation client-side (garbage), so reject early.
CHUNK_DTYPES: Dict[str, str] = {
    "float64": "d",
    "float32": "f",
    "int32": "i",
    "int16": "h",
    "uint8": "B",
}

# float32 halves the payload vs float64 with ample precision for kHz
# scalars (§7.3 recommendation).
DEFAULT_CHUNK_DTYPE = "float32"

_INTEGER_DTYPES = frozenset({"int32", "int16", "uint8"})


def should_chunk(requested_interval_ms: float, hardware_clocked: bool) -> bool:
    """The negotiation-free chunk rule (§7.2).

    True only for hardware-clocked sources with a requested interval in
    (0, 100) ms. ``interval_ms >= 100`` never chunks (old clouds reset
    sub-100 requests to 1000 ms, so the interval is the negotiation);
    polled commands never chunk regardless of interval.
    """
    return bool(hardware_clocked) and 0 < requested_interval_ms < CHUNK_TRIGGER_MS


def clamp_sample_period(requested_interval_ms: float) -> float:
    """Reinterpret a chunked request's interval as the sample period.

    The daemon-side floor for hardware-clocked sample periods is 1 ms
    (1 kHz). Returns the period to *request* from the hardware; the
    actual ``dt_ms`` must still come from the hardware readback.
    """
    return max(float(requested_interval_ms), HW_PERIOD_FLOOR_MS)


class ScalarChunker:
    """Accumulates hardware-clocked samples into ScalarChunk windows.

    One chunker per stream. Not thread-safe (the daemon's stream loop is
    a single coroutine).

    The chunker owns the stream's timebase: ``t0_ms`` anchors hardware
    tick 0 onto the daemon wall-clock captured at acquisition start, and
    every subsequent tick lands at ``t0_ms + k * dt_ms``. Windows drain
    contiguously, so consecutive chunks on one channel satisfy
    ``next.t0_ms == prev.t0_ms + prev.n * prev.dt_ms`` exactly; a
    ``rebase()`` (hardware FIFO overflow) starts a fresh timebase that is
    clamped to never regress below the end of the emitted timeline.
    """

    def __init__(
        self,
        dt_ms: float,
        t0_ms: float,
        window_ms: float = CHUNK_WINDOW_MS,
        y_dtype: str = DEFAULT_CHUNK_DTYPE,
    ) -> None:
        """
        Args:
            dt_ms: ACTUAL configured sample period in ms (instrument
                readback when the hardware quantizes the request) —
                never the requested value.
            t0_ms: daemon wall-clock (epoch ms) at acquisition start;
                hardware tick 0 maps to this instant.
            window_ms: target block duration (clamped to <= 100 ms).
            y_dtype: one of ``CHUNK_DTYPES``; float32 default.
        """
        if dt_ms <= 0:
            raise ValueError(f"dt_ms must be > 0, got {dt_ms!r}")
        if y_dtype not in CHUNK_DTYPES:
            raise ValueError(
                f"y_dtype must be one of {sorted(CHUNK_DTYPES)}, got {y_dtype!r}"
            )
        self.dt_ms = float(dt_ms)
        self.y_dtype = y_dtype

        # Samples per block: ~window_ms worth, never 0, never beyond the
        # 100 ms display-cadence bound (§7.3).
        window_ms = min(float(window_ms), MAX_CHUNK_WINDOW_MS)
        target = max(1, round(window_ms / self.dt_ms))
        max_n = max(1, int(MAX_CHUNK_WINDOW_MS // self.dt_ms))
        self.target_n = min(target, max_n)

        self._anchor = float(t0_ms)  # wall-clock of hardware tick 0
        self._tick = 0               # global hardware tick counter
        # Lower bound for the next window's t0 (monotonicity across
        # rebase(): the timeline must never regress).
        self._min_next_t0 = float(t0_ms)
        # field -> (tick indices, values); "" = primary value channel.
        self._fields: Dict[str, Tuple[List[int], List[float]]] = {}
        self._count = 0              # ticks buffered in the current window

    # ------------------------------------------------------------------

    @property
    def pending(self) -> bool:
        """True when at least one sample is buffered."""
        return self._count > 0

    def _t_of(self, tick: int) -> float:
        return self._anchor + tick * self.dt_ms

    def add(self, value: float, values: Optional[Dict[str, float]] = None) -> bool:
        """Append one hardware tick (primary value + optional named fields).

        Returns True when the window is full and the caller should
        ``take()`` and emit a chunk-bearing point.
        """
        k = self._tick
        self._append("", k, value)
        if values:
            for name, v in values.items():
                if name:
                    self._append(name, k, v)
        self._tick += 1
        self._count += 1
        return self._count >= self.target_n

    def _append(self, field: str, tick: int, y: float) -> None:
        ticks, ys = self._fields.setdefault(field, ([], []))
        ticks.append(tick)
        ys.append(float(y))

    def take(self) -> List[dict]:
        """Drain the buffered window into chunk dicts (one per field).

        Keys match the ScalarChunk proto field names:

            {field, t0_ms, dt_ms, n, y_data, y_dtype, y_scale,
             y_offset, t_data}

        ``t_data`` is ``b""`` for the common contiguous case (uniform
        ``t0/dt`` timebase); when a named field skipped ticks inside the
        window, that field's chunk carries explicit float64 epoch-ms
        timestamps instead (``t0_ms``/``dt_ms`` stay populated as the
        nominal window but are ignored by consumers).
        """
        fmt = CHUNK_DTYPES[self.y_dtype]
        integer = self.y_dtype in _INTEGER_DTYPES
        chunks: List[dict] = []
        for field, (ticks, ys) in self._fields.items():
            n = len(ys)
            if n == 0:
                continue
            samples = [int(round(y)) for y in ys] if integer else ys
            contiguous = ticks[-1] - ticks[0] == n - 1
            t_data = b""
            if not contiguous:
                t_data = struct.pack(
                    "<%dd" % n, *(self._t_of(k) for k in ticks)
                )
            chunks.append(
                {
                    "field": field,
                    "t0_ms": self._t_of(ticks[0]),
                    "dt_ms": self.dt_ms,  # ACTUAL period — never stretched
                    "n": n,
                    "y_data": struct.pack("<%d%s" % (n, fmt), *samples),
                    "y_dtype": self.y_dtype,
                    "y_scale": 1.0,  # explicit — never an unset zero (§7.3)
                    "y_offset": 0.0,
                    "t_data": t_data,
                }
            )
        self._fields = {}
        self._count = 0
        # The next window may not start before the end of this one.
        self._min_next_t0 = self._t_of(self._tick)
        return chunks

    def rebase(self, t0_ms: float) -> None:
        """Start a fresh timebase after a hardware FIFO overflow / gap.

        Per §7.4 the next chunk starts a fresh ``t0_ms`` — ``dt_ms`` is
        NEVER stretched to paper over a gap. The new anchor is clamped so
        the per-channel timeline never regresses below the end of the
        already-emitted windows (§7.3 monotonicity).

        The pending window must be flushed (``take()``) first; buffered
        samples belong to the old timebase and would otherwise be
        silently re-stamped.
        """
        if self.pending:
            raise RuntimeError("take() the pending window before rebase()")
        # Account for ticks consumed since the last take() (none, given
        # the not-pending precondition, but keep the bound tight).
        self._min_next_t0 = max(self._min_next_t0, self._t_of(self._tick))
        self._anchor = max(float(t0_ms), self._min_next_t0)
        self._tick = 0
