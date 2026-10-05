"""edge-api.md §1 config keys, §9 DYNAMIC_PROFILE_DIR (lane fixes-config)."""
from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

from galois_edge import config as cfgmod
from galois_edge.config import Config, _KNOWN_GALOIS_VARS, load_config

pytestmark = pytest.mark.critical

NEW_KEYS = [
    "DYNAMIC_PROFILE_DIR", "VISA_BACKEND", "SIM_MODE", "SIM_BENCH", "SIM_SEED", "SIM_CLOCK",
    "SIM_REMOTE_SOCKET", "SIM_MARK_INSTRUMENTS", "SIM_CONTROL_TOOLS", "GRPC_BIND_HOST",
    "WS_BIND_HOST", "MCP_BIND_HOST", "MCP_DYNAMIC_TOOLS_MAX", "TRACE_DIR",
]


@pytest.fixture
def clean_env(monkeypatch):
    for key in NEW_KEYS:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_defaults(clean_env):
    c = Config()
    assert (c.visa_backend, c.sim_mode, c.sim_bench, c.sim_seed, c.sim_clock) == ("", False, "", "", "")
    assert (c.sim_remote_socket, c.sim_mark_instruments, c.sim_control_tools) == ("", False, False)
    assert (c.grpc_bind_host, c.ws_bind_host, c.mcp_bind_host) == ("127.0.0.1",) * 3
    assert c.mcp_dynamic_tools_max == 200
    assert c.trace_dir == ""


@pytest.mark.parametrize(
    "key,value,attr,expected",
    [
        ("VISA_BACKEND", "@sim", "visa_backend", "@sim"),
        ("SIM_MODE", "true", "sim_mode", True),
        ("SIM_MODE", "1", "sim_mode", True),
        ("SIM_MODE", "", "sim_mode", False),
        ("SIM_BENCH", "/b.json", "sim_bench", "/b.json"),
        ("SIM_SEED", "42", "sim_seed", "42"),
        ("SIM_CLOCK", "stepped", "sim_clock", "stepped"),
        ("SIM_REMOTE_SOCKET", "/tmp/w.sock", "sim_remote_socket", "/tmp/w.sock"),
        ("SIM_MARK_INSTRUMENTS", "true", "sim_mark_instruments", True),
        ("SIM_CONTROL_TOOLS", "yes", "sim_control_tools", True),
        ("GRPC_BIND_HOST", "0.0.0.0", "grpc_bind_host", "0.0.0.0"),
        ("WS_BIND_HOST", "0.0.0.0", "ws_bind_host", "0.0.0.0"),
        ("MCP_BIND_HOST", "::1", "mcp_bind_host", "::1"),
        ("GRPC_BIND_HOST", "   ", "grpc_bind_host", "127.0.0.1"),
        ("MCP_DYNAMIC_TOOLS_MAX", "50", "mcp_dynamic_tools_max", 50),
        ("MCP_DYNAMIC_TOOLS_MAX", "0", "mcp_dynamic_tools_max", 0),
        ("MCP_DYNAMIC_TOOLS_MAX", "-3", "mcp_dynamic_tools_max", 200),
        ("MCP_DYNAMIC_TOOLS_MAX", "lots", "mcp_dynamic_tools_max", 200),
        ("TRACE_DIR", "/var/trace", "trace_dir", "/var/trace"),
    ],
)
def test_env_overrides(clean_env, key, value, attr, expected):
    clean_env.setenv(key, value)
    assert getattr(Config(), attr) == expected


def test_every_new_key_is_known():
    assert set(NEW_KEYS) <= _KNOWN_GALOIS_VARS


def test_every_env_key_read_by_config_py_is_known():
    """CI-23: Python-side drift check (the Go-side check cannot see Python-only keys)."""
    src = Path(cfgmod.__file__).read_text(encoding="utf-8")
    read = set(re.findall(r'_(?:str|int|bool|float|host)_env\(\s*"([A-Z][A-Z0-9_]+)"', src))
    read |= set(re.findall(r'os\.environ\.get\(\s*"([A-Z][A-Z0-9_]+)"', src))
    allowed_unknown = {"SCAN_INTERVAL_S", "PROGRAMDATA"}  # deprecated alias; Windows system var
    assert read - allowed_unknown <= _KNOWN_GALOIS_VARS, sorted(read - allowed_unknown - _KNOWN_GALOIS_VARS)


@pytest.mark.parametrize("key", NEW_KEYS)
def test_no_unknown_var_warning(clean_env, caplog, key):
    clean_env.setenv(key, "x")
    with caplog.at_level(logging.WARNING, logger="galois_edge.config"):
        load_config()
    assert not [r for r in caplog.records if f"'{key}'" in r.getMessage()]


def test_sim_config_errors_ok(clean_env, tmp_path):
    bench = tmp_path / "b.json"
    bench.write_text("{}")
    clean_env.setenv("SIM_MODE", "true")
    clean_env.setenv("SIM_BENCH", str(bench))
    clean_env.setenv("SIM_SEED", "7")
    clean_env.setenv("SIM_CLOCK", "scaled")
    assert Config().sim_config_errors() == []


@pytest.mark.parametrize(
    "env,needle",
    [
        ({"SIM_MODE": "true"}, "SIM_MODE=true requires SIM_BENCH or SIM_REMOTE_SOCKET"),
        ({"SIM_MODE": "true", "SIM_BENCH": "/nonexistent/b.json"}, "SIM_BENCH: no such file"),
        ({"SIM_SEED": "-1"}, "SIM_SEED must be an integer in [0, 2**64)"),
        ({"SIM_SEED": "abc"}, "SIM_SEED must be an integer in [0, 2**64)"),
        ({"SIM_SEED": str(2**64)}, "SIM_SEED must be an integer in [0, 2**64)"),
        ({"SIM_CLOCK": "fast"}, "SIM_CLOCK must be one of stepped|scaled|wall"),
    ],
)
def test_sim_config_errors_report(clean_env, env, needle):
    for k, v in env.items():
        clean_env.setenv(k, v)
    assert any(needle in e for e in Config().sim_config_errors())


def test_remote_socket_satisfies_sim_mode_without_bench(clean_env):
    clean_env.setenv("SIM_MODE", "true")
    clean_env.setenv("SIM_REMOTE_SOCKET", "/tmp/world.sock")
    assert Config().sim_config_errors() == []
