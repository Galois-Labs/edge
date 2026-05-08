"""Tests for the production-grade CANBusManager (drivers/can/transport.py).

The python-can ``virtual`` interface is used to exercise real Bus
instances without hardware; backoff schedules are shortened so BusOff
recovery tests run in milliseconds rather than seconds.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

import can as python_can

from galois_edge.drivers.can.transport import (
    CANBusManager,
    _filters_to_python_can,
    _normalize_filters,
)


# ---------------------------------------------------------------------------
# Filter normalisation helpers
# ---------------------------------------------------------------------------


def test_normalize_filters_empty():
    assert _normalize_filters(None) == ()
    assert _normalize_filters([]) == ()


def test_normalize_filters_orders_deterministically():
    a = [{"can_id": 0x200, "can_mask": 0x7FF}, {"can_id": 0x100, "can_mask": 0x7FF}]
    b = [{"can_id": 0x100, "can_mask": 0x7FF}, {"can_id": 0x200, "can_mask": 0x7FF}]
    assert _normalize_filters(a) == _normalize_filters(b)


def test_normalize_filters_separates_extended_flag():
    a = _normalize_filters([{"can_id": 0x100, "can_mask": 0x7FF, "extended": False}])
    b = _normalize_filters([{"can_id": 0x100, "can_mask": 0x7FF, "extended": True}])
    assert a != b


def test_filters_to_python_can_roundtrip():
    raw = [{"can_id": 0x100, "can_mask": 0x7FF}]
    norm = _normalize_filters(raw)
    out = _filters_to_python_can(norm)
    assert out is not None
    assert out[0]["can_id"] == 0x100
    assert out[0]["can_mask"] == 0x7FF


# ---------------------------------------------------------------------------
# Bus caching by parameters and filters
# ---------------------------------------------------------------------------


def test_get_bus_returns_lock_and_bus():
    mgr = CANBusManager()
    try:
        bus, lock = mgr.get_bus(channel="t_basic", interface="virtual", bitrate=500000)
        assert bus is not None
        assert isinstance(lock, type(threading.RLock()))
    finally:
        mgr.shutdown_all()


def test_same_key_returns_same_bus():
    mgr = CANBusManager()
    try:
        bus1, lock1 = mgr.get_bus(channel="t_same", interface="virtual", bitrate=500000)
        bus2, lock2 = mgr.get_bus(channel="t_same", interface="virtual", bitrate=500000)
        assert bus1 is bus2
        assert lock1 is lock2
    finally:
        mgr.shutdown_all()


def test_different_filters_different_bus():
    mgr = CANBusManager()
    try:
        bus1, _ = mgr.get_bus(
            channel="t_filt",
            interface="virtual",
            bitrate=500000,
            filters=[{"can_id": 0x100, "can_mask": 0x7FF}],
        )
        bus2, _ = mgr.get_bus(
            channel="t_filt",
            interface="virtual",
            bitrate=500000,
            filters=[{"can_id": 0x200, "can_mask": 0x7FF}],
        )
        assert bus1 is not bus2
    finally:
        mgr.shutdown_all()


def test_different_channels_different_bus():
    mgr = CANBusManager()
    try:
        bus1, _ = mgr.get_bus(channel="t_chA", interface="virtual")
        bus2, _ = mgr.get_bus(channel="t_chB", interface="virtual")
        assert bus1 is not bus2
    finally:
        mgr.shutdown_all()


def test_filter_order_does_not_matter_for_key():
    mgr = CANBusManager()
    try:
        bus1, _ = mgr.get_bus(
            channel="t_order",
            interface="virtual",
            filters=[
                {"can_id": 0x100, "can_mask": 0x7FF},
                {"can_id": 0x200, "can_mask": 0x7FF},
            ],
        )
        bus2, _ = mgr.get_bus(
            channel="t_order",
            interface="virtual",
            filters=[
                {"can_id": 0x200, "can_mask": 0x7FF},
                {"can_id": 0x100, "can_mask": 0x7FF},
            ],
        )
        assert bus1 is bus2
    finally:
        mgr.shutdown_all()


def test_release_decrements_refcount_and_closes():
    mgr = CANBusManager()
    bus, _ = mgr.get_bus(channel="t_ref", interface="virtual")
    bus2, _ = mgr.get_bus(channel="t_ref", interface="virtual")
    assert bus is bus2
    # First release: still 1 ref outstanding
    mgr.release(channel="t_ref", interface="virtual")
    assert mgr.get_current_bus(channel="t_ref", interface="virtual", bitrate=500000, filters=None) is bus
    # Second release: ref_count hits 0, bus is dropped
    mgr.release(channel="t_ref", interface="virtual")
    assert mgr.get_current_bus(channel="t_ref", interface="virtual", bitrate=500000, filters=None) is None


def test_release_unknown_key_is_noop():
    mgr = CANBusManager()
    # Releasing without ever opening must not raise
    mgr.release(channel="never_opened", interface="virtual")
    mgr.shutdown_all()


def test_get_lock_returns_none_when_unopened():
    mgr = CANBusManager()
    assert mgr.get_lock(channel="ghost", bitrate=500000, interface="virtual", filters=None) is None


def test_get_current_bus_after_recovery_returns_new_instance():
    """After BusOff recovery the current bus reference is replaced."""
    mgr = CANBusManager()
    mgr._set_backoff_schedule((0.01,))
    try:
        bus1, _ = mgr.get_bus(channel="t_recov", interface="virtual")
        # Trigger recovery
        mgr.notify_bus_off("t_recov")
        # Wait briefly for the recovery thread
        for _ in range(50):
            time.sleep(0.02)
            current = mgr.get_current_bus(
                channel="t_recov", bitrate=500000, interface="virtual", filters=None
            )
            if current is not None and current is not bus1:
                break
        assert current is not None
        assert current is not bus1
    finally:
        mgr.shutdown_all()


def test_recovery_callback_invoked_with_new_bus():
    mgr = CANBusManager()
    mgr._set_backoff_schedule((0.01,))
    received: list[Any] = []
    event = threading.Event()
    try:
        bus1, _ = mgr.get_bus(channel="t_cb", interface="virtual")

        def cb(new_bus: Any) -> None:
            received.append(new_bus)
            event.set()

        mgr.register_recovery_callback(
            channel="t_cb",
            bitrate=500000,
            interface="virtual",
            filters=None,
            callback=cb,
        )
        mgr.notify_bus_off("t_cb")
        assert event.wait(2.0), "Recovery callback was not invoked in time"
        assert received[0] is not bus1
    finally:
        mgr.shutdown_all()


def test_recovery_skipped_if_bus_released_during_backoff():
    mgr = CANBusManager()
    # Long enough delay to release before the recovery loop completes
    mgr._set_backoff_schedule((0.2,))
    try:
        mgr.get_bus(channel="t_drop", interface="virtual")
        mgr.notify_bus_off("t_drop")
        # Release immediately so recovery sees a missing entry
        mgr.release(channel="t_drop", interface="virtual")
        # Give the thread time to run
        time.sleep(0.5)
        # No exception, no zombie buses
        assert mgr.get_current_bus(
            channel="t_drop", bitrate=500000, interface="virtual", filters=None
        ) is None
    finally:
        mgr.shutdown_all()


def test_shutdown_all_clears_state():
    mgr = CANBusManager()
    mgr.get_bus(channel="t_sa1", interface="virtual")
    mgr.get_bus(channel="t_sa2", interface="virtual")
    mgr.shutdown_all()
    assert mgr.get_current_bus(channel="t_sa1", bitrate=500000, interface="virtual", filters=None) is None
    assert mgr.get_current_bus(channel="t_sa2", bitrate=500000, interface="virtual", filters=None) is None


def test_filters_passed_to_python_can_bus():
    """Open a bus with filters and assert python-can applied them."""
    mgr = CANBusManager()
    try:
        bus, _ = mgr.get_bus(
            channel="t_apply",
            interface="virtual",
            filters=[{"can_id": 0x123, "can_mask": 0x7FF}],
        )
        # python-can stores applied filters on the bus
        assert bus.filters is not None
        assert any(f["can_id"] == 0x123 for f in bus.filters)
    finally:
        mgr.shutdown_all()


def test_recovery_reapplies_filters():
    """After recovery, the new bus has the same filters installed."""
    mgr = CANBusManager()
    mgr._set_backoff_schedule((0.01,))
    received: list[Any] = []
    event = threading.Event()
    filters = [{"can_id": 0x456, "can_mask": 0x7FF}]
    try:
        mgr.get_bus(channel="t_refilt", interface="virtual", filters=filters)

        def cb(new_bus: Any) -> None:
            received.append(new_bus)
            event.set()

        mgr.register_recovery_callback(
            channel="t_refilt",
            bitrate=500000,
            interface="virtual",
            filters=filters,
            callback=cb,
        )
        mgr.notify_bus_off("t_refilt")
        assert event.wait(2.0)
        new_bus = received[0]
        assert new_bus.filters is not None
        assert any(f["can_id"] == 0x456 for f in new_bus.filters)
    finally:
        mgr.shutdown_all()


def test_notify_bus_off_unknown_channel_is_noop():
    mgr = CANBusManager()
    # Should not raise even if the channel was never opened
    mgr.notify_bus_off("nonexistent_channel")
    time.sleep(0.05)


def test_python_can_not_installed(monkeypatch):
    """When python-can is missing, get_bus raises a clear runtime error."""
    monkeypatch.setattr(
        "galois_edge.drivers.can.transport.CAN_AVAILABLE", False
    )
    mgr = CANBusManager()
    with pytest.raises(RuntimeError, match="python-can"):
        mgr.get_bus(channel="t_missing", interface="virtual")
