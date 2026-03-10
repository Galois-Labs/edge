"""
Tests for grpc_server.py -- test each RPC handler.

Uses mock subsystems and the gRPC async test infrastructure.
"""

import asyncio
import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "src")
)

from galois_edge.grpc_server import (
    EdgeDaemonServicer,
    GRPCServer,
    _detect_connection_type,
    _build_instrument_proto,
)
from galois_edge import edge_pb2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_context():
    """Create a mock gRPC servicer context."""
    ctx = MagicMock()
    ctx.cancelled.return_value = False
    ctx.set_code = MagicMock()
    ctx.set_details = MagicMock()
    return ctx


def _make_servicer(
    mock_instrument_manager,
    mock_command_handler,
    mock_capability_manager=None,
    mock_sdk_executor=None,
):
    """Construct an EdgeDaemonServicer with mocked subsystems."""
    return EdgeDaemonServicer(
        instrument_manager=mock_instrument_manager,
        command_handler=mock_command_handler,
        edge_id="test-edge-001",
        capability_manager=mock_capability_manager,
        sdk_executor=mock_sdk_executor,
        max_workers=2,
    )


# ---------------------------------------------------------------------------
# Connection type detection
# ---------------------------------------------------------------------------


class TestConnectionTypeDetection:

    def test_gpib(self):
        assert _detect_connection_type("GPIB0::25::INSTR") == edge_pb2.CONNECTION_TYPE_GPIB

    def test_usb(self):
        assert _detect_connection_type("USB0::0x1234::0x5678::INSTR") == edge_pb2.CONNECTION_TYPE_USB

    def test_tcpip(self):
        assert _detect_connection_type("TCPIP0::192.168.1.1::INSTR") == edge_pb2.CONNECTION_TYPE_LAN

    def test_serial(self):
        assert _detect_connection_type("ASRL/dev/ttyUSB0::INSTR") == edge_pb2.CONNECTION_TYPE_SERIAL

    def test_unknown(self):
        assert _detect_connection_type("SOMETHING::ELSE") == edge_pb2.CONNECTION_TYPE_UNSPECIFIED


# ---------------------------------------------------------------------------
# SendCommand RPC
# ---------------------------------------------------------------------------


class TestSendCommand:

    @pytest.mark.asyncio
    async def test_successful_query(
        self, mock_instrument_manager, mock_command_handler,
    ):
        mock_instrument_manager.set_query_response(
            "GPIB0::25::INSTR", "*IDN?", "KEITHLEY,2400,SN,v1",
        )
        servicer = _make_servicer(
            mock_instrument_manager, mock_command_handler,
        )
        ctx = _make_context()

        request = edge_pb2.SendCommandRequest(
            command_id="cmd-001",
            scpi_command="*IDN?",
            instrument_id="GPIB0::25::INSTR",
            timeout_ms=5000,
        )

        response = await servicer.SendCommand(request, ctx)
        assert response.status == "completed"
        assert response.command_id == "cmd-001"
        assert "KEITHLEY" in response.response

    @pytest.mark.asyncio
    async def test_successful_write(
        self, mock_instrument_manager, mock_command_handler,
    ):
        servicer = _make_servicer(
            mock_instrument_manager, mock_command_handler,
        )
        ctx = _make_context()

        request = edge_pb2.SendCommandRequest(
            command_id="cmd-002",
            scpi_command="*RST",
            instrument_id="GPIB0::25::INSTR",
        )

        response = await servicer.SendCommand(request, ctx)
        assert response.status == "completed"
        assert response.error == ""

    @pytest.mark.asyncio
    async def test_error_on_exception(
        self, mock_instrument_manager,
    ):
        # Make the handler raise
        handler = MagicMock()
        handler.execute_command.side_effect = RuntimeError("boom")

        servicer = _make_servicer(mock_instrument_manager, handler)
        ctx = _make_context()

        request = edge_pb2.SendCommandRequest(
            command_id="cmd-003",
            scpi_command="*IDN?",
            instrument_id="GPIB0::25::INSTR",
        )

        response = await servicer.SendCommand(request, ctx)
        assert response.status == "error"
        assert "boom" in response.error


# ---------------------------------------------------------------------------
# ListInstruments RPC
# ---------------------------------------------------------------------------


class TestListInstruments:

    @pytest.mark.asyncio
    async def test_lists_instruments(
        self, mock_instrument_manager, mock_command_handler,
    ):
        servicer = _make_servicer(
            mock_instrument_manager, mock_command_handler,
        )
        ctx = _make_context()

        request = edge_pb2.ListInstrumentsRequest()
        response = await servicer.ListInstruments(request, ctx)

        assert response.edge_id == "test-edge-001"
        assert len(response.instruments) >= 1
        assert response.instruments[0].address == "GPIB0::25::INSTR"

    @pytest.mark.asyncio
    async def test_empty_when_no_instruments(self, mock_command_handler):
        # Build a bare InstrumentManager mock with no resources
        mgr = MagicMock()
        mgr.list_resources.return_value = ()
        mgr.is_connected.return_value = False

        servicer = _make_servicer(mgr, mock_command_handler)
        ctx = _make_context()

        request = edge_pb2.ListInstrumentsRequest()
        response = await servicer.ListInstruments(request, ctx)

        assert len(response.instruments) == 0


# ---------------------------------------------------------------------------
# GetInstrument RPC
# ---------------------------------------------------------------------------


class TestGetInstrument:

    @pytest.mark.asyncio
    async def test_found(
        self, mock_instrument_manager, mock_command_handler,
    ):
        servicer = _make_servicer(
            mock_instrument_manager, mock_command_handler,
        )
        ctx = _make_context()

        request = edge_pb2.GetInstrumentRequest(
            instrument_id="GPIB0::25::INSTR",
        )
        response = await servicer.GetInstrument(request, ctx)
        assert response.is_connected is True

    @pytest.mark.asyncio
    async def test_not_found(
        self, mock_instrument_manager, mock_command_handler,
    ):
        servicer = _make_servicer(
            mock_instrument_manager, mock_command_handler,
        )
        ctx = _make_context()

        request = edge_pb2.GetInstrumentRequest(
            instrument_id="GPIB0::99::INSTR",
        )
        response = await servicer.GetInstrument(request, ctx)
        ctx.set_code.assert_called()


# ---------------------------------------------------------------------------
# GetCapabilities RPC
# ---------------------------------------------------------------------------


class TestGetCapabilities:

    @pytest.mark.asyncio
    async def test_no_capability_manager(
        self, mock_instrument_manager, mock_command_handler,
    ):
        servicer = _make_servicer(
            mock_instrument_manager, mock_command_handler,
            mock_capability_manager=None,
        )
        ctx = _make_context()

        request = edge_pb2.GetCapabilitiesRequest()
        response = await servicer.GetCapabilities(request, ctx)

        assert len(response.capabilities) == 0
        assert response.edge_id == "test-edge-001"


# ---------------------------------------------------------------------------
# Ping RPC
# ---------------------------------------------------------------------------


class TestPing:

    @pytest.mark.asyncio
    async def test_ping_returns_timestamp(
        self, mock_instrument_manager, mock_command_handler,
    ):
        servicer = _make_servicer(
            mock_instrument_manager, mock_command_handler,
        )
        ctx = _make_context()

        request = edge_pb2.PingRequest()
        response = await servicer.Ping(request, ctx)

        assert response.HasField("timestamp")


# ---------------------------------------------------------------------------
# GetStatus RPC
# ---------------------------------------------------------------------------


class TestGetStatus:

    @pytest.mark.asyncio
    async def test_status_online(
        self, mock_instrument_manager, mock_command_handler,
    ):
        servicer = _make_servicer(
            mock_instrument_manager, mock_command_handler,
        )
        ctx = _make_context()

        request = edge_pb2.GetStatusRequest()
        response = await servicer.GetStatus(request, ctx)

        assert response.edge_id == "test-edge-001"
        assert response.status == edge_pb2.EDGE_STATUS_CODE_ONLINE
        assert response.instrument_count >= 1
        assert response.uptime_seconds >= 0


# ---------------------------------------------------------------------------
# Heartbeat RPC
# ---------------------------------------------------------------------------


class TestHeartbeat:

    @pytest.mark.asyncio
    async def test_heartbeat_ack(
        self, mock_instrument_manager, mock_command_handler,
    ):
        servicer = _make_servicer(
            mock_instrument_manager, mock_command_handler,
        )
        ctx = _make_context()

        request = edge_pb2.HeartbeatRequest(
            edge_id="test-edge-001",
            instrument_count=1,
        )
        response = await servicer.Heartbeat(request, ctx)

        assert response.acknowledged is True
        assert response.server_timestamp_ms > 0


# ---------------------------------------------------------------------------
# RegisterEdge RPC
# ---------------------------------------------------------------------------


class TestRegisterEdge:

    @pytest.mark.asyncio
    async def test_register_ack(
        self, mock_instrument_manager, mock_command_handler,
    ):
        servicer = _make_servicer(
            mock_instrument_manager, mock_command_handler,
        )
        ctx = _make_context()

        request = edge_pb2.RegisterEdgeRequest(
            edge_id="edge-abc",
            hostname="lab-pc-1",
        )
        response = await servicer.RegisterEdge(request, ctx)

        assert response.success is True
        assert response.assigned_edge_id == "edge-abc"


# ---------------------------------------------------------------------------
# StopStream RPC
# ---------------------------------------------------------------------------


class TestStopStream:

    @pytest.mark.asyncio
    async def test_stop_nonexistent_stream(
        self, mock_instrument_manager, mock_command_handler,
    ):
        servicer = _make_servicer(
            mock_instrument_manager, mock_command_handler,
        )
        ctx = _make_context()

        request = edge_pb2.StopStreamRequest(stream_id="nonexistent")
        response = await servicer.StopStream(request, ctx)

        assert response.success is False


# ---------------------------------------------------------------------------
# ExecuteCommand RPC (profile-based)
# ---------------------------------------------------------------------------


class TestExecuteCommand:

    @pytest.mark.asyncio
    async def test_no_capability_manager(
        self, mock_instrument_manager, mock_command_handler,
    ):
        servicer = _make_servicer(
            mock_instrument_manager, mock_command_handler,
            mock_capability_manager=None,
        )
        ctx = _make_context()

        request = edge_pb2.ExecuteCommandRequest(
            command_id="ecmd-001",
            instrument_id="GPIB0::25::INSTR",
            command_name="measure_voltage",
        )
        response = await servicer.ExecuteCommand(request, ctx)

        assert response.success is False
        assert "not available" in response.error_message.lower()


# ---------------------------------------------------------------------------
# GRPCServer lifecycle
# ---------------------------------------------------------------------------


class TestGRPCServerLifecycle:

    def test_server_init(
        self, mock_instrument_manager, mock_command_handler,
    ):
        server = GRPCServer(
            instrument_manager=mock_instrument_manager,
            command_handler=mock_command_handler,
            edge_id="test-001",
            port=50099,
        )
        assert server.port == 50099

    def test_servicer_accessible(
        self, mock_instrument_manager, mock_command_handler,
    ):
        server = GRPCServer(
            instrument_manager=mock_instrument_manager,
            command_handler=mock_command_handler,
            edge_id="test-001",
        )
        assert server.servicer is not None


# ---------------------------------------------------------------------------
# Instrument proto builder
# ---------------------------------------------------------------------------


class TestBuildInstrumentProto:

    def test_basic_build(self):
        inst = _build_instrument_proto(
            instrument_id="GPIB0::25::INSTR",
            visa_address="GPIB0::25::INSTR",
            idn_response="KEITHLEY,MODEL 2400,SN123,v1.0",
            is_connected=True,
        )
        assert inst.id == "GPIB0::25::INSTR"
        assert inst.manufacturer == "KEITHLEY"
        assert inst.model == "MODEL 2400"
        assert inst.serial_number == "SN123"
        assert inst.firmware == "v1.0"
        assert inst.is_connected is True
        assert inst.connection_type == edge_pb2.CONNECTION_TYPE_GPIB

    def test_empty_idn(self):
        inst = _build_instrument_proto(
            instrument_id="USB0::INSTR",
            visa_address="USB0::INSTR",
            idn_response="",
            is_connected=False,
        )
        assert inst.manufacturer == ""
        assert inst.model == ""
