# tests/sim/test_psu_dmm_e2e.py
"""M1 criterion 1 (edge side): an agent navigates, configures, and measures PSU -> 1 kΩ -> DMM over MCP."""
from __future__ import annotations

import pytest

from tests.mcp._m1_profiles import parse  # noqa: F401  (used by the E2E tests in this module)
from tests.sim.conftest import PSU_BENCH

pytestmark = pytest.mark.critical


def test_instruments_register_with_hinted_profiles(sim_stack):
    stack = sim_stack(PSU_BENCH)
    keys = {c.profile_key for c in stack.daemon._capability_manager.all_instruments.values()}
    assert {"galois_sim-psu-2", "galois_sim-dmm"} <= keys
    for address in stack.backend.list_resources():
        assert stack.daemon._instrument_manager.backend_for(address) is stack.backend
