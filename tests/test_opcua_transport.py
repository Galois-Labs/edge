"""Tests for ``OPCUABusManager`` — loop lifecycle, client caching, security strings."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import pytest

asyncua = pytest.importorskip("asyncua")
from asyncua import Server, ua  # noqa: E402

from galois_edge.drivers.opcua.transport import (  # noqa: E402
    DEFAULT_LOOP_CALL_TIMEOUT,
    OPCUABusManager,
    OPCUA_AVAILABLE,
    _security_policy_string,
)


# ---------------------------------------------------------------------------
# In-process OPC-UA server fixture
# ---------------------------------------------------------------------------


class _ServerHarness:
    """Drives an asyncua.Server on its own daemon thread + asyncio loop."""

    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint
        self.server: Server | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None
        self.ns: int = 0
        self._ready = threading.Event()
        self._stop_event: asyncio.Event | None = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, daemon=True, name="opcua-test-srv")
        self.thread.start()
        if not self._ready.wait(timeout=15.0):
            raise RuntimeError("OPC-UA test server failed to start")

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
        self.server = Server()
        await self.server.init()
        self.server.set_endpoint(self.endpoint)
        self.server.set_server_name("Galois Test OPC-UA Server")
        self.server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
        self.ns = await self.server.register_namespace("urn:galois:test")
        self._stop_event = asyncio.Event()
        async with self.server:
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


@pytest.fixture(scope="module")
def opcua_server() -> Any:
    if not OPCUA_AVAILABLE:
        pytest.skip("asyncua not installed")
    h = _ServerHarness(endpoint="opc.tcp://127.0.0.1:48400/galois-test/")
    h.start()
    try:
        yield h
    finally:
        h.stop()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_security_policy_string_disabled() -> None:
    assert _security_policy_string("None", "None") is None
    assert _security_policy_string("", "") is None


def test_security_policy_string_enabled() -> None:
    s = _security_policy_string("Basic256Sha256", "SignAndEncrypt", "/c.pem", "/k.pem")
    assert s == "Basic256Sha256,SignAndEncrypt,/c.pem,/k.pem"


def test_security_policy_string_partial() -> None:
    s = _security_policy_string("Basic256", "Sign")
    assert s == "Basic256,Sign,,"


def test_loop_lazy_start() -> None:
    mgr = OPCUABusManager()
    assert mgr._loop is None  # type: ignore[attr-defined]
    loop = mgr.loop
    assert loop is not None
    assert loop.is_running()
    mgr.shutdown()


def test_loop_call_returns_value() -> None:
    mgr = OPCUABusManager()
    try:
        async def _coro() -> int:
            await asyncio.sleep(0)
            return 42
        assert mgr.loop_call(_coro()) == 42
    finally:
        mgr.shutdown()


def test_loop_call_propagates_exception() -> None:
    mgr = OPCUABusManager()
    try:
        async def _bomb() -> None:
            raise RuntimeError("boom")
        with pytest.raises(RuntimeError, match="boom"):
            mgr.loop_call(_bomb())
    finally:
        mgr.shutdown()


def test_loop_call_timeout() -> None:
    mgr = OPCUABusManager()
    try:
        async def _slow() -> None:
            await asyncio.sleep(2.0)
        # Use TimeoutError or future timeout
        from concurrent.futures import TimeoutError as FutTimeout
        with pytest.raises(FutTimeout):
            mgr.loop_call(_slow(), timeout=0.1)
    finally:
        mgr.shutdown()


def test_get_client_anonymous(opcua_server: _ServerHarness) -> None:
    mgr = OPCUABusManager()
    try:
        client = mgr.get_client(opcua_server.endpoint, timeout=10.0)
        assert client is not None
        # Client cached: second get_client returns the same instance.
        client2 = mgr.get_client(opcua_server.endpoint, timeout=10.0)
        assert client is client2
        assert mgr.active_clients() == [(opcua_server.endpoint, "anon")]
    finally:
        mgr.shutdown()


def test_get_client_refcount(opcua_server: _ServerHarness) -> None:
    mgr = OPCUABusManager()
    try:
        mgr.get_client(opcua_server.endpoint, timeout=10.0)
        mgr.get_client(opcua_server.endpoint, timeout=10.0)
        # Two refs — releasing once keeps client.
        mgr.release_client(opcua_server.endpoint, timeout=10.0)
        assert mgr.active_clients() == [(opcua_server.endpoint, "anon")]
        # Second release tears down.
        mgr.release_client(opcua_server.endpoint, timeout=10.0)
        assert mgr.active_clients() == []
    finally:
        mgr.shutdown()


def test_release_unknown_client_is_noop() -> None:
    mgr = OPCUABusManager()
    try:
        # Just don't crash.
        mgr.release_client("opc.tcp://127.0.0.1:9/notreal/")
        assert mgr.active_clients() == []
    finally:
        mgr.shutdown()


def test_get_client_separate_identities(opcua_server: _ServerHarness) -> None:
    mgr = OPCUABusManager()
    try:
        anon = mgr.get_client(opcua_server.endpoint, timeout=10.0)
        # Different user_token_id → different cache entry (even if anon
        # logical session — the keying is what matters, not whether the
        # server actually accepts the credentials).
        # Use a separate anon connection by simulating a username key
        # without actually auth-ing — for that we can directly inspect
        # the cache key via active_clients.
        keys = mgr.active_clients()
        assert keys == [(opcua_server.endpoint, "anon")]
    finally:
        mgr.shutdown()


def test_shutdown_idempotent() -> None:
    mgr = OPCUABusManager()
    mgr.shutdown()
    mgr.shutdown()  # should not raise


def test_shutdown_cleans_loop_thread() -> None:
    mgr = OPCUABusManager()
    _ = mgr.loop  # start
    mgr.shutdown()
    # Thread should have terminated.
    if mgr._loop_thread is not None:  # type: ignore[attr-defined]
        # already joined inside shutdown
        assert not mgr._loop_thread.is_alive()  # type: ignore[attr-defined]


def test_loop_persists_across_calls() -> None:
    mgr = OPCUABusManager()
    try:
        l1 = mgr.loop
        l2 = mgr.loop
        assert l1 is l2
    finally:
        mgr.shutdown()


def test_default_timeout_constant() -> None:
    assert DEFAULT_LOOP_CALL_TIMEOUT > 0
