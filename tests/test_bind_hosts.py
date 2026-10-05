"""E10 bind hosts (edge-api.md §1); port 0 is read back so tests stay parallel-safe."""
from __future__ import annotations

import logging
import socket

import aiohttp
import grpc
import pytest

from galois_edge import edge_pb2, edge_pb2_grpc
from galois_edge.grpc_server import GRPCServer
from galois_edge.ws_server import WebSocketServer

pytestmark = pytest.mark.critical

# A loopback address that is not the 127.0.0.1 default, so a listener that ignores
# its configured host (a hard-coded "127.0.0.1") cannot pass the listen tests below.
ALT_LOOPBACK = "127.0.0.2"


@pytest.fixture
def alt_loopback() -> str:
    probe = socket.socket()
    try:
        probe.bind((ALT_LOOPBACK, 0))
    except OSError:
        pytest.skip(f"{ALT_LOOPBACK} is not bindable here (macOS routes only 127.0.0.1 by default)")
    finally:
        probe.close()
    return ALT_LOOPBACK


def _mcp_server(mock_instrument_manager, mock_command_handler, mock_capability_manager, **kwargs):
    from galois_edge.mcp.server import MCPServer

    return MCPServer(capability_manager=mock_capability_manager, command_handler=mock_command_handler,
                     instrument_manager=mock_instrument_manager, port=0, dynamic_tools_enabled=False, **kwargs)


def test_grpc_bind_host_defaults_from_env(monkeypatch, mock_instrument_manager, mock_command_handler):
    monkeypatch.setenv("GRPC_BIND_HOST", "0.0.0.0")
    srv = GRPCServer(mock_instrument_manager, mock_command_handler, edge_id="t", port=0)
    assert srv.bind_host == "0.0.0.0"
    monkeypatch.delenv("GRPC_BIND_HOST")
    assert GRPCServer(mock_instrument_manager, mock_command_handler, edge_id="t", port=0).bind_host == "127.0.0.1"


def test_explicit_bind_host_wins_over_env(monkeypatch, mock_instrument_manager, mock_command_handler):
    monkeypatch.setenv("GRPC_BIND_HOST", "0.0.0.0")
    srv = GRPCServer(mock_instrument_manager, mock_command_handler, edge_id="t", port=0, bind_host="127.0.0.1")
    assert srv.bind_host == "127.0.0.1"


async def test_grpc_listen_address_uses_bind_host(monkeypatch, mock_instrument_manager, mock_command_handler):
    seen = []

    class _FakeServer:
        def add_insecure_port(self, addr):
            seen.append(addr)
            return 50999

        async def start(self):
            return None

    monkeypatch.setattr("galois_edge.grpc_server.grpc_aio.server", lambda **kw: _FakeServer())
    monkeypatch.setattr(
        "galois_edge.grpc_server.edge_pb2_grpc.add_EdgeDaemonServiceServicer_to_server", lambda *a: None
    )
    srv = GRPCServer(mock_instrument_manager, mock_command_handler, edge_id="t", port=0, bind_host="0.0.0.0")
    assert await srv.start() is True
    assert seen == ["0.0.0.0:0"]
    assert srv.port == 50999
    v6 = GRPCServer(mock_instrument_manager, mock_command_handler, edge_id="t", port=0, bind_host="::1")
    await v6.start()
    assert seen[-1] == "[::1]:0"


async def test_grpc_port_zero_reads_back_and_serves(caplog, mock_instrument_manager, mock_command_handler):
    srv = GRPCServer(mock_instrument_manager, mock_command_handler, edge_id="t", port=0, bind_host="127.0.0.1")
    with caplog.at_level(logging.INFO, logger="galois_edge.grpc_server"):
        assert await srv.start() is True
    try:
        assert srv.port > 0
        assert any(f"127.0.0.1:{srv.port} " in r.getMessage() for r in caplog.records)  # the bound port, not :0
        async with grpc.aio.insecure_channel(f"127.0.0.1:{srv.port}") as ch:
            reply = await edge_pb2_grpc.EdgeDaemonServiceStub(ch).Ping(edge_pb2.PingRequest(), timeout=2)
        assert reply is not None
    finally:
        await srv.stop(grace_period=0)


async def test_ws_port_zero_reads_back_and_serves(monkeypatch, mock_instrument_manager, mock_command_handler):
    monkeypatch.setenv("WS_BIND_HOST", "127.0.0.1")
    srv = WebSocketServer(mock_instrument_manager, mock_command_handler, port=0)
    assert srv.bind_host == "127.0.0.1"
    await srv.start()
    try:
        assert srv.port > 0
        async with aiohttp.ClientSession() as s, s.get(f"http://127.0.0.1:{srv.port}/health") as r:
            assert r.status == 200
    finally:
        await srv.stop()


async def test_mcp_host_from_env_and_port_zero(monkeypatch, mock_instrument_manager, mock_command_handler,
                                                mock_capability_manager):
    from galois_edge.mcp.server import MCPServer

    monkeypatch.setenv("MCP_BIND_HOST", "127.0.0.1")
    srv = MCPServer(capability_manager=mock_capability_manager, command_handler=mock_command_handler,
                    instrument_manager=mock_instrument_manager, port=0, dynamic_tools_enabled=False)
    assert srv.host == "127.0.0.1"
    await srv.start()
    try:
        assert srv.port > 0
    finally:
        await srv.stop()


def test_ws_bind_host_from_env_and_explicit_kwarg_wins(monkeypatch, mock_instrument_manager, mock_command_handler):
    monkeypatch.setenv("WS_BIND_HOST", "0.0.0.0")
    assert WebSocketServer(mock_instrument_manager, mock_command_handler, port=0).bind_host == "0.0.0.0"
    explicit = WebSocketServer(mock_instrument_manager, mock_command_handler, port=0, bind_host="127.0.0.1")
    assert explicit.bind_host == "127.0.0.1"
    monkeypatch.delenv("WS_BIND_HOST")
    assert WebSocketServer(mock_instrument_manager, mock_command_handler, port=0).bind_host == "127.0.0.1"


@pytest.mark.parametrize("source", ["env", "kwarg"])
async def test_ws_listens_on_its_bind_host(source, monkeypatch, alt_loopback, mock_instrument_manager,
                                           mock_command_handler):
    if source == "env":
        monkeypatch.setenv("WS_BIND_HOST", alt_loopback)
        srv = WebSocketServer(mock_instrument_manager, mock_command_handler, port=0)
    else:
        monkeypatch.setenv("WS_BIND_HOST", "127.0.0.1")  # the explicit kwarg beats it
        srv = WebSocketServer(mock_instrument_manager, mock_command_handler, port=0, bind_host=alt_loopback)
    assert srv.bind_host == alt_loopback
    await srv.start()
    try:
        assert [addr[0] for addr in srv._runner.addresses] == [alt_loopback]  # the socket actually bound
        assert srv.port == srv._runner.addresses[0][1]
        async with aiohttp.ClientSession() as s, s.get(f"http://{alt_loopback}:{srv.port}/health") as r:
            assert r.status == 200
    finally:
        await srv.stop()


def test_mcp_host_from_env_and_explicit_kwarg_wins(monkeypatch, mock_instrument_manager, mock_command_handler,
                                                   mock_capability_manager):
    mocks = (mock_instrument_manager, mock_command_handler, mock_capability_manager)
    monkeypatch.setenv("MCP_BIND_HOST", "0.0.0.0")
    assert _mcp_server(*mocks).host == "0.0.0.0"
    assert _mcp_server(*mocks, host="127.0.0.1").host == "127.0.0.1"
    monkeypatch.delenv("MCP_BIND_HOST")
    assert _mcp_server(*mocks).host == "127.0.0.1"


@pytest.mark.parametrize("source", ["env", "kwarg"])
async def test_mcp_listens_on_its_host(source, monkeypatch, alt_loopback, mock_instrument_manager,
                                       mock_command_handler, mock_capability_manager):
    mocks = (mock_instrument_manager, mock_command_handler, mock_capability_manager)
    if source == "env":
        monkeypatch.setenv("MCP_BIND_HOST", alt_loopback)
        srv = _mcp_server(*mocks)
    else:
        monkeypatch.setenv("MCP_BIND_HOST", "127.0.0.1")  # the explicit kwarg beats it
        srv = _mcp_server(*mocks, host=alt_loopback)
    assert srv.host == alt_loopback
    await srv.start()
    try:
        bound = [sock.getsockname()[:2] for server in srv._server.servers for sock in server.sockets]
        assert bound == [(alt_loopback, srv.port)]  # the socket actually bound, and the port read back
    finally:
        await srv.stop()


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_explicit_bind_host_means_the_loopback_default_not_all_interfaces(
        blank, monkeypatch, mock_instrument_manager, mock_command_handler, mock_capability_manager):
    # "" would reach asyncio/uvicorn as "every interface" and gRPC as ":<port>"; treat it like None.
    for key in ("GRPC_BIND_HOST", "WS_BIND_HOST", "MCP_BIND_HOST"):
        monkeypatch.delenv(key, raising=False)
    mocks = (mock_instrument_manager, mock_command_handler, mock_capability_manager)
    assert GRPCServer(mock_instrument_manager, mock_command_handler, edge_id="t", port=0,
                      bind_host=blank).bind_host == "127.0.0.1"
    assert WebSocketServer(mock_instrument_manager, mock_command_handler, port=0,
                           bind_host=blank).bind_host == "127.0.0.1"
    assert _mcp_server(*mocks, host=blank).host == "127.0.0.1"


def test_explicit_bind_host_is_stripped_like_the_env_keys(monkeypatch, mock_instrument_manager,
                                                          mock_command_handler, mock_capability_manager):
    mocks = (mock_instrument_manager, mock_command_handler, mock_capability_manager)
    assert GRPCServer(mock_instrument_manager, mock_command_handler, edge_id="t", port=0,
                      bind_host=" 0.0.0.0 ").bind_host == "0.0.0.0"
    assert WebSocketServer(mock_instrument_manager, mock_command_handler, port=0,
                           bind_host=" 0.0.0.0 ").bind_host == "0.0.0.0"
    assert _mcp_server(*mocks, host=" 0.0.0.0 ").host == "0.0.0.0"
