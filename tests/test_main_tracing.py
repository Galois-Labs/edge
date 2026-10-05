"""edge-api.md §2, §5: TRACE_DIR wires a TraceWriter to the CommandHandler for the daemon's run."""
from __future__ import annotations

import json

import pytest

from galois_edge.command_handler import CommandHandler
from tests.conftest import MockInstrumentManager
from tests.test_main_backends import make_daemon  # noqa: F401  (fixture)

pytestmark = pytest.mark.critical


def test_trace_dir_wires_a_writer(make_daemon, tmp_path):
    daemon = make_daemon(trace_dir=str(tmp_path / "trace"))
    mgr = MockInstrumentManager(resources=["GPIB0::1::INSTR"])
    daemon._command_handler = CommandHandler(mgr)
    daemon._start_tracing()
    daemon._command_handler.execute_command("*RST", "GPIB0::1::INSTR")
    daemon._stop_tracing()
    kinds = [json.loads(l)["kind"] for l in daemon._trace_writer.path.read_text().splitlines()]
    assert kinds == ["run_start", "transition", "run_end"]


def test_no_trace_dir_no_writer(make_daemon):
    daemon = make_daemon()
    daemon._command_handler = CommandHandler(MockInstrumentManager())
    daemon._start_tracing()
    assert daemon._trace_writer is None


def test_unusable_trace_dir_disables_tracing_without_crashing(make_daemon, tmp_path, caplog):  # Review Focus 5
    blocker = tmp_path / "file"
    blocker.write_text("x")
    daemon = make_daemon(trace_dir=str(blocker / "sub"))
    daemon._command_handler = CommandHandler(MockInstrumentManager())
    daemon._start_tracing()
    assert daemon._trace_writer is None and "tracing disabled" in caplog.text
