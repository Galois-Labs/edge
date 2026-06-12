"""
Hardware-clocked sample sources for chunked scalar streaming (doc §7).

Chunked emission (see ``scalar_chunker.py``) is fed exclusively by
hardware-clocked sources — acquisition paths where an instrument-side
sample clock (DAQ FIFO, CAN bus timestamps, lock-in curve buffers, ...)
paces the samples, not the daemon's poll loop. Ordinary polled commands
keep per-point emission and the 10 ms poll floor regardless of the
requested interval (§7.2).

A protocol driver (``drivers/base.py`` subclass or any driver-shaped
object registered through ``CapabilityManager.register_protocol_driver``)
advertises chunk capability by exposing:

    open_hw_stream(command_name, params) -> source | None

The returned source contract (duck-typed):

    start(period_ms: float) -> float
        Configure the hardware sample clock to ``period_ms`` (the daemon
        has already clamped the request to the 1 ms floor). Returns the
        ACTUAL configured period in ms — the hardware readback when the
        instrument quantizes the request. The daemon uses the return
        value as ``ScalarChunk.dt_ms``, never the request (§7.3).
    read() -> HardwareSampleBlock
        Drain the samples accumulated since the previous ``read()`` (the
        hardware FIFO). Must not block waiting for future samples. Set
        ``overflow=True`` when the FIFO overran and samples were lost:
        the daemon then emits a sequenced ``status:"error"`` point and
        the next chunk starts a fresh ``t0_ms`` (§7.4 — ``dt_ms`` is
        never stretched to paper over the gap).
    stop() -> None
        Tear down the acquisition. Must be cheap / non-blocking (it is
        called from the stream generator's finally block).

Optional attribute: ``unit`` (str) — copied onto each
``MeasurementDataPoint.unit``.

``SyntheticDAQSource`` / ``SyntheticDAQDriver`` below are the in-repo
synthetic producer: a sine generator whose "hardware clock" is the host
monotonic clock. They exist so the chunked path is exercisable end to
end (tests, demos, soak runs) without DAQ hardware, and they double as
the reference implementation of the contract for the CAN/I2C/SPI/OPC-UA
driver tracks.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .scalar_chunker import HW_PERIOD_FLOOR_MS


@dataclass
class HardwareSampleBlock:
    """One FIFO drain from a hardware-clocked source.

    ``samples`` is the primary channel ("" field); ``values`` maps named
    channels to sample lists of the same length (shorter lists are
    tolerated — missing ticks fall back to explicit timestamps in the
    chunker). ``overflow`` reports a FIFO overrun: samples were lost
    *before* this block, and the block's samples (if any) belong to the
    fresh post-overflow timebase.
    """

    samples: List[float] = field(default_factory=list)
    values: Dict[str, List[float]] = field(default_factory=dict)
    overflow: bool = False

    def __len__(self) -> int:
        return len(self.samples)

    def rows(self) -> Iterator[Tuple[float, Optional[Dict[str, float]]]]:
        """Iterate hardware ticks as (value, values{}) pairs."""
        for k, v in enumerate(self.samples):
            if self.values:
                row = {
                    name: vs[k]
                    for name, vs in self.values.items()
                    if k < len(vs)
                }
                yield v, row or None
            else:
                yield v, None


class SyntheticDAQSource:
    """Synthetic hardware-clocked source: a sine generator paced by the
    host monotonic clock standing in for a DAQ sample clock + FIFO.

    The "hardware" quantizes requested periods to multiples of
    ``quantum_ms`` (like real DAQ clock dividers), so ``start()`` returns
    a readback that can differ from the request — exactly the case the
    §7.3 "dt from readback, not request" rule exists for.
    """

    def __init__(
        self,
        frequency_hz: float = 10.0,
        amplitude: float = 1.0,
        unit: str = "V",
        quantum_ms: float = 1.0,
        fifo_depth: int = 8192,
        emit_quadrature: bool = False,
    ) -> None:
        if quantum_ms <= 0:
            raise ValueError("quantum_ms must be > 0")
        if fifo_depth < 1:
            raise ValueError("fifo_depth must be >= 1")
        self.frequency_hz = float(frequency_hz)
        self.amplitude = float(amplitude)
        self.unit = unit
        self.quantum_ms = float(quantum_ms)
        self.fifo_depth = int(fifo_depth)
        self.emit_quadrature = bool(emit_quadrature)

        self._period_ms = 0.0
        self._t_start = 0.0   # monotonic time of hardware tick 0
        self._consumed = 0    # ticks already drained
        self._running = False

    # -- source contract ------------------------------------------------

    def start(self, period_ms: float) -> float:
        """Configure the sample clock; return the ACTUAL (quantized) period."""
        period_ms = max(float(period_ms), HW_PERIOD_FLOOR_MS)
        # Hardware clock divider: quantize to the nearest quantum, >= 1.
        divider = max(1, round(period_ms / self.quantum_ms))
        self._period_ms = divider * self.quantum_ms
        self._t_start = time.monotonic()
        self._consumed = 0
        self._running = True
        return self._period_ms

    def read(self) -> HardwareSampleBlock:
        """Drain every sample the hardware clock has produced so far."""
        if not self._running:
            return HardwareSampleBlock()
        elapsed_ms = (time.monotonic() - self._t_start) * 1000.0
        produced = int(elapsed_ms / self._period_ms)
        backlog = produced - self._consumed
        if backlog > self.fifo_depth:
            # FIFO overran: the backlog is lost. Drop it and report the
            # overflow; the daemon rebases the timebase (§7.4).
            self._consumed = produced
            return HardwareSampleBlock(overflow=True)
        samples: List[float] = []
        quad: List[float] = []
        w = 2.0 * math.pi * self.frequency_hz
        for k in range(self._consumed, produced):
            t_s = k * self._period_ms / 1000.0
            samples.append(self.amplitude * math.sin(w * t_s))
            if self.emit_quadrature:
                quad.append(self.amplitude * math.cos(w * t_s))
        self._consumed = produced
        values = {"Q": quad} if self.emit_quadrature else {}
        return HardwareSampleBlock(samples=samples, values=values)

    def stop(self) -> None:
        self._running = False


class SyntheticDAQDriver:
    """Protocol-driver-shaped synthetic DAQ.

    Register through ``CapabilityManager.register_protocol_driver`` to
    expose one chunk-capable command, ``read_sine``:

    * ``StreamMeasurement`` with ``interval_ms < 100`` → chunked
      hardware-clocked emission via ``open_hw_stream``.
    * ``interval_ms >= 100`` (or ``ExecuteCommand``) → ordinary per-point
      polling via ``execute_command`` — the §7.2 back-compat half of the
      negotiation rule.

    Parameters accepted by ``read_sine``: ``frequency_hz``, ``amplitude``
    (both optional).
    """

    HW_COMMANDS = ("read_sine",)

    def __init__(
        self,
        instrument_id: str = "synthetic-daq-0",
        transport_uri: str = "synthetic://daq0",
        quantum_ms: float = 1.0,
        fifo_depth: int = 8192,
    ) -> None:
        self.instrument_id = instrument_id
        self.transport_uri = transport_uri
        self.quantum_ms = float(quantum_ms)
        self.fifo_depth = int(fifo_depth)
        self._connected = True
        self._t0 = time.monotonic()

    # -- driver surface (subset used by CapabilityManager) ---------------

    @property
    def connected(self) -> bool:
        return self._connected

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def identify(self) -> str:
        return "Galois,SyntheticDAQ,000000,1.0"

    def get_capabilities(self) -> dict:
        return {
            "protocol": "synthetic",
            "commands": [
                {
                    "name": "read_sine",
                    "description": "Synthetic sine sample (chunk-capable)",
                    "type": "query",
                    "unit": "V",
                    "is_streamable": True,
                }
            ],
            "registers": 0,
        }

    def execute_command(self, command_name: str, params: Optional[dict] = None) -> Any:
        """Per-point read: one immediate sine sample (wall-clock phase)."""
        if command_name not in self.HW_COMMANDS:
            raise ValueError(f"Unknown command: {command_name}")
        freq, amp = self._sine_params(params)
        t_s = time.monotonic() - self._t0
        return amp * math.sin(2.0 * math.pi * freq * t_s)

    # -- chunk capability (doc §7) ---------------------------------------

    def open_hw_stream(self, command_name: str, params: Optional[dict] = None):
        """Return a hardware-clocked source for chunk-capable commands."""
        if command_name not in self.HW_COMMANDS:
            return None
        freq, amp = self._sine_params(params)
        return SyntheticDAQSource(
            frequency_hz=freq,
            amplitude=amp,
            unit="V",
            quantum_ms=self.quantum_ms,
            fifo_depth=self.fifo_depth,
        )

    @staticmethod
    def _sine_params(params: Optional[dict]) -> Tuple[float, float]:
        params = params or {}
        try:
            freq = float(params.get("frequency_hz", 10.0))
        except (TypeError, ValueError):
            freq = 10.0
        try:
            amp = float(params.get("amplitude", 1.0))
        except (TypeError, ValueError):
            amp = 1.0
        return freq, amp
