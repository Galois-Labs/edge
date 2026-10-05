"""Test-tier invariants (contracts/edge-api.md §7; spec §10 parallel-safety rules)."""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.critical
TESTS = Path(__file__).resolve().parent


def test_known_galois_vars_are_cleared_for_every_test():
    from galois_edge.config import _KNOWN_GALOIS_VARS
    leaked = sorted(k for k in _KNOWN_GALOIS_VARS | {"SCAN_INTERVAL_S"} if k in os.environ)
    assert leaked == []


def test_home_is_a_per_test_temp_dir(tmp_path_factory):
    home = Path(os.environ["HOME"]).resolve()
    assert home.is_relative_to(tmp_path_factory.getbasetemp().resolve())


def test_no_config_reloads_in_tests():
    needle = "importlib." + "reload("          # split so this file does not match itself
    offenders = [p.name for p in TESTS.rglob("*.py") if needle in p.read_text(encoding="utf-8")]
    assert offenders == []


def _pytest_imported_modules():
    """Modules pytest itself imports. Standalone scripts (tests/mcp/smoke_hotplug.py) pre-load
    the mcp SDK before touching sys.path, so they cannot hit the P5 shadowing and are out of scope."""
    return [p for p in TESTS.rglob("*.py") if p.name == "conftest.py" or p.name.startswith("test_")]


def test_no_bare_conftest_imports():
    pattern = re.compile(r"^\s*(from conftest import|import conftest)", re.M)
    offenders = [
        str(p.relative_to(TESTS)) for p in _pytest_imported_modules() if pattern.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_opcua_test_servers_use_port_zero():
    # Server endpoints the harnesses bind (/galois-test/, /galois-driver-test/). Never-bound
    # client-side literals such as opc.tcp://127.0.0.1:9/notreal/ are not listeners.
    for name in ("test_opcua_driver.py", "test_opcua_transport.py"):
        ports = re.findall(r"opc\.tcp://127\.0\.0\.1:(\d+)/galois-", (TESTS / name).read_text(encoding="utf-8"))
        assert ports and set(ports) == {"0"}, (name, ports)
