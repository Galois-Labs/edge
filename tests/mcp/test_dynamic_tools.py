"""Tests for the Phase 3 DynamicToolRegistry.

Covers §4.7 acceptance gates 3 (schema validation rejects out-of-range
*before* dispatch) and 6 (tools/list latency under 100 ms with 200
dynamic tools registered) plus §4.8 unit coverage.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest


def _build_minimal_smu_profile(profile_key: str = "keithley_2400"):
    from galois_edge.profile_schema import (
        CommandConfig,
        IdentityConfig,
        InstrumentMetadata,
        InstrumentProfile,
        ParameterConfig,
        ReturnConfig,
        SequenceConfig,
        SequenceStepConfig,
        SettingsConfig,
    )

    # profile_key is a derived property: manufacturer_model (lowered).
    # Pick metadata that yields the requested key.
    if profile_key == "keithley_2400":
        manufacturer, model = "Keithley", "2400"
    elif "_" in profile_key:
        manufacturer, _, model = profile_key.partition("_")
    else:
        manufacturer, model = profile_key, "x"

    return InstrumentProfile(
        instrument=InstrumentMetadata(
            manufacturer=manufacturer, model=model, instrument_class="smu",
        ),
        identity=IdentityConfig(patterns=[".*"]),
        settings=SettingsConfig(timeout_ms=5000),
        commands={
            "source_voltage": CommandConfig(
                scpi=":SOUR:VOLT {value}",
                type="write",
                description="Set source voltage",
                params={
                    "value": ParameterConfig(
                        type="float", unit="V", min=-200.0, max=200.0,
                    ),
                },
            ),
            "measure": CommandConfig(
                scpi=":MEAS?",
                type="query",
                description="Measure",
                returns=ReturnConfig(type="float"),
            ),
            "self_test": CommandConfig(
                scpi="*TST?",
                type="query",
                description="Self-test",
                is_dangerous=True,
                returns=ReturnConfig(type="string"),
            ),
            "set_mode": CommandConfig(
                scpi=":SOUR:FUNC {mode}",
                type="write",
                params={
                    "mode": ParameterConfig(
                        type="enum",
                        options=["VOLT", "CURR"],
                    ),
                },
            ),
        },
        sequences={
            "iv_sweep": SequenceConfig(
                description="IV sweep",
                steps=[
                    SequenceStepConfig(
                        scpi=":MEAS?",
                        capture="current",
                    ),
                ],
                returns="current",
            ),
        },
    )


def _make_registry(edge_context):
    from mcp.server.fastmcp import FastMCP
    from galois_edge.mcp.dynamic_tools import DynamicToolRegistry

    mcp = FastMCP(name="phase3-test")
    registry = DynamicToolRegistry(mcp, edge_context, emit_list_changed=False)
    return mcp, registry


def test_register_emits_per_command_tool(edge_context: Any) -> None:
    """Registering an instrument creates one tool per profile command."""
    mcp, registry = _make_registry(edge_context)
    # The conftest fixture already pre-registered Keithley + Keysight,
    # so the registry has snapshotted them at construction.
    snapshot = registry.registered_tools()
    assert "GPIB0::24::INSTR" in snapshot
    names = set(snapshot["GPIB0::24::INSTR"])
    # Verify keithley_2400__set_voltage is present
    expected_prefixes = {
        "keithley_2400__identify",
        "keithley_2400__set_voltage",
        "keithley_2400__measure_voltage",
        "keithley_2400__trigger_self_test",
    }
    for prefix in expected_prefixes:
        assert any(n == prefix for n in names), f"missing tool {prefix}"


@pytest.mark.asyncio
async def test_unregister_removes_tools(edge_context: Any) -> None:
    """Unregistering an instrument drops all of its tools."""
    mcp, registry = _make_registry(edge_context)
    initial_tools = await mcp.list_tools()
    initial_names = {t.name for t in initial_tools}
    keithley_names = {n for n in initial_names if n.startswith("keithley_2400__")}
    assert keithley_names

    edge_context.capability_manager.unregister_instrument("GPIB0::24::INSTR")

    after_tools = await mcp.list_tools()
    after_names = {t.name for t in after_tools}
    leftover = keithley_names & after_names
    assert not leftover, f"tools survived unregister: {leftover}"


def test_multi_instance_disambiguation(synthetic_command_handler, synthetic_instrument_manager) -> None:
    """Two instruments with the same profile_key get short_id-suffixed tools."""
    from galois_edge.capability_manager import CapabilityManager
    from galois_edge.mcp.context import EdgeContext

    cap_mgr = CapabilityManager()
    profile = _build_minimal_smu_profile("smu_dup")
    cap_mgr.register_instrument(
        instrument_id="GPIB0::24::INSTR",
        visa_address="GPIB0::24::INSTR",
        idn_response="A",
        profile=profile,
    )
    cap_mgr.register_instrument(
        instrument_id="GPIB0::25::INSTR",
        visa_address="GPIB0::25::INSTR",
        idn_response="B",
        profile=profile,
    )
    ctx = EdgeContext(
        capability_manager=cap_mgr,
        command_handler=synthetic_command_handler,
        instrument_manager=synthetic_instrument_manager,
    )
    _, registry = _make_registry(ctx)
    snap = registry.registered_tools()
    a_tools = snap["GPIB0::24::INSTR"]
    b_tools = snap["GPIB0::25::INSTR"]
    # Both lists should contain a source_voltage tool but NEITHER should
    # be the bare "smu_dup__source_voltage" — both must be suffixed.
    assert any("__source_voltage" in n for n in a_tools)
    assert any("__source_voltage" in n for n in b_tools)
    assert not any(n == "smu_dup__source_voltage" for n in a_tools + b_tools)
    # The suffixes must come from the last 8 chars of each instrument_id,
    # with non-MCP characters scrubbed (``::`` → ``__``).
    assert any("4__INSTR" in n for n in a_tools), a_tools
    assert any("5__INSTR" in n for n in b_tools), b_tools


def test_schema_propagates_min_max_and_enum(edge_context: Any) -> None:
    """Numeric min/max and enum options propagate into the JSON schema."""
    import asyncio as _aio
    mcp, registry = _make_registry(edge_context)

    tools = _aio.get_event_loop().run_until_complete(mcp.list_tools()) if not _aio.iscoroutinefunction(mcp.list_tools) else None
    # mcp.list_tools is async; use a fresh loop.
    async def _list():
        return await mcp.list_tools()
    loop = _aio.new_event_loop()
    try:
        tools = loop.run_until_complete(_list())
    finally:
        loop.close()

    by_name = {t.name: t for t in tools}
    set_voltage = by_name.get("keithley_2400__set_voltage")
    assert set_voltage is not None
    schema = set_voltage.inputSchema
    props = schema["properties"]["value"]
    assert props["minimum"] == -200.0
    assert props["maximum"] == 200.0

    set_mode = by_name.get("keithley_2400__set_mode")
    assert set_mode is not None
    mode_props = set_mode.inputSchema["properties"]["mode"]
    assert mode_props.get("enum") == ["VOLT", "CURR"]


@pytest.mark.asyncio
async def test_dangerous_hint_propagates(edge_context: Any) -> None:
    """is_dangerous=True flips MCP destructiveHint in the tool annotations."""
    mcp, registry = _make_registry(edge_context)
    tools = await mcp.list_tools()
    by_name = {t.name: t for t in tools}
    danger = by_name.get("keithley_2400__trigger_self_test")
    assert danger is not None
    assert danger.annotations is not None
    assert danger.annotations.destructiveHint is True

    safe = by_name.get("keithley_2400__measure_voltage")
    assert safe is not None
    # measure_voltage isn't dangerous so destructiveHint must be False/None
    if safe.annotations is not None:
        assert safe.annotations.destructiveHint in (False, None)


@pytest.mark.asyncio
async def test_sequences_register(edge_context: Any) -> None:
    """Profile sequences emit __sequence__ tools alongside commands."""
    mcp, registry = _make_registry(edge_context)
    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    assert "keithley_2400__sequence__iv_sweep" in names


@pytest.mark.asyncio
async def test_listener_cleanup_on_detach(edge_context: Any) -> None:
    """detach() removes the registry's listener and tool surface."""
    mcp, registry = _make_registry(edge_context)
    pre = await mcp.list_tools()
    assert any(t.name.startswith("keithley_2400__") for t in pre)

    registry.detach()

    post = await mcp.list_tools()
    assert not any(t.name.startswith("keithley_2400__") for t in post)

    # Subsequent capability events must not fault — listener is gone.
    edge_context.capability_manager.unregister_instrument("USB::34461A::INSTR")


@pytest.mark.asyncio
async def test_partial_failure_rolls_back(edge_context: Any) -> None:
    """A registration that raises mid-flight rolls back the partial set."""
    from mcp.server.fastmcp import FastMCP
    from galois_edge.mcp.dynamic_tools import DynamicToolRegistry

    mcp = FastMCP(name="rollback-test")
    registry = DynamicToolRegistry(mcp, edge_context, emit_list_changed=False)

    # Force the next call to _add_command_tool to raise after one tool added
    call_count = {"n": 0}
    real_add = registry._add_command_tool

    def wrapper(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("synthetic add failure")
        return real_add(*args, **kwargs)

    registry._add_command_tool = wrapper  # type: ignore[assignment]

    # Add a third synthetic instrument to trigger the half-and-fail path.
    profile = _build_minimal_smu_profile("synth_fail")
    edge_context.capability_manager.register_instrument(
        instrument_id="VIRTUAL::SYNTH",
        visa_address="VIRTUAL::SYNTH",
        idn_response="x",
        profile=profile,
    )

    snap = registry.registered_tools()
    assert snap.get("VIRTUAL::SYNTH", []) == [], "rollback must clear tracking"
    tools = await mcp.list_tools()
    assert not any(t.name.startswith("synth_fail__") for t in tools)


@pytest.mark.asyncio
async def test_out_of_range_rejected_before_dispatch(
    edge_context: Any,
) -> None:
    """Acceptance §4.7 #3 — out-of-range arg raises before SCPI dispatch."""
    mcp, registry = _make_registry(edge_context)

    # Snapshot the manager's recorded writes — set_voltage uses write semantics
    mgr = edge_context.instrument_manager
    pre_writes = list(getattr(mgr, "writes", []))

    with pytest.raises(Exception):
        await mcp.call_tool(
            "keithley_2400__set_voltage", {"value": 999.0},
        )

    post_writes = list(getattr(mgr, "writes", []))
    # No write should have been issued — Pydantic / our handler rejected it.
    assert post_writes == pre_writes


@pytest.mark.asyncio
async def test_perf_tools_list_under_100ms_at_200_tools(
    synthetic_command_handler, synthetic_instrument_manager,
) -> None:
    """Acceptance §4.7 #6 — tools/list returns in <100 ms with 200 tools."""
    from galois_edge.capability_manager import CapabilityManager
    from galois_edge.mcp.context import EdgeContext
    from galois_edge.profile_schema import (
        CommandConfig,
        IdentityConfig,
        InstrumentMetadata,
        InstrumentProfile,
        ParameterConfig,
        SettingsConfig,
    )

    # Build a 7-command profile and register 30 copies → 210 dynamic tools.
    cmds = {
        f"cmd_{i}": CommandConfig(
            scpi=f":CMD{i} {{value}}",
            type="write",
            params={
                "value": ParameterConfig(
                    type="float", min=-100.0, max=100.0,
                ),
            },
        )
        for i in range(7)
    }
    profile_template = InstrumentProfile(
        instrument=InstrumentMetadata(
            manufacturer="perf", model="profile", instrument_class="smu",
        ),
        identity=IdentityConfig(patterns=[".*"]),
        settings=SettingsConfig(timeout_ms=5000),
        commands=cmds,
    )

    cap_mgr = CapabilityManager()
    for i in range(30):
        # Use unique-tail instrument_ids so the dynamic registry's
        # short-id disambiguation produces 30 distinct tool names per
        # command (rather than colliding on the same suffix).
        cap_mgr.register_instrument(
            instrument_id=f"PERF::{i:05d}",
            visa_address=f"PERF::{i:05d}",
            idn_response=str(i),
            profile=profile_template,
        )
    ctx = EdgeContext(
        capability_manager=cap_mgr,
        command_handler=synthetic_command_handler,
        instrument_manager=synthetic_instrument_manager,
    )
    mcp, registry = _make_registry(ctx)

    tools = await mcp.list_tools()
    assert len(tools) >= 200

    iters = 5
    samples = []
    for _ in range(iters):
        t0 = time.perf_counter()
        await mcp.list_tools()
        samples.append(time.perf_counter() - t0)
    avg_ms = (sum(samples) / iters) * 1000.0
    print(f"\nperf tools/list avg over {iters} samples at {len(tools)} tools: {avg_ms:.2f} ms")
    assert avg_ms < 100.0, f"avg tools/list latency {avg_ms:.2f} ms exceeds 100 ms gate"
