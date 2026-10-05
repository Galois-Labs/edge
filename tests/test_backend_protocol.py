"""edge-api.md §2: edge's InstrumentBackend copy is structurally identical to the contract."""
from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path

import pytest

pytestmark = pytest.mark.critical
CONTRACT = (Path(__file__).resolve().parents[1]
            / "third_party/edgesim/contracts/python/edge_instrument_backend.py")


def _contract_protocol():
    spec = importlib.util.spec_from_file_location("_contract_edge_instrument_backend", CONTRACT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.InstrumentBackend


def _members(proto) -> set[str]:
    return set(proto.__annotations__) | {n for n, v in vars(proto).items() if not n.startswith("_") and callable(v)}


def test_protocol_matches_the_contract():
    from galois_edge.backends.base import InstrumentBackend as edge

    contract = _contract_protocol()
    assert _members(edge) == _members(contract)
    assert edge.__annotations__ == contract.__annotations__  # {'name': 'str', 'simulated': 'bool'}
    for name in sorted(_members(contract) - set(contract.__annotations__)):
        assert str(inspect.signature(getattr(edge, name))) == str(inspect.signature(getattr(contract, name))), name
    assert getattr(edge, "_is_runtime_protocol", False)
    assert "set_timeout" not in _members(edge)


def test_edge_runtime_never_imports_the_submodule():
    src = Path(__file__).resolve().parents[1] / "src" / "galois_edge"
    offenders = [p for p in src.rglob("*.py") if "third_party" in p.read_text(encoding="utf-8")]
    assert offenders == []
