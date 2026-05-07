"""Shared fixtures for MCP-server tests.

Builds a CapabilityManager populated with two synthetic instruments
(a Keithley-style SMU and a generic DMM) and wires them through a
real CommandHandler against the existing MockInstrumentManager.
"""

from __future__ import annotations

import os
import sys
from typing import Any

import pytest

# Ensure src/ is on sys.path even without `pip install -e .` having run.
_HERE = os.path.dirname(__file__)
_SRC = os.path.abspath(os.path.join(_HERE, "..", "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


def _build_keithley_profile() -> Any:
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
        SweepConfig,
    )

    return InstrumentProfile(
        instrument=InstrumentMetadata(
            manufacturer="Keithley",
            model="2400",
            instrument_class="smu",
        ),
        identity=IdentityConfig(
            patterns=["KEITHLEY.*MODEL 2400"],
        ),
        settings=SettingsConfig(timeout_ms=5000),
        commands={
            "identify": CommandConfig(
                scpi="*IDN?",
                type="query",
                description="Read instrument identity",
                returns=ReturnConfig(type="string"),
            ),
            "set_voltage": CommandConfig(
                scpi=":SOUR:VOLT {value}",
                type="write",
                description="Source a DC voltage",
                params={
                    "value": ParameterConfig(
                        type="float",
                        unit="V",
                        min=-200.0,
                        max=200.0,
                        description="Target voltage",
                    ),
                },
            ),
            "ramp_voltage": CommandConfig(
                type="write",
                description="Ramp to a setpoint at a controlled rate",
                requires_sweep=True,
                sweep=SweepConfig(
                    rate_param="sweep_rate",
                    command=":SOUR:VOLT:RAMP {value} {sweep_rate}",
                    check_command=":STAT:RAMP?",
                    check_idle_match="^IDLE$",
                    stop_command=":SOUR:VOLT:STOP",
                    poll_interval_ms=50,
                ),
                params={
                    "value": ParameterConfig(type="float", unit="V"),
                    "sweep_rate": ParameterConfig(
                        type="float", unit="V/s",
                    ),
                },
            ),
            "set_mode": CommandConfig(
                scpi=":SOUR:FUNC {mode}",
                type="write",
                description="Source function mode",
                params={
                    "mode": ParameterConfig(
                        type="enum",
                        options=["VOLT", "CURR"],
                        description="Source mode",
                    ),
                },
            ),
            "measure_voltage": CommandConfig(
                scpi=":MEAS:VOLT?",
                type="query",
                description="One-shot voltage read (streamable)",
                streamable=True,
                returns=ReturnConfig(type="float", unit="V"),
            ),
            "trigger_self_test": CommandConfig(
                scpi="*TST?",
                type="query",
                description="Run instrument self-test",
                is_dangerous=True,
                returns=ReturnConfig(type="string"),
            ),
        },
        sequences={
            "iv_sweep": SequenceConfig(
                description="Step voltage and capture current",
                steps=[
                    SequenceStepConfig(
                        command="set_voltage",
                        args={"value": "{step_voltage}"},
                    ),
                    SequenceStepConfig(
                        scpi=":MEAS:CURR?",
                        capture="current",
                    ),
                ],
                parameters={
                    "step_voltage": ParameterConfig(type="float"),
                },
                returns="current",
            ),
        },
    )


def _build_dmm_profile() -> Any:
    from galois_edge.profile_schema import (
        CommandConfig,
        IdentityConfig,
        InstrumentMetadata,
        InstrumentProfile,
        ReturnConfig,
        SettingsConfig,
    )

    return InstrumentProfile(
        instrument=InstrumentMetadata(
            manufacturer="Keysight",
            model="34461A",
            instrument_class="dmm",
        ),
        identity=IdentityConfig(
            patterns=["KEYSIGHT.*34461A"],
        ),
        settings=SettingsConfig(timeout_ms=5000),
        commands={
            "measure_voltage_dc": CommandConfig(
                scpi=":MEAS:VOLT:DC?",
                type="query",
                description="One-shot DC voltage measurement",
                returns=ReturnConfig(type="float", unit="V"),
            ),
        },
    )


@pytest.fixture
def synthetic_capability_manager() -> Any:
    """Two-instrument CapabilityManager: a Keithley SMU and a Keysight DMM."""
    from galois_edge.capability_manager import CapabilityManager

    cap_mgr = CapabilityManager()
    cap_mgr.register_instrument(
        instrument_id="GPIB0::24::INSTR",
        visa_address="GPIB0::24::INSTR",
        idn_response="KEITHLEY INSTRUMENTS INC.,MODEL 2400,1234567,A01",
        profile=_build_keithley_profile(),
    )
    cap_mgr.register_instrument(
        instrument_id="USB::34461A::INSTR",
        visa_address="USB::34461A::INSTR",
        idn_response="KEYSIGHT TECHNOLOGIES,34461A,SN001,A.01",
        profile=_build_dmm_profile(),
    )
    return cap_mgr


@pytest.fixture
def synthetic_instrument_manager() -> Any:
    sys.path.insert(0, os.path.abspath(os.path.join(_HERE, "..")))
    from conftest import MockInstrumentManager

    mgr = MockInstrumentManager(
        resources=["GPIB0::24::INSTR", "USB::34461A::INSTR"],
        idn_map={
            "GPIB0::24::INSTR": (
                "KEITHLEY INSTRUMENTS INC.,MODEL 2400,1234567,A01"
            ),
            "USB::34461A::INSTR": "KEYSIGHT TECHNOLOGIES,34461A,SN001,A.01",
        },
    )
    mgr.connect("GPIB0::24::INSTR")
    mgr.connect("USB::34461A::INSTR")
    mgr.set_query_response(
        "GPIB0::24::INSTR",
        "*IDN?",
        "KEITHLEY INSTRUMENTS INC.,MODEL 2400,1234567,A01",
    )
    mgr.set_query_response(
        "USB::34461A::INSTR",
        ":MEAS:VOLT:DC?",
        "1.234500E+00",
    )
    mgr.set_query_response(
        "GPIB0::24::INSTR",
        ":STAT:RAMP?",
        "IDLE",
    )
    mgr.set_query_response(
        "GPIB0::24::INSTR",
        ":MEAS:VOLT?",
        "1.500000E+00",
    )
    mgr.set_query_response(
        "GPIB0::24::INSTR",
        "*TST?",
        "0",
    )
    return mgr


@pytest.fixture
def synthetic_command_handler(synthetic_instrument_manager: Any) -> Any:
    from galois_edge.command_handler import CommandHandler

    return CommandHandler(synthetic_instrument_manager)


@pytest.fixture
def edge_context(
    synthetic_capability_manager: Any,
    synthetic_command_handler: Any,
    synthetic_instrument_manager: Any,
) -> Any:
    from galois_edge.mcp.context import EdgeContext

    return EdgeContext(
        capability_manager=synthetic_capability_manager,
        command_handler=synthetic_command_handler,
        instrument_manager=synthetic_instrument_manager,
        edge_id="test-edge-1",
        edge_name="test-host",
    )


@pytest.fixture
def fastmcp_with_tools(edge_context: Any) -> Any:
    from mcp.server.fastmcp import FastMCP
    from galois_edge.mcp.tools import (
        register_discovery_tools,
        register_execute_tools,
        register_stream_tools,
        register_sweep_tools,
    )

    mcp = FastMCP(name="galois-edge-test")
    register_discovery_tools(mcp, edge_context)
    register_execute_tools(mcp, edge_context)
    register_sweep_tools(mcp, edge_context)
    register_stream_tools(mcp, edge_context)
    return mcp
