"""Every test under tests/e2e is `e2e` and `slow`, whether or not its module says so.

`make test-python` deselects only `slow`, and `make test-e2e` selects only `e2e`. A module that forgot
either mark would spawn real daemons in the default tier, or drop out of the E2E run. tests/e2e's
conftest adds both marks at collection. This test runs that conftest in a throwaway pytest session
whose only test is unmarked.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.e2e.conftest import CLI_TIMEOUT_S, base_env

pytestmark = [pytest.mark.e2e, pytest.mark.slow]

CONFTEST = Path(__file__).with_name("conftest.py")


def test_an_unmarked_e2e_test_is_still_e2e_and_slow(tmp_path):
    suite = tmp_path / "e2e"
    suite.mkdir()
    shutil.copy(CONFTEST, suite / "conftest.py")
    (suite / "test_unmarked.py").write_text("def test_unmarked():\n    pass\n")
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\naddopts = --strict-markers\nmarkers =\n    e2e: e2e\n    slow: slow\n")

    def collect(expression: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
             "-p", "no:randomly", "-m", expression, str(suite)],
            cwd=tmp_path, env=base_env(tmp_path), capture_output=True, text=True, timeout=CLI_TIMEOUT_S,
            check=False)

    default_tier = collect("not serial and not slow and not hardware")   # make test-python's selection
    assert default_tier.returncode == 5, default_tier.stdout + default_tier.stderr   # 5: nothing collected
    assert "1 deselected" in default_tier.stdout, default_tier.stdout

    e2e_run = collect("e2e and slow")
    assert e2e_run.returncode == 0, e2e_run.stdout + e2e_run.stderr
    assert "e2e/test_unmarked.py::test_unmarked" in e2e_run.stdout, e2e_run.stdout
