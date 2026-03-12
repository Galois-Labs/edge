"""
Tests for grpc_server.py -- test each RPC handler.

Uses mock subsystems and the gRPC async test infrastructure.
"""

import asyncio
import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import grpc
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

    @pytest.mark.asyncio
    async def test_force_query_from_profile(
        self, mock_instrument_manager, mock_command_handler,
    ):
        """When a profile command has force_query=True, the command handler
        should receive force_query=True even if the SCPI doesn't end with '?'."""
        from galois_edge.capability_manager import CapabilityManager
        from galois_edge.profile_schema import (
            CommandConfig, InstrumentProfile, InstrumentMetadata,
            IdentityConfig, SettingsConfig, ReturnConfig,
        )

        # Build a profile with a force_query command
        profile = InstrumentProfile(
            instrument=InstrumentMetadata(
                manufacturer="TestCo", model="X1", instrument_class="generic",
            ),
            identity=IdentityConfig(pattern="TESTCO.*X1"),
            settings=SettingsConfig(),
            commands={
                "read_status": CommandConfig(
                    scpi="STATUS",
                    type="query",
                    force_query=True,
                    returns=ReturnConfig(type="string"),
                ),
            },
        )

        cap_mgr = CapabilityManager()
        cap_mgr.register_instrument(
            instrument_id="GPIB0::25::INSTR",
            visa_address="GPIB0::25::INSTR",
            idn_response="TESTCO X1 SN v1",
            profile=profile,
        )

        # Use a MagicMock for command_handler to capture the force_query arg
        handler_mock = MagicMock()
        handler_mock.execute_command.return_value = {
            "success": True,
            "response": "0",
            "error": "",
            "execution_time_ms": 1.0,
        }

        servicer = _make_servicer(
            mock_instrument_manager,
            handler_mock,
            mock_capability_manager=cap_mgr,
        )
        ctx = _make_context()

        request = edge_pb2.ExecuteCommandRequest(
            command_id="ecmd-fq-001",
            instrument_id="GPIB0::25::INSTR",
            command_name="read_status",
        )
        response = await servicer.ExecuteCommand(request, ctx)

        assert response.success is True
        # Verify force_query was passed as True to execute_command
        handler_mock.execute_command.assert_called_once()
        call_kwargs = handler_mock.execute_command.call_args
        # The call may be positional or keyword — check the force_query value
        if call_kwargs.kwargs:
            assert call_kwargs.kwargs.get("force_query") is True
        else:
            # positional: (scpi_cmd, instrument_id, timeout_ms, command_id, force_query)
            assert call_kwargs.args[-1] is True

    @pytest.mark.asyncio
    async def test_execute_with_response_parser(
        self, mock_instrument_manager, mock_command_handler,
    ):
        """When a profile command has a returns.parser, _apply_response_processing
        should transform the raw response."""
        from galois_edge.capability_manager import CapabilityManager
        from galois_edge.profile_schema import (
            CommandConfig, InstrumentProfile, InstrumentMetadata,
            IdentityConfig, SettingsConfig, ReturnConfig,
        )

        profile = InstrumentProfile(
            instrument=InstrumentMetadata(
                manufacturer="TestCo", model="V1", instrument_class="dmm",
            ),
            identity=IdentityConfig(pattern="TESTCO.*V1"),
            settings=SettingsConfig(),
            commands={
                "measure": CommandConfig(
                    scpi=":MEAS?",
                    type="query",
                    returns=ReturnConfig(
                        type="float",
                        unit="V",
                        parser={"type": "strip", "prefix": "VOLT "},
                    ),
                ),
            },
        )

        cap_mgr = CapabilityManager()
        cap_mgr.register_instrument(
            instrument_id="GPIB0::25::INSTR",
            visa_address="GPIB0::25::INSTR",
            idn_response="TESTCO V1 SN v1",
            profile=profile,
        )

        handler_mock = MagicMock()
        handler_mock.execute_command.return_value = {
            "success": True,
            "response": "VOLT 3.14",
            "error": "",
            "execution_time_ms": 2.0,
        }

        servicer = _make_servicer(
            mock_instrument_manager,
            handler_mock,
            mock_capability_manager=cap_mgr,
        )
        ctx = _make_context()

        request = edge_pb2.ExecuteCommandRequest(
            command_id="ecmd-parse-001",
            instrument_id="GPIB0::25::INSTR",
            command_name="measure",
        )
        response = await servicer.ExecuteCommand(request, ctx)

        assert response.success is True
        # The parser should have stripped the "VOLT " prefix
        assert response.data == "3.14"


# ---------------------------------------------------------------------------
# ExecuteCommand — vector/binary query path
# ---------------------------------------------------------------------------


class TestExecuteCommandVectorPath:
    """Tests for the vector/binary query path in ExecuteCommand."""

    @pytest.mark.asyncio
    async def test_vector_command_uses_binary_query(
        self, mock_instrument_manager,
    ):
        """When returns.type == 'vector', ExecuteCommand uses execute_binary_query."""
        from galois_edge.capability_manager import CapabilityManager
        from galois_edge.profile_schema import (
            CommandConfig, InstrumentProfile, InstrumentMetadata,
            IdentityConfig, SettingsConfig, ReturnConfig,
        )

        profile = InstrumentProfile(
            instrument=InstrumentMetadata(
                manufacturer="TestCo", model="Scope1", instrument_class="oscilloscope",
            ),
            identity=IdentityConfig(pattern="TESTCO.*SCOPE1"),
            settings=SettingsConfig(),
            commands={
                "get_trace": CommandConfig(
                    scpi=":WAV:DATA?",
                    type="query",
                    returns=ReturnConfig(
                        type="vector",
                        format="ieee_binary",
                        unit="V",
                        x_name="Time",
                        x_unit="s",
                    ),
                ),
            },
        )

        cap_mgr = CapabilityManager()
        cap_mgr.register_instrument(
            instrument_id="TCPIP::192.168.1.1::INSTR",
            visa_address="TCPIP::192.168.1.1::INSTR",
            idn_response="TESTCO SCOPE1 SN v1",
            profile=profile,
        )

        handler_mock = MagicMock()
        handler_mock.execute_binary_query.return_value = {
            "success": True,
            "data": [1.0, 2.0, 3.0],
            "error": "",
            "execution_time_ms": 5.0,
        }

        servicer = _make_servicer(
            mock_instrument_manager,
            handler_mock,
            mock_capability_manager=cap_mgr,
        )
        ctx = _make_context()

        request = edge_pb2.ExecuteCommandRequest(
            command_id="vec-001",
            instrument_id="TCPIP::192.168.1.1::INSTR",
            command_name="get_trace",
            is_query=True,
        )
        response = await servicer.ExecuteCommand(request, ctx)

        assert response.success is True
        handler_mock.execute_binary_query.assert_called_once()
        # execute_command should NOT have been called
        handler_mock.execute_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_vector_response_has_vector_data(
        self, mock_instrument_manager,
    ):
        """VectorData fields are populated correctly in the response."""
        import struct

        from galois_edge.capability_manager import CapabilityManager
        from galois_edge.profile_schema import (
            CommandConfig, InstrumentProfile, InstrumentMetadata,
            IdentityConfig, SettingsConfig, ReturnConfig,
        )

        profile = InstrumentProfile(
            instrument=InstrumentMetadata(
                manufacturer="TestCo", model="Scope2", instrument_class="oscilloscope",
            ),
            identity=IdentityConfig(pattern="TESTCO.*SCOPE2"),
            settings=SettingsConfig(),
            commands={
                "get_waveform": CommandConfig(
                    scpi=":WAV:DATA?",
                    type="query",
                    returns=ReturnConfig(
                        type="vector",
                        format="ieee_binary",
                        unit="V",
                        x_name="Time",
                        x_unit="s",
                    ),
                ),
            },
        )

        cap_mgr = CapabilityManager()
        cap_mgr.register_instrument(
            instrument_id="TCPIP::192.168.1.2::INSTR",
            visa_address="TCPIP::192.168.1.2::INSTR",
            idn_response="TESTCO SCOPE2 SN v1",
            profile=profile,
        )

        y_values = [1.1, 2.2, 3.3, 4.4, 5.5]
        handler_mock = MagicMock()
        handler_mock.execute_binary_query.return_value = {
            "success": True,
            "data": y_values,
            "error": "",
            "execution_time_ms": 3.0,
        }

        servicer = _make_servicer(
            mock_instrument_manager,
            handler_mock,
            mock_capability_manager=cap_mgr,
        )
        ctx = _make_context()

        request = edge_pb2.ExecuteCommandRequest(
            command_id="vec-002",
            instrument_id="TCPIP::192.168.1.2::INSTR",
            command_name="get_waveform",
            is_query=True,
        )
        response = await servicer.ExecuteCommand(request, ctx)

        assert response.success is True
        vd = response.vector_data
        assert vd.y_dtype == "float64"
        assert vd.y_length == 5
        assert vd.x_name == "Time"
        assert vd.x_unit == "s"
        assert vd.y_unit == "V"
        # Verify y_data can be unpacked back to the original values
        unpacked = struct.unpack(f'<{vd.y_length}d', vd.y_data)
        for orig, got in zip(y_values, unpacked):
            assert abs(orig - got) < 1e-10

    @pytest.mark.asyncio
    async def test_vector_x_axis_queries(
        self, mock_instrument_manager,
    ):
        """x_start_query and x_increment_query are executed and populate VectorData."""
        from galois_edge.capability_manager import CapabilityManager
        from galois_edge.profile_schema import (
            CommandConfig, InstrumentProfile, InstrumentMetadata,
            IdentityConfig, SettingsConfig, ReturnConfig,
        )

        profile = InstrumentProfile(
            instrument=InstrumentMetadata(
                manufacturer="TestCo", model="Scope3", instrument_class="oscilloscope",
            ),
            identity=IdentityConfig(pattern="TESTCO.*SCOPE3"),
            settings=SettingsConfig(),
            commands={
                "get_trace_xy": CommandConfig(
                    scpi=":WAV:DATA?",
                    type="query",
                    returns=ReturnConfig(
                        type="vector",
                        format="ieee_binary",
                        unit="V",
                        x_name="Time",
                        x_unit="s",
                        x_start_query=":WAV:XORIGIN?",
                        x_increment_query=":WAV:XINCREMENT?",
                    ),
                ),
            },
        )

        cap_mgr = CapabilityManager()
        cap_mgr.register_instrument(
            instrument_id="TCPIP::192.168.1.3::INSTR",
            visa_address="TCPIP::192.168.1.3::INSTR",
            idn_response="TESTCO SCOPE3 SN v1",
            profile=profile,
        )

        handler_mock = MagicMock()
        handler_mock.execute_binary_query.return_value = {
            "success": True,
            "data": [1.0, 2.0],
            "error": "",
            "execution_time_ms": 3.0,
        }
        # Mock the x-axis queries: execute_command called twice
        handler_mock.execute_command.side_effect = [
            {"success": True, "response": "-0.001", "error": "", "execution_time_ms": 1.0},
            {"success": True, "response": "0.00001", "error": "", "execution_time_ms": 1.0},
        ]

        servicer = _make_servicer(
            mock_instrument_manager,
            handler_mock,
            mock_capability_manager=cap_mgr,
        )
        ctx = _make_context()

        request = edge_pb2.ExecuteCommandRequest(
            command_id="vec-003",
            instrument_id="TCPIP::192.168.1.3::INSTR",
            command_name="get_trace_xy",
            is_query=True,
        )
        response = await servicer.ExecuteCommand(request, ctx)

        assert response.success is True
        vd = response.vector_data
        assert abs(vd.x_start - (-0.001)) < 1e-10
        assert abs(vd.x_increment - 0.00001) < 1e-10
        # execute_command called twice for x-axis queries
        assert handler_mock.execute_command.call_count == 2

    @pytest.mark.asyncio
    async def test_vector_binary_query_failure(
        self, mock_instrument_manager,
    ):
        """When binary query fails, ExecuteCommand returns error response."""
        from galois_edge.capability_manager import CapabilityManager
        from galois_edge.profile_schema import (
            CommandConfig, InstrumentProfile, InstrumentMetadata,
            IdentityConfig, SettingsConfig, ReturnConfig,
        )

        profile = InstrumentProfile(
            instrument=InstrumentMetadata(
                manufacturer="TestCo", model="Scope4", instrument_class="oscilloscope",
            ),
            identity=IdentityConfig(pattern="TESTCO.*SCOPE4"),
            settings=SettingsConfig(),
            commands={
                "get_trace_fail": CommandConfig(
                    scpi=":WAV:DATA?",
                    type="query",
                    returns=ReturnConfig(
                        type="vector",
                        format="ieee_binary",
                    ),
                ),
            },
        )

        cap_mgr = CapabilityManager()
        cap_mgr.register_instrument(
            instrument_id="TCPIP::192.168.1.4::INSTR",
            visa_address="TCPIP::192.168.1.4::INSTR",
            idn_response="TESTCO SCOPE4 SN v1",
            profile=profile,
        )

        handler_mock = MagicMock()
        handler_mock.execute_binary_query.return_value = {
            "success": False,
            "data": [],
            "error": "VISA transfer error",
            "execution_time_ms": 1.0,
        }

        servicer = _make_servicer(
            mock_instrument_manager,
            handler_mock,
            mock_capability_manager=cap_mgr,
        )
        ctx = _make_context()

        request = edge_pb2.ExecuteCommandRequest(
            command_id="vec-004",
            instrument_id="TCPIP::192.168.1.4::INSTR",
            command_name="get_trace_fail",
            is_query=True,
        )
        response = await servicer.ExecuteCommand(request, ctx)

        assert response.success is False
        assert "VISA transfer error" in response.error_message

    @pytest.mark.asyncio
    async def test_vector_float32_format(
        self, mock_instrument_manager,
    ):
        """When format contains 'float32', datatype='f' is used."""
        import struct

        from galois_edge.capability_manager import CapabilityManager
        from galois_edge.profile_schema import (
            CommandConfig, InstrumentProfile, InstrumentMetadata,
            IdentityConfig, SettingsConfig, ReturnConfig,
        )

        profile = InstrumentProfile(
            instrument=InstrumentMetadata(
                manufacturer="TestCo", model="Scope5", instrument_class="oscilloscope",
            ),
            identity=IdentityConfig(pattern="TESTCO.*SCOPE5"),
            settings=SettingsConfig(),
            commands={
                "get_trace_f32": CommandConfig(
                    scpi=":WAV:DATA?",
                    type="query",
                    returns=ReturnConfig(
                        type="vector",
                        format="ieee_binary_float32",
                        unit="V",
                    ),
                ),
            },
        )

        cap_mgr = CapabilityManager()
        cap_mgr.register_instrument(
            instrument_id="TCPIP::192.168.1.5::INSTR",
            visa_address="TCPIP::192.168.1.5::INSTR",
            idn_response="TESTCO SCOPE5 SN v1",
            profile=profile,
        )

        handler_mock = MagicMock()
        handler_mock.execute_binary_query.return_value = {
            "success": True,
            "data": [1.0, 2.0],
            "error": "",
            "execution_time_ms": 2.0,
        }

        servicer = _make_servicer(
            mock_instrument_manager,
            handler_mock,
            mock_capability_manager=cap_mgr,
        )
        ctx = _make_context()

        request = edge_pb2.ExecuteCommandRequest(
            command_id="vec-005",
            instrument_id="TCPIP::192.168.1.5::INSTR",
            command_name="get_trace_f32",
            is_query=True,
        )
        response = await servicer.ExecuteCommand(request, ctx)

        assert response.success is True
        vd = response.vector_data
        assert vd.y_dtype == "float32"
        # binary query mock was called with datatype='f'
        call_kwargs = handler_mock.execute_binary_query.call_args
        assert call_kwargs.kwargs.get("datatype") == 'f' or (
            len(call_kwargs.args) > 2 and call_kwargs.args[2] == 'f'
        )
        # Verify y_data is packed as float32 (4 bytes per value)
        unpacked = struct.unpack(f'<{vd.y_length}f', vd.y_data)
        assert len(unpacked) == 2

    @pytest.mark.asyncio
    async def test_non_vector_command_skips_binary_path(
        self, mock_instrument_manager,
    ):
        """When returns.type != 'vector', normal SCPI path is used."""
        from galois_edge.capability_manager import CapabilityManager
        from galois_edge.profile_schema import (
            CommandConfig, InstrumentProfile, InstrumentMetadata,
            IdentityConfig, SettingsConfig, ReturnConfig,
        )

        profile = InstrumentProfile(
            instrument=InstrumentMetadata(
                manufacturer="TestCo", model="DMM1", instrument_class="dmm",
            ),
            identity=IdentityConfig(pattern="TESTCO.*DMM1"),
            settings=SettingsConfig(),
            commands={
                "measure_voltage": CommandConfig(
                    scpi=":MEAS:VOLT:DC?",
                    type="query",
                    returns=ReturnConfig(type="float", unit="V"),
                ),
            },
        )

        cap_mgr = CapabilityManager()
        cap_mgr.register_instrument(
            instrument_id="GPIB0::25::INSTR",
            visa_address="GPIB0::25::INSTR",
            idn_response="TESTCO DMM1 SN v1",
            profile=profile,
        )

        handler_mock = MagicMock()
        handler_mock.execute_command.return_value = {
            "success": True,
            "response": "1.234",
            "error": "",
            "execution_time_ms": 2.0,
        }

        servicer = _make_servicer(
            mock_instrument_manager,
            handler_mock,
            mock_capability_manager=cap_mgr,
        )
        ctx = _make_context()

        request = edge_pb2.ExecuteCommandRequest(
            command_id="scalar-001",
            instrument_id="GPIB0::25::INSTR",
            command_name="measure_voltage",
            is_query=True,
        )
        response = await servicer.ExecuteCommand(request, ctx)

        assert response.success is True
        assert response.data == "1.234"
        # Binary query should NOT have been called
        handler_mock.execute_binary_query.assert_not_called()
        handler_mock.execute_command.assert_called_once()


# ---------------------------------------------------------------------------
# _apply_response_processing (unit-level)
# ---------------------------------------------------------------------------


class TestApplyResponseProcessing:
    """Direct tests for _apply_response_processing on the servicer."""

    def test_no_capability_manager_returns_raw(
        self, mock_instrument_manager, mock_command_handler,
    ):
        servicer = _make_servicer(
            mock_instrument_manager, mock_command_handler,
            mock_capability_manager=None,
        )
        assert servicer._apply_response_processing(
            "raw value", "GPIB0::25::INSTR", "some_cmd",
        ) == "raw value"

    def test_no_matching_instrument_returns_raw(
        self, mock_instrument_manager, mock_command_handler,
        mock_capability_manager,
    ):
        servicer = _make_servicer(
            mock_instrument_manager, mock_command_handler,
            mock_capability_manager=mock_capability_manager,
        )
        # No instrument registered in cap_mgr
        assert servicer._apply_response_processing(
            "raw value", "UNKNOWN::INSTR", "some_cmd",
        ) == "raw value"

    def test_with_parser_transforms_response(
        self, mock_instrument_manager, mock_command_handler,
    ):
        from galois_edge.capability_manager import CapabilityManager
        from galois_edge.profile_schema import (
            CommandConfig, InstrumentProfile, InstrumentMetadata,
            IdentityConfig, SettingsConfig, ReturnConfig,
        )

        profile = InstrumentProfile(
            instrument=InstrumentMetadata(
                manufacturer="TestCo", model="Z1", instrument_class="generic",
            ),
            identity=IdentityConfig(pattern="TESTCO.*Z1"),
            settings=SettingsConfig(),
            commands={
                "get_freq": CommandConfig(
                    scpi=":FREQ?",
                    type="query",
                    returns=ReturnConfig(
                        type="float",
                        parser={"type": "split", "delimiter": ";", "index": 1},
                    ),
                ),
            },
        )

        cap_mgr = CapabilityManager()
        cap_mgr.register_instrument(
            instrument_id="GPIB0::25::INSTR",
            visa_address="GPIB0::25::INSTR",
            idn_response="TESTCO Z1",
            profile=profile,
        )

        servicer = _make_servicer(
            mock_instrument_manager, mock_command_handler,
            mock_capability_manager=cap_mgr,
        )

        result = servicer._apply_response_processing(
            "STATUS;1000.0;Hz", "GPIB0::25::INSTR", "get_freq",
        )
        assert result == "1000.0"

    def test_command_without_returns_passes_through(
        self, mock_instrument_manager, mock_command_handler,
    ):
        from galois_edge.capability_manager import CapabilityManager
        from galois_edge.profile_schema import (
            CommandConfig, InstrumentProfile, InstrumentMetadata,
            IdentityConfig, SettingsConfig,
        )

        profile = InstrumentProfile(
            instrument=InstrumentMetadata(
                manufacturer="TestCo", model="W1", instrument_class="generic",
            ),
            identity=IdentityConfig(pattern="TESTCO.*W1"),
            settings=SettingsConfig(),
            commands={
                "reset": CommandConfig(
                    scpi="*RST",
                    type="write",
                    # no returns
                ),
            },
        )

        cap_mgr = CapabilityManager()
        cap_mgr.register_instrument(
            instrument_id="GPIB0::25::INSTR",
            visa_address="GPIB0::25::INSTR",
            idn_response="TESTCO W1",
            profile=profile,
        )

        servicer = _make_servicer(
            mock_instrument_manager, mock_command_handler,
            mock_capability_manager=cap_mgr,
        )

        result = servicer._apply_response_processing(
            "OK", "GPIB0::25::INSTR", "reset",
        )
        assert result == "OK"


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


# ---------------------------------------------------------------------------
# Sweep RPC tests
# ---------------------------------------------------------------------------


def _make_sweep_profile():
    """Build a test profile with a sweep-enabled command."""
    from galois_edge.profile_schema import (
        CommandConfig, InstrumentProfile, InstrumentMetadata,
        IdentityConfig, SettingsConfig, ReturnConfig, SweepConfig,
    )

    return InstrumentProfile(
        instrument=InstrumentMetadata(
            manufacturer="Oxford", model="IPS120", instrument_class="magnet_controller",
        ),
        identity=IdentityConfig(pattern="Oxford.*IPS120"),
        settings=SettingsConfig(timeout_ms=5000),
        commands={
            "b": CommandConfig(
                getter="R7",
                setter="J{value}",
                type="property",
                force_query=True,
                requires_sweep=True,
                sweep=SweepConfig(
                    rate_param="sweep_rate",
                    command="T{sweep_rate}\nJ{value}\nA1",
                    check_command="X",
                    check_idle_match="X0",
                    stop_command="A0",
                    poll_interval_ms=100,  # fast for tests
                ),
                returns=ReturnConfig(type="float", unit="T"),
            ),
            "status": CommandConfig(
                scpi="STATUS?",
                type="query",
                returns=ReturnConfig(type="string"),
            ),
            "set_current": CommandConfig(
                scpi="CURR {value}",
                type="write",
            ),
        },
    )


def _make_cap_mgr_with_sweep(instrument_id="GPIB0::25::INSTR"):
    """Build a CapabilityManager with a sweep-enabled instrument registered."""
    from galois_edge.capability_manager import CapabilityManager

    profile = _make_sweep_profile()
    cap_mgr = CapabilityManager()
    cap_mgr.register_instrument(
        instrument_id=instrument_id,
        visa_address=instrument_id,
        idn_response="Oxford IPS120 SN v1",
        profile=profile,
    )
    return cap_mgr


class TestSweepSafetyInterlock:
    """Test that ExecuteCommand rejects commands with requires_sweep=True."""

    @pytest.mark.asyncio
    async def test_requires_sweep_blocks_execute_command(
        self, mock_instrument_manager, mock_command_handler,
    ):
        """ExecuteCommand returns an error for requires_sweep commands."""
        cap_mgr = _make_cap_mgr_with_sweep()

        servicer = _make_servicer(
            mock_instrument_manager, mock_command_handler,
            mock_capability_manager=cap_mgr,
        )
        ctx = _make_context()

        request = edge_pb2.ExecuteCommandRequest(
            command_id="ecmd-sweep-001",
            instrument_id="GPIB0::25::INSTR",
            command_name="b",
        )
        response = await servicer.ExecuteCommand(request, ctx)

        assert response.success is False
        assert "requires sweep" in response.error_message.lower()
        ctx.set_code.assert_called_with(grpc.StatusCode.FAILED_PRECONDITION)

    @pytest.mark.asyncio
    async def test_non_sweep_command_passes_through(
        self, mock_instrument_manager,
    ):
        """Non-sweep commands execute normally."""
        cap_mgr = _make_cap_mgr_with_sweep()

        handler_mock = MagicMock()
        handler_mock.execute_command.return_value = {
            "success": True,
            "response": "RUNNING",
            "error": "",
            "execution_time_ms": 1.0,
        }

        servicer = _make_servicer(
            mock_instrument_manager, handler_mock,
            mock_capability_manager=cap_mgr,
        )
        ctx = _make_context()

        request = edge_pb2.ExecuteCommandRequest(
            command_id="ecmd-normal-001",
            instrument_id="GPIB0::25::INSTR",
            command_name="status",
            is_query=True,
        )
        response = await servicer.ExecuteCommand(request, ctx)

        assert response.success is True


class TestSweepReservationGate:
    """Test that write commands are blocked during active sweep."""

    @pytest.mark.asyncio
    async def test_write_blocked_during_sweep(
        self, mock_instrument_manager, mock_command_handler,
    ):
        """Write commands to a sweeping instrument are rejected."""
        cap_mgr = _make_cap_mgr_with_sweep()

        servicer = _make_servicer(
            mock_instrument_manager, mock_command_handler,
            mock_capability_manager=cap_mgr,
        )
        ctx = _make_context()

        # Simulate the instrument being in a sweep
        servicer._sweeping_instruments.add("GPIB0::25::INSTR")

        request = edge_pb2.ExecuteCommandRequest(
            command_id="ecmd-blocked-001",
            instrument_id="GPIB0::25::INSTR",
            command_name="set_current",
            is_query=False,
        )
        response = await servicer.ExecuteCommand(request, ctx)

        assert response.success is False
        assert "sweeping" in response.error_message.lower()
        ctx.set_code.assert_called_with(grpc.StatusCode.FAILED_PRECONDITION)

    @pytest.mark.asyncio
    async def test_query_allowed_during_sweep(
        self, mock_instrument_manager,
    ):
        """Query commands are allowed during a sweep."""
        cap_mgr = _make_cap_mgr_with_sweep()

        handler_mock = MagicMock()
        handler_mock.execute_command.return_value = {
            "success": True,
            "response": "RUNNING",
            "error": "",
            "execution_time_ms": 1.0,
        }

        servicer = _make_servicer(
            mock_instrument_manager, handler_mock,
            mock_capability_manager=cap_mgr,
        )
        ctx = _make_context()

        # Simulate the instrument being in a sweep
        servicer._sweeping_instruments.add("GPIB0::25::INSTR")

        request = edge_pb2.ExecuteCommandRequest(
            command_id="ecmd-query-001",
            instrument_id="GPIB0::25::INSTR",
            command_name="status",
            is_query=True,
        )
        response = await servicer.ExecuteCommand(request, ctx)

        assert response.success is True


class TestStartSweep:
    """Test the StartSweep RPC handler."""

    @pytest.mark.asyncio
    async def test_start_sweep_no_capability_manager(
        self, mock_instrument_manager, mock_command_handler,
    ):
        servicer = _make_servicer(
            mock_instrument_manager, mock_command_handler,
            mock_capability_manager=None,
        )
        ctx = _make_context()

        request = edge_pb2.StartSweepRequest(
            instrument_id="GPIB0::25::INSTR",
            command_name="b",
            target_value=1.0,
            sweep_rate=0.1,
        )
        response = await servicer.StartSweep(request, ctx)

        assert response.accepted is False
        assert "not available" in response.error.lower()

    @pytest.mark.asyncio
    async def test_start_sweep_unknown_instrument(
        self, mock_instrument_manager, mock_command_handler,
    ):
        cap_mgr = _make_cap_mgr_with_sweep()
        servicer = _make_servicer(
            mock_instrument_manager, mock_command_handler,
            mock_capability_manager=cap_mgr,
        )
        ctx = _make_context()

        request = edge_pb2.StartSweepRequest(
            instrument_id="UNKNOWN::INSTR",
            command_name="b",
            target_value=1.0,
            sweep_rate=0.1,
        )
        response = await servicer.StartSweep(request, ctx)

        assert response.accepted is False
        assert "not found" in response.error.lower()

    @pytest.mark.asyncio
    async def test_start_sweep_no_sweep_config(
        self, mock_instrument_manager, mock_command_handler,
    ):
        cap_mgr = _make_cap_mgr_with_sweep()
        servicer = _make_servicer(
            mock_instrument_manager, mock_command_handler,
            mock_capability_manager=cap_mgr,
        )
        ctx = _make_context()

        request = edge_pb2.StartSweepRequest(
            instrument_id="GPIB0::25::INSTR",
            command_name="status",  # no sweep config
            target_value=1.0,
            sweep_rate=0.1,
        )
        response = await servicer.StartSweep(request, ctx)

        assert response.accepted is False
        assert "no sweep configuration" in response.error.lower()

    @pytest.mark.asyncio
    async def test_start_sweep_already_sweeping(
        self, mock_instrument_manager, mock_command_handler,
    ):
        cap_mgr = _make_cap_mgr_with_sweep()
        servicer = _make_servicer(
            mock_instrument_manager, mock_command_handler,
            mock_capability_manager=cap_mgr,
        )
        ctx = _make_context()

        # Mark instrument as already sweeping
        servicer._sweeping_instruments.add("GPIB0::25::INSTR")

        request = edge_pb2.StartSweepRequest(
            instrument_id="GPIB0::25::INSTR",
            command_name="b",
            target_value=1.0,
            sweep_rate=0.1,
        )
        response = await servicer.StartSweep(request, ctx)

        assert response.accepted is False
        assert "already sweeping" in response.error.lower()

    @pytest.mark.asyncio
    async def test_start_sweep_accepted(
        self, mock_instrument_manager,
    ):
        """A valid StartSweep request is accepted and sets up state."""
        cap_mgr = _make_cap_mgr_with_sweep()

        handler_mock = MagicMock()
        # The sweep start command succeeds
        handler_mock.execute_command.return_value = {
            "success": True,
            "response": "OK",
            "error": "",
            "execution_time_ms": 1.0,
        }

        servicer = _make_servicer(
            mock_instrument_manager, handler_mock,
            mock_capability_manager=cap_mgr,
        )
        ctx = _make_context()

        request = edge_pb2.StartSweepRequest(
            instrument_id="GPIB0::25::INSTR",
            command_name="b",
            target_value=1.5,
            sweep_rate=0.1,
        )
        response = await servicer.StartSweep(request, ctx)

        assert response.accepted is True
        assert response.sweep_id != ""
        assert "GPIB0::25::INSTR" in response.sweep_id
        assert "b" in response.sweep_id

        # State should be set up
        assert response.sweep_id in servicer._sweep_states
        state = servicer._sweep_states[response.sweep_id]
        assert state["status"] == "sweeping"
        assert state["target_value"] == 1.5
        assert state["sweep_rate"] == 0.1

        # Instrument should be in sweeping set
        assert "GPIB0::25::INSTR" in servicer._sweeping_instruments

        # Clean up: cancel the poll task
        task = servicer._active_sweeps.get(response.sweep_id)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


class TestGetSweepStatus:
    """Test the GetSweepStatus RPC handler."""

    @pytest.mark.asyncio
    async def test_sweep_not_found(
        self, mock_instrument_manager, mock_command_handler,
    ):
        servicer = _make_servicer(
            mock_instrument_manager, mock_command_handler,
        )
        ctx = _make_context()

        request = edge_pb2.GetSweepStatusRequest(sweep_id="nonexistent")
        response = await servicer.GetSweepStatus(request, ctx)

        assert response.status == "not_found"
        assert "not found" in response.error.lower()

    @pytest.mark.asyncio
    async def test_sweep_status_returned(
        self, mock_instrument_manager, mock_command_handler,
    ):
        servicer = _make_servicer(
            mock_instrument_manager, mock_command_handler,
        )
        ctx = _make_context()

        # Manually populate state
        servicer._sweep_states["test-sweep-001"] = {
            "status": "sweeping",
            "current_value": 0.5,
            "target_value": 1.5,
            "sweep_rate": 0.1,
            "error": "",
        }

        request = edge_pb2.GetSweepStatusRequest(sweep_id="test-sweep-001")
        response = await servicer.GetSweepStatus(request, ctx)

        assert response.sweep_id == "test-sweep-001"
        assert response.status == "sweeping"
        assert response.current_value == 0.5
        assert response.target_value == 1.5
        assert response.sweep_rate == 0.1
        assert response.error == ""


class TestStopSweep:
    """Test the StopSweep RPC handler."""

    @pytest.mark.asyncio
    async def test_stop_nonexistent_sweep(
        self, mock_instrument_manager, mock_command_handler,
    ):
        servicer = _make_servicer(
            mock_instrument_manager, mock_command_handler,
        )
        ctx = _make_context()

        request = edge_pb2.StopSweepRequest(sweep_id="nonexistent")
        response = await servicer.StopSweep(request, ctx)

        assert response.success is False
        assert response.status == "not_found"

    @pytest.mark.asyncio
    async def test_stop_wildcard(
        self, mock_instrument_manager, mock_command_handler,
    ):
        """StopSweep with '*' sets cancel flags for all active sweeps."""
        servicer = _make_servicer(
            mock_instrument_manager, mock_command_handler,
        )
        ctx = _make_context()

        # Set up a few cancel events
        event1 = asyncio.Event()
        event2 = asyncio.Event()
        servicer._sweep_cancel_flags["sweep-1"] = event1
        servicer._sweep_cancel_flags["sweep-2"] = event2

        request = edge_pb2.StopSweepRequest(sweep_id="*")
        response = await servicer.StopSweep(request, ctx)

        assert response.success is True
        assert response.status == "stopping_all"
        assert event1.is_set()
        assert event2.is_set()

    @pytest.mark.asyncio
    async def test_stop_specific_sweep(
        self, mock_instrument_manager, mock_command_handler,
    ):
        """StopSweep sets the cancel event for a specific sweep."""
        servicer = _make_servicer(
            mock_instrument_manager, mock_command_handler,
        )
        ctx = _make_context()

        cancel_event = asyncio.Event()
        servicer._sweep_cancel_flags["sweep-abc"] = cancel_event

        request = edge_pb2.StopSweepRequest(sweep_id="sweep-abc")
        response = await servicer.StopSweep(request, ctx)

        assert response.success is True
        assert response.status == "holding"
        assert cancel_event.is_set()


class TestSweepPollLoop:
    """Test the _sweep_poll_loop completes when check_idle_match is satisfied."""

    @pytest.mark.asyncio
    async def test_poll_loop_completes_on_idle_match(
        self, mock_instrument_manager,
    ):
        """Sweep poll loop marks status as 'completed' when idle match found."""
        from galois_edge.profile_schema import SweepConfig

        cap_mgr = _make_cap_mgr_with_sweep()

        call_count = [0]
        def mock_execute(scpi_cmd, instrument_id, timeout_ms=5000, force_query=False, command_id=None):
            call_count[0] += 1
            # First poll: not done yet. Second poll: done.
            if call_count[0] <= 1:
                return {"success": True, "response": "X1", "error": "", "execution_time_ms": 1.0}
            return {"success": True, "response": "X0", "error": "", "execution_time_ms": 1.0}

        handler_mock = MagicMock()
        handler_mock.execute_command.side_effect = mock_execute

        servicer = _make_servicer(
            mock_instrument_manager, handler_mock,
            mock_capability_manager=cap_mgr,
        )

        sweep_id = "test:b:12345678"
        sweep_cfg = SweepConfig(
            command="T{sweep_rate}\nJ{value}\nA1",
            check_command="X",
            check_idle_match="X0",
            stop_command="A0",
            poll_interval_ms=50,
        )
        cancel_event = asyncio.Event()
        servicer._sweep_states[sweep_id] = {
            "status": "sweeping",
            "current_value": 0.0,
            "target_value": 1.0,
            "sweep_rate": 0.1,
            "error": "",
        }
        servicer._sweeping_instruments.add("GPIB0::25::INSTR")
        servicer._sweep_cancel_flags[sweep_id] = cancel_event

        task = asyncio.create_task(
            servicer._sweep_poll_loop(
                sweep_id, sweep_cfg, "GPIB0::25::INSTR", 5000, cancel_event,
            )
        )
        servicer._active_sweeps[sweep_id] = task

        await asyncio.wait_for(task, timeout=5.0)

        assert servicer._sweep_states[sweep_id]["status"] == "completed"
        assert "GPIB0::25::INSTR" not in servicer._sweeping_instruments

    @pytest.mark.asyncio
    async def test_poll_loop_aborts_on_cancel(
        self, mock_instrument_manager,
    ):
        """Sweep poll loop fires stop_command and sets status 'aborted' on cancel."""
        from galois_edge.profile_schema import SweepConfig

        cap_mgr = _make_cap_mgr_with_sweep()

        def mock_execute(scpi_cmd, instrument_id, timeout_ms=5000, force_query=False, command_id=None):
            # Never reports idle
            return {"success": True, "response": "X1", "error": "", "execution_time_ms": 1.0}

        handler_mock = MagicMock()
        handler_mock.execute_command.side_effect = mock_execute

        servicer = _make_servicer(
            mock_instrument_manager, handler_mock,
            mock_capability_manager=cap_mgr,
        )

        sweep_id = "test:b:cancel01"
        sweep_cfg = SweepConfig(
            command="T{sweep_rate}\nJ{value}\nA1",
            check_command="X",
            check_idle_match="X0",
            stop_command="A0",
            poll_interval_ms=50,
        )
        cancel_event = asyncio.Event()
        servicer._sweep_states[sweep_id] = {
            "status": "sweeping",
            "current_value": 0.0,
            "target_value": 1.0,
            "sweep_rate": 0.1,
            "error": "",
        }
        servicer._sweeping_instruments.add("GPIB0::25::INSTR")
        servicer._sweep_cancel_flags[sweep_id] = cancel_event

        task = asyncio.create_task(
            servicer._sweep_poll_loop(
                sweep_id, sweep_cfg, "GPIB0::25::INSTR", 5000, cancel_event,
            )
        )
        servicer._active_sweeps[sweep_id] = task

        # Let one poll happen, then cancel
        await asyncio.sleep(0.15)
        cancel_event.set()

        await asyncio.wait_for(task, timeout=5.0)

        assert servicer._sweep_states[sweep_id]["status"] == "aborted"
        assert "GPIB0::25::INSTR" not in servicer._sweeping_instruments

        # Verify stop_command was sent (it should be the last call or near-last)
        all_calls = handler_mock.execute_command.call_args_list
        stop_calls = [c for c in all_calls if "A0" in str(c)]
        assert len(stop_calls) > 0, "stop_command 'A0' should have been sent"


# ---------------------------------------------------------------------------
# ProxySDKCall — module allowlist & private method rejection
# ---------------------------------------------------------------------------


class TestProxySDKCallSecurity:
    """Security tests for the ProxySDKCall fallback dynamic import path."""

    @pytest.mark.asyncio
    async def test_proxy_sdk_call_rejects_disallowed_module(self):
        """Modules not in the allowlist (e.g. 'os') must be rejected."""
        im = MagicMock()
        handler = MagicMock()
        servicer = _make_servicer(im, handler)

        request = edge_pb2.ProxySDKCallRequest(
            call_id="sec-001",
            instrument_id="GPIB0::1::INSTR",
            module="os",
            method="system",
        )
        ctx = _make_context()
        resp = await servicer.ProxySDKCall(request, ctx)

        assert resp.success is False
        assert "not in the allowed modules list" in resp.error_message
        assert "os" in resp.error_message

    @pytest.mark.asyncio
    async def test_proxy_sdk_call_rejects_stdlib_subprocess(self):
        """Another disallowed module — subprocess — must be rejected."""
        im = MagicMock()
        handler = MagicMock()
        servicer = _make_servicer(im, handler)

        request = edge_pb2.ProxySDKCallRequest(
            call_id="sec-002",
            instrument_id="GPIB0::1::INSTR",
            module="subprocess",
            method="run",
        )
        ctx = _make_context()
        resp = await servicer.ProxySDKCall(request, ctx)

        assert resp.success is False
        assert "not in the allowed modules list" in resp.error_message

    @pytest.mark.asyncio
    async def test_proxy_sdk_call_rejects_private_method(self):
        """Methods starting with '_' must be rejected even for allowed modules."""
        im = MagicMock()
        handler = MagicMock()
        servicer = _make_servicer(im, handler)

        request = edge_pb2.ProxySDKCallRequest(
            call_id="sec-003",
            instrument_id="GPIB0::1::INSTR",
            module="galois_edge.sdk_wrappers.some_wrapper",
            method="_private_thing",
        )
        ctx = _make_context()
        resp = await servicer.ProxySDKCall(request, ctx)

        assert resp.success is False
        assert "private" in resp.error_message.lower()
        assert "_private_thing" in resp.error_message

    @pytest.mark.asyncio
    async def test_proxy_sdk_call_rejects_dunder_method(self):
        """Dunder methods (e.g. __import__) must also be rejected."""
        im = MagicMock()
        handler = MagicMock()
        servicer = _make_servicer(im, handler)

        request = edge_pb2.ProxySDKCallRequest(
            call_id="sec-004",
            instrument_id="GPIB0::1::INSTR",
            module="galois_edge.sdk_wrappers.some_wrapper",
            method="__import__",
        )
        ctx = _make_context()
        resp = await servicer.ProxySDKCall(request, ctx)

        assert resp.success is False
        assert "private" in resp.error_message.lower()

    @pytest.mark.asyncio
    async def test_proxy_sdk_call_allows_sdk_wrapper_module(self):
        """A module under galois_edge.sdk_wrappers.* should pass the
        allowlist check.  It will fail on ImportError (the module doesn't
        exist in tests), but it must NOT fail on the allowlist check."""
        im = MagicMock()
        handler = MagicMock()
        servicer = _make_servicer(im, handler)

        request = edge_pb2.ProxySDKCallRequest(
            call_id="sec-005",
            instrument_id="GPIB0::1::INSTR",
            module="galois_edge.sdk_wrappers.some_wrapper",
            method="do_something",
        )
        ctx = _make_context()
        resp = await servicer.ProxySDKCall(request, ctx)

        # Should fail with an ImportError, NOT an allowlist error
        assert resp.success is False
        assert "not in the allowed modules list" not in resp.error_message
        # The error should be about the module not being found
        assert "No module named" in resp.error_message or "import" in resp.error_message.lower()

    @pytest.mark.asyncio
    async def test_proxy_sdk_call_allows_vendor_module(self):
        """A module under galois_edge.vendor.* should also pass the
        allowlist check."""
        im = MagicMock()
        handler = MagicMock()
        servicer = _make_servicer(im, handler)

        request = edge_pb2.ProxySDKCallRequest(
            call_id="sec-006",
            instrument_id="GPIB0::1::INSTR",
            module="galois_edge.vendor.some_vendor_lib",
            method="connect",
        )
        ctx = _make_context()
        resp = await servicer.ProxySDKCall(request, ctx)

        # Should fail with an ImportError, NOT an allowlist error
        assert resp.success is False
        assert "not in the allowed modules list" not in resp.error_message
