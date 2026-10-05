# tests/sim/test_sim_mode_startup.py
"""edge-api.md §2 SIM_MODE wiring: lazy import, from_env, ImportError/ValueError ⇒ log + exit 2, SIM before DEMO."""
from __future__ import annotations

import json
import logging
import os
import sys

import pytest

from galois_edge.backends.base import InstrumentBackend
from galois_edge.backends.demo import DemoBackend
from galois_edge.config import Config
from galois_edge.main import EdgeDaemon
from tests.sim.conftest import CONTRACTS, PSU_BENCH

pytestmark = pytest.mark.critical


@pytest.fixture
def daemon_for(tmp_path):
    made = []

    def make(**cfg):
        daemon = EdgeDaemon(Config(gpib_enabled=False, usb_monitor_enabled=False, lan_instruments="",
                                   mcp_enabled=False, dynamic_profile_dir=str(tmp_path / "dyn"), **cfg))
        made.append(daemon)
        return daemon

    yield make
    for daemon in made:
        if daemon._instrument_manager is not None:
            daemon._instrument_manager.close_backends()
        daemon._io_executor.shutdown(wait=False)


def test_sim_backend_comes_before_demo(daemon_for, monkeypatch):
    monkeypatch.setenv("SIM_BENCH", str(PSU_BENCH))
    monkeypatch.setenv("SIM_CLOCK", "stepped")
    daemon = daemon_for(sim_mode=True, demo=True)
    mgr = daemon._instrument_manager = daemon._build_instrument_manager()
    sim, demo = mgr.extra_backends
    assert type(sim).__name__ == "EdgeSimBackend" and isinstance(demo, DemoBackend)
    assert isinstance(sim, InstrumentBackend) and sim.simulated is True and sim.name == "edgesim"
    assert all(os.path.isabs(d) for d in sim.profile_dirs())


def test_backend_conforms_to_the_edgesim_contract(daemon_for, monkeypatch):
    monkeypatch.syspath_prepend(str(CONTRACTS))
    from edgesim_edge_backend import EdgeSimBackend as Contract
    monkeypatch.setenv("SIM_BENCH", str(PSU_BENCH))
    daemon = daemon_for(sim_mode=True)
    mgr = daemon._instrument_manager = daemon._build_instrument_manager()
    assert isinstance(mgr.extra_backends[0], Contract)


def test_missing_bench_exits_2(daemon_for, monkeypatch, caplog):
    monkeypatch.delenv("SIM_BENCH", raising=False)
    with pytest.raises(SystemExit) as ei:
        daemon_for(sim_mode=True)._build_instrument_manager()
    assert ei.value.code == 2 and "SIM_MODE" in caplog.text


def test_missing_edgesim_exits_2(daemon_for, monkeypatch, caplog):
    monkeypatch.setenv("SIM_BENCH", str(PSU_BENCH))
    monkeypatch.setitem(sys.modules, "edgesim.edge", None)   # import raises ImportError
    with pytest.raises(SystemExit) as ei:
        daemon_for(sim_mode=True)._build_instrument_manager()
    assert ei.value.code == 2 and "SIM_MODE" in caplog.text


def test_bench_errors_log_every_diagnostic_and_exit_2(daemon_for, monkeypatch, caplog, tmp_path):
    bench = tmp_path / "broken.bench.json"
    nodes = [{"id": f"inst-{n}", "kind": "instrument", "label": n, "position": {"x": 0, "y": 0},
              "ext": {"sim": {"profile": f"no_such_profile_{n}"}}} for n in ("a", "b")]
    bench.write_text(json.dumps({"version": 1, "nodes": nodes, "edges": []}))
    monkeypatch.setenv("SIM_BENCH", str(bench))
    monkeypatch.setenv("SIM_CLOCK", "stepped")
    with caplog.at_level(logging.ERROR, logger="galois_edge.main"), pytest.raises(SystemExit) as ei:
        daemon_for(sim_mode=True)._build_instrument_manager()
    assert ei.value.code == 2
    lines = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    for n, pointer in (("a", "/nodes/0/ext/sim/profile"), ("b", "/nodes/1/ext/sim/profile")):
        assert any(f"no_such_profile_{n}" in line and pointer in line and line.startswith("SIM_MODE")
                   for line in lines), lines


def test_config_problems_are_logged_before_exiting(daemon_for, monkeypatch, caplog):
    monkeypatch.setenv("SIM_BENCH", str(PSU_BENCH))
    monkeypatch.setenv("SIM_SEED", "not-a-seed")
    with pytest.raises(SystemExit) as ei:
        daemon_for(sim_mode=True)._build_instrument_manager()
    assert ei.value.code == 2
    assert "SIM_MODE config: SIM_SEED" in caplog.text and "SIM_MODE: SIM_SEED" in caplog.text


def test_sim_mode_off_adds_nothing(daemon_for):
    daemon = daemon_for(sim_mode=False)
    assert daemon._apply_sim_mode([]) == []
