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


def _unset_sim_keys(monkeypatch):
    monkeypatch.setattr(sys, "path", list(sys.path))  # main() may prepend src/ and the repo root
    for k in ("DEMO_MODE", "GRPC_BIND_HOST", "WS_BIND_HOST", "MCP_BIND_HOST"):
        monkeypatch.setenv(k, "placeholder")  # registers "absent" as the value to restore…
        monkeypatch.delenv(k)                 # …so apply_defaults() cannot leak into later tests


def test_main_runs_the_stock_daemon_entry_point(monkeypatch):
    from contrib.simulation import run_sim

    calls = []
    monkeypatch.setattr("galois_edge.main.main", lambda: calls.append("main"))
    _unset_sim_keys(monkeypatch)
    run_sim.main(hold_stdin=False)
    assert calls == ["main"] and os.environ["DEMO_MODE"] == "true"


def test_repo_dotenv_values_beat_the_sim_defaults(monkeypatch):
    """galois_edge.config loads the repo .env (override=False) when it is first imported.

    run_sim must import the daemon before applying its defaults; otherwise its
    0.0.0.0 defaults silently replace a .env line such as WS_BIND_HOST=127.0.0.1.
    """
    import builtins

    from contrib.simulation import run_sim

    seen = {}
    monkeypatch.setattr("galois_edge.main.main", lambda: seen.update(os.environ))
    _unset_sim_keys(monkeypatch)
    real_import = builtins.__import__

    def import_then_load_dotenv(name, *args, **kwargs):
        module = real_import(name, *args, **kwargs)
        if name == "galois_edge.main":
            # What the first import of galois_edge.config does for a repo .env
            # holding WS_BIND_HOST=127.0.0.1 (load_dotenv never overrides).
            os.environ.setdefault("WS_BIND_HOST", "127.0.0.1")
        return module

    monkeypatch.setattr(builtins, "__import__", import_then_load_dotenv)
    run_sim.main(hold_stdin=False)
    assert seen["WS_BIND_HOST"] == "127.0.0.1"  # the .env value survives
    assert (seen["DEMO_MODE"], seen["GRPC_BIND_HOST"], seen["MCP_BIND_HOST"]) == ("true", "0.0.0.0", "0.0.0.0")


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
