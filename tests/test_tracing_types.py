from __future__ import annotations

import json
import math
import pickle

import pytest

from galois_edge import edge_pb2
from galois_edge.profile_schema import CommandConfig
from galois_edge.tracing import CommandContext, CommandEvent, ResolvedSCPI, WriteTarget, json_safe

pytestmark = pytest.mark.critical
CTX = CommandContext(instrument_id="GPIB0::1::INSTR", path="set_voltage", params={"value": 1.5},
                     command=CommandConfig(scpi=":SOUR:VOLT {value}"), is_query=False, form="write")


def test_resolved_scpi_is_a_plain_string_with_context():
    r = ResolvedSCPI(":SOUR:VOLT 1.5", CTX)
    assert isinstance(r, str) and r == ":SOUR:VOLT 1.5" and hash(r) == hash(":SOUR:VOLT 1.5")
    assert r.context is CTX and ResolvedSCPI("x").context is None
    assert type(str(r)) is str and str(r) == ":SOUR:VOLT 1.5"
    assert json.dumps({"s": r}) == '{"s": ":SOUR:VOLT 1.5"}'
    assert type(pickle.loads(pickle.dumps(r))) is str


def test_protobuf_accepts_resolved_scpi():
    msg = edge_pb2.ExecuteCommandResponse(scpi_command=ResolvedSCPI(":X?", CTX))
    assert msg.scpi_command == ":X?"


def test_json_safe_encodes_non_finite_and_containers():
    assert json_safe({"a": (1, float("nan")), "b": float("inf"), "c": -math.inf, "d": object}) == {
        "a": [1, "nan"], "b": "inf", "c": "-inf", "d": str(object)}


def test_event_and_target_defaults():
    ev = CommandEvent(instrument_id="i", scpi="*RST", context=None, is_query=False, success=True,
                      response=None, response_bytes=None, error="", t_wall_ns=1, latency_ns=2)
    assert (ev.kind, ev.timed_out, ev.simulated) == ("scpi", False, False)
    assert WriteTarget("output.enabled").index_name is None
