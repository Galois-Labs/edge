"""Tests for ``GenericOpcuaDriver`` against an in-process ``asyncua.Server``.

The fixture spins a real OPC-UA server on a dedicated thread/loop, populates
it with one variable per VariantType and a couple of methods, then exercises
the driver end-to-end. Subscriptions are validated by triggering server-side
writes and waiting for the user callback to fire.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import threading
import time
from typing import Any, Callable

import pytest

asyncua = pytest.importorskip("asyncua")
from asyncua import Server, ua  # noqa: E402
from asyncua.common.methods import uamethod  # noqa: E402

from galois_edge.drivers.opcua.driver import GenericOpcuaDriver  # noqa: E402
from galois_edge.drivers.opcua.transport import (  # noqa: E402
    OPCUABusManager,
    OPCUA_AVAILABLE,
)


# ---------------------------------------------------------------------------
# In-process OPC-UA server
# ---------------------------------------------------------------------------


@uamethod
def _add(parent: Any, a: int, b: int) -> int:
    return a + b


@uamethod
def _start(parent: Any, rate: int) -> bool:
    return rate > 0


@uamethod
def _no_args(parent: Any) -> int:
    return 7


class _ServerFixture:
    """Drives a populated asyncua.Server on its own thread + loop."""

    ENDPOINT = "opc.tcp://127.0.0.1:48410/galois-driver-test/"

    def __init__(self) -> None:
        self.server: Server | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None
        self.ns: int = 0
        self._ready = threading.Event()
        self._stop_event: asyncio.Event | None = None
        # Map from name → (NodeId, Node) populated during setup.
        self.vars: dict[str, Any] = {}
        self.method_node: Any = None
        self.start_method_node: Any = None
        self.no_args_method_node: Any = None
        self.object_node: Any = None

    # -- Lifecycle --

    def start(self) -> None:
        self.thread = threading.Thread(
            target=self._run, daemon=True, name="opcua-test-srv-driver"
        )
        self.thread.start()
        if not self._ready.wait(timeout=20.0):
            raise RuntimeError("OPC-UA driver-test server failed to start")

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop
        try:
            loop.run_until_complete(self._async_main())
        finally:
            try:
                loop.close()
            except Exception:
                pass

    async def _async_main(self) -> None:
        srv = Server()
        await srv.init()
        srv.set_endpoint(self.ENDPOINT)
        srv.set_server_name("Galois Driver Test Server")
        srv.set_security_policy([ua.SecurityPolicyType.NoSecurity])
        self.ns = await srv.register_namespace("urn:galois:test")
        self.server = srv

        # Populate address space.
        objects = srv.nodes.objects
        obj = await objects.add_object(self.ns, "TestObject")
        self.object_node = obj

        # One variable of each common Variant type.
        self.vars["bool"] = await obj.add_variable(self.ns, "Bool", True, ua.VariantType.Boolean)
        self.vars["int16"] = await obj.add_variable(self.ns, "Int16", ua.Variant(0, ua.VariantType.Int16))
        self.vars["uint16"] = await obj.add_variable(self.ns, "UInt16", ua.Variant(0, ua.VariantType.UInt16))
        self.vars["int32"] = await obj.add_variable(self.ns, "Int32", ua.Variant(0, ua.VariantType.Int32))
        self.vars["uint32"] = await obj.add_variable(self.ns, "UInt32", ua.Variant(0, ua.VariantType.UInt32))
        self.vars["int64"] = await obj.add_variable(self.ns, "Int64", ua.Variant(0, ua.VariantType.Int64))
        self.vars["uint64"] = await obj.add_variable(self.ns, "UInt64", ua.Variant(0, ua.VariantType.UInt64))
        self.vars["float"] = await obj.add_variable(self.ns, "Float", ua.Variant(0.0, ua.VariantType.Float))
        self.vars["double"] = await obj.add_variable(self.ns, "Double", ua.Variant(0.0, ua.VariantType.Double))
        self.vars["string"] = await obj.add_variable(self.ns, "String", "hello", ua.VariantType.String)
        self.vars["datetime"] = await obj.add_variable(self.ns, "DateTime", dt.datetime.now(dt.timezone.utc), ua.VariantType.DateTime)
        self.vars["bytestring"] = await obj.add_variable(self.ns, "ByteString", b"\x01\x02\x03", ua.VariantType.ByteString)

        # Make them all writable.
        for v in self.vars.values():
            await v.set_writable()

        # Methods.
        self.method_node = await obj.add_method(
            self.ns, "Add",
            _add,
            [ua.VariantType.Int32, ua.VariantType.Int32],
            [ua.VariantType.Int32],
        )
        self.start_method_node = await obj.add_method(
            self.ns, "Start",
            _start,
            [ua.VariantType.UInt32],
            [ua.VariantType.Boolean],
        )
        self.no_args_method_node = await obj.add_method(
            self.ns, "NoArgs",
            _no_args,
            [],
            [ua.VariantType.Int32],
        )

        self._stop_event = asyncio.Event()
        async with srv:
            self._ready.set()
            await self._stop_event.wait()

    def call(self, coro: Any, timeout: float = 10.0) -> Any:
        assert self.loop is not None
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return fut.result(timeout=timeout)

    def stop(self) -> None:
        if self.loop is not None and self._stop_event is not None and self.loop.is_running():
            self.loop.call_soon_threadsafe(self._stop_event.set)
        if self.thread is not None:
            self.thread.join(timeout=10.0)

    # -- Helpers --

    def node_id(self, name: str) -> str:
        return self.vars[name].nodeid.to_string()

    def write_server_side(self, name: str, value: Any, vtype: Any | None = None) -> None:
        """Write a value directly through the server (bypassing the driver)."""
        node = self.vars[name]
        if vtype is None:
            self.call(node.write_value(value))
        else:
            self.call(node.write_value(ua.Variant(value, vtype)))


@pytest.fixture(scope="module")
def opcua_srv() -> Any:
    if not OPCUA_AVAILABLE:
        pytest.skip("asyncua not installed")
    h = _ServerFixture()
    h.start()
    try:
        yield h
    finally:
        h.stop()


@pytest.fixture
def bus_manager() -> Any:
    mgr = OPCUABusManager()
    yield mgr
    mgr.shutdown()


def _make_profile(srv: _ServerFixture, **overrides: Any) -> dict[str, Any]:
    """Build a full profile referencing the fixture's nodes by NodeId."""
    profile: dict[str, Any] = {
        "protocol": "opcua",
        "identity": {
            "manufacturer": "GaloisTest",
            "model": "InProcServer",
            "description": "Test profile",
        },
        "connection": {
            "endpoint_url": srv.ENDPOINT,
            "security_policy": "None",
            "security_mode": "None",
            "user_token": "anonymous",
        },
        "nodes": {
            "v_bool":     {"node_id": srv.node_id("bool"),     "data_type": "Boolean",   "access": "read_write"},
            "v_int16":    {"node_id": srv.node_id("int16"),    "data_type": "Int16",     "access": "read_write"},
            "v_uint16":   {"node_id": srv.node_id("uint16"),   "data_type": "UInt16",    "access": "read_write"},
            "v_int32":    {"node_id": srv.node_id("int32"),    "data_type": "Int32",     "access": "read_write"},
            "v_uint32":   {"node_id": srv.node_id("uint32"),   "data_type": "UInt32",    "access": "read_write"},
            "v_int64":    {"node_id": srv.node_id("int64"),    "data_type": "Int64",     "access": "read_write"},
            "v_uint64":   {"node_id": srv.node_id("uint64"),   "data_type": "UInt64",    "access": "read_write"},
            "v_float":    {"node_id": srv.node_id("float"),    "data_type": "Float",     "access": "read_write"},
            "v_double":   {"node_id": srv.node_id("double"),   "data_type": "Double",    "access": "read_write", "deadband": 0.5},
            "v_string":   {"node_id": srv.node_id("string"),   "data_type": "String",    "access": "read_write"},
            "v_datetime": {"node_id": srv.node_id("datetime"), "data_type": "DateTime",  "access": "read"},
            "v_bytes":    {"node_id": srv.node_id("bytestring"), "data_type": "ByteString", "access": "read_write"},
            "v_ranged":   {"node_id": srv.node_id("int32"),    "data_type": "Int32",     "access": "read_write", "range": [0, 100]},
            "v_readonly": {"node_id": srv.node_id("string"),   "data_type": "String",    "access": "read"},
        },
        "methods": {
            "add": {
                "object_node_id": srv.object_node.nodeid.to_string(),
                "method_node_id": srv.method_node.nodeid.to_string(),
                "input_arguments": [
                    {"name": "a", "data_type": "Int32"},
                    {"name": "b", "data_type": "Int32"},
                ],
                "output_arguments": [
                    {"name": "sum", "data_type": "Int32"},
                ],
            },
            "start_acquisition": {
                "object_node_id": srv.object_node.nodeid.to_string(),
                "method_node_id": srv.start_method_node.nodeid.to_string(),
                "input_arguments": [
                    {"name": "rate", "data_type": "UInt32"},
                ],
                "output_arguments": [
                    {"name": "status", "data_type": "Boolean"},
                ],
            },
            "no_args": {
                "object_node_id": srv.object_node.nodeid.to_string(),
                "method_node_id": srv.no_args_method_node.nodeid.to_string(),
                "input_arguments": [],
                "output_arguments": [
                    {"name": "value", "data_type": "Int32"},
                ],
            },
        },
        "commands": {
            "read_double": {"type": "query", "reads": ["v_double"]},
            "set_int32":   {"type": "action", "writes": [{"register": "v_int32", "value": "{val}"}]},
            "do_add":      {"type": "opcua_method", "method": "add", "arguments": {"a": "{a}", "b": "{b}"}},
        },
    }
    profile.update(overrides)
    return profile


def _connected_driver(srv: _ServerFixture, mgr: OPCUABusManager) -> GenericOpcuaDriver:
    drv = GenericOpcuaDriver(
        instrument_id="inst-1",
        transport_uri=srv.ENDPOINT,
        profile=_make_profile(srv),
        bus_manager=mgr,
    )
    drv.connect()
    return drv


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def test_connect_and_disconnect(opcua_srv: _ServerFixture, bus_manager: OPCUABusManager) -> None:
    drv = _connected_driver(opcua_srv, bus_manager)
    assert drv.connected is True
    drv.disconnect()
    assert drv.connected is False


def test_connect_idempotent(opcua_srv: _ServerFixture, bus_manager: OPCUABusManager) -> None:
    drv = _connected_driver(opcua_srv, bus_manager)
    drv.connect()  # second connect = no-op
    assert drv.connected
    drv.disconnect()


def test_disconnect_when_not_connected(opcua_srv: _ServerFixture, bus_manager: OPCUABusManager) -> None:
    drv = GenericOpcuaDriver(
        instrument_id="inst-x",
        transport_uri=opcua_srv.ENDPOINT,
        profile=_make_profile(opcua_srv),
        bus_manager=bus_manager,
    )
    drv.disconnect()  # no-op
    assert drv.connected is False


def test_identify_returns_string(opcua_srv: _ServerFixture, bus_manager: OPCUABusManager) -> None:
    drv = _connected_driver(opcua_srv, bus_manager)
    try:
        s = drv.identify()
        assert isinstance(s, str)
        assert opcua_srv.ENDPOINT in s
    finally:
        drv.disconnect()


def test_identify_when_not_connected_uses_profile(
    opcua_srv: _ServerFixture, bus_manager: OPCUABusManager,
) -> None:
    drv = GenericOpcuaDriver(
        instrument_id="x",
        transport_uri=opcua_srv.ENDPOINT,
        profile=_make_profile(opcua_srv),
        bus_manager=bus_manager,
    )
    s = drv.identify()
    assert "GaloisTest" in s and "InProcServer" in s


def test_get_capabilities(opcua_srv: _ServerFixture, bus_manager: OPCUABusManager) -> None:
    drv = _connected_driver(opcua_srv, bus_manager)
    try:
        caps = drv.get_capabilities()
        assert caps["protocol"] == "opcua"
        assert caps["supports_native_subscription"] is True
        assert "add" in caps["methods"]
        assert "do_add" in caps["commands"]
        assert caps["node_count"] >= 12
        assert caps["writable"] >= 1
    finally:
        drv.disconnect()


# ---------------------------------------------------------------------------
# Read all variant types
# ---------------------------------------------------------------------------


def test_read_boolean(opcua_srv: _ServerFixture, bus_manager: OPCUABusManager) -> None:
    drv = _connected_driver(opcua_srv, bus_manager)
    try:
        opcua_srv.write_server_side("bool", True, ua.VariantType.Boolean)
        assert drv.read_point(drv._points["v_bool"]) is True
        opcua_srv.write_server_side("bool", False, ua.VariantType.Boolean)
        assert drv.read_point(drv._points["v_bool"]) is False
    finally:
        drv.disconnect()


def test_read_int_types(opcua_srv: _ServerFixture, bus_manager: OPCUABusManager) -> None:
    drv = _connected_driver(opcua_srv, bus_manager)
    try:
        opcua_srv.write_server_side("int16", -123, ua.VariantType.Int16)
        opcua_srv.write_server_side("int32", -123456, ua.VariantType.Int32)
        opcua_srv.write_server_side("int64", -1234567890, ua.VariantType.Int64)
        assert drv.read_point(drv._points["v_int16"]) == -123
        assert drv.read_point(drv._points["v_int32"]) == -123456
        assert drv.read_point(drv._points["v_int64"]) == -1234567890
    finally:
        drv.disconnect()


def test_read_uint_types(opcua_srv: _ServerFixture, bus_manager: OPCUABusManager) -> None:
    drv = _connected_driver(opcua_srv, bus_manager)
    try:
        opcua_srv.write_server_side("uint16", 65000, ua.VariantType.UInt16)
        opcua_srv.write_server_side("uint32", 4_000_000_000, ua.VariantType.UInt32)
        opcua_srv.write_server_side("uint64", 9_000_000_000_000, ua.VariantType.UInt64)
        assert drv.read_point(drv._points["v_uint16"]) == 65000
        assert drv.read_point(drv._points["v_uint32"]) == 4_000_000_000
        assert drv.read_point(drv._points["v_uint64"]) == 9_000_000_000_000
    finally:
        drv.disconnect()


def test_read_float_double(opcua_srv: _ServerFixture, bus_manager: OPCUABusManager) -> None:
    drv = _connected_driver(opcua_srv, bus_manager)
    try:
        opcua_srv.write_server_side("float", 1.5, ua.VariantType.Float)
        opcua_srv.write_server_side("double", 2.71828, ua.VariantType.Double)
        assert abs(drv.read_point(drv._points["v_float"]) - 1.5) < 1e-3
        assert abs(drv.read_point(drv._points["v_double"]) - 2.71828) < 1e-9
    finally:
        drv.disconnect()


def test_read_string(opcua_srv: _ServerFixture, bus_manager: OPCUABusManager) -> None:
    drv = _connected_driver(opcua_srv, bus_manager)
    try:
        opcua_srv.write_server_side("string", "world", ua.VariantType.String)
        assert drv.read_point(drv._points["v_string"]) == "world"
    finally:
        drv.disconnect()


def test_read_datetime(opcua_srv: _ServerFixture, bus_manager: OPCUABusManager) -> None:
    drv = _connected_driver(opcua_srv, bus_manager)
    try:
        now = dt.datetime.now(dt.timezone.utc)
        opcua_srv.write_server_side("datetime", now, ua.VariantType.DateTime)
        v = drv.read_point(drv._points["v_datetime"])
        assert isinstance(v, dt.datetime)
    finally:
        drv.disconnect()


def test_read_bytestring(opcua_srv: _ServerFixture, bus_manager: OPCUABusManager) -> None:
    drv = _connected_driver(opcua_srv, bus_manager)
    try:
        opcua_srv.write_server_side("bytestring", b"\xde\xad\xbe\xef", ua.VariantType.ByteString)
        assert drv.read_point(drv._points["v_bytes"]) == b"\xde\xad\xbe\xef"
    finally:
        drv.disconnect()


# ---------------------------------------------------------------------------
# Write each type and roundtrip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "point_name,value",
    [
        ("v_bool", True),
        ("v_bool", False),
        ("v_int16", -42),
        ("v_uint16", 7),
        ("v_int32", -100000),
        ("v_uint32", 100000),
        ("v_int64", -10**12),
        ("v_uint64", 10**12),
        ("v_float", 3.25),
        ("v_double", 2.5),
        ("v_string", "abc"),
    ],
)
def test_write_roundtrip(
    opcua_srv: _ServerFixture, bus_manager: OPCUABusManager,
    point_name: str, value: Any,
) -> None:
    drv = _connected_driver(opcua_srv, bus_manager)
    try:
        p = drv._points[point_name]
        drv.write_point(p, value)
        got = drv.read_point(p)
        if isinstance(value, float):
            assert abs(got - value) < 1e-3
        else:
            assert got == value
    finally:
        drv.disconnect()


def test_write_bytestring_roundtrip(opcua_srv: _ServerFixture, bus_manager: OPCUABusManager) -> None:
    drv = _connected_driver(opcua_srv, bus_manager)
    try:
        p = drv._points["v_bytes"]
        drv.write_point(p, b"\x10\x20\x30")
        assert drv.read_point(p) == b"\x10\x20\x30"
    finally:
        drv.disconnect()


def test_write_read_only_raises(opcua_srv: _ServerFixture, bus_manager: OPCUABusManager) -> None:
    drv = _connected_driver(opcua_srv, bus_manager)
    try:
        with pytest.raises(PermissionError):
            drv.write_point(drv._points["v_readonly"], "nope")
    finally:
        drv.disconnect()


def test_write_out_of_range_raises(opcua_srv: _ServerFixture, bus_manager: OPCUABusManager) -> None:
    drv = _connected_driver(opcua_srv, bus_manager)
    try:
        with pytest.raises(ValueError):
            drv.write_point(drv._points["v_ranged"], 9999)
    finally:
        drv.disconnect()


def test_write_in_range_ok(opcua_srv: _ServerFixture, bus_manager: OPCUABusManager) -> None:
    drv = _connected_driver(opcua_srv, bus_manager)
    try:
        drv.write_point(drv._points["v_ranged"], 50)
    finally:
        drv.disconnect()


def test_read_when_not_connected_raises(opcua_srv: _ServerFixture, bus_manager: OPCUABusManager) -> None:
    drv = GenericOpcuaDriver(
        instrument_id="x",
        transport_uri=opcua_srv.ENDPOINT,
        profile=_make_profile(opcua_srv),
        bus_manager=bus_manager,
    )
    with pytest.raises(IOError):
        drv.read_point(drv._points["v_int32"])


# ---------------------------------------------------------------------------
# Batch read
# ---------------------------------------------------------------------------


def test_batch_read(opcua_srv: _ServerFixture, bus_manager: OPCUABusManager) -> None:
    drv = _connected_driver(opcua_srv, bus_manager)
    try:
        opcua_srv.write_server_side("int32", 11, ua.VariantType.Int32)
        opcua_srv.write_server_side("string", "batch", ua.VariantType.String)
        results = drv.read_points([drv._points["v_int32"], drv._points["v_string"]])
        assert results == {"v_int32": 11, "v_string": "batch"}
    finally:
        drv.disconnect()


def test_batch_read_empty(opcua_srv: _ServerFixture, bus_manager: OPCUABusManager) -> None:
    drv = _connected_driver(opcua_srv, bus_manager)
    try:
        assert drv.read_points([]) == {}
    finally:
        drv.disconnect()


# ---------------------------------------------------------------------------
# Bad node id surfaces as KeyError
# ---------------------------------------------------------------------------


def test_read_bad_node_id_raises_key_error(
    opcua_srv: _ServerFixture, bus_manager: OPCUABusManager,
) -> None:
    profile = _make_profile(opcua_srv)
    profile["nodes"]["bad"] = {
        "node_id": "ns=99;s=DoesNotExist",
        "data_type": "Int32",
        "access": "read",
    }
    drv = GenericOpcuaDriver(
        instrument_id="x", transport_uri=opcua_srv.ENDPOINT,
        profile=profile, bus_manager=bus_manager,
    )
    drv.connect()
    try:
        with pytest.raises(KeyError):
            drv.read_point(drv._points["bad"])
    finally:
        drv.disconnect()


def test_write_bad_node_id_raises_key_error(
    opcua_srv: _ServerFixture, bus_manager: OPCUABusManager,
) -> None:
    profile = _make_profile(opcua_srv)
    profile["nodes"]["bad"] = {
        "node_id": "ns=99;s=DoesNotExist",
        "data_type": "Int32",
        "access": "read_write",
    }
    drv = GenericOpcuaDriver(
        instrument_id="x", transport_uri=opcua_srv.ENDPOINT,
        profile=profile, bus_manager=bus_manager,
    )
    drv.connect()
    try:
        with pytest.raises(KeyError):
            drv.write_point(drv._points["bad"], 1)
    finally:
        drv.disconnect()


# ---------------------------------------------------------------------------
# Method invocation
# ---------------------------------------------------------------------------


def test_call_method_basic(opcua_srv: _ServerFixture, bus_manager: OPCUABusManager) -> None:
    drv = _connected_driver(opcua_srv, bus_manager)
    try:
        result = drv.call_method("add", {"a": 3, "b": 4})
        assert result == 7
    finally:
        drv.disconnect()


def test_call_method_missing_arg(opcua_srv: _ServerFixture, bus_manager: OPCUABusManager) -> None:
    drv = _connected_driver(opcua_srv, bus_manager)
    try:
        with pytest.raises(KeyError):
            drv.call_method("add", {"a": 1})
    finally:
        drv.disconnect()


def test_call_method_unknown_method(opcua_srv: _ServerFixture, bus_manager: OPCUABusManager) -> None:
    drv = _connected_driver(opcua_srv, bus_manager)
    try:
        with pytest.raises(KeyError):
            drv.call_method("nope", {})
    finally:
        drv.disconnect()


def test_call_method_no_args(opcua_srv: _ServerFixture, bus_manager: OPCUABusManager) -> None:
    drv = _connected_driver(opcua_srv, bus_manager)
    try:
        result = drv.call_method("no_args", {})
        assert result == 7
    finally:
        drv.disconnect()


def test_method_via_command(opcua_srv: _ServerFixture, bus_manager: OPCUABusManager) -> None:
    drv = _connected_driver(opcua_srv, bus_manager)
    try:
        result = drv.execute_command("do_add", {"a": 10, "b": 5})
        assert result == 15
    finally:
        drv.disconnect()


def test_command_query_routes_to_base(
    opcua_srv: _ServerFixture, bus_manager: OPCUABusManager,
) -> None:
    drv = _connected_driver(opcua_srv, bus_manager)
    try:
        opcua_srv.write_server_side("double", 9.0, ua.VariantType.Double)
        result = drv.execute_command("read_double")
        assert abs(result - 9.0) < 1e-9
    finally:
        drv.disconnect()


def test_command_action_routes_to_base(
    opcua_srv: _ServerFixture, bus_manager: OPCUABusManager,
) -> None:
    drv = _connected_driver(opcua_srv, bus_manager)
    try:
        drv.execute_command("set_int32", {"val": 99})
        assert drv.read_point(drv._points["v_int32"]) == 99
    finally:
        drv.disconnect()


def test_unknown_command_raises(opcua_srv: _ServerFixture, bus_manager: OPCUABusManager) -> None:
    drv = _connected_driver(opcua_srv, bus_manager)
    try:
        with pytest.raises(ValueError):
            drv.execute_command("does_not_exist")
    finally:
        drv.disconnect()


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------


def _wait_for(predicate: Callable[[], bool], timeout: float = 5.0, interval: float = 0.05) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_subscribe_fires_on_change(
    opcua_srv: _ServerFixture, bus_manager: OPCUABusManager,
) -> None:
    drv = _connected_driver(opcua_srv, bus_manager)
    try:
        # Use the int32 point — no deadband.
        events: list[dict[str, Any]] = []
        ev_lock = threading.Lock()

        def cb(values: dict[str, Any]) -> None:
            with ev_lock:
                events.append(values)

        sub_id = drv.subscribe([drv._points["v_int32"]], cb, interval_ms=100)
        try:
            # Initial publish lands once.
            assert _wait_for(lambda: len(events) >= 1, timeout=5.0)
            opcua_srv.write_server_side("int32", 555, ua.VariantType.Int32)
            assert _wait_for(
                lambda: any(e.get("v_int32") == 555 for e in events),
                timeout=5.0,
            )
        finally:
            drv.unsubscribe(sub_id)
    finally:
        drv.disconnect()


def test_subscribe_deadband_suppresses_small_change(
    opcua_srv: _ServerFixture, bus_manager: OPCUABusManager,
) -> None:
    """v_double has deadband=0.5 — changes < 0.5 should NOT fire."""
    drv = _connected_driver(opcua_srv, bus_manager)
    try:
        opcua_srv.write_server_side("double", 10.0, ua.VariantType.Double)

        events: list[dict[str, Any]] = []
        ev_lock = threading.Lock()

        def cb(values: dict[str, Any]) -> None:
            with ev_lock:
                events.append(values)

        sub_id = drv.subscribe([drv._points["v_double"]], cb, interval_ms=100)
        try:
            # Wait for initial publish.
            assert _wait_for(lambda: len(events) >= 1, timeout=5.0)
            initial_count = len(events)

            # Tiny change well below deadband — must NOT fire.
            opcua_srv.write_server_side("double", 10.05, ua.VariantType.Double)
            time.sleep(1.0)
            assert len(events) == initial_count, (
                f"deadband didn't suppress sub-threshold change: events={events}"
            )

            # Big change above deadband — must fire.
            opcua_srv.write_server_side("double", 12.0, ua.VariantType.Double)
            assert _wait_for(
                lambda: any(
                    isinstance(e.get("v_double"), (int, float))
                    and abs(e["v_double"] - 12.0) < 1e-3
                    for e in events
                ),
                timeout=5.0,
            )
        finally:
            drv.unsubscribe(sub_id)
    finally:
        drv.disconnect()


def test_subscribe_empty_points_raises(
    opcua_srv: _ServerFixture, bus_manager: OPCUABusManager,
) -> None:
    drv = _connected_driver(opcua_srv, bus_manager)
    try:
        with pytest.raises(ValueError):
            drv.subscribe([], lambda v: None, interval_ms=100)
    finally:
        drv.disconnect()


def test_subscribe_when_not_connected_raises(
    opcua_srv: _ServerFixture, bus_manager: OPCUABusManager,
) -> None:
    drv = GenericOpcuaDriver(
        instrument_id="x", transport_uri=opcua_srv.ENDPOINT,
        profile=_make_profile(opcua_srv), bus_manager=bus_manager,
    )
    with pytest.raises(IOError):
        drv.subscribe([drv._points["v_int32"]], lambda v: None, interval_ms=100)


def test_unsubscribe_unknown_id_is_noop(
    opcua_srv: _ServerFixture, bus_manager: OPCUABusManager,
) -> None:
    drv = _connected_driver(opcua_srv, bus_manager)
    try:
        drv.unsubscribe("nope-never-existed")
    finally:
        drv.disconnect()


def test_subscribe_multiple_points(
    opcua_srv: _ServerFixture, bus_manager: OPCUABusManager,
) -> None:
    drv = _connected_driver(opcua_srv, bus_manager)
    try:
        events: list[dict[str, Any]] = []
        ev_lock = threading.Lock()

        def cb(values: dict[str, Any]) -> None:
            with ev_lock:
                events.append(values)

        sub_id = drv.subscribe(
            [drv._points["v_int32"], drv._points["v_string"]],
            cb, interval_ms=100,
        )
        try:
            opcua_srv.write_server_side("int32", 444, ua.VariantType.Int32)
            opcua_srv.write_server_side("string", "subbed", ua.VariantType.String)
            assert _wait_for(
                lambda: (
                    any(e.get("v_int32") == 444 for e in events)
                    and any(e.get("v_string") == "subbed" for e in events)
                ),
                timeout=5.0,
            )
        finally:
            drv.unsubscribe(sub_id)
    finally:
        drv.disconnect()


# ---------------------------------------------------------------------------
# Reconnect
# ---------------------------------------------------------------------------


def test_reconnect_after_disconnect(
    opcua_srv: _ServerFixture, bus_manager: OPCUABusManager,
) -> None:
    drv = _connected_driver(opcua_srv, bus_manager)
    drv.disconnect()
    # Re-connect using same driver instance.
    drv.connect()
    try:
        opcua_srv.write_server_side("int32", 7, ua.VariantType.Int32)
        assert drv.read_point(drv._points["v_int32"]) == 7
    finally:
        drv.disconnect()


# ---------------------------------------------------------------------------
# Browse path resolution
# ---------------------------------------------------------------------------


def test_browse_path_resolution(
    opcua_srv: _ServerFixture, bus_manager: OPCUABusManager,
) -> None:
    drv = _connected_driver(opcua_srv, bus_manager)
    try:
        # Browse from Objects → TestObject → Int32
        ns = opcua_srv.ns
        resolved = drv.browse_path([f"{ns}:TestObject", f"{ns}:Int32"], starting_node="i=85")
        # Should match the fixture's known NodeId for int32.
        assert resolved == opcua_srv.node_id("int32")
    finally:
        drv.disconnect()


# ---------------------------------------------------------------------------
# Variant coercion
# ---------------------------------------------------------------------------


def test_coerce_variant_known_type(
    opcua_srv: _ServerFixture, bus_manager: OPCUABusManager,
) -> None:
    drv = GenericOpcuaDriver(
        instrument_id="x",
        transport_uri=opcua_srv.ENDPOINT,
        profile=_make_profile(opcua_srv),
        bus_manager=bus_manager,
    )
    v = drv._coerce_variant(5, "Int32")
    assert v.VariantType == ua.VariantType.Int32
    assert v.Value == 5


def test_coerce_variant_unknown_type_falls_back(
    opcua_srv: _ServerFixture, bus_manager: OPCUABusManager,
) -> None:
    drv = GenericOpcuaDriver(
        instrument_id="x",
        transport_uri=opcua_srv.ENDPOINT,
        profile=_make_profile(opcua_srv),
        bus_manager=bus_manager,
    )
    v = drv._coerce_variant(7, "Variant")
    # asyncua sniffs the type; it just shouldn't raise.
    assert v.Value == 7


def test_coerce_variant_passthrough(
    opcua_srv: _ServerFixture, bus_manager: OPCUABusManager,
) -> None:
    drv = GenericOpcuaDriver(
        instrument_id="x",
        transport_uri=opcua_srv.ENDPOINT,
        profile=_make_profile(opcua_srv),
        bus_manager=bus_manager,
    )
    src = ua.Variant(3.14, ua.VariantType.Double)
    v = drv._coerce_variant(src, "Double")
    assert v is src


# ---------------------------------------------------------------------------
# Profile-driven config: env-var password
# ---------------------------------------------------------------------------


def test_password_from_env(monkeypatch: Any, opcua_srv: _ServerFixture, bus_manager: OPCUABusManager) -> None:
    monkeypatch.setenv("OPCUA_TEST_PASSWORD", "s3cr3t")
    profile = _make_profile(opcua_srv)
    profile["connection"]["password_env"] = "OPCUA_TEST_PASSWORD"
    drv = GenericOpcuaDriver(
        instrument_id="x",
        transport_uri=opcua_srv.ENDPOINT,
        profile=profile,
        bus_manager=bus_manager,
    )
    assert drv._password == "s3cr3t"


# ---------------------------------------------------------------------------
# Auth: anonymous flag flows through
# ---------------------------------------------------------------------------


def test_anonymous_is_default(opcua_srv: _ServerFixture, bus_manager: OPCUABusManager) -> None:
    drv = _connected_driver(opcua_srv, bus_manager)
    try:
        assert drv.user_token == "anonymous"
    finally:
        drv.disconnect()


def test_username_token_string(opcua_srv: _ServerFixture, bus_manager: OPCUABusManager) -> None:
    profile = _make_profile(opcua_srv)
    profile["connection"]["user_token"] = "username"
    profile["connection"]["username"] = "alice"
    # We don't actually try to connect (server has no user manager) — just
    # assert the configuration is accepted.
    drv = GenericOpcuaDriver(
        instrument_id="x",
        transport_uri=opcua_srv.ENDPOINT,
        profile=profile,
        bus_manager=bus_manager,
    )
    assert drv.user_token == "username"
    assert drv.username == "alice"


# ---------------------------------------------------------------------------
# Self-registration: import doesn't crash
# ---------------------------------------------------------------------------


def test_package_import() -> None:
    # Import path lights the registry guard.
    from galois_edge.drivers.opcua import (
        GenericOpcuaDriver as _G, OPCUABusManager as _M, OPCUA_AVAILABLE as _A,
    )
    assert _G is GenericOpcuaDriver
    assert _M is OPCUABusManager
    assert _A is True
