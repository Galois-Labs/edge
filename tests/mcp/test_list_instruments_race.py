"""F1 through MCP: list_instruments while discovery registers an instrument on the I/O thread.

The process-level E2E failed ~1 in 5 runs with "Error executing tool list_instruments: dictionary
changed size during iteration". Here the registration is forced into the middle of the tool's
loop: the first is_connected() it makes runs register_instrument on a worker thread and waits.
"""
from __future__ import annotations

import threading
from typing import Any

import pytest

from tests.mcp._m1_profiles import parse

pytestmark = pytest.mark.critical

LATE = "TCPIP0::127.0.0.1::5099::SOCKET"


@pytest.fixture
def register_during_first_is_connected(edge_context: Any, monkeypatch):
    inst_mgr, cap_mgr = edge_context.instrument_manager, edge_context.capability_manager
    real_is_connected = inst_mgr.is_connected
    fired = threading.Event()

    def is_connected(instrument_id: str) -> bool:
        if not fired.is_set():
            fired.set()
            io = threading.Thread(target=cap_mgr.register_instrument, args=(LATE, LATE, "ACME,LATE,0,1"),
                                  name="instrument-io-test")
            io.start()
            io.join(timeout=5.0)
            assert not io.is_alive(), "register_instrument blocked while list_instruments iterated"
        return real_is_connected(instrument_id)

    monkeypatch.setattr(inst_mgr, "is_connected", is_connected)
    return fired


async def test_list_instruments_survives_a_registration_mid_iteration(
    fastmcp_with_tools, register_during_first_is_connected,
):
    listed = parse(await fastmcp_with_tools.call_tool("list_instruments", {}))

    assert register_during_first_is_connected.is_set()
    assert {i["id"] for i in listed} == {"GPIB0::24::INSTR", "USB::34461A::INSTR"}   # the snapshot
    again = parse(await fastmcp_with_tools.call_tool("list_instruments", {}))
    assert LATE in {i["id"] for i in again}
