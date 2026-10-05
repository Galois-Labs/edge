"""Shutdown polish (M1): stdin-EOF watching (EQ1) and stopping in-flight discovery (EQ2)."""
from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from galois_edge.main import EdgeDaemon

pytestmark = pytest.mark.critical
SRC = Path(__file__).resolve().parents[1] / "src"


class _StopRecorder:
    """Stands in for EdgeDaemon: _watch_stdin only calls self.stop()."""

    def __init__(self) -> None:
        self.stops = 0

    async def stop(self) -> None:
        self.stops += 1


# ---------------------------------------------------------------------------
# EQ1: _watch_stdin. A subprocess stub runs the real watcher with a real stdin.
# ---------------------------------------------------------------------------

# Blocking galois_edge.mcp keeps the stub's import well under a second; the watcher never uses MCP.
_STUB = r"""
import asyncio, logging, sys
sys.path.insert(0, sys.argv[1])
sys.modules["galois_edge.mcp"] = None
from galois_edge.main import EdgeDaemon

logging.basicConfig(level=logging.INFO, stream=sys.stderr)

class Recorder:
    stops = 0
    async def stop(self):
        Recorder.stops += 1

async def main():
    watcher = asyncio.ensure_future(EdgeDaemon._watch_stdin(Recorder()))
    print("watching", flush=True)
    try:
        await asyncio.wait_for(watcher, timeout=float(sys.argv[2]))
    except asyncio.TimeoutError:
        print("hung", flush=True)
        return
    print("returned stops=%d" % Recorder.stops, flush=True)

asyncio.run(main())
"""


def _run_stub(stdin, close_after_ready: bool = False, timeout_s: float = 3.0):
    proc = subprocess.Popen([sys.executable, "-c", _STUB, str(SRC), str(timeout_s)], stdin=stdin,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "watching"
        if close_after_ready:
            proc.stdin.close()   # what the Go supervisor does to stop the daemon
        out, err = proc.communicate(timeout=timeout_s + 5)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
    return out.strip(), err


def test_devnull_stdin_is_not_watched_and_does_not_stop_the_daemon():
    # epoll cannot register /dev/null: the asyncio pipe reader would wait for EOF forever.
    # No supervisor pipe means the watcher steps aside; SIGTERM/SIGINT stop the daemon.
    out, err = _run_stub(subprocess.DEVNULL)
    assert out == "returned stops=0", err
    assert "stdin watcher not active" in err


def test_closing_a_stdin_pipe_still_stops_the_daemon():
    # Go supervisor contract: it holds the write end of a pipe and closes it to shut down.
    out, err = _run_stub(subprocess.PIPE, close_after_ready=True)
    assert out == "returned stops=1", err
    assert "Stdin closed (EOF)" in err


async def test_regular_file_stdin_is_not_watched(tmp_path, monkeypatch):
    # A redirected file used to fail connect_read_pipe and stop the daemon at once.
    path = tmp_path / "stdin.txt"
    path.write_text("not a supervisor\n")
    recorder = _StopRecorder()
    with open(path) as stdin:
        monkeypatch.setattr(sys, "stdin", stdin)
        await asyncio.wait_for(EdgeDaemon._watch_stdin(recorder), timeout=1.0)
    assert recorder.stops == 0


async def test_closing_a_socket_stdin_stops_the_daemon(monkeypatch):
    ours, theirs = socket.socketpair()
    recorder = _StopRecorder()
    with ours, os.fdopen(theirs.detach(), "rb") as stdin:
        monkeypatch.setattr(sys, "stdin", stdin)
        watcher = asyncio.ensure_future(EdgeDaemon._watch_stdin(recorder))
        ours.close()
        await asyncio.wait_for(watcher, timeout=1.0)
    assert recorder.stops == 1
