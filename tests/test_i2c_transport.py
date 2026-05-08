"""Tests for I2CBusManager — handle caching, refcounts, locking, gating."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest

from galois_edge.drivers.i2c import transport as i2c_transport
from galois_edge.drivers.i2c.transport import I2CBusManager


class FakeSMBus:
    """Minimal SMBus stand-in: tracks open/close per bus number."""

    instances: list["FakeSMBus"] = []

    def __init__(self, bus_num: int) -> None:
        self.bus_num = bus_num
        self.closed = False
        self.pec = False
        FakeSMBus.instances.append(self)

    def close(self) -> None:
        self.closed = True

    def enable_pec(self, enable: bool = True) -> None:
        self.pec = bool(enable)


@pytest.fixture(autouse=True)
def _reset_fake_smbus():
    FakeSMBus.instances.clear()
    yield
    FakeSMBus.instances.clear()


@pytest.fixture
def manager():
    return I2CBusManager(smbus_factory=FakeSMBus)


# ---------------------------------------------------------------------------
# Handle caching + reference counting
# ---------------------------------------------------------------------------

class TestHandleCaching:
    def test_get_smbus_returns_handle(self, manager):
        bus = manager.get_smbus(1)
        assert isinstance(bus, FakeSMBus)
        assert bus.bus_num == 1

    def test_repeated_get_returns_same_handle(self, manager):
        a = manager.get_smbus(1)
        b = manager.get_smbus(1)
        assert a is b
        assert len(FakeSMBus.instances) == 1

    def test_different_bus_numbers_get_separate_handles(self, manager):
        a = manager.get_smbus(1)
        b = manager.get_smbus(2)
        assert a is not b
        assert len(FakeSMBus.instances) == 2


class TestRefCounting:
    def test_release_after_single_get_closes(self, manager):
        bus = manager.get_smbus(1)
        manager.release(1)
        assert bus.closed is True

    def test_two_gets_one_release_keeps_open(self, manager):
        bus = manager.get_smbus(1)
        manager.get_smbus(1)
        manager.release(1)
        assert bus.closed is False

    def test_two_gets_two_releases_closes(self, manager):
        bus = manager.get_smbus(1)
        manager.get_smbus(1)
        manager.release(1)
        manager.release(1)
        assert bus.closed is True

    def test_release_unknown_bus_is_safe(self, manager):
        # Releasing a bus we never opened should not raise.
        manager.release(99)

    def test_get_after_close_reopens(self, manager):
        bus1 = manager.get_smbus(1)
        manager.release(1)
        assert bus1.closed is True
        bus2 = manager.get_smbus(1)
        assert bus2 is not bus1
        assert bus2.closed is False


# ---------------------------------------------------------------------------
# Per-device locks
# ---------------------------------------------------------------------------

class TestDeviceLocks:
    def test_same_device_returns_same_lock(self, manager):
        a = manager.device_lock(1, 0x76)
        b = manager.device_lock(1, 0x76)
        assert a is b

    def test_different_address_returns_different_lock(self, manager):
        a = manager.device_lock(1, 0x76)
        b = manager.device_lock(1, 0x77)
        assert a is not b

    def test_different_bus_same_address_different_lock(self, manager):
        a = manager.device_lock(0, 0x40)
        b = manager.device_lock(1, 0x40)
        assert a is not b

    def test_lock_is_reentrant(self, manager):
        # RLock should allow same-thread re-acquire.
        lock = manager.device_lock(1, 0x40)
        with lock:
            with lock:
                assert True

    def test_locks_dropped_when_bus_closes(self, manager):
        manager.get_smbus(1)
        lock_before = manager.device_lock(1, 0x76)
        manager.release(1)  # closes bus, drops locks
        lock_after = manager.device_lock(1, 0x76)
        assert lock_before is not lock_after

    def test_concurrent_reads_serialize_per_device(self, manager):
        """Two threads contending for the same (bus, addr) lock serialize."""
        lock = manager.device_lock(1, 0x76)
        order: list[str] = []
        first_in = threading.Event()
        release_first = threading.Event()

        def worker_a():
            with lock:
                order.append("a-in")
                first_in.set()
                release_first.wait(timeout=2)
                order.append("a-out")

        def worker_b():
            first_in.wait(timeout=2)
            with lock:
                order.append("b-in")
                order.append("b-out")

        ta = threading.Thread(target=worker_a)
        tb = threading.Thread(target=worker_b)
        ta.start()
        tb.start()
        first_in.wait(timeout=2)
        # b should be blocked while a holds the lock.
        assert order == ["a-in"]
        release_first.set()
        ta.join(timeout=2)
        tb.join(timeout=2)
        assert order == ["a-in", "a-out", "b-in", "b-out"]

    def test_concurrent_reads_different_addresses_do_not_block(self, manager):
        """Locks for different (bus, addr) pairs are independent."""
        lock_a = manager.device_lock(1, 0x76)
        lock_b = manager.device_lock(1, 0x77)
        a_done = threading.Event()
        b_done = threading.Event()

        def worker_a():
            with lock_a:
                # Hold the lock while b finishes — proves the locks are independent.
                b_done.wait(timeout=2)
                a_done.set()

        def worker_b():
            with lock_b:
                b_done.set()

        ta = threading.Thread(target=worker_a)
        tb = threading.Thread(target=worker_b)
        ta.start()
        tb.start()
        assert b_done.wait(timeout=2), "b was blocked by a"
        ta.join(timeout=2)
        tb.join(timeout=2)
        assert a_done.is_set()


# ---------------------------------------------------------------------------
# Capability gating
# ---------------------------------------------------------------------------

class TestCapabilityGating:
    def test_unavailable_when_no_devices(self, monkeypatch):
        monkeypatch.setattr(i2c_transport, "_smbus2_imported", lambda: True)
        monkeypatch.setattr(i2c_transport, "_i2c_devices_present", lambda: False)
        assert I2CBusManager.is_available() is False
        assert I2CBusManager.unsupported_reason() == "no /dev/i2c-* devices found"

    def test_unavailable_when_smbus2_missing(self, monkeypatch):
        monkeypatch.setattr(i2c_transport, "_smbus2_imported", lambda: False)
        monkeypatch.setattr(i2c_transport, "_i2c_devices_present", lambda: True)
        assert I2CBusManager.is_available() is False
        assert I2CBusManager.unsupported_reason() == "smbus2 not installed"

    def test_available_when_both_present(self, monkeypatch):
        monkeypatch.setattr(i2c_transport, "_smbus2_imported", lambda: True)
        monkeypatch.setattr(i2c_transport, "_i2c_devices_present", lambda: True)
        assert I2CBusManager.is_available() is True
        assert I2CBusManager.unsupported_reason() is None

    def test_factory_required_when_smbus2_missing(self, monkeypatch):
        # Ensure the manager can still be constructed for tests.
        mgr = I2CBusManager(smbus_factory=FakeSMBus)
        bus = mgr.get_smbus(1)
        assert isinstance(bus, FakeSMBus)
