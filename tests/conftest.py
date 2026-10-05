"""
Shared test fixtures for daemon-clean tests.

Provides mock InstrumentManager, CommandHandler, CapabilityManager,
SDKExecutor, and gRPC test infrastructure.
"""

from __future__ import annotations

import asyncio
import json
import sys
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

# Ensure the source tree is importable
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "src")
)


# ---------------------------------------------------------------------------
# Hermetic environment (spec §10 parallel-safety rules)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _hermetic_galois_env(monkeypatch):
    """No ambient Galois config (shell, CI, a found .env) leaks into any test (spec §10 rule 7)."""
    from galois_edge.config import _KNOWN_GALOIS_VARS

    # Config also reads the deprecated SCAN_INTERVAL_S alias and DYNAMIC_PROFILE_DIR,
    # which is not in _KNOWN_GALOIS_VARS until fixes-config (CI-23); harmless after.
    for key in sorted(_KNOWN_GALOIS_VARS | {"SCAN_INTERVAL_S", "DYNAMIC_PROFILE_DIR"}):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path_factory, monkeypatch):
    """Per-test HOME so ~/.config/galois-edge (profile cache, dynamic profiles) is never shared (spec §10 rule 2)."""
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)


# ---------------------------------------------------------------------------
# Loopback HTTP (spec §10 rules 1 and 6: port 0, loopback only)
# ---------------------------------------------------------------------------


@pytest.fixture
def serve_json(monkeypatch):
    """Serve JSON documents from a per-test HTTP server on 127.0.0.1 and an OS-assigned port.

    ``serve_json("/jwks.json", doc)`` returns ``http://127.0.0.1:<port>/jwks.json``. Clients
    such as PyJWKClient (>= 2.13 rejects file:// URIs) fetch it with urllib, which honours
    *_proxy variables, so loopback is exempted from any ambient proxy for this test.
    """
    for var in ("no_proxy", "NO_PROXY"):
        monkeypatch.setenv(var, "127.0.0.1")
    documents: Dict[str, bytes] = {}

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = documents.get(self.path)
            if body is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):  # keep request lines out of test output
            pass

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    host, port = server.server_address[:2]

    def serve(path: str, document: Any) -> str:
        documents[path] = json.dumps(document).encode("utf-8")
        return f"http://{host}:{port}{path}"

    try:
        yield serve
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Tier marker policy (contracts/edge-api.md §7)
# ---------------------------------------------------------------------------


def marker_policy_violations(items) -> list:
    """edge-api.md §7: critical is never serial (nor slow/hardware); serial needs reason=.

    serial is never slow/hardware either: make test's serial pass is `pytest -m serial
    tests/` with no slow/hardware exclusion, and spec §10 never runs those at merge.
    """
    problems = []
    for item in items:
        serial = item.get_closest_marker("serial")
        critical = item.get_closest_marker("critical")
        if critical is not None:
            for other in ("serial", "slow", "hardware"):
                if item.get_closest_marker(other) is not None:
                    problems.append(f"{item.nodeid}: critical tests must not be {other}")
        if serial is not None and not (serial.kwargs.get("reason") or serial.args):
            problems.append(f"{item.nodeid}: @pytest.mark.serial needs reason=...")
        if serial is not None:
            for other in ("slow", "hardware"):
                if item.get_closest_marker(other) is not None:
                    problems.append(f"{item.nodeid}: serial tests must not be {other} (the serial pass runs them all)")
    return problems


def pytest_collection_modifyitems(config, items):
    problems = marker_policy_violations(items)
    if problems:
        message = "marker policy violations:\n  " + "\n  ".join(problems)
        # Under xdist a worker's UsageError reaches the console only as an opaque
        # INTERNALERROR, so one worker also prints the reason (make test-critical uses -n auto).
        if getattr(config, "workerinput", {}).get("workerid") == "gw0":
            with config.pluginmanager.getplugin("capturemanager").global_and_fixture_disabled():
                sys.stderr.write(f"ERROR: {message}\n")
                sys.stderr.flush()
        raise pytest.UsageError(message)


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
        grpc_port=0,
        ws_port=0,
        log_level="DEBUG",
        scan_interval_s=0,  # disable periodic scan in tests
    )
