"""
Shared test fixtures for daemon-clean tests.

Provides mock InstrumentManager, CommandHandler, CapabilityManager,
SDKExecutor, and gRPC test infrastructure.
"""

from __future__ import annotations

import asyncio
import sys
import os
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

# Ensure the source tree is importable
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "src")
)


# ---------------------------------------------------------------------------
# Mock InstrumentManager
# ---------------------------------------------------------------------------


class MockInstrumentManager:
    """Minimal mock of InstrumentManager for unit tests."""

    def __init__(
        self,
        resources: Optional[List[str]] = None,
        idn_map: Optional[Dict[str, str]] = None,
    ) -> None:
        self._resources = list(resources or [])
        self._connected: set[str] = set()
        self._idn_map: Dict[str, str] = dict(idn_map or {})
        self._query_responses: Dict[str, str] = {}
        self._raw_responses: Dict[str, bytes] = {}
        self._writes: List[tuple] = []

    # -- Resource listing --

    def list_resources(self) -> tuple[str, ...]:
        return tuple(self._resources)

    def rescan_all(self) -> tuple[str, ...]:
        return self.list_resources()

    def rescan_gpib(self) -> list[str]:
        return []

    @property
    def gpib_available(self) -> bool:
        return False

    # -- Connection --

    def connect(
        self,
        visa_address: str,
        timeout: int = 5000,
        max_attempts: int = 1,
        retry_delay: float = 2.0,
    ) -> Optional[str]:
        self._connected.add(visa_address)
        return visa_address

    def disconnect(self, instrument_id: str) -> None:
        self._connected.discard(instrument_id)

    def disconnect_all(self) -> None:
        self._connected.clear()

    def is_connected(self, instrument_id: str) -> bool:
        return instrument_id in self._connected

    def canonical_id(self, instrument_id: str) -> str:
        return instrument_id

    # -- I/O --

    def query(self, instrument_id: str, command: str) -> str:
        key = f"{instrument_id}:{command}"
        if key in self._query_responses:
            return self._query_responses[key]
        return self._idn_map.get(instrument_id, "")

    def query_raw(self, instrument_id: str, command: str) -> bytes:
        key = f"{instrument_id}:{command}"
        if key in self._raw_responses:
            return self._raw_responses[key]
        raise ValueError(
            f"Binary (raw) reads are not supported on this transport: "
            f"{instrument_id}"
        )

    def write(self, instrument_id: str, command: str) -> None:
        self._writes.append((instrument_id, command))

    def identify(self, instrument_id: str) -> str:
        return self._idn_map.get(instrument_id, "")

    def set_gpib_identity_probes(self, probes: list) -> None:
        pass

    # -- Test helpers --

    def set_query_response(
        self, instrument_id: str, command: str, response: str,
    ) -> None:
        self._query_responses[f"{instrument_id}:{command}"] = response

    def set_raw_response(
        self, instrument_id: str, command: str, response: bytes,
    ) -> None:
        self._raw_responses[f"{instrument_id}:{command}"] = response


@pytest.fixture
def mock_instrument_manager() -> MockInstrumentManager:
    """Fixture providing a MockInstrumentManager with one test instrument."""
    mgr = MockInstrumentManager(
        resources=["GPIB0::25::INSTR"],
        idn_map={
            "GPIB0::25::INSTR": "KEITHLEY INSTRUMENTS INC.,MODEL 2400,1234567,A01",
        },
    )
    mgr.connect("GPIB0::25::INSTR")
    return mgr


# ---------------------------------------------------------------------------
# Mock CommandHandler (wraps a real one around mock instrument mgr)
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_command_handler(
    mock_instrument_manager: MockInstrumentManager,
) -> Any:
    """Fixture providing a CommandHandler backed by mock instruments."""
    from galois_edge.command_handler import CommandHandler
    return CommandHandler(mock_instrument_manager)


# ---------------------------------------------------------------------------
# Mock CapabilityManager
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_capability_manager() -> Any:
    """Fixture providing an empty CapabilityManager."""
    from galois_edge.capability_manager import CapabilityManager
    return CapabilityManager()


# ---------------------------------------------------------------------------
# Mock SDKExecutor
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_sdk_executor(
    mock_instrument_manager: MockInstrumentManager,
) -> Any:
    """Fixture providing an SDKExecutor backed by mock instruments."""
    from galois_edge.sdk_executor import SDKExecutor
    return SDKExecutor(mock_instrument_manager)


# ---------------------------------------------------------------------------
# Config fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def test_config() -> Any:
    """Fixture providing a Config with test-friendly defaults."""
    from galois_edge.config import Config
    return Config(
        grpc_port=50099,
        ws_port=8799,
        log_level="DEBUG",
        scan_interval_s=0,  # disable periodic scan in tests
    )
