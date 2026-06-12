"""
Tests for grpc_server.py -- test each RPC handler.

Uses mock subsystems and the gRPC async test infrastructure.
"""

import asyncio
import os
import struct
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
        mock_capability_manager,
    ):
        mock_capability_manager.register_instrument(
            instrument_id="GPIB0::25::INSTR",
            visa_address="GPIB0::25::INSTR",
            idn_response="KEITHLEY INSTRUMENTS INC.,MODEL 2400,1234567,A01",
        )
        servicer = _make_servicer(
            mock_instrument_manager, mock_command_handler,
            mock_capability_manager=mock_capability_manager,
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


# ---------------------------------------------------------------------------
# ConnectInstrument RPC (F0.5)
# ---------------------------------------------------------------------------


class TestConnectInstrument:
    """Smoke tests for the new generic ConnectInstrument RPC and the
    legacy ConnectModbusInstrument shim."""

    @pytest.mark.asyncio
    async def test_connect_instrument_requires_instrument_id(
        self, mock_instrument_manager, mock_command_handler,
    ):
        servicer = _make_servicer(
            mock_instrument_manager, mock_command_handler,
        )
        ctx = _make_context()

        request = edge_pb2.ConnectInstrumentRequest(
            transport_uri="tcp://1.2.3.4:502",
            protocol="modbus",
        )
        resp = await servicer.ConnectInstrument(request, ctx)
        assert resp.success is False
        assert "instrument_id" in resp.error_message

    @pytest.mark.asyncio
    async def test_connect_instrument_no_registry(
        self, mock_instrument_manager, mock_command_handler,
    ):
        servicer = _make_servicer(
            mock_instrument_manager, mock_command_handler,
        )
        ctx = _make_context()

        request = edge_pb2.ConnectInstrumentRequest(
            instrument_id="foo",
            transport_uri="tcp://1.2.3.4:502",
            profile_name="prof",
            protocol="modbus",
        )
        resp = await servicer.ConnectInstrument(request, ctx)
        assert resp.success is False
        assert "registry" in resp.error_message.lower()

    @pytest.mark.asyncio
    async def test_connect_modbus_instrument_routes_to_new_path(
        self, mock_instrument_manager, mock_command_handler,
    ):
        """The legacy ConnectModbusInstrument RPC must produce the same
        outcome as ConnectInstrument for Modbus traffic — i.e. it routes
        through _connect_instrument_impl internally."""
        servicer = _make_servicer(
            mock_instrument_manager, mock_command_handler,
        )
        ctx = _make_context()

        request = edge_pb2.ConnectModbusInstrumentRequest(
            instrument_id="foo",
            transport_uri="tcp://1.2.3.4:502",
            profile_name="prof",
            protocol="modbus",
            slave_id=7,
        )
        resp = await servicer.ConnectModbusInstrument(request, ctx)
        # No registry attached → both code paths should report the same
        # "registry not available" sentinel.
        assert resp.success is False
        assert "registry" in resp.error_message.lower()


# ---------------------------------------------------------------------------
# IEEE 488.2 block path over gRPC (doc §2, §3.0–§3.5)
# ---------------------------------------------------------------------------


SCOPE_ADDR = "TCPIP::192.168.1.50::INSTR"

# 10-field DSOX-shape preambles with NONZERO x/y reference points, so a
# producer that skips the §2.4 composition (x_start = xorigin -
# xreference*xincrement, y_offset = yorigin - yreference*yincrement)
# emits visibly wrong values:
#   format,type,points,count,xincrement,xorigin,xreference,
#   yincrement,yorigin,yreference
CH1_PREAMBLE = "+1,+0,+4,+1,+2.0E-06,-1.0E-03,+100,+4.0E-03,+1.25,+128"
CH2_PREAMBLE = "+1,+0,+4,+1,+2.0E-06,-1.0E-03,+100,+2.0E-03,+0.5,+64"

CH1_X_START = -1.0e-03 - 100 * 2.0e-06       # -0.0012
CH1_Y_SCALE = 4.0e-03
CH1_Y_OFFSET = 1.25 - 128 * 4.0e-03          # 0.738
CH2_Y_SCALE = 2.0e-03
CH2_Y_OFFSET = 0.5 - 64 * 2.0e-03            # 0.372

CH1_SAMPLES = [0, 1000, -1000, 256]
CH2_SAMPLES = [10, -20, 30, -40]


def _ieee_block(samples):
    """Pack int16 samples into a definite-length IEEE 488.2 block."""
    payload = struct.pack(f"<{len(samples)}h", *samples)
    header = f"#{len(str(len(payload)))}{len(payload)}".encode()
    return header + payload + b"\n"


class _BlockInstrumentManager:
    """Instrument-manager stub for IEEE-block tests.

    Text queries serve the preamble of the currently selected source
    (per-channel preambles, doc §3.5); raw reads serve the block of the
    currently selected source. Every query()/query_raw() call is
    recorded so tests can assert binary never touches the text path.
    A source's block may be a list, consumed one read at a time, for
    malformed-mid-stream scripting.
    """

    def __init__(self, preambles, blocks):
        self._preambles = dict(preambles)
        self._blocks = {k: v for k, v in blocks.items()}
        self._source = next(iter(self._preambles))
        self.query_calls = []
        self.raw_calls = []
        self.writes = []

    def is_connected(self, instrument_id):
        return True

    def connect(self, instrument_id, timeout=5000):
        return instrument_id

    def write(self, instrument_id, command):
        self.writes.append(command)
        if command.startswith(":WAVeform:SOURce"):
            self._source = command.split()[-1]

    def query(self, instrument_id, command):
        self.query_calls.append(command)
        if command == ":WAVeform:PREamble?":
            return self._preambles[self._source]
        return ""

    def query_raw(self, instrument_id, command):
        self.raw_calls.append(command)
        block = self._blocks[self._source]
        if isinstance(block, list):
            return block.pop(0)
        return block


def _make_block_profile(with_source_command=True):
    """DSOX-shaped profile: waveform_source + waveform_preamble +
    waveform_data (returns: type binary / format ieee_block)."""
    from galois_edge.profile_schema import (
        BinaryConfig, CommandConfig, IdentityConfig, InstrumentMetadata,
        InstrumentProfile, ParameterConfig, PreambleMap, ReturnConfig,
        SettingsConfig,
    )

    binary = BinaryConfig(
        dtype="int16",
        byte_order="little",
        preamble_command="waveform_preamble",
        preamble_map=PreambleMap(
            x_increment=4, x_start=5, x_reference=6,
            y_scale=7, y_offset=8, y_reference=9,
        ),
        source_command="waveform_source" if with_source_command else None,
    )
    profile = InstrumentProfile(
        instrument=InstrumentMetadata(
            manufacturer="Keysight", model="DSOX-LIKE",
            instrument_class="oscilloscope",
        ),
        identity=IdentityConfig(pattern="KEYSIGHT.*DSOX"),
        settings=SettingsConfig(),
        commands={
            "waveform_source": CommandConfig(
                getter=":WAVeform:SOURce?",
                setter=":WAVeform:SOURce {source}",
                type="property",
                params={"source": ParameterConfig(type="enum")},
            ),
            "waveform_preamble": CommandConfig(
                scpi=":WAVeform:PREamble?",
                type="query",
            ),
            "waveform_data": CommandConfig(
                scpi=":WAVeform:DATA?",
                type="query",
                streamable=True,
                returns=ReturnConfig(
                    type="binary",
                    format="ieee_block",
                    unit="V",
                    x_unit="s",
                    x_name="Time",
                    binary=binary,
                ),
            ),
        },
    )
    return profile


def _make_block_servicer(manager, with_source_command=True):
    """Servicer wired with a REAL CommandHandler + CapabilityManager so
    the test exercises the full gRPC → handler → query_raw pipeline."""
    from galois_edge.capability_manager import CapabilityManager
    from galois_edge.command_handler import CommandHandler

    cap_mgr = CapabilityManager()
    cap_mgr.register_instrument(
        instrument_id=SCOPE_ADDR,
        visa_address=SCOPE_ADDR,
        idn_response="KEYSIGHT,DSOX-LIKE,SN,v1",
        profile=_make_block_profile(with_source_command),
    )
    return _make_servicer(
        manager,
        CommandHandler(manager),
        mock_capability_manager=cap_mgr,
    )


class _CancelAfter:
    """context.cancelled() stub: False for `ticks` polls, then True."""

    def __init__(self, ticks):
        self._left = ticks

    def __call__(self):
        self._left -= 1
        return self._left < 0


def _make_stream_context(ticks):
    ctx = MagicMock()
    ctx.cancelled = _CancelAfter(ticks)
    return ctx


def _block_request(command_id="blk-001", parameters=None):
    return edge_pb2.ExecuteCommandRequest(
        command_id=command_id,
        instrument_id=SCOPE_ADDR,
        command_name="waveform_data",
        is_query=True,
        parameters=parameters or {},
    )


def _block_stream_request(parameters=None, interval_ms=10):
    return edge_pb2.StreamMeasurementRequest(
        stream_id="blk-stream-1",
        instrument_id=SCOPE_ADDR,
        command_name="waveform_data",
        interval_ms=interval_ms,
        parameters=parameters or {},
    )


def _assert_ch1_vector(vd, samples=CH1_SAMPLES):
    """Assert the §2.4/§3.0 population for a CH1-preamble vector."""
    assert vd.y_dtype == "int16"
    assert vd.y_length == len(samples)
    assert struct.unpack(f"<{vd.y_length}h", vd.y_data) == tuple(samples)
    assert vd.y_scale != 0.0                       # §3.0 — never 0
    assert abs(vd.y_scale - CH1_Y_SCALE) < 1e-12
    assert abs(vd.y_offset - CH1_Y_OFFSET) < 1e-12
    assert abs(vd.x_start - CH1_X_START) < 1e-12   # reference-composed
    assert abs(vd.x_increment - 2.0e-06) < 1e-18
    assert vd.x_unit == "s"
    assert vd.y_unit == "V"
    assert vd.x_name == "Time"


class TestExecuteCommandIEEEBlockPath:
    """One-shot ExecuteCommand on returns.type==binary (doc §2.1/§2.2)."""

    @pytest.mark.asyncio
    async def test_one_shot_returns_composed_vector(self):
        mgr = _BlockInstrumentManager(
            {"CHANnel1": CH1_PREAMBLE},
            {"CHANnel1": _ieee_block(CH1_SAMPLES)},
        )
        servicer = _make_block_servicer(mgr)

        response = await servicer.ExecuteCommand(
            _block_request(), _make_context(),
        )

        assert response.success is True, response.error_message
        _assert_ch1_vector(response.vector_data)

    @pytest.mark.asyncio
    async def test_binary_never_touches_text_query(self):
        """The data SCPI must reach query_raw() only — the text query()
        path corrupts binary and stops at 0x0A payload bytes (§2.1)."""
        mgr = _BlockInstrumentManager(
            {"CHANnel1": CH1_PREAMBLE},
            {"CHANnel1": _ieee_block(CH1_SAMPLES)},
        )
        servicer = _make_block_servicer(mgr)

        response = await servicer.ExecuteCommand(
            _block_request(), _make_context(),
        )

        assert response.success is True
        assert ":WAVeform:DATA?" not in mgr.query_calls
        assert mgr.raw_calls == [":WAVeform:DATA?"]
        # The preamble is a text CSV — it stays on the text path.
        assert ":WAVeform:PREamble?" in mgr.query_calls

    @pytest.mark.asyncio
    async def test_binary_never_routed_to_execute_command(
        self, mock_instrument_manager,
    ):
        """Handler-level guard: the servicer must dispatch ieee_block
        commands to execute_binary_block_query, never execute_command."""
        from galois_edge.capability_manager import CapabilityManager

        cap_mgr = CapabilityManager()
        cap_mgr.register_instrument(
            instrument_id=SCOPE_ADDR,
            visa_address=SCOPE_ADDR,
            idn_response="KEYSIGHT,DSOX-LIKE,SN,v1",
            profile=_make_block_profile(),
        )
        handler_mock = MagicMock()
        handler_mock.execute_binary_block_query.return_value = {
            "success": True,
            "response": "<binary block: 4 int16 samples>",
            "error": "",
            "execution_time_ms": 1.0,
            "block": {
                "y_data": struct.pack("<4h", *CH1_SAMPLES),
                "y_dtype": "int16",
                "y_length": 4,
                "x_start": CH1_X_START,
                "x_increment": 2.0e-06,
                "y_scale": CH1_Y_SCALE,
                "y_offset": CH1_Y_OFFSET,
            },
        }
        servicer = _make_servicer(
            mock_instrument_manager, handler_mock,
            mock_capability_manager=cap_mgr,
        )

        response = await servicer.ExecuteCommand(
            _block_request(), _make_context(),
        )

        assert response.success is True
        handler_mock.execute_command.assert_not_called()
        handler_mock.execute_binary_query.assert_not_called()
        handler_mock.execute_binary_block_query.assert_called_once()
        kwargs = handler_mock.execute_binary_block_query.call_args.kwargs
        assert kwargs["scpi_cmd"] == ":WAVeform:DATA?"
        assert kwargs["binary_config"].dtype == "int16"
        assert kwargs["preamble_scpi"] == ":WAVeform:PREamble?"

    @pytest.mark.asyncio
    async def test_malformed_block_returns_error_response(self):
        """Truncated payload → success:false + message, no crash, no
        partial vector (§2.2 rule 4)."""
        truncated = b"#18" + struct.pack("<2h", 1, 2)  # declares 8, sends 4
        mgr = _BlockInstrumentManager(
            {"CHANnel1": CH1_PREAMBLE},
            {"CHANnel1": truncated},
        )
        servicer = _make_block_servicer(mgr)

        response = await servicer.ExecuteCommand(
            _block_request(), _make_context(),
        )

        assert response.success is False
        assert "Malformed binary block" in response.error_message
        assert response.vector_data.y_length == 0
        assert len(response.vector_data.y_data) == 0

    @pytest.mark.asyncio
    async def test_bad_header_returns_error_response(self):
        mgr = _BlockInstrumentManager(
            {"CHANnel1": CH1_PREAMBLE},
            {"CHANnel1": b"not a block"},
        )
        servicer = _make_block_servicer(mgr)

        response = await servicer.ExecuteCommand(
            _block_request(), _make_context(),
        )

        assert response.success is False
        assert "Malformed binary block" in response.error_message

    @pytest.mark.asyncio
    async def test_one_shot_multichannel_rejected(self):
        """vectors[] frames exist only on MeasurementDataPoint — a
        multi-channel one-shot fails loudly instead of dropping data."""
        mgr = _BlockInstrumentManager(
            {"CHANnel1": CH1_PREAMBLE, "CHANnel2": CH2_PREAMBLE},
            {
                "CHANnel1": _ieee_block(CH1_SAMPLES),
                "CHANnel2": _ieee_block(CH2_SAMPLES),
            },
        )
        servicer = _make_block_servicer(mgr)

        response = await servicer.ExecuteCommand(
            _block_request(
                parameters={"channels": "CHANnel1,CHANnel2"},
            ),
            _make_context(),
        )

        assert response.success is False
        assert "stream-only" in response.error_message
        assert mgr.raw_calls == []  # nothing was read


class TestStreamMeasurementIEEEBlockPath:
    """StreamMeasurement on returns.type==binary (doc §2.2, §3.5, §3.6)."""

    @pytest.mark.asyncio
    async def test_stream_points_carry_vector_data(self):
        mgr = _BlockInstrumentManager(
            {"CHANnel1": CH1_PREAMBLE},
            {"CHANnel1": _ieee_block(CH1_SAMPLES)},
        )
        servicer = _make_block_servicer(mgr)

        points = [
            p async for p in servicer.StreamMeasurement(
                _block_stream_request(), _make_stream_context(2),
            )
        ]

        assert [p.status for p in points] == ["ok", "ok", "stopped"]
        assert [p.seq for p in points] == [1, 2, 3]
        for p in points[:-1]:
            _assert_ch1_vector(p.vector_data)
            # Single-channel: vectors[] stays empty (back-compat §3.5).
            assert len(p.vectors) == 0
        # Binary never touched the text path mid-stream either.
        assert ":WAVeform:DATA?" not in mgr.query_calls
        assert mgr.raw_calls == [":WAVeform:DATA?"] * 2

    @pytest.mark.asyncio
    async def test_malformed_block_mid_stream_is_sequenced_error(self):
        """good → malformed → good: the malformed read yields a
        sequenced status:"error" point and the stream continues."""
        mgr = _BlockInstrumentManager(
            {"CHANnel1": CH1_PREAMBLE},
            {
                "CHANnel1": [
                    _ieee_block(CH1_SAMPLES),
                    b"#18" + b"\x01\x02",       # declared 8, received 2
                    _ieee_block(CH1_SAMPLES),
                ],
            },
        )
        servicer = _make_block_servicer(mgr)

        points = [
            p async for p in servicer.StreamMeasurement(
                _block_stream_request(), _make_stream_context(3),
            )
        ]

        assert [p.status for p in points] == ["ok", "error", "ok", "stopped"]
        # Errors are data: seq is contiguous straight through them (§3.6).
        assert [p.seq for p in points] == [1, 2, 3, 4]
        assert "Malformed binary block" in points[1].error
        assert points[1].vector_data.y_length == 0
        _assert_ch1_vector(points[2].vector_data)

    @pytest.mark.asyncio
    async def test_multichannel_frame_vectors_and_backcompat(self):
        """2-channel command → ONE point per tick with vectors[2]:
        distinct channel labels, distinct per-channel scaling from
        distinct preambles, and vector_data == channel 1 (§3.5)."""
        mgr = _BlockInstrumentManager(
            {"CHANnel1": CH1_PREAMBLE, "CHANnel2": CH2_PREAMBLE},
            {
                "CHANnel1": _ieee_block(CH1_SAMPLES),
                "CHANnel2": _ieee_block(CH2_SAMPLES),
            },
        )
        servicer = _make_block_servicer(mgr)

        points = [
            p async for p in servicer.StreamMeasurement(
                _block_stream_request(
                    parameters={"channels": "CHANnel1,CHANnel2"},
                ),
                _make_stream_context(1),
            )
        ]

        assert [p.status for p in points] == ["ok", "stopped"]
        point = points[0]
        assert point.seq == 1
        assert len(point.vectors) == 2

        ch1, ch2 = point.vectors
        assert ch1.channel == "CHANnel1"
        assert ch2.channel == "CHANnel2"
        # Distinct per-channel scaling from distinct preambles — never
        # one shared preamble (§3.5).
        assert abs(ch1.y_scale - CH1_Y_SCALE) < 1e-12
        assert abs(ch1.y_offset - CH1_Y_OFFSET) < 1e-12
        assert abs(ch2.y_scale - CH2_Y_SCALE) < 1e-12
        assert abs(ch2.y_offset - CH2_Y_OFFSET) < 1e-12
        assert struct.unpack("<4h", ch2.y_data) == tuple(CH2_SAMPLES)

        # Back-compat producer rule: field 8 carries the FIRST channel.
        assert point.vector_data == ch1

        # The daemon actually selected each source before each read.
        assert ":WAVeform:SOURce CHANnel1" in mgr.writes
        assert ":WAVeform:SOURce CHANnel2" in mgr.writes
        # And the preamble was re-read once per channel.
        assert mgr.query_calls.count(":WAVeform:PREamble?") == 2

    @pytest.mark.asyncio
    async def test_multichannel_without_source_command_errors(self):
        """channels with no binary.source_command → a sequenced error
        (per-channel reads are impossible without a source selector)."""
        mgr = _BlockInstrumentManager(
            {"CHANnel1": CH1_PREAMBLE},
            {"CHANnel1": _ieee_block(CH1_SAMPLES)},
        )
        servicer = _make_block_servicer(mgr, with_source_command=False)

        points = [
            p async for p in servicer.StreamMeasurement(
                _block_stream_request(
                    parameters={"channels": "CHANnel1,CHANnel2"},
                ),
                _make_stream_context(2),
            )
        ]

        assert len(points) == 1
        assert points[0].status == "error"
        assert points[0].seq == 1
        assert "source_command" in points[0].error


# ---------------------------------------------------------------------------
# Waveform-assembly path — §2.4 reference-point composition (live paths)
# ---------------------------------------------------------------------------


class _WaveformAssemblyHandler:
    """CommandHandler stub for the legacy waveform_assembly pipeline.

    Tracks the selected source so the preamble query returns the
    per-channel preamble — distinct vertical scaling per channel.
    """

    def __init__(self, preambles, data_samples):
        self._preambles = dict(preambles)
        self._data = dict(data_samples)
        self._source = next(iter(self._preambles))
        self.commands = []

    def execute_command(self, scpi_cmd, instrument_id, timeout_ms=5000,
                        command_id=None, force_query=False):
        self.commands.append(scpi_cmd)
        if scpi_cmd.startswith(":WAVeform:SOURce"):
            self._source = scpi_cmd.split()[-1]
        if scpi_cmd == ":WAVeform:PREamble?":
            return {
                "success": True,
                "response": self._preambles[self._source],
                "error": "",
                "execution_time_ms": 1.0,
            }
        return {
            "success": True, "response": "OK", "error": "",
            "execution_time_ms": 1.0,
        }

    def execute_binary_query(self, scpi_cmd, instrument_id, datatype='B',
                             is_big_endian=False, timeout_ms=5000):
        return {
            "success": True,
            "data": list(self._data[self._source]),
            "error": "",
            "execution_time_ms": 1.0,
        }


def _make_wf_assembly_servicer(handler, streamable=True):
    from galois_edge.capability_manager import CapabilityManager
    from galois_edge.profile_schema import (
        CommandConfig, IdentityConfig, InstrumentMetadata,
        InstrumentProfile, ReturnConfig, SettingsConfig,
    )
    from galois_edge.waveform_assembly import WaveformAssemblyConfig

    profile = InstrumentProfile(
        instrument=InstrumentMetadata(
            manufacturer="TestCo", model="WfScope",
            instrument_class="oscilloscope",
        ),
        identity=IdentityConfig(pattern="TESTCO.*WFSCOPE"),
        settings=SettingsConfig(),
        commands={
            "get_waveform": CommandConfig(
                scpi=":WAVeform:DATA?",
                type="query",
                streamable=streamable,
                returns=ReturnConfig(type="float", unit="V"),
                waveform_assembly=WaveformAssemblyConfig(data_format="byte"),
            ),
        },
    )
    cap_mgr = CapabilityManager()
    cap_mgr.register_instrument(
        instrument_id=SCOPE_ADDR,
        visa_address=SCOPE_ADDR,
        idn_response="TESTCO WFSCOPE SN v1",
        profile=profile,
    )
    return _make_servicer(
        MagicMock(), handler, mock_capability_manager=cap_mgr,
    )


class TestWaveformAssemblyReferenceComposition:

    @pytest.mark.asyncio
    async def test_one_shot_x_start_includes_reference_term(self):
        """x_start = xorigin - xreference*xincrement (§2.4): with
        xreference=100 in the preamble, emitting bare xorigin would be
        caught here."""
        raw_counts = [0, 128, 255]
        handler = _WaveformAssemblyHandler(
            {"CHANnel1": CH1_PREAMBLE}, {"CHANnel1": raw_counts},
        )
        servicer = _make_wf_assembly_servicer(handler)

        request = edge_pb2.ExecuteCommandRequest(
            command_id="wf-001",
            instrument_id=SCOPE_ADDR,
            command_name="get_waveform",
            is_query=True,
        )
        response = await servicer.ExecuteCommand(request, _make_context())

        assert response.success is True, response.error_message
        vd = response.vector_data
        assert abs(vd.x_start - CH1_X_START) < 1e-12
        assert abs(vd.x_increment - 2.0e-06) < 1e-18
        # Pre-scaled float64 samples with the y side composed too:
        # volts = (raw - yref)*yinc + yorig = raw*y_scale + y_offset.
        assert vd.y_scale == 1.0   # explicit, never the proto3 zero
        assert vd.y_offset == 0.0
        volts = struct.unpack(f"<{vd.y_length}d", vd.y_data)
        for raw, got in zip(raw_counts, volts):
            expected = raw * CH1_Y_SCALE + CH1_Y_OFFSET
            assert abs(got - expected) < 1e-12

    @pytest.mark.asyncio
    async def test_stream_x_start_includes_reference_term(self):
        handler = _WaveformAssemblyHandler(
            {"CHANnel1": CH1_PREAMBLE}, {"CHANnel1": [0, 1, 2]},
        )
        servicer = _make_wf_assembly_servicer(handler)

        request = edge_pb2.StreamMeasurementRequest(
            stream_id="wf-stream-1",
            instrument_id=SCOPE_ADDR,
            command_name="get_waveform",
            interval_ms=10,
        )
        points = [
            p async for p in servicer.StreamMeasurement(
                request, _make_stream_context(1),
            )
        ]

        assert [p.status for p in points] == ["ok", "stopped"]
        vd = points[0].vector_data
        assert abs(vd.x_start - CH1_X_START) < 1e-12
        assert vd.y_scale == 1.0
        # Single-channel waveform assembly keeps vectors[] empty.
        assert len(points[0].vectors) == 0

    @pytest.mark.asyncio
    async def test_multichannel_waveform_assembly_frame(self):
        """The waveform_assembly stream path also produces §3.5 frames:
        one point, vectors[2] with per-channel preambles, field 8 =
        channel 1."""
        handler = _WaveformAssemblyHandler(
            {"CHANnel1": CH1_PREAMBLE, "CHANnel2": CH2_PREAMBLE},
            {"CHANnel1": [0, 128], "CHANnel2": [10, 20]},
        )
        servicer = _make_wf_assembly_servicer(handler)

        request = edge_pb2.StreamMeasurementRequest(
            stream_id="wf-stream-2",
            instrument_id=SCOPE_ADDR,
            command_name="get_waveform",
            interval_ms=10,
            parameters={"channels": "CHANnel1,CHANnel2"},
        )
        points = [
            p async for p in servicer.StreamMeasurement(
                request, _make_stream_context(1),
            )
        ]

        assert [p.status for p in points] == ["ok", "stopped"]
        point = points[0]
        assert point.seq == 1
        assert len(point.vectors) == 2
        ch1, ch2 = point.vectors
        assert ch1.channel == "CHANnel1"
        assert ch2.channel == "CHANnel2"
        # Distinct per-channel preambles → distinct physical values.
        ch1_volts = struct.unpack(f"<{ch1.y_length}d", ch1.y_data)
        ch2_volts = struct.unpack(f"<{ch2.y_length}d", ch2.y_data)
        assert abs(ch1_volts[0] - (0 * CH1_Y_SCALE + CH1_Y_OFFSET)) < 1e-12
        assert abs(ch2_volts[0] - (10 * CH2_Y_SCALE + CH2_Y_OFFSET)) < 1e-12
        # Back-compat: field 8 carries the first channel.
        assert point.vector_data == ch1
        # Both sources were actually selected.
        assert ":WAVeform:SOURce CHANnel1" in handler.commands
        assert ":WAVeform:SOURce CHANnel2" in handler.commands
