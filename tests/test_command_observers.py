from __future__ import annotations

import logging
import struct
import time
from types import SimpleNamespace

import pytest

from galois_edge.command_handler import CommandHandler
from galois_edge.profile_schema import BinaryConfig, CommandConfig
from galois_edge.tracing import CommandContext, ResolvedSCPI
from tests.conftest import MockInstrumentManager

pytestmark = pytest.mark.critical
ADDR = "GPIB0::1::INSTR"


def _handler():
    mgr = MockInstrumentManager(resources=[ADDR])
    mgr.connect(ADDR)
    mgr.set_query_response(ADDR, ":MEAS:VOLT?", "1.5")
    h = CommandHandler(mgr)
    events = []
    h.add_observer(events.append)
    return h, mgr, events


def test_query_and_write_events():
    h, _mgr, events = _handler()
    before = time.time_ns()
    h.execute_command(":MEAS:VOLT?", ADDR)
    h.execute_command("*RST", ADDR)
    q, w = events
    assert (q.kind, q.is_query, q.success, q.response, q.scpi) == ("scpi", True, True, "1.5", ":MEAS:VOLT?")
    assert (w.is_query, w.response) == (False, None)
    assert before <= q.t_wall_ns <= time.time_ns() and q.latency_ns >= 0


def test_resolved_scpi_carries_context_into_the_event():
    h, _mgr, events = _handler()
    ctx = CommandContext(ADDR, "meas", {}, CommandConfig(scpi=":MEAS:VOLT?"), True, "query")
    h.execute_command(ResolvedSCPI(":MEAS:VOLT?", ctx), ADDR)
    assert events[0].context is ctx and events[0].kind == "command"


def test_failure_and_timeout_events():
    h, mgr, events = _handler()
    mgr.query = lambda *a: (_ for _ in ()).throw(TimeoutError("slow"))
    h.execute_command(":X?", ADDR)
    assert (events[0].success, events[0].timed_out, events[0].response) == (False, True, None)


def test_observer_errors_are_logged_not_raised(caplog):
    h, _mgr, events = _handler()
    h.add_observer(lambda e: 1 / 0)
    with caplog.at_level(logging.ERROR, logger="galois_edge.command_handler"):
        result = h.execute_command(":MEAS:VOLT?", ADDR)
    assert result["success"] is True and len(events) == 1
    assert any("observer" in r.getMessage() for r in caplog.records)


def test_remove_observer():
    h, _mgr, events = _handler()
    h.remove_observer(events.append)
    h.execute_command(":MEAS:VOLT?", ADDR)
    assert events == []


def test_block_query_event_carries_raw_bytes():
    h, mgr, events = _handler()
    block = b"#14" + struct.pack("<2h", 1, -1) + b"\n"
    mgr.set_raw_response(ADDR, ":WAV:DATA?", block)
    result = h.execute_binary_block_query(":WAV:DATA?", ADDR, BinaryConfig(dtype="int16"))
    assert result["success"] is True
    assert events[0].response_bytes == block and events[0].response is None


def test_binary_values_event_packs_float64():
    h, mgr, events = _handler()
    mgr.query_binary_values = lambda *a, **k: [1.0, 2.0]
    h.execute_binary_query(":TRAC?", ADDR)
    assert events[0].response_bytes == struct.pack("<2d", 1.0, 2.0)


def test_simulated_flag_comes_from_the_owning_backend():
    h, mgr, events = _handler()
    mgr.backend_for = lambda addr: SimpleNamespace(simulated=True)
    h.execute_command("*RST", ADDR)
    assert events[0].simulated is True


def test_unpackable_binary_values_never_break_the_command():  # Review Focus 5
    h, mgr, events = _handler()
    mgr.query_binary_values = lambda *a, **k: ["not-a-number"]
    result = h.execute_binary_query(":TRAC?", ADDR)
    assert result["success"] is True and result["data"] == ["not-a-number"]
    assert events[0].success is True and events[0].response_bytes is None
