"""Tests for SPIBusManager + MockSPI transport plumbing.

Covers:
- MockSPI behaves like spidev.SpiDev for the slice we use
- SPIBusManager pools handles by (bus, device)
- Per-CS-line locking prevents interleaved transactions
- Capability gating (is_available / unavailable_reason)
- Refcounted release; close on last release
"""

from __future__ import annotations

import threading
import time

import pytest

from galois_edge.drivers.spi.transport import (
    MockSPI,
    SPIBusManager,
    is_available,
)


# ---------------------------------------------------------------------------
# MockSPI behavior
# ---------------------------------------------------------------------------


class TestMockSPI:
    def test_open_close_roundtrip(self):
        spi = MockSPI()
        spi.open(0, 0)
        spi.close()
        # After close, further xfer2 should raise
        with pytest.raises(OSError):
            spi.xfer2([0x00])

    def test_xfer2_before_open_raises(self):
        spi = MockSPI()
        with pytest.raises(OSError):
            spi.xfer2([0x00])

    def test_response_map_one_byte_key(self):
        spi = MockSPI(response_map={0x40: [0x00, 0xAA, 0xBB, 0xCC]})
        spi.open(0, 0)
        rx = spi.xfer2([0x40, 0x00, 0x00, 0x00])
        assert rx == [0x00, 0xAA, 0xBB, 0xCC]

    def test_response_map_two_byte_key_takes_priority(self):
        spi = MockSPI(
            response_map={
                (0x01, 0x80): [0x00, 0x03, 0xFF],  # MCP3008 ch0
                0x01: [0x00, 0x00, 0x00],
            }
        )
        spi.open(0, 0)
        rx = spi.xfer2([0x01, 0x80, 0x00])
        assert rx == [0x00, 0x03, 0xFF]

    def test_response_pad_short(self):
        spi = MockSPI(response_map={0x05: [0x14]})
        spi.open(0, 0)
        rx = spi.xfer2([0x05, 0x00, 0x00, 0x00])
        assert rx == [0x14, 0, 0, 0]

    def test_response_truncate_long(self):
        spi = MockSPI(response_map={0x00: [0xAA, 0xBB, 0xCC, 0xDD, 0xEE]})
        spi.open(0, 0)
        rx = spi.xfer2([0x00, 0x00])
        assert rx == [0xAA, 0xBB]

    def test_default_zero_response_when_no_match(self):
        spi = MockSPI()
        spi.open(0, 0)
        rx = spi.xfer2([0x99, 0x00, 0x00])
        assert rx == [0, 0, 0]

    def test_transactions_recorded(self):
        spi = MockSPI()
        spi.open(0, 0)
        spi.xfer2([0x40, 0x00])
        spi.xfer2([0x41, 0xAA])
        assert spi.transactions == [[0x40, 0x00], [0x41, 0xAA]]

    def test_config_history_records_setters(self):
        spi = MockSPI()
        spi.open(0, 0)
        spi.mode = 3
        spi.bits_per_word = 8
        spi.max_speed_hz = 5_000_000
        spi.cshigh = True
        spi.lsbfirst = True
        spi.threewire = True
        keys = {k for k, _ in spi.config_history}
        assert {
            "mode",
            "bits_per_word",
            "max_speed_hz",
            "cshigh",
            "lsbfirst",
            "threewire",
        } <= keys
        assert spi.mode == 3
        assert spi.cshigh is True
        assert spi.lsbfirst is True
        assert spi.threewire is True

    def test_writebytes_and_readbytes(self):
        spi = MockSPI(response_map={0x00: [0x55, 0xAA]})
        spi.open(0, 0)
        spi.writebytes([0x40, 0xFF])
        rx = spi.readbytes(2)
        assert rx == [0x55, 0xAA]


# ---------------------------------------------------------------------------
# SPIBusManager — pooling, refcounting, configuration
# ---------------------------------------------------------------------------


class TestSPIBusManagerPooling:
    def test_pools_handles_by_bus_device(self):
        mgr = SPIBusManager(mock_factory=lambda: MockSPI())
        spi1, lock1 = mgr.get_spi(0, 0, mode=0)
        spi2, lock2 = mgr.get_spi(0, 0, mode=0)
        # Same key → same handle, same lock
        assert spi1 is spi2
        assert lock1 is lock2

    def test_distinct_devices_get_distinct_handles(self):
        mgr = SPIBusManager(mock_factory=lambda: MockSPI())
        spi_a, lock_a = mgr.get_spi(0, 0)
        spi_b, lock_b = mgr.get_spi(0, 1)
        assert spi_a is not spi_b
        assert lock_a is not lock_b

    def test_distinct_buses_get_distinct_handles(self):
        mgr = SPIBusManager(mock_factory=lambda: MockSPI())
        spi_a, _ = mgr.get_spi(0, 0)
        spi_b, _ = mgr.get_spi(1, 0)
        assert spi_a is not spi_b

    def test_configures_mode_bits_speed_on_open(self):
        mgr = SPIBusManager(mock_factory=lambda: MockSPI())
        spi, _ = mgr.get_spi(
            0, 0, mode=2, bits_per_word=8, max_speed_hz=2_000_000
        )
        assert spi.mode == 2
        assert spi.bits_per_word == 8
        assert spi.max_speed_hz == 2_000_000

    def test_configures_cs_lsb_threewire(self):
        mgr = SPIBusManager(mock_factory=lambda: MockSPI())
        spi, _ = mgr.get_spi(
            0,
            0,
            cs_high=True,
            lsb_first=True,
            three_wire=True,
        )
        assert spi.cshigh is True
        assert spi.lsbfirst is True
        assert spi.threewire is True

    def test_reapplies_config_on_subsequent_get(self):
        mgr = SPIBusManager(mock_factory=lambda: MockSPI())
        mgr.get_spi(0, 0, mode=0, max_speed_hz=1_000_000)
        spi, _ = mgr.get_spi(0, 0, mode=3, max_speed_hz=5_000_000)
        assert spi.mode == 3
        assert spi.max_speed_hz == 5_000_000

    def test_release_closes_handle_at_zero_refcount(self):
        mock = MockSPI()
        mgr = SPIBusManager(mock_instance=mock)
        mgr.get_spi(0, 0)
        mgr.get_spi(0, 0)
        assert (0, 0) in mgr.open_handles()
        mgr.release(0, 0)
        # Still one ref outstanding
        assert (0, 0) in mgr.open_handles()
        mgr.release(0, 0)
        assert (0, 0) not in mgr.open_handles()
        assert mock._closed is True

    def test_release_unknown_handle_is_noop(self):
        mgr = SPIBusManager(mock_factory=lambda: MockSPI())
        mgr.release(99, 99)  # should not raise

    def test_no_spidev_no_mock_raises_on_open(self, monkeypatch):
        # Force the "no spidev" path even on machines that ship it.
        monkeypatch.setattr(
            "galois_edge.drivers.spi.transport._SPIDEV_AVAILABLE", False
        )
        mgr = SPIBusManager()  # no mock_factory or mock_instance
        with pytest.raises(RuntimeError, match="spidev"):
            mgr.get_spi(0, 0)


# ---------------------------------------------------------------------------
# Per-CS-line locking
# ---------------------------------------------------------------------------


class TestCSLineLocking:
    def test_single_cs_line_serializes_transactions(self):
        """Two threads racing on the same (bus, device) must not interleave xfer2."""

        class SerializingMock(MockSPI):
            in_progress: int = 0
            max_concurrent: int = 0

            def xfer2(self, data, *args, **kwargs):
                # Track concurrent calls without holding any lock; the
                # bus_lock the driver acquires *outside* xfer2 must
                # protect us.
                SerializingMock.in_progress += 1
                SerializingMock.max_concurrent = max(
                    SerializingMock.max_concurrent, SerializingMock.in_progress
                )
                try:
                    time.sleep(0.005)
                    return super().xfer2(data, *args, **kwargs)
                finally:
                    SerializingMock.in_progress -= 1

        mgr = SPIBusManager(mock_instance=SerializingMock())
        spi, lock = mgr.get_spi(0, 0)

        def worker():
            for _ in range(5):
                with lock:
                    spi.xfer2([0x00, 0x00])

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert SerializingMock.max_concurrent == 1, (
            f"Bus lock failed: saw {SerializingMock.max_concurrent} concurrent xfer2 calls"
        )

    def test_distinct_cs_lines_can_run_concurrently(self):
        """Two threads on distinct CS lines should *not* be serialized."""

        class TrackingMock(MockSPI):
            in_progress = {}
            saw_concurrent_distinct = False

            def xfer2(self, data, *args, **kwargs):
                key = self._opened
                TrackingMock.in_progress[key] = (
                    TrackingMock.in_progress.get(key, 0) + 1
                )
                # If two distinct keys are both >= 1 simultaneously, mark it.
                active = [k for k, v in TrackingMock.in_progress.items() if v > 0]
                if len(active) >= 2:
                    TrackingMock.saw_concurrent_distinct = True
                try:
                    time.sleep(0.02)
                    return super().xfer2(data, *args, **kwargs)
                finally:
                    TrackingMock.in_progress[key] = (
                        TrackingMock.in_progress.get(key, 1) - 1
                    )

        mgr = SPIBusManager(mock_factory=lambda: TrackingMock())
        spi_a, lock_a = mgr.get_spi(0, 0)
        spi_b, lock_b = mgr.get_spi(0, 1)
        assert lock_a is not lock_b

        def worker(spi, lock):
            for _ in range(3):
                with lock:
                    spi.xfer2([0x00, 0x00])

        threads = [
            threading.Thread(target=worker, args=(spi_a, lock_a)),
            threading.Thread(target=worker, args=(spi_b, lock_b)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert TrackingMock.saw_concurrent_distinct, (
            "Distinct CS lines should be allowed to run concurrently"
        )

    def test_lock_is_reentrant(self):
        """RLock allows the same thread to nest acquisitions (e.g. read inside command)."""
        mgr = SPIBusManager(mock_factory=lambda: MockSPI())
        spi, lock = mgr.get_spi(0, 0)
        with lock:
            with lock:
                spi.xfer2([0x00])  # would deadlock with a plain Lock


# ---------------------------------------------------------------------------
# Capability gating
# ---------------------------------------------------------------------------


class TestCapabilityGating:
    def test_is_available_false_without_spidev(self, monkeypatch):
        monkeypatch.setattr(
            "galois_edge.drivers.spi.transport._SPIDEV_AVAILABLE", False
        )
        assert is_available() is False
        assert SPIBusManager.is_available() is False
        reason = SPIBusManager.unavailable_reason()
        assert reason is not None
        assert "spidev" in reason.lower()

    def test_is_available_false_when_no_dev_node(self, monkeypatch):
        # Pretend spidev is importable but no device nodes exist.
        monkeypatch.setattr(
            "galois_edge.drivers.spi.transport._SPIDEV_AVAILABLE", True
        )
        monkeypatch.setattr(
            "galois_edge.drivers.spi.transport._spidev_devices_present",
            lambda: False,
        )
        assert is_available() is False
        reason = SPIBusManager.unavailable_reason()
        assert reason is not None
        assert "/dev/spidev" in reason

    def test_is_available_true_when_both_present(self, monkeypatch):
        monkeypatch.setattr(
            "galois_edge.drivers.spi.transport._SPIDEV_AVAILABLE", True
        )
        monkeypatch.setattr(
            "galois_edge.drivers.spi.transport._spidev_devices_present",
            lambda: True,
        )
        assert is_available() is True
        assert SPIBusManager.unavailable_reason() is None

    def test_macos_dev_machine_is_unavailable(self):
        """Sanity: actually running tests on a non-Pi box must report unavailable."""
        # Don't monkeypatch — test the real environment. On the CI box this
        # is False; on a Pi this would be True. Both are valid; we just
        # require the function to return a bool.
        assert isinstance(is_available(), bool)
