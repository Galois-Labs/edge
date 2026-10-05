"""Shutdown polish (M1, EQ1) end to end: the stdin watcher in a child process with a real fd 0.

Default tier, not critical: each test starts an interpreter that imports galois_edge.main,
which takes several seconds on a loaded host (critical tests must stay under 2 s). The
critical tier covers the same watcher paths in-process (tests/test_shutdown_m1.py).
"""
from __future__ import annotations

import select
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"

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
    # Unbuffered binary stdout, so readline() takes only the "watching" line off the pipe.
    # A buffered readline can also pull in the next line, and communicate() (which reads
    # the raw fd) would then never see it.
    proc = subprocess.Popen([sys.executable, "-c", _STUB, str(SRC), str(timeout_s)], stdin=stdin,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    try:
        ready, _, _ = select.select([proc.stdout], [], [], timeout_s + 5)
        first = proc.stdout.readline() if ready else b""
        if first != b"watching\n":
            proc.kill()
            _, err = proc.communicate()
            pytest.fail(f"the stub never started watching stdin: {first!r}\n{err.decode()}")
        if close_after_ready:
            proc.stdin.close()   # what the Go supervisor does to stop the daemon
        out, err = proc.communicate(timeout=timeout_s + 5)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
    return out.decode().strip(), err.decode()


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
