"""The galois-profiles surface edge relies on matches contracts/python/*.py (names, params, defaults)."""
from __future__ import annotations

import importlib.util
import inspect
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.critical
CONTRACTS = Path(__file__).resolve().parents[1] / "third_party/edgesim/contracts/python"


def _contract(name):
    spec = importlib.util.spec_from_file_location(f"_contract_{name}", CONTRACTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    # @dataclass resolves string annotations through sys.modules[cls.__module__] while the module runs.
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(spec.name, None)
    return mod


def _shape(fn):
    return [(p.name, p.kind, p.default) for p in inspect.signature(fn).parameters.values()]


def test_loader_api_matches_the_model_contract():
    from galois_edge.profile_schema import _gp_api

    contract = _contract("galois_profiles_model")
    for name in ("load_yaml", "profile_from_mapping", "load_profile", "load_profiles", "sendable_template"):
        assert _shape(_gp_api(name)) == _shape(getattr(contract, name)), name
    assert issubclass(_gp_api("ProfileError"), Exception)
    assert isinstance(_gp_api("MODEL_VERSION"), int)


def test_nav_api_matches_the_nav_contract():
    import galois_profiles.nav as nav

    contract = _contract("galois_profiles_nav")
    for name in ("list_groups", "search", "describe", "related", "find_capability", "state_schema"):
        assert _shape(getattr(nav, name)) == _shape(getattr(contract, name)), name


def test_yaml12_floats():
    from galois_edge.profile_schema import _gp_api

    data = _gp_api("load_yaml")("a: 1e7\nb: 1.0e7\nc: -2.5E-3\nd: .5\ne: .inf\n")
    assert data == {"a": 1e7, "b": 1e7, "c": -2.5e-3, "d": 0.5, "e": float("inf")}
