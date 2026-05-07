"""
Spec G — CI drift check: _KNOWN_GALOIS_VARS ⊇ Go fieldMapping keys.

Parses ``internal/config/config.go``'s ``fieldMapping`` table and asserts
that every key present in that table also appears in ``_KNOWN_GALOIS_VARS``.

If this test fails it means a new env-var key was added to the Go config but
the Python allow-list was not updated — the unknown-var guard would then warn
on every startup even with a correctly written config.env file.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


# ---------------------------------------------------------------------------
# Helper: parse fieldMapping from config.go
# ---------------------------------------------------------------------------

# Repo root is two directories up from this test file's directory.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_GO = _REPO_ROOT / "internal" / "config" / "config.go"

# Match lines like:  {"EDGE_NAME", "EdgeName"},
# or                 { "GRPC_PORT", "GRPCPort" },
_FIELD_ENTRY_RE = re.compile(
    r'\{\s*"([A-Z][A-Z0-9_]+)"\s*,\s*"[A-Za-z][A-Za-z0-9]+"\s*\}'
)


def _parse_field_mapping_keys(path: Path) -> set[str]:
    """Return the set of UPPER_SNAKE_CASE keys from the fieldMapping table.

    Reads only the lines inside the ``var fieldMapping = []fieldEntry{...}``
    block so that it does not pick up keys from struct literals elsewhere in
    the file.
    """
    text = path.read_text(encoding="utf-8")

    # Locate the fieldMapping block.
    start_marker = "var fieldMapping = []fieldEntry{"
    start = text.find(start_marker)
    if start == -1:
        raise ValueError(
            f"Could not find 'var fieldMapping = []fieldEntry{{' in {path}"
        )

    # Find the matching closing brace for the block.
    depth = 0
    block_start = text.index("{", start)
    i = block_start
    while i < len(text):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                block_end = i
                break
        i += 1
    else:
        raise ValueError(f"Unmatched brace in fieldMapping block in {path}")

    block = text[block_start : block_end + 1]
    return {m.group(1) for m in _FIELD_ENTRY_RE.finditer(block)}


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


class TestKnownVarsDrift:
    """Assert _KNOWN_GALOIS_VARS ⊇ Go fieldMapping keys."""

    def test_config_go_exists(self):
        """Guard: ensures we're actually parsing the right file."""
        assert _CONFIG_GO.exists(), (
            f"internal/config/config.go not found at {_CONFIG_GO}. "
            "Is the test being run from the repo root?"
        )

    def test_known_galois_vars_covers_field_mapping(self):
        """Every key in Go fieldMapping must appear in _KNOWN_GALOIS_VARS.

        If this fails, update _KNOWN_GALOIS_VARS in src/galois_edge/config.py
        to include the missing key(s) shown in the assertion message.
        """
        from galois_edge.config import _KNOWN_GALOIS_VARS

        go_keys = _parse_field_mapping_keys(_CONFIG_GO)

        missing = go_keys - _KNOWN_GALOIS_VARS
        assert not missing, (
            f"These Go fieldMapping keys are not in _KNOWN_GALOIS_VARS: {sorted(missing)}\n"
            "Add them to _KNOWN_GALOIS_VARS in src/galois_edge/config.py."
        )

    def test_known_galois_vars_is_nonempty(self):
        """Sanity: the allow-list must have at least the 32 Go fieldMapping entries."""
        from galois_edge.config import _KNOWN_GALOIS_VARS

        assert len(_KNOWN_GALOIS_VARS) >= 32, (
            f"_KNOWN_GALOIS_VARS has only {len(_KNOWN_GALOIS_VARS)} entries; "
            "expected at least 32 (the Go fieldMapping count)."
        )

    def test_inbound_auth_token_present(self):
        """Spec C pre-requisite: INBOUND_AUTH_TOKEN must be in the allow-list."""
        from galois_edge.config import _KNOWN_GALOIS_VARS

        assert "INBOUND_AUTH_TOKEN" in _KNOWN_GALOIS_VARS, (
            "INBOUND_AUTH_TOKEN is required for Spec C compatibility but is "
            "missing from _KNOWN_GALOIS_VARS."
        )
