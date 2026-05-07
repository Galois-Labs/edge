"""Tests for the Phase 3 typed SDK wrapper MCP tools (§4.4)."""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest


def _parse_call(result: Any) -> Any:
    if isinstance(result, tuple) and len(result) == 2:
        structured = result[1]
        if isinstance(structured, dict) and "result" in structured:
            return structured["result"]
        return structured
    blocks = result if isinstance(result, list) else result[0]
    if not blocks:
        return None
    text = getattr(blocks[0], "text", None)
    if text is None:
        return None
    return json.loads(text)


class _StubClient:
    """In-memory stand-in for an SDK wrapper client.

    Records every method call so tests can assert on dispatch ordering.
    """

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def enable_output(self):
        self.calls.append({"method": "enable_output"})
        return "OK"

    def disable_output(self):
        self.calls.append({"method": "disable_output"})
        return "OK"

    def set_voltage(self, value=0.0):
        self.calls.append({"method": "set_voltage", "value": value})
        return "OK"

    def set_current(self, value=0.0):
        self.calls.append({"method": "set_current", "value": value})
        return "OK"


def _make_executor_with_stub(instrument_id: str = "STUB::PSU"):
    """Build an SDKExecutor wrapping a _StubClient pre-registered as
    ``instrument_id``. Bypasses the SDK import path so we don't need
    pyserial / dwfpy installed.
    """
    from galois_edge.sdk_executor import SDKExecutor, _SDKClient

    class _NoopMgr:
        def is_connected(self, _id):
            return False

    executor = SDKExecutor(_NoopMgr())
    stub = _StubClient()
    # Inject the entry directly.
    sdk_config = type(
        "_FakeSDKCfg",
        (),
        {
            "disconnect": type("_Disc", (), {"method": "disconnect"})(),
        },
    )()
    executor._clients[instrument_id] = _SDKClient(client=stub, sdk_config=sdk_config)
    return executor, stub


def _make_registry_for_sdk(executor):
    from mcp.server.fastmcp import FastMCP
    from galois_edge.capability_manager import CapabilityManager
    from galois_edge.command_handler import CommandHandler
    from galois_edge.mcp.context import EdgeContext
    from galois_edge.mcp.dynamic_tools import DynamicToolRegistry

    cap_mgr = CapabilityManager()

    class _DummyHandler(CommandHandler):
        def __init__(self):
            pass

    mcp = FastMCP(name="sdk-test")
    ctx = EdgeContext(
        capability_manager=cap_mgr,
        command_handler=_DummyHandler(),
        instrument_manager=None,
    )
    registry = DynamicToolRegistry(mcp, ctx, emit_list_changed=False)
    return mcp, registry


@pytest.mark.asyncio
async def test_register_with_mcp_emits_typed_tools() -> None:
    """SDK wrappers with MCP_TOOL_SPECS produce dps150_wrapper__* tools."""
    executor, stub = _make_executor_with_stub()
    mcp, registry = _make_registry_for_sdk(executor)
    count = executor.register_with_mcp(registry)
    assert count > 0

    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    assert "dps150_wrapper__set_voltage" in names
    assert "dps150_wrapper__enable_output" in names


@pytest.mark.asyncio
async def test_no_specs_no_tools(monkeypatch) -> None:
    """A wrapper module without MCP_TOOL_SPECS contributes zero tools."""
    import importlib
    # Walk the wrappers and verify the dps150 specs add tools, but
    # acqiris_wrapper (which has no MCP_TOOL_SPECS) does not.
    executor, _ = _make_executor_with_stub()
    mcp, registry = _make_registry_for_sdk(executor)
    executor.register_with_mcp(registry)
    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    assert not any(n.startswith("acqiris_wrapper__") for n in names)


@pytest.mark.asyncio
async def test_out_of_range_rejected_pre_dispatch() -> None:
    """A typed SDK tool rejects out-of-range params before any client call."""
    executor, stub = _make_executor_with_stub()
    mcp, registry = _make_registry_for_sdk(executor)
    executor.register_with_mcp(registry)

    # set_voltage spec has minimum=0, maximum=30
    with pytest.raises(Exception):
        await mcp.call_tool(
            "dps150_wrapper__set_voltage",
            {"instrument_id": "STUB::PSU", "value": 999.0},
        )
    # No SDK call should have been made.
    assert not any(c["method"] == "set_voltage" for c in stub.calls), stub.calls


@pytest.mark.asyncio
async def test_dangerous_method_gated_by_jwt() -> None:
    """When caller_jwt has danger_allow=False, dangerous tools are rejected."""
    from galois_edge.mcp.auth import CallerJWT, authorize
    from galois_edge.mcp.context import set_current_caller, reset_current_caller

    executor, stub = _make_executor_with_stub()
    mcp, registry = _make_registry_for_sdk(executor)
    executor.register_with_mcp(registry)

    # Stub a JWTValidator on the EdgeContext so authorize() actually runs.
    class _StubValidator:
        async def validate(self, token):
            return None

    registry._ctx.jwt_validator = _StubValidator()

    claims = CallerJWT(
        iss="https://cloud.test",
        aud="edge:test",
        sub="user:1",
        exp=9999999999,
        edge_id="test",
        tools_allow=["dps150_wrapper__set_voltage"],
        danger_allow=False,
    )
    token = set_current_caller(claims)
    try:
        with pytest.raises(Exception):
            await mcp.call_tool(
                "dps150_wrapper__set_voltage",
                {"instrument_id": "STUB::PSU", "value": 5.0},
            )
    finally:
        reset_current_caller(token)

    # No call landed on the stub
    assert not any(c["method"] == "set_voltage" for c in stub.calls)


@pytest.mark.asyncio
async def test_dangerous_method_allowed_with_danger_allow() -> None:
    """When danger_allow=True and tool is in tools_allow, the call goes through."""
    from galois_edge.mcp.auth import CallerJWT
    from galois_edge.mcp.context import set_current_caller, reset_current_caller

    executor, stub = _make_executor_with_stub()
    mcp, registry = _make_registry_for_sdk(executor)
    executor.register_with_mcp(registry)

    class _StubValidator:
        async def validate(self, token):
            return None

    registry._ctx.jwt_validator = _StubValidator()

    claims = CallerJWT(
        iss="https://cloud.test",
        aud="edge:test",
        sub="user:1",
        exp=9999999999,
        edge_id="test",
        tools_allow=["dps150_wrapper__set_voltage"],
        danger_allow=True,
    )
    token = set_current_caller(claims)
    try:
        result = await mcp.call_tool(
            "dps150_wrapper__set_voltage",
            {"instrument_id": "STUB::PSU", "value": 5.0},
        )
    finally:
        reset_current_caller(token)

    data = _parse_call(result)
    assert data["success"] is True
    assert any(
        c["method"] == "set_voltage" and c["value"] == 5.0
        for c in stub.calls
    ), stub.calls
