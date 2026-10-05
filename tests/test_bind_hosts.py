"""E10 bind hosts (edge-api.md §1); port 0 is read back so tests stay parallel-safe."""
from __future__ import annotations

import aiohttp
import grpc
import pytest

from galois_edge import edge_pb2, edge_pb2_grpc
from galois_edge.grpc_server import GRPCServer
from galois_edge.ws_server import WebSocketServer

pytestmark = pytest.mark.critical


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


async def test_grpc_port_zero_reads_back_and_serves(mock_instrument_manager, mock_command_handler):
    srv = GRPCServer(mock_instrument_manager, mock_command_handler, edge_id="t", port=0, bind_host="127.0.0.1")
    assert await srv.start() is True
    try:
        assert srv.port > 0
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
