"""tests/sim: edge in SIM_MODE against edgesim benches. Needs the `sim` extra (make test-prereqs)."""
from __future__ import annotations

from pathlib import Path

import pytest

try:
    import edgesim.edge  # noqa: F401
except ImportError as exc:  # never skip silently (edge-api.md §7)
    raise pytest.UsageError(
        "tests/sim needs the sim extra: uv pip install -e '.[dev,test,sim]' with cargo + maturin on PATH"
    ) from exc

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "third_party/edgesim/contracts/python"
BENCHES = ROOT / "third_party/edgesim/contracts/examples/benches"
PSU_BENCH = BENCHES / "psu_resistor_dmm.bench.json"
SCOPE_BENCH = BENCHES / "awg_rc_scope.bench.yaml"
SCHEMA = ROOT / "third_party/edgesim/contracts/schemas/trace-v1.schema.json"
