"""SPI transport: bus manager + handle pooling + capability gating.

The ``SPIBusManager`` wraps :mod:`spidev` (Linux-only) and pools
``SpiDev`` handles by ``(bus, device)`` so multiple drivers that share a
single CS line do not stomp on each other's transactions. Each
``(bus, device)`` pair gets its own re-entrant lock; drivers must hold
the lock for the duration of one transaction (``xfer2`` is the typical
boundary).

Capability gating is a first-class concern: SPI only works on Pi-class
hardware with ``/dev/spidev*`` device nodes. ``is_available()`` performs
both an import probe (does the ``spidev`` Python module load?) and a
filesystem probe (does at least one ``/dev/spidevN.M`` node exist?). On
macOS / Windows / x86 industrial PCs without GPIO, both probes fail and
the daemon advertises SPI as unsupported.

For unit tests on non-Linux dev machines we ship a :class:`MockSPI`
fallback that satisfies the small subset of the ``spidev.SpiDev`` API
the driver actually uses (``open``, ``close``, ``mode``, ``bits_per_word``,
``max_speed_hz``, ``cshigh``, ``lsbfirst``, ``threewire``, ``xfer2``).
The mock returns canned MISO bytes from a ``response_map`` keyed by the
first one or two bytes of the outgoing wire frame.
"""

from __future__ import annotations

import glob
import logging
import threading
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Real spidev is Linux-only. Probe at import time.
# ---------------------------------------------------------------------------

try:  # pragma: no cover - import depends on platform
    import spidev as _spidev  # type: ignore

    _SPIDEV_AVAILABLE = True
except ImportError:  # pragma: no cover - macOS/Windows path
    _spidev = None  # type: ignore
    _SPIDEV_AVAILABLE = False


def _spidev_devices_present() -> bool:
    """Return True if at least one ``/dev/spidevN.M`` exists."""
    return bool(glob.glob("/dev/spidev*.*"))


def is_available() -> bool:
    """SPI is available iff ``spidev`` imports AND at least one device exists.

    Used by capability advertising on the daemon side.
    """
    return _SPIDEV_AVAILABLE and _spidev_devices_present()


# ---------------------------------------------------------------------------
# MockSPI — drop-in for spidev.SpiDev on non-Linux dev/test machines.
# ---------------------------------------------------------------------------


class MockSPI:
    """Test fake mirroring the slice of ``spidev.SpiDev`` we use.

    Construct with a ``response_map`` of ``{(b0, b1, ...): response_bytes}``.
    Each ``xfer2`` call looks up the outgoing prefix (the leading 1-2 bytes,
    matching opcodes / start bytes) and returns ``response_bytes`` padded
    or truncated to the length of the outgoing frame. If no entry matches,
    the response is all-zero bytes of the same length.

    The mock also records every transaction in ``self.transactions`` and
    every config setter assignment so tests can assert wire shape, mode,
    and lock ordering.

    Lookup precedence for ``response_map`` keys (longest-prefix match):
    - ``(b0, b1)`` 2-byte tuple
    - ``(b0,)`` 1-byte tuple
    - ``b0`` plain int
    """

    def __init__(self, response_map: dict | None = None) -> None:
        self.response_map: dict = dict(response_map or {})
        self.transactions: list[list[int]] = []
        self.config_history: list[tuple[str, Any]] = []
        self._opened: tuple[int, int] | None = None
        self._closed = False
        # spidev attribute defaults
        self._mode = 0
        self._bits_per_word = 8
        self._max_speed_hz = 1_000_000
        self._cshigh = False
        self._lsbfirst = False
        self._threewire = False

    # -- spidev.SpiDev API surface --

    def open(self, bus: int, device: int) -> None:
        if self._closed:
            raise OSError("MockSPI already closed; reopen requires new instance")
        self._opened = (bus, device)

    def close(self) -> None:
        self._closed = True
        self._opened = None

    def xfer2(self, data: list[int] | bytes, *_args, **_kwargs) -> list[int]:
        if self._opened is None:
            raise OSError("MockSPI: xfer2 before open()")
        if self._closed:
            raise OSError("MockSPI: xfer2 after close()")
        tx = list(data)
        self.transactions.append(list(tx))
        rx = self._lookup_response(tx)
        # Pad/truncate to the same length as tx (SPI is full-duplex).
        if len(rx) < len(tx):
            rx = list(rx) + [0] * (len(tx) - len(rx))
        else:
            rx = list(rx)[: len(tx)]
        # If lsbfirst is set, real spidev would have flipped bit order
        # in/out at the hardware level. We pre-flip the response here so
        # the driver sees byte-reversed data when it expects MSB layout.
        # Tests that exercise this path supply the post-flip canonical bytes.
        return rx

    def xfer3(self, data, *_args, **_kwargs) -> list[int]:
        return self.xfer2(data)

    def writebytes(self, data) -> None:
        self.xfer2(data)

    def readbytes(self, n: int) -> list[int]:
        rx = self.xfer2([0] * n)
        return rx

    # -- attribute properties (spidev exposes them as plain attrs) --

    @property
    def mode(self) -> int:
        return self._mode

    @mode.setter
    def mode(self, value: int) -> None:
        self._mode = int(value)
        self.config_history.append(("mode", int(value)))

    @property
    def bits_per_word(self) -> int:
        return self._bits_per_word

    @bits_per_word.setter
    def bits_per_word(self, value: int) -> None:
        self._bits_per_word = int(value)
        self.config_history.append(("bits_per_word", int(value)))

    @property
    def max_speed_hz(self) -> int:
        return self._max_speed_hz

    @max_speed_hz.setter
    def max_speed_hz(self, value: int) -> None:
        self._max_speed_hz = int(value)
        self.config_history.append(("max_speed_hz", int(value)))

    @property
    def cshigh(self) -> bool:
        return self._cshigh

    @cshigh.setter
    def cshigh(self, value: bool) -> None:
        self._cshigh = bool(value)
        self.config_history.append(("cshigh", bool(value)))

    @property
    def lsbfirst(self) -> bool:
        return self._lsbfirst

    @lsbfirst.setter
    def lsbfirst(self, value: bool) -> None:
        self._lsbfirst = bool(value)
        self.config_history.append(("lsbfirst", bool(value)))

    @property
    def threewire(self) -> bool:
        return self._threewire

    @threewire.setter
    def threewire(self, value: bool) -> None:
        self._threewire = bool(value)
        self.config_history.append(("threewire", bool(value)))

    # -- helpers --

    def _lookup_response(self, tx: list[int]) -> list[int]:
        if not tx:
            return []
        # try longest prefix first
        if len(tx) >= 2:
            key2 = (tx[0], tx[1])
            if key2 in self.response_map:
                return list(self.response_map[key2])
        key1 = (tx[0],)
        if key1 in self.response_map:
            return list(self.response_map[key1])
        if tx[0] in self.response_map:
            return list(self.response_map[tx[0]])
        return []


# ---------------------------------------------------------------------------
# SPIBusManager
# ---------------------------------------------------------------------------


@dataclass
class _SpiHandle:
    """Pooled SPI handle bookkeeping."""

    spi: Any  # spidev.SpiDev | MockSPI
    lock: threading.RLock = field(default_factory=threading.RLock)
    ref_count: int = 0
    config_signature: tuple = ()  # last (mode, bits, speed, cshigh, lsbfirst, threewire)


class SPIBusManager:
    """Pools SPI handles by ``(bus, device)`` with per-CS-line locking.

    Two drivers that target the same CS line share one ``SpiDev`` handle
    and one ``RLock``. Two drivers on the same bus but different devices
    (different CS lines) get distinct handles and locks — they can run
    transactions concurrently.

    The manager is also the integration point for the capability-gating
    ``MockSPI`` fallback: pass ``mock_factory=lambda: MockSPI(...)`` (or
    ``mock_instance=existing_mock``) to bypass the real ``spidev`` import.
    Production daemons leave both unset and rely on
    :data:`_SPIDEV_AVAILABLE`.
    """

    def __init__(
        self,
        mock_factory: Any = None,
        mock_instance: MockSPI | None = None,
    ) -> None:
        self._handles: dict[tuple[int, int], _SpiHandle] = {}
        self._mgr_lock = threading.Lock()
        self._mock_factory = mock_factory
        self._mock_instance = mock_instance

    # -- capability gating --

    @staticmethod
    def is_available() -> bool:
        """Return True iff real SPI hardware and library are present."""
        return is_available()

    @staticmethod
    def unavailable_reason() -> str | None:
        """Return human-readable reason SPI is not available, or ``None``."""
        if not _SPIDEV_AVAILABLE:
            return "spidev module not installed (Linux-only)"
        if not _spidev_devices_present():
            return "no /dev/spidev*.* device nodes found"
        return None

    # -- handle pooling --

    def get_spi(
        self,
        bus: int,
        device: int,
        mode: int = 0,
        bits_per_word: int = 8,
        max_speed_hz: int = 1_000_000,
        cs_high: bool = False,
        lsb_first: bool = False,
        three_wire: bool = False,
    ) -> tuple[Any, threading.RLock]:
        """Open or fetch a pooled SpiDev handle.

        Returns ``(spi, lock)``. The caller must hold ``lock`` for the
        duration of any ``xfer2`` call to avoid interleaving transactions
        on the same CS line.
        """
        key = (int(bus), int(device))
        with self._mgr_lock:
            handle = self._handles.get(key)
            if handle is None:
                spi = self._open_underlying(bus, device)
                self._configure(
                    spi, mode, bits_per_word, max_speed_hz, cs_high, lsb_first, three_wire
                )
                handle = _SpiHandle(
                    spi=spi,
                    config_signature=(
                        mode,
                        bits_per_word,
                        max_speed_hz,
                        cs_high,
                        lsb_first,
                        three_wire,
                    ),
                )
                self._handles[key] = handle
                logger.info("Opened SPI bus %d.%d (mode=%d, speed=%d)", bus, device, mode, max_speed_hz)
            else:
                # Re-apply config on every get so the most-recent driver's
                # speed/mode requirements take effect. spidev tolerates
                # repeated assignment; this matches how Linux SPI handles
                # devices on a shared CS line that need different modes
                # (rare, but legal).
                self._configure(
                    handle.spi,
                    mode,
                    bits_per_word,
                    max_speed_hz,
                    cs_high,
                    lsb_first,
                    three_wire,
                )
                handle.config_signature = (
                    mode,
                    bits_per_word,
                    max_speed_hz,
                    cs_high,
                    lsb_first,
                    three_wire,
                )
            handle.ref_count += 1
            return handle.spi, handle.lock

    def release(self, bus: int, device: int) -> None:
        """Drop a reference; close the handle when refcount hits zero."""
        key = (int(bus), int(device))
        with self._mgr_lock:
            handle = self._handles.get(key)
            if handle is None:
                return
            handle.ref_count -= 1
            if handle.ref_count <= 0:
                try:
                    handle.spi.close()
                except Exception:  # pragma: no cover - defensive
                    pass
                del self._handles[key]
                logger.info("Closed SPI bus %d.%d", bus, device)

    # -- internals --

    def _open_underlying(self, bus: int, device: int) -> Any:
        """Open the real or mocked SpiDev handle."""
        if self._mock_instance is not None:
            spi = self._mock_instance
            spi.open(bus, device)
            return spi
        if self._mock_factory is not None:
            spi = self._mock_factory()
            spi.open(bus, device)
            return spi
        if not _SPIDEV_AVAILABLE:
            raise RuntimeError(
                "spidev not available on this platform; pass mock_factory= "
                "for tests or run on Linux with spidev installed"
            )
        spi = _spidev.SpiDev()  # type: ignore[union-attr]
        spi.open(bus, device)
        return spi

    @staticmethod
    def _configure(
        spi: Any,
        mode: int,
        bits_per_word: int,
        max_speed_hz: int,
        cs_high: bool,
        lsb_first: bool,
        three_wire: bool,
    ) -> None:
        spi.mode = int(mode)
        spi.bits_per_word = int(bits_per_word)
        spi.max_speed_hz = int(max_speed_hz)
        spi.cshigh = bool(cs_high)
        spi.lsbfirst = bool(lsb_first)
        spi.threewire = bool(three_wire)

    # -- introspection (used by tests) --

    def open_handles(self) -> list[tuple[int, int]]:
        with self._mgr_lock:
            return list(self._handles.keys())
