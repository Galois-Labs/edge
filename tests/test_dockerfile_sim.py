"""E7 (edge-api.md §9): Dockerfile.sim installs real dependencies, exposes MCP, uses bind-host keys."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.critical
TEXT = (Path(__file__).resolve().parents[1] / "Dockerfile.sim").read_text()


def test_installs_requirements_then_the_package():
    assert TEXT.index("pip install --no-cache-dir -r requirements.txt") < TEXT.index("pip install --no-cache-dir /app")


def test_copies_sources_and_the_submodule_explicitly():
    for line in ("COPY src /app/src", "COPY contrib /app/contrib", "COPY third_party /app/third_party",
                 "COPY pyproject.toml requirements.txt LICENSE /app/"):
        assert line in TEXT
    assert not re.search(r"^COPY \. ", TEXT, re.M)


def test_exposes_grpc_ws_and_mcp():
    ports = set(re.search(r"^EXPOSE (.+)$", TEXT, re.M).group(1).split())
    assert {"50052", "8766", "8767"} <= ports


def test_uses_bind_host_keys_and_the_thin_wrapper():
    for kv in ("GRPC_BIND_HOST=0.0.0.0", "WS_BIND_HOST=0.0.0.0", "MCP_BIND_HOST=0.0.0.0", "DEMO_MODE=true"):
        assert kv in TEXT
    assert 'CMD ["python", "-m", "contrib.simulation.run_sim"]' in TEXT
