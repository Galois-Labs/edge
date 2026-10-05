from __future__ import annotations

from unittest.mock import MagicMock

import grpc
import pytest

from galois_edge import edge_pb2
from galois_edge.command_handler import CommandHandler
from galois_edge.grpc_server import EdgeDaemonServicer
from tests.test_resolve_command_validation import ADDR, _manager

pytestmark = pytest.mark.critical
MSG = "value: 999.0 is out of range [-200.0, 200.0]"


def _servicer(mgr):
    return EdgeDaemonServicer(instrument_manager=mgr, command_handler=CommandHandler(mgr), edge_id="t",
                              capability_manager=_manager(), max_workers=2)


async def test_execute_command_maps_to_invalid_argument(mock_instrument_manager):
    ctx = MagicMock()
    req = edge_pb2.ExecuteCommandRequest(command_id="c1", instrument_id=ADDR, command_name="set_voltage",
                                         parameters={"value": "999"}, is_query=False)
    resp = await _servicer(mock_instrument_manager).ExecuteCommand(req, ctx)
    assert (resp.success, resp.error_message, resp.command_id) == (False, MSG, "c1")
    ctx.set_code.assert_called_once_with(grpc.StatusCode.INVALID_ARGUMENT)
    ctx.set_details.assert_called_once_with(MSG)
    assert mock_instrument_manager._writes == []


async def test_stream_yields_one_error_point():
    cm = _manager()
    from galois_edge.profile_schema import CommandConfig, ParameterConfig as P
    cm.get_instrument_caps(ADDR).profile.commands["meas"] = CommandConfig(
        scpi=":MEAS{channel}?", type="query", streamable=True, params={"channel": P(type="int", min=1, max=2)})
    servicer = EdgeDaemonServicer(instrument_manager=MagicMock(), command_handler=MagicMock(), edge_id="t",
                                  capability_manager=cm, max_workers=2)
    req = edge_pb2.StreamMeasurementRequest(stream_id="s1", instrument_id=ADDR, command_name="meas",
                                            interval_ms=10, parameters={"channel": "9"})
    points = [p async for p in servicer.StreamMeasurement(req, MagicMock())]
    assert [(p.status, p.error, p.seq) for p in points] == [("error", "channel: 9 is out of range [1, 2]", 1)]
