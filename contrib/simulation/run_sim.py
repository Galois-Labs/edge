"""Run the edge daemon with DEMO_MODE virtual instruments, reachable from other hosts.

Thin wrapper (contracts/edge-api.md §9, E10): it sets DEMO_MODE=true and binds
gRPC/WS/MCP on 0.0.0.0 unless the environment or the repo .env already sets those
variables, then runs the stock daemon entry point. Nothing is monkey-patched.

Caution on a host with real instruments: DEMO_MODE adds the virtual instruments
to the real InstrumentManager instead of replacing it (main.py
_DemoInstrumentManagerProxy). Outside a container the daemon still discovers the
host's real VISA/USB/LAN instruments and, with the 0.0.0.0 defaults, serves them
on every interface: WS and MCP without authentication, gRPC too unless
INBOUND_AUTH_TOKEN is set. On a lab machine, run Dockerfile.sim's container, or
set GRPC_BIND_HOST, WS_BIND_HOST and MCP_BIND_HOST to 127.0.0.1.

Usage:
    python -m contrib.simulation.run_sim
"""

from __future__ import annotations

import os
import sys
from typing import MutableMapping

_DEFAULTS = {
    "DEMO_MODE": "true",
    "GRPC_BIND_HOST": "0.0.0.0",
    "WS_BIND_HOST": "0.0.0.0",
    "MCP_BIND_HOST": "0.0.0.0",
}

_KEEPALIVE: list[int] = []


def apply_defaults(environ: MutableMapping[str, str] = os.environ) -> None:
    """Set the simulation defaults without overriding explicit values."""
    for key, value in _DEFAULTS.items():
        environ.setdefault(key, value)


def _hold_stdin_open() -> None:
    """Replace a non-TTY stdin with a pipe that never reaches EOF.

    The daemon shuts down on stdin EOF (Go-supervisor contract, main.py
    _watch_stdin); a container's stdin is /dev/null. SIGTERM/SIGINT still stop it.
    """
    if sys.stdin is not None and sys.stdin.isatty():
        return
    read_fd, write_fd = os.pipe()
    os.dup2(read_fd, 0)
    os.close(read_fd)
    sys.stdin = os.fdopen(0, "r", closefd=False)
    _KEEPALIVE.append(write_fd)  # never closed, so no EOF while the process lives


def main(hold_stdin: bool = True) -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (os.path.join(here, "..", "..", "src"), os.path.join(here, "..", "..")):
        if path not in sys.path:
            sys.path.insert(0, path)
    # Import first: galois_edge.config loads the repo .env on import, without
    # overriding, so its values must already be set when the defaults go in.
    from galois_edge.main import main as daemon_main

    apply_defaults()
    if hold_stdin:
        _hold_stdin_open()
    daemon_main()


if __name__ == "__main__":
    main()
