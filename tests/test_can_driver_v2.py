"""Tests for the production-grade GenericCANDriver (drivers/can/driver.py).

Strategy:

* Pure-arithmetic tests (extract / pack) run against the static helpers
  with no bus involvement.
* End-to-end tests use python-can's ``virtual`` interface paired in
  *two* directions: a *driver bus* and a *peer bus* on the same virtual
  channel.  The peer simulates the instrument by sending frames the
  driver should receive, and observes frames the driver writes.
* BusOff and error-frame tests use direct manipulation of the driver's
  internal hooks rather than trying to coerce the virtual interface
  into producing real bus errors (which it cannot).
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any

import pytest

import can as python_can

from galois_edge.drivers.can.driver import (
    GenericCANDriver,
    _select_data_type,
)
from galois_edge.drivers.can.transport import CANBusManager


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _unique_channel(prefix: str = "test") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


SAMPLE_PROFILE = {
    "protocol": "can",
    "identity": {"manufacturer": "TestCo", "model": "TM-CAN1"},
    "connection": {
        "channel": "auto",  # overridden in fixture
        "interface": "virtual",
        "bitrate": 500000,
        "recv_timeout": 0.5,
        "filters": [
            {"can_id": 0x100, "can_mask": 0x7FF},
            {"can_id": 0x200, "can_mask": 0x7FF},
        ],
    },
    "messages": {
        "motor_status": {
            "can_id": 0x100,
            "is_extended": False,
            "dlc": 8,
            "direction": "rx",
            "signals": {
                "speed": {
                    "start_bit": 0,
                    "bit_length": 16,
                    "byte_order": "little_endian",
                    "signed": False,
                    "scale": 0.1,
                    "offset": 0,
                    "unit": "rpm",
                    "range": [0, 8000],
                },
                "temp": {
                    "start_bit": 16,
                    "bit_length": 8,
                    "byte_order": "little_endian",
                    "signed": True,
                    "scale": 1.0,
                    "offset": -40,
                    "unit": "C",
                },
            },
        },
        "command_frame": {
            "can_id": 0x200,
            "is_extended": False,
            "dlc": 8,
            "direction": "tx",
            "signals": {
                "target_speed": {
                    "start_bit": 0,
                    "bit_length": 16,
                    "byte_order": "little_endian",
                    "signed": False,
                    "scale": 1.0,
                    "offset": 0,
                    "range": [0, 8000],
                },
                "mode": {
                    "start_bit": 16,
                    "bit_length": 8,
                    "byte_order": "little_endian",
                    "signed": False,
                    "enum": {0: "stopped", 1: "running", 2: "fault"},
                },
            },
        },
    },
    "commands": {
        "get_speed": {"type": "query", "reads": ["speed"]},
        "set_target_speed": {
            "type": "action",
            "writes": [{"register": "target_speed", "value": "{rpm}"}],
        },
    },
}


MUX_PROFILE = {
    "protocol": "can",
    "identity": {"manufacturer": "TestCo", "model": "TM-MUX"},
    "connection": {
        "channel": "auto",
        "interface": "virtual",
        "bitrate": 500000,
        "recv_timeout": 0.5,
        "filters": [{"can_id": 0x300, "can_mask": 0x7FF}],
    },
    "messages": {
        "muxed_status": {
            "can_id": 0x300,
            "is_extended": False,
            "dlc": 8,
            "direction": "rx",
            "multiplex": {"mux_signal": "page", "mux_values": [0, 1]},
            "signals": {
                "page": {
                    "start_bit": 0,
                    "bit_length": 8,
                    "byte_order": "little_endian",
                    "signed": False,
                },
                "voltage": {
                    "start_bit": 8,
                    "bit_length": 16,
                    "byte_order": "little_endian",
                    "signed": False,
                    "scale": 0.001,
                    "unit": "V",
                    "mux_value": 0,
                },
                "current": {
                    "start_bit": 8,
                    "bit_length": 16,
                    "byte_order": "little_endian",
                    "signed": True,
                    "scale": 0.01,
                    "unit": "A",
                    "mux_value": 1,
                },
            },
        },
    },
}


EXTENDED_PROFILE = {
    "protocol": "can",
    "identity": {"manufacturer": "TestCo", "model": "TM-EXT"},
    "connection": {
        "channel": "auto",
        "interface": "virtual",
        "bitrate": 500000,
        "recv_timeout": 0.5,
        # Extended ID 0x18FF50F4 (J1939-style)
        "filters": [
            {"can_id": 0x18FF50F4, "can_mask": 0x1FFFFFFF, "extended": True}
        ],
    },
    "messages": {
        "j1939_status": {
            "can_id": 0x18FF50F4,
            "is_extended": True,
            "dlc": 8,
            "direction": "rx",
            "signals": {
                "battery_voltage": {
                    "start_bit": 0,
                    "bit_length": 16,
                    "byte_order": "little_endian",
                    "signed": False,
                    "scale": 0.001,
                    "unit": "V",
                },
            },
        },
    },
}


@pytest.fixture
def virtual_setup():
    """Yield (driver, peer_bus, channel) wired to the same virtual channel."""
    channel = _unique_channel()
    profile = _profile_with_channel(SAMPLE_PROFILE, channel)
    mgr = CANBusManager()
    # Speed up any recovery thread spawned during a test.
    mgr._set_backoff_schedule((0.01,))
    driver = GenericCANDriver(
        instrument_id="inst-1",
        transport_uri=f"can://{channel}",
        profile=profile,
        bus_manager=mgr,
    )
    driver.connect()
    peer = python_can.Bus(channel=channel, interface="virtual")
    try:
        yield driver, peer, channel, mgr
    finally:
        try:
            peer.shutdown()
        except Exception:
            pass
        try:
            driver.disconnect()
        except Exception:
            pass
        mgr.shutdown_all()


def _profile_with_channel(template: dict[str, Any], channel: str) -> dict[str, Any]:
    """Deep-copy ``template`` and set the channel field."""
    import copy

    p = copy.deepcopy(template)
    p["connection"]["channel"] = channel
    return p


# ---------------------------------------------------------------------------
# Static helpers
# ---------------------------------------------------------------------------


def test_select_data_type_widths():
    assert _select_data_type(1, False) == "uint8"
    assert _select_data_type(8, True) == "int8"
    assert _select_data_type(16, False) == "uint16"
    assert _select_data_type(16, True) == "int16"
    assert _select_data_type(24, False) == "uint32"
    assert _select_data_type(32, True) == "int32"
    assert _select_data_type(40, False) == "uint64"
    assert _select_data_type(64, True) == "int64"


def test_extract_signal_little_endian_unsigned():
    # 16-bit value 0x1234 little-endian at start_bit 0
    data = bytes([0x34, 0x12, 0, 0, 0, 0, 0, 0])
    val = GenericCANDriver._extract_signal(data, 0, 16, "little_endian", False)
    assert val == 0x1234


def test_extract_signal_little_endian_signed_positive():
    data = bytes([0x7F, 0x00, 0, 0, 0, 0, 0, 0])
    val = GenericCANDriver._extract_signal(data, 0, 8, "little_endian", True)
    assert val == 127


def test_extract_signal_little_endian_signed_negative():
    # Two's complement: 0xFF == -1 as signed 8-bit
    data = bytes([0xFF, 0, 0, 0, 0, 0, 0, 0])
    val = GenericCANDriver._extract_signal(data, 0, 8, "little_endian", True)
    assert val == -1


def test_extract_signal_offset_start_bit():
    # Value 0xAB at start_bit 8 (so byte 1)
    data = bytes([0x00, 0xAB, 0, 0, 0, 0, 0, 0])
    val = GenericCANDriver._extract_signal(data, 8, 8, "little_endian", False)
    assert val == 0xAB


def test_extract_signal_big_endian():
    # Big-endian (Motorola): start_bit is the MSB position counted from
    # the high byte.  shift = total_bits - start_bit - bit_length, so
    # start_bit=0, bit_length=8 selects the high byte of the data array.
    data = bytes([0xAB, 0x00, 0, 0, 0, 0, 0, 0])
    val = GenericCANDriver._extract_signal(data, 0, 8, "big_endian", False)
    assert val == 0xAB


def test_extract_signal_big_endian_offset():
    # Selecting the second byte: start_bit=8, bit_length=8 → shift=48
    data = bytes([0x00, 0xCD, 0, 0, 0, 0, 0, 0])
    val = GenericCANDriver._extract_signal(data, 8, 8, "big_endian", False)
    assert val == 0xCD


def test_extract_signal_24bit_unsigned():
    data = bytes([0x12, 0x34, 0x56, 0, 0, 0, 0, 0])
    val = GenericCANDriver._extract_signal(data, 0, 24, "little_endian", False)
    assert val == 0x563412


def test_pack_signal_little_endian():
    out = GenericCANDriver._pack_signal(0x1234, 0, 16, "little_endian", 8)
    assert len(out) == 8
    assert out[0] == 0x34
    assert out[1] == 0x12


def test_pack_signal_offset_bits():
    # Value 0xFF at start_bit 16 → byte 2 should be 0xFF
    out = GenericCANDriver._pack_signal(0xFF, 16, 8, "little_endian", 8)
    assert out[2] == 0xFF
    assert out[0] == 0 and out[1] == 0


def test_pack_signal_big_endian():
    # Pack 0xAB as 8-bit big-endian at start_bit 0 — high byte = 0xAB
    out = GenericCANDriver._pack_signal(0xAB, 0, 8, "big_endian", 8)
    assert out[0] == 0xAB
    assert out[1] == 0


def test_pack_extract_roundtrip_big_endian():
    for value, sb in ((0xAB, 0), (0xCD, 8), (0x1234, 0)):
        bl = 16 if value > 0xFF else 8
        packed = GenericCANDriver._pack_signal(value, sb, bl, "big_endian", 8)
        unpacked = GenericCANDriver._extract_signal(packed, sb, bl, "big_endian", False)
        assert unpacked == value


def test_pack_extract_roundtrip_unsigned():
    for value in (0, 1, 100, 0xFF, 0xFFFF):
        packed = GenericCANDriver._pack_signal(value, 0, 16, "little_endian", 8)
        unpacked = GenericCANDriver._extract_signal(packed, 0, 16, "little_endian", False)
        assert unpacked == value


def test_pack_extract_roundtrip_signed():
    for value in (-128, -1, 0, 1, 127):
        packed = GenericCANDriver._pack_signal(value, 0, 8, "little_endian", 8)
        unpacked = GenericCANDriver._extract_signal(packed, 0, 8, "little_endian", True)
        assert unpacked == value


# ---------------------------------------------------------------------------
# Profile parsing / point construction
# ---------------------------------------------------------------------------


def test_profile_builds_points():
    mgr = CANBusManager()
    driver = GenericCANDriver(
        instrument_id="x",
        transport_uri="can://x",
        profile=_profile_with_channel(SAMPLE_PROFILE, "p1"),
        bus_manager=mgr,
    )
    assert "speed" in driver._points
    assert "temp" in driver._points
    assert "target_speed" in driver._points
    assert driver._points["speed"].access == "read"
    assert driver._points["target_speed"].access == "read_write"
    mgr.shutdown_all()


def test_profile_extended_id_marked():
    mgr = CANBusManager()
    driver = GenericCANDriver(
        instrument_id="x",
        transport_uri="can://x",
        profile=_profile_with_channel(EXTENDED_PROFILE, "p2"),
        bus_manager=mgr,
    )
    pt = driver._points["battery_voltage"]
    assert pt.addressing["is_extended"] is True
    assert pt.addressing["can_id"] == 0x18FF50F4
    mgr.shutdown_all()


def test_profile_mux_signal_recorded():
    mgr = CANBusManager()
    driver = GenericCANDriver(
        instrument_id="x",
        transport_uri="can://x",
        profile=_profile_with_channel(MUX_PROFILE, "p3"),
        bus_manager=mgr,
    )
    voltage = driver._points["voltage"]
    assert voltage.addressing.get("mux_signal") == "page"
    assert voltage.addressing.get("mux_value") == 0
    current = driver._points["current"]
    assert current.addressing.get("mux_value") == 1
    mgr.shutdown_all()


# ---------------------------------------------------------------------------
# Connection lifecycle
# ---------------------------------------------------------------------------


def test_connect_installs_filters(virtual_setup):
    driver, peer, channel, mgr = virtual_setup
    assert driver.bus is not None
    assert driver.connected
    # python-can's virtual bus stores filters
    applied = driver.bus.filters or []
    ids = [f["can_id"] for f in applied]
    assert 0x100 in ids
    assert 0x200 in ids


def test_disconnect_releases_bus(virtual_setup):
    driver, peer, channel, mgr = virtual_setup
    driver.disconnect()
    assert not driver.connected
    # Bus reference cleared
    assert driver.bus is None


def test_get_capabilities_shape(virtual_setup):
    driver, peer, channel, mgr = virtual_setup
    caps = driver.get_capabilities()
    assert caps["protocol"] == "can"
    assert caps["signals"] >= 4
    assert caps["writable"] >= 2
    assert "speed" in [p["name"] for p in caps["points"]]
    assert "filters" in caps
    assert "error_frames" in caps
    assert "bus_off_events" in caps


def test_identify_returns_string(virtual_setup):
    driver, peer, channel, mgr = virtual_setup
    s = driver.identify()
    assert "TestCo" in s
    assert "TM-CAN1" in s


# ---------------------------------------------------------------------------
# read_point / write_point against virtual peer
# ---------------------------------------------------------------------------


def test_read_point_decodes_frame_from_peer(virtual_setup):
    driver, peer, channel, mgr = virtual_setup
    # Encode raw value 1500 at little-endian 16-bit @ start_bit 0
    raw_speed = 1500  # → engineering 150.0 rpm via scale 0.1
    data = (raw_speed).to_bytes(2, "little") + b"\x00" * 6
    # Send from peer
    peer.send(python_can.Message(arbitration_id=0x100, data=data, is_extended_id=False))
    val = driver.read_point(driver._points["speed"])
    assert abs(val - 150.0) < 1e-6


def test_read_point_signed_with_offset(virtual_setup):
    driver, peer, channel, mgr = virtual_setup
    # temp: signed 8-bit at start_bit 16 (byte 2), scale 1, offset -40
    raw = 50  # → engineering 50 + (-40) = 10
    data = b"\x00\x00" + bytes([raw]) + b"\x00" * 5
    peer.send(python_can.Message(arbitration_id=0x100, data=data, is_extended_id=False))
    val = driver.read_point(driver._points["temp"])
    assert val == 10


def test_read_point_timeout_raises(virtual_setup):
    driver, peer, channel, mgr = virtual_setup
    # Don't send anything — should time out
    with pytest.raises(IOError, match="timeout"):
        driver.read_point(driver._points["speed"])


def test_read_point_skips_other_ids(virtual_setup):
    """If the kernel filter mask permits another ID through, we skip it."""
    driver, peer, channel, mgr = virtual_setup
    # Send the other ID first (0x200 — under filter), then the target.
    other = python_can.Message(
        arbitration_id=0x200, data=b"\x00" * 8, is_extended_id=False
    )
    peer.send(other)
    raw_speed = 100
    data = (raw_speed).to_bytes(2, "little") + b"\x00" * 6
    peer.send(python_can.Message(arbitration_id=0x100, data=data, is_extended_id=False))
    val = driver.read_point(driver._points["speed"])
    assert abs(val - 10.0) < 1e-6


def test_write_point_encodes_frame_visible_to_peer(virtual_setup):
    driver, peer, channel, mgr = virtual_setup
    driver.write_point(driver._points["target_speed"], 4321)
    msg = peer.recv(timeout=0.5)
    assert msg is not None
    assert msg.arbitration_id == 0x200
    assert int.from_bytes(msg.data[0:2], "little") == 4321


def test_write_point_enum_inverse(virtual_setup):
    driver, peer, channel, mgr = virtual_setup
    driver.write_point(driver._points["mode"], "running")
    msg = peer.recv(timeout=0.5)
    assert msg is not None
    # mode is at start_bit 16 (byte 2), uint8, enum 1=running
    assert msg.data[2] == 1


def test_write_point_read_only_rejects(virtual_setup):
    driver, peer, channel, mgr = virtual_setup
    with pytest.raises(PermissionError):
        driver.write_point(driver._points["speed"], 100)


def test_write_point_range_check(virtual_setup):
    driver, peer, channel, mgr = virtual_setup
    with pytest.raises(ValueError):
        driver.write_point(driver._points["target_speed"], 99999)


def test_extended_id_send_and_recv():
    """Round-trip an extended-ID frame through a fresh driver+peer pair."""
    channel = _unique_channel("ext")
    profile = _profile_with_channel(EXTENDED_PROFILE, channel)
    # Switch direction to tx so we can write
    profile["messages"]["j1939_status"]["direction"] = "tx"
    mgr = CANBusManager()
    driver = GenericCANDriver(
        instrument_id="inst-ext",
        transport_uri=f"can://{channel}",
        profile=profile,
        bus_manager=mgr,
    )
    driver.connect()
    peer = python_can.Bus(channel=channel, interface="virtual")
    try:
        driver.write_point(driver._points["battery_voltage"], 12.345)
        msg = peer.recv(timeout=0.5)
        assert msg is not None
        assert msg.is_extended_id is True
        assert msg.arbitration_id == 0x18FF50F4
        # 12.345 V → raw = round(12.345 / 0.001) = 12345
        assert int.from_bytes(msg.data[0:2], "little") == 12345
    finally:
        peer.shutdown()
        driver.disconnect()
        mgr.shutdown_all()


# ---------------------------------------------------------------------------
# Multiplex
# ---------------------------------------------------------------------------


@pytest.fixture
def mux_setup():
    channel = _unique_channel("mux")
    mgr = CANBusManager()
    driver = GenericCANDriver(
        instrument_id="mux-1",
        transport_uri=f"can://{channel}",
        profile=_profile_with_channel(MUX_PROFILE, channel),
        bus_manager=mgr,
    )
    driver.connect()
    peer = python_can.Bus(channel=channel, interface="virtual")
    try:
        yield driver, peer, channel, mgr
    finally:
        peer.shutdown()
        driver.disconnect()
        mgr.shutdown_all()


def test_mux_signal_decodes_correct_value(mux_setup):
    driver, peer, channel, mgr = mux_setup
    # Page 0 → voltage at bytes 1..2.  raw = 12345 → 12.345 V
    data = bytes([0]) + (12345).to_bytes(2, "little") + b"\x00" * 5
    peer.send(python_can.Message(arbitration_id=0x300, data=data, is_extended_id=False))
    val = driver.read_point(driver._points["voltage"])
    assert abs(val - 12.345) < 1e-6


def test_mux_mismatch_raises_io_error(mux_setup):
    driver, peer, channel, mgr = mux_setup
    # Send a frame on page 1 but try to read voltage (mux_value 0)
    data = bytes([1]) + (200).to_bytes(2, "little") + b"\x00" * 5
    peer.send(python_can.Message(arbitration_id=0x300, data=data, is_extended_id=False))
    with pytest.raises(IOError, match="mux value"):
        driver.read_point(driver._points["voltage"])


def test_decode_frame_returns_only_active_mux_signals(mux_setup):
    driver, peer, channel, mgr = mux_setup
    # Manually drive _decode_frame with a page-0 frame
    data = bytes([0]) + (5000).to_bytes(2, "little") + b"\x00" * 5
    msg = python_can.Message(arbitration_id=0x300, data=data, is_extended_id=False)
    points = [
        driver._points["page"],
        driver._points["voltage"],
        driver._points["current"],
    ]
    decoded = driver._decode_frame(msg, points)
    assert "page" in decoded
    assert "voltage" in decoded
    # current has mux_value=1 but page=0, so it must be excluded
    assert "current" not in decoded


# ---------------------------------------------------------------------------
# Native subscription
# ---------------------------------------------------------------------------


def test_subscribe_receives_signals(virtual_setup):
    driver, peer, channel, mgr = virtual_setup
    received: list[dict[str, Any]] = []
    event = threading.Event()

    def cb(values: dict[str, Any]) -> None:
        received.append(values)
        event.set()

    sub_id = driver.subscribe([driver._points["speed"], driver._points["temp"]], cb)
    try:
        # Send a frame; expect cb to fire with both signals
        speed_raw = 250
        data = (speed_raw).to_bytes(2, "little") + bytes([60]) + b"\x00" * 5
        peer.send(python_can.Message(arbitration_id=0x100, data=data, is_extended_id=False))
        assert event.wait(2.0), "Subscription callback never fired"
        last = received[-1]
        assert "speed" in last
        assert "temp" in last
        assert abs(last["speed"] - 25.0) < 1e-6
        # temp = 60 - 40 = 20
        assert last["temp"] == 20
    finally:
        driver.unsubscribe(sub_id)


def test_unsubscribe_stops_callbacks(virtual_setup):
    driver, peer, channel, mgr = virtual_setup
    received: list[dict[str, Any]] = []

    def cb(values: dict[str, Any]) -> None:
        received.append(values)

    sub_id = driver.subscribe([driver._points["speed"]], cb)
    driver.unsubscribe(sub_id)
    # Send a frame; cb should not be invoked
    data = (100).to_bytes(2, "little") + b"\x00" * 6
    peer.send(python_can.Message(arbitration_id=0x100, data=data, is_extended_id=False))
    time.sleep(0.2)
    assert received == []


def test_unsubscribe_unknown_id_is_noop(virtual_setup):
    driver, peer, channel, mgr = virtual_setup
    # Should not raise even for a never-issued id
    driver.unsubscribe("no-such-subscription")


# ---------------------------------------------------------------------------
# Error frames and BusOff
# ---------------------------------------------------------------------------


def test_error_frame_increments_counter(virtual_setup):
    driver, peer, channel, mgr = virtual_setup
    err = python_can.Message(is_error_frame=True, data=b"\x00" * 8)
    driver._on_error_frame(err)
    caps = driver.get_capabilities()
    assert caps["error_frames"] == 1
    assert caps["last_error_at"] is not None


def test_force_bus_off_recovery_increments_counters(virtual_setup):
    driver, peer, channel, mgr = virtual_setup
    bus_before = driver.bus
    driver.force_bus_off_recovery()
    # Wait for recovery to complete
    deadline = time.time() + 2.0
    while time.time() < deadline:
        if driver.get_capabilities()["reconnects"] >= 1 and driver.bus is not None and driver.bus is not bus_before:
            break
        time.sleep(0.02)
    caps = driver.get_capabilities()
    assert caps["bus_off_events"] >= 1
    assert caps["reconnects"] >= 1
    assert driver.bus is not None
    assert driver.bus is not bus_before


def test_busoff_recovery_reattaches_subscription():
    """After BusOff/recovery, an active subscription continues to deliver."""
    channel = _unique_channel("recov")
    profile = _profile_with_channel(SAMPLE_PROFILE, channel)
    mgr = CANBusManager()
    mgr._set_backoff_schedule((0.01,))
    driver = GenericCANDriver(
        instrument_id="inst-rec",
        transport_uri=f"can://{channel}",
        profile=profile,
        bus_manager=mgr,
    )
    driver.connect()
    received: list[dict[str, Any]] = []
    delivery_event = threading.Event()

    def cb(values: dict[str, Any]) -> None:
        received.append(values)
        delivery_event.set()

    sub_id = driver.subscribe([driver._points["speed"]], cb)
    try:
        # Trigger recovery
        driver.force_bus_off_recovery()
        # Wait for the reconnect to take effect
        deadline = time.time() + 2.0
        while time.time() < deadline and driver.get_capabilities()["reconnects"] < 1:
            time.sleep(0.02)
        assert driver.get_capabilities()["reconnects"] >= 1

        # Open a fresh peer on the recovered bus
        peer = python_can.Bus(channel=channel, interface="virtual")
        try:
            data = (777).to_bytes(2, "little") + b"\x00" * 6
            peer.send(python_can.Message(arbitration_id=0x100, data=data, is_extended_id=False))
            assert delivery_event.wait(2.0), "Subscription did not deliver after recovery"
            assert any("speed" in r for r in received)
        finally:
            peer.shutdown()
    finally:
        driver.unsubscribe(sub_id)
        driver.disconnect()
        mgr.shutdown_all()


def test_on_bus_error_busoff_string_triggers_recovery(virtual_setup):
    driver, peer, channel, mgr = virtual_setup
    bus_before = driver.bus
    driver._on_bus_error(Exception("CAN bus is OFF"))
    # Allow the recovery thread to run
    deadline = time.time() + 2.0
    while time.time() < deadline and driver.bus is bus_before:
        time.sleep(0.02)
    assert driver.bus is not bus_before


# ---------------------------------------------------------------------------
# Command execution (uses base class machinery)
# ---------------------------------------------------------------------------


def test_execute_query_command_returns_value(virtual_setup):
    driver, peer, channel, mgr = virtual_setup
    raw_speed = 1234
    data = (raw_speed).to_bytes(2, "little") + b"\x00" * 6
    peer.send(python_can.Message(arbitration_id=0x100, data=data, is_extended_id=False))
    val = driver.execute_command("get_speed")
    assert abs(val - 123.4) < 1e-6


def test_execute_action_command_writes(virtual_setup):
    driver, peer, channel, mgr = virtual_setup
    result = driver.execute_command("set_target_speed", {"rpm": 555})
    assert result == {"status": "ok"}
    msg = peer.recv(timeout=0.5)
    assert msg is not None
    assert msg.arbitration_id == 0x200
    assert int.from_bytes(msg.data[0:2], "little") == 555


# ---------------------------------------------------------------------------
# Read/write when disconnected
# ---------------------------------------------------------------------------


def test_read_when_disconnected_raises():
    mgr = CANBusManager()
    profile = _profile_with_channel(SAMPLE_PROFILE, _unique_channel("dc"))
    driver = GenericCANDriver(
        instrument_id="x",
        transport_uri="can://x",
        profile=profile,
        bus_manager=mgr,
    )
    # Did not call connect()
    with pytest.raises(IOError):
        driver.read_point(driver._points["speed"])
    mgr.shutdown_all()


def test_write_when_disconnected_raises():
    mgr = CANBusManager()
    profile = _profile_with_channel(SAMPLE_PROFILE, _unique_channel("dc2"))
    driver = GenericCANDriver(
        instrument_id="x",
        transport_uri="can://x",
        profile=profile,
        bus_manager=mgr,
    )
    with pytest.raises(IOError):
        driver.write_point(driver._points["target_speed"], 100)
    mgr.shutdown_all()


# ---------------------------------------------------------------------------
# Example profile parse coverage
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "profile_filename,min_signals,min_commands",
    [
        ("bms_orion_jr2.yaml", 20, 1),
        ("motor_controller_curtis_1238e.yaml", 10, 1),
    ],
)
def test_example_profile_parses(profile_filename, min_signals, min_commands):
    """Ground-truth profiles in profiles/can/ instantiate cleanly."""
    import os
    import yaml

    here = os.path.dirname(__file__)
    profile_path = os.path.join(
        here, "..", "src", "galois_edge", "profiles", "can", profile_filename
    )
    with open(profile_path) as f:
        profile = yaml.safe_load(f)
    assert profile["protocol"] == "can"
    mgr = CANBusManager()
    driver = GenericCANDriver(
        instrument_id="example",
        transport_uri=f"can://{profile_filename}",
        profile=profile,
        bus_manager=mgr,
    )
    try:
        assert len(driver._points) >= min_signals
        assert len(driver._commands) >= min_commands
        # Identity always present
        assert profile["identity"]["manufacturer"]
        assert profile["identity"]["model"]
        # Filters list parses
        assert isinstance(driver._raw_filters, list)
    finally:
        mgr.shutdown_all()


def test_orion_profile_has_multiplex_signals():
    """Verify the Orion profile actually exercises the mux pathway."""
    import os
    import yaml

    here = os.path.dirname(__file__)
    profile_path = os.path.join(
        here, "..", "src", "galois_edge", "profiles", "can",
        "bms_orion_jr2.yaml",
    )
    with open(profile_path) as f:
        profile = yaml.safe_load(f)
    mgr = CANBusManager()
    driver = GenericCANDriver(
        instrument_id="example",
        transport_uri="can://example",
        profile=profile,
        bus_manager=mgr,
    )
    try:
        # cell_voltage_status carries a mux selector
        assert driver._mux_for_message.get("cell_voltage_status") == "cell_index"
        # And cell_voltage_0 is bound to mux_value 0
        cv0 = driver._points["cell_voltage_0"]
        assert cv0.addressing.get("mux_value") == 0
    finally:
        mgr.shutdown_all()
