"""E10 (edge-api.md §9): run_sim.py is a thin DEMO_MODE wrapper over the bind-host keys."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.critical
ROOT = Path(__file__).resolve().parents[1]


def test_apply_defaults_sets_demo_and_all_bind_hosts_without_overriding():
    from contrib.simulation import run_sim

    env = {"WS_BIND_HOST": "127.0.0.1"}
    run_sim.apply_defaults(env)
    assert env == {
        "DEMO_MODE": "true",
        "GRPC_BIND_HOST": "0.0.0.0",
        "WS_BIND_HOST": "127.0.0.1",  # caller's explicit value wins
        "MCP_BIND_HOST": "0.0.0.0",
    }


def test_main_runs_the_stock_daemon_entry_point(monkeypatch):
    from contrib.simulation import run_sim

    calls = []
    monkeypatch.setattr("galois_edge.main.main", lambda: calls.append("main"))
    monkeypatch.setattr(sys, "path", list(sys.path))  # main() may prepend src/ and the repo root
    for k in ("DEMO_MODE", "GRPC_BIND_HOST", "WS_BIND_HOST", "MCP_BIND_HOST"):
        monkeypatch.setenv(k, "placeholder")  # registers "absent" as the value to restore…
        monkeypatch.delenv(k)                 # …so apply_defaults() cannot leak into later tests
    run_sim.main(hold_stdin=False)
    assert calls == ["main"] and os.environ["DEMO_MODE"] == "true"


def test_no_monkey_patching_left():
    text = (ROOT / "contrib/simulation/run_sim.py").read_text()
    for banned in ("patched_start", "grpc_aio.server(", "add_insecure_port", "SimulatedInstrumentManager"):
        assert banned not in text


def test_hold_stdin_open_prevents_eof():
    code = (
        "import os, select, sys; sys.path.insert(0, %r);"
        "from contrib.simulation import run_sim; run_sim._hold_stdin_open();"
        "r, _, _ = select.select([0], [], [], 0.2); print('eof' if r else 'open')"
    ) % str(ROOT)
    out = subprocess.run([sys.executable, "-c", code], stdin=subprocess.DEVNULL,
                         capture_output=True, text=True, timeout=10)
    assert out.stdout.strip() == "open", out.stderr
