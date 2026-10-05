"""Test-tier invariants (contracts/edge-api.md §7; spec §10 parallel-safety rules)."""
from __future__ import annotations

import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 (edge supports >=3.10); pytest itself requires tomli there
    import tomli as tomllib

pytestmark = pytest.mark.critical
TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent


def _env_keys_read_by_config_py() -> set:
    """Every env key config.py reads, found independently of the fixture's list."""
    import galois_edge.config as cfgmod

    src = Path(cfgmod.__file__).read_text(encoding="utf-8")
    read = set(re.findall(r'_(?:str|int|bool|float)_env\(\s*"([A-Z][A-Z0-9_]+)"', src))
    read |= set(re.findall(r'os\.environ\.get\(\s*"([A-Z][A-Z0-9_]+)"', src))
    assert {"GRPC_PORT", "SCAN_INTERVAL_S", "DYNAMIC_PROFILE_DIR"} <= read  # the scan still works
    return read - {"PROGRAMDATA"}  # Windows system variable, not Galois config


def test_known_galois_vars_are_cleared_for_every_test(request):
    """Holds in a clean environment too: the fixture is autouse, and it clears every key config.py reads."""
    from galois_edge.config import _KNOWN_GALOIS_VARS
    from tests.conftest import HERMETIC_GALOIS_ENV_KEYS

    assert "_hermetic_galois_env" in request.fixturenames  # autouse: active without being requested
    keys = _KNOWN_GALOIS_VARS | {"SCAN_INTERVAL_S"} | _env_keys_read_by_config_py()
    assert sorted(keys - HERMETIC_GALOIS_ENV_KEYS) == []
    leaked = sorted(k for k in HERMETIC_GALOIS_ENV_KEYS if k in os.environ)
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


def _item(nodeid, **marks):
    def closest(name):
        return SimpleNamespace(args=(), kwargs=marks[name]) if name in marks else None
    return SimpleNamespace(nodeid=nodeid, get_closest_marker=closest)


def test_marker_policy():
    from tests.conftest import marker_policy_violations

    ok = [_item("a", critical={}), _item("b", serial={"reason": "fixed port in vendor sim"}), _item("c")]
    assert marker_policy_violations(ok) == []
    bad = marker_policy_violations([
        _item("x", critical={}, serial={"reason": "r"}), _item("y", serial={}), _item("z", critical={}, slow={}),
    ])
    assert len(bad) == 3 and all(any(n in v for v in bad) for n in "xyz")


def test_marker_policy_serial_is_never_slow_or_hardware():
    """make test's serial pass is `pytest -m serial tests/` (edge-api §7) with no slow/hardware
    exclusion, and spec §10 never runs slow/hardware with the default tier."""
    from tests.conftest import marker_policy_violations

    bad = marker_policy_violations([
        _item("s1", serial={"reason": "r"}, slow={}), _item("s2", serial={"reason": "r"}, hardware={}),
    ])
    assert len(bad) == 2 and all(any(n in v for v in bad) for n in ("s1", "s2"))
    assert marker_policy_violations([_item("ok1", slow={}, hardware={}), _item("ok2", serial={"reason": "r"})]) == []


def test_markers_are_registered(pytestconfig):
    names = {m.split(":")[0].split("(")[0].strip() for m in pytestconfig.getini("markers")}
    assert {"critical", "slow", "hardware", "serial"} <= names
    assert "--strict-markers" in pytestconfig.getini("addopts")


def test_test_extra():
    extra = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["optional-dependencies"]["test"]
    names = {re.split(r"[<>=!\[ ;]", d, maxsplit=1)[0].lower() for d in extra}
    assert {"pytest", "pytest-asyncio", "pytest-xdist", "pytest-randomly", "jsonschema", "python-can"} <= names


def test_make_targets_match_edge_api_section_7():
    mk = (ROOT / "Makefile").read_text()
    assert '-m "critical and not serial" -n auto -p no:randomly tests/' in mk
    assert '-m "not serial and not slow and not hardware" -n auto tests/' in mk
    assert re.search(r"-m serial tests/ \|\| \[ \$\$\? -eq 5 \]", mk)
    assert "test: test-go test-python" in mk
    assert "[ -d tests/sim ]" in mk and "cargo" in mk and "maturin" in mk


def _mcp_pin_ok(dependencies) -> bool:
    """CI-28 as a rule, not a spelling: exactly one mcp requirement; it admits no 2.x (pre-releases
    included) and nothing below the 1.27 floor, and some 1.x release from 1.27 up still resolves."""
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    specs = [r.specifier for r in map(Requirement, dependencies) if canonicalize_name(r.name) == "mcp"]
    if len(specs) != 1:
        return False
    spec = specs[0]
    if any(spec.contains(v, prereleases=True) for v in ("2.0", "2.0.0a1", "2.3.0", "1.26.99")):
        return False
    return any(spec.contains(f"1.{minor}") for minor in range(27, 100))


@pytest.mark.parametrize("dep, ok", [
    ("mcp>=1.27,<2", True), ("mcp >= 1.30, < 2", True), ("mcp<2,>=1.27.0", True),
    ("mcp>=1.27", False), ("mcp>=1.27,<3", False), ("mcp>=1.27,<=2.0", False), ("mcp>=1.20,<2", False),
])
def test_mcp_pin_rule_checks_bounds_not_spelling(dep, ok):
    assert _mcp_pin_ok(["pyyaml>=6.0", dep]) is ok


def test_mcp_dependency_is_pinned_below_2():
    """CI-28: a fresh resolve otherwise picks mcp 2.x, where FastMCP is renamed (3 collection errors)."""
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    assert _mcp_pin_ok(deps), [d for d in deps if d.lower().startswith("mcp")]
