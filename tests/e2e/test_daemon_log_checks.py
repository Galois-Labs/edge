"""The daemon-log checks that every E2E daemon passes must see what they claim to see.

assert_clean_daemon_log promises that a stopped daemon logged no ERROR or CRITICAL record at all. The
daemon writes records in two formats. Its root handler uses "%(asctime)s - %(name)s - %(levelname)s -
%(message)s" (galois_edge.main._configure_logging). The MCP server's uvicorn.Config installs uvicorn's own
handler on the `uvicorn` loggers, which do not propagate, with the format "%(levelprefix)s %(message)s"
("ERROR:    ..."). This test makes the daemon's real logging setup emit one record of each kind.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

from tests.e2e.conftest import CLI_TIMEOUT_S, base_env, daemon_log_errors

pytestmark = [pytest.mark.e2e, pytest.mark.slow]

#: The daemon's logging setup, then the MCP server's uvicorn.Config (mcp/server.py), then one record per
#: logger and level. Everything goes to stderr, as in the daemon.
EMIT = """
import logging

import uvicorn

from galois_edge.main import _configure_logging

_configure_logging("INFO")
uvicorn.Config(app=None, log_level="warning", lifespan="on", access_log=False)
logging.getLogger("uvicorn.error").error("ASGI callable returned without completing response.")
logging.getLogger("uvicorn.error").critical("uvicorn critical")
logging.getLogger("uvicorn.error").warning("uvicorn warning")
logging.getLogger("asyncio").error("Task was destroyed but it is pending!")
logging.getLogger("galois_edge.main").critical("root critical")
logging.getLogger("galois_edge.main").warning("root warning")
logging.getLogger("galois_edge.main").info("query SYST:ERR? -> ERROR: none")
logging.getLogger("galois_edge.main").info("Edge daemon stopped.")
"""


def test_daemon_log_errors_sees_uvicorn_and_root_records(tmp_path):
    run = subprocess.run([sys.executable, "-c", EMIT], cwd=tmp_path, env=base_env(tmp_path),
                         capture_output=True, text=True, timeout=CLI_TIMEOUT_S, check=False)
    assert run.returncode == 0, run.stderr
    log = run.stderr
    # uvicorn really writes its own format, which the root format's " - ERROR - " never matches.
    assert "ERROR:    ASGI callable returned without completing response." in log.splitlines(), log

    flagged = "\n".join(daemon_log_errors(log))
    for message in ("ASGI callable returned without completing response.", "uvicorn critical",
                    "Task was destroyed but it is pending!", "root critical"):
        assert message in flagged, f"{message!r} not flagged in:\n{log}"
    for message in ("uvicorn warning", "root warning", "SYST:ERR?", "Edge daemon stopped."):
        assert message not in flagged, f"{message!r} wrongly flagged in:\n{log}"
