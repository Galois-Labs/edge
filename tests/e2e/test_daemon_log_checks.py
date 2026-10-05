"""The daemon-log checks that every E2E daemon passes must see what they claim to see.

assert_clean_daemon_log promises that a stopped daemon logged no ERROR or CRITICAL record at all. The
daemon writes records in two formats. Its root handler uses "%(asctime)s - %(name)s - %(levelname)s -
%(message)s" (galois_edge.main._configure_logging). The MCP server's uvicorn.Config installs uvicorn's own
handler on the `uvicorn` loggers, which do not propagate, with the format "%(levelprefix)s %(message)s"
("ERROR:    ..."). The first test makes the daemon's real logging setup emit one record of each kind.

edge_daemon promises that raw USB is off. InstrumentManager logs one of several "USB transport" messages
whenever USB_RAW_ENABLED did not reach it, and the second test checks that raw_usb_lines flags each one.
"""
from __future__ import annotations

import ast
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests.e2e.conftest import CLI_TIMEOUT_S, base_env, daemon_log_errors, raw_usb_lines

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


def root_line(logger: str, level: str, message: str) -> str:
    """One record in the daemon's root handler format."""
    return f"2026-10-05 16:19:01,466 - {logger} - {level} - {message}"


def raw_usb_messages() -> list[tuple[str, str]]:
    """(LEVEL, message) for every logger call in InstrumentManager's module whose message names the
    USB transport, with each %-placeholder filled in, read from the source without importing it."""
    origin = importlib.util.find_spec("galois_edge.instrument_manager").origin
    found = []
    for node in ast.walk(ast.parse(Path(origin).read_text())):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "logger"
                and node.args and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str) and "USB transport" in node.args[0].value):
            found.append((node.func.attr.upper(), re.sub(r"%[sdr]", "[Errno 13] Access denied",
                                                         node.args[0].value)))
    return found


def test_raw_usb_lines_sees_every_instrument_manager_raw_usb_message():
    messages = raw_usb_messages()
    # If USB_RAW_ENABLED is ignored: pyusb present -> enabled or initialisation failed; absent -> warning.
    assert {m for _, m in messages} >= {"Raw USB transport enabled",
                                        "USB transport initialisation failed: [Errno 13] Access denied",
                                        "USB transport enabled but pyusb not installed"}, messages
    for level, message in messages:
        line = root_line("galois_edge.instrument_manager", level, message)
        assert raw_usb_lines(f"{root_line('galois_edge.main', 'INFO', 'Edge daemon starting')}\n{line}\n") \
            == [line], line

    unrelated = [   # neither says raw USB is on
        root_line("galois_edge.main", "INFO", "USB hotplug monitor not available (pyudev not installed)"),
        root_line("galois_edge.usb_transport", "INFO", "pyusb not available — raw USB transport disabled"),
    ]
    assert raw_usb_lines("\n".join(unrelated)) == []
