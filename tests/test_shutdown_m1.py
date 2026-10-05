"""Shutdown polish (M1): stdin-EOF watching (EQ1) and stopping in-flight discovery (EQ2)."""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import socket
import sys
import threading
import time

import pytest

from galois_edge.capability_manager import CapabilityManager
from galois_edge.config import Config
from galois_edge.instrument_manager import InstrumentManager
from galois_edge.main import EdgeDaemon

pytestmark = pytest.mark.critical


class _StopRecorder:
    """Stands in for EdgeDaemon: _watch_stdin only calls self.stop()."""

    def __init__(self) -> None:
        self.stops = 0

    async def stop(self) -> None:
        self.stops += 1


# ---------------------------------------------------------------------------
# EQ1: _watch_stdin. A real stdin fd stands in for the daemon's fd 0; the
# subprocess stubs in test_shutdown_m1_stdin_stub.py run the same paths end to end.
# ---------------------------------------------------------------------------


async def test_devnull_stdin_is_not_watched(monkeypatch, caplog):
    # epoll cannot register /dev/null: the asyncio pipe reader would wait for EOF forever.
    # No supervisor pipe means the watcher steps aside; SIGTERM/SIGINT stop the daemon.
    caplog.set_level(logging.INFO, logger="galois_edge.main")
    recorder = _StopRecorder()
    with open(os.devnull) as stdin:
        monkeypatch.setattr(sys, "stdin", stdin)
        await asyncio.wait_for(EdgeDaemon._watch_stdin(recorder), timeout=1.0)
    assert recorder.stops == 0
    assert "stdin watcher not active" in caplog.text


async def test_closing_a_stdin_pipe_stops_the_daemon(monkeypatch, caplog):
    # Go supervisor contract: it holds the write end of a pipe and closes it to shut down.
    caplog.set_level(logging.INFO, logger="galois_edge.main")
    read_fd, write_fd = os.pipe()
    recorder = _StopRecorder()
    with os.fdopen(read_fd) as stdin:
        monkeypatch.setattr(sys, "stdin", stdin)
        watcher = asyncio.ensure_future(EdgeDaemon._watch_stdin(recorder))
        os.close(write_fd)
        await asyncio.wait_for(watcher, timeout=1.0)
    assert recorder.stops == 1
    assert "Stdin closed (EOF)" in caplog.text


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


# ---------------------------------------------------------------------------
# EQ2: stop() ends in-flight discovery; nothing on the I/O thread outlives it.
# ---------------------------------------------------------------------------

PORTS = tuple(f"PRLGX-ASRL::/dev/ttyS{n}::INTFC" for n in (31, 30, 29))


class _DeniedVisa:
    """A PyVISA ResourceManager listing permission-denied /dev/ttyS* Prologix ports."""

    def __init__(self, on_open=lambda: None) -> None:
        self.opens = 0
        self.on_open = on_open

    def list_resources(self, query="?*"):
        return PORTS

    def open_resource(self, address):
        self.opens += 1
        self.on_open()
        raise PermissionError(13, "Permission denied", address)


@pytest.fixture
def no_host_io(monkeypatch):
    # Hermetic (spec §10): never enumerate host VISA/USB devices or real serial ports.
    monkeypatch.setattr("galois_edge.instrument_manager.PYVISA_AVAILABLE", False)
    monkeypatch.setattr("galois_edge.instrument_manager.USB_AVAILABLE", False)


@pytest.fixture
def daemon(tmp_path, no_host_io):
    d = EdgeDaemon(Config(
        gpib_enabled=False, usb_monitor_enabled=False, lan_instruments="", include_serial_ports=False,
        profile_dir=str(tmp_path / "bundled"), dynamic_profile_dir=str(tmp_path / "dynamic"),
        driver_profile_dir=str(tmp_path / "drivers"), mcp_enabled=False, demo=False, sim_mode=False,
        visa_backend="", trace_dir=""))
    d._running = True   # as if start() had run; stop() is a no-op otherwise
    yield d
    d._io_executor.shutdown(wait=False, cancel_futures=True)


@pytest.mark.parametrize("cancel_during_first_attempt", [True, False])
def test_connect_makes_no_further_attempt_once_cancelled(no_host_io, cancel_during_first_attempt):
    cancel = threading.Event()
    if not cancel_during_first_attempt:
        cancel.set()                    # stop requested before connect() began: open nothing
    mgr = InstrumentManager(gpib_enabled=False, usb_raw_enabled=False)
    mgr._rm = _DeniedVisa(on_open=cancel.set)
    started = time.monotonic()
    assert mgr.connect(PORTS[0], max_attempts=3, retry_delay=30.0, cancel=cancel) is None
    assert mgr._rm.opens == (1 if cancel_during_first_attempt else 0)
    assert time.monotonic() - started < 1.0   # the 30 s retry wait ended as soon as cancel was set


async def test_stop_during_a_connect_retry_ends_discovery_and_the_io_thread(daemon):
    entered = threading.Event()
    seen = {}

    def on_open():
        seen["io_thread"] = threading.current_thread()
        entered.set()

    daemon._instrument_manager = daemon._build_instrument_manager()
    daemon._instrument_manager._rm = _DeniedVisa(on_open)
    daemon._capability_manager = CapabilityManager()
    discovery = asyncio.ensure_future(daemon._background_profile_match())
    assert await asyncio.to_thread(entered.wait, 1.0)   # attempt 1 of 3 (2 s apart) is in flight

    await asyncio.wait_for(daemon.stop(), timeout=1.5)

    assert not seen["io_thread"].is_alive()   # nothing left to keep the interpreter alive at exit
    assert daemon._instrument_manager._rm.opens == 1   # no retry, no further port
    await asyncio.wait_for(discovery, timeout=1.0)


def test_no_profile_matching_starts_once_stopping(daemon):
    # Every discovery path (initial GPIB scan, trickle, reconcile, hotplug) goes through
    # _try_match_profile; once stop() has begun it connects nothing, even on a backend
    # whose connect() takes no cancel event.
    from tests.test_instrument_manager_backends import FakeBackend

    backend = FakeBackend(["SIM::1"])
    daemon._instrument_manager = InstrumentManager(gpib_enabled=False, usb_raw_enabled=False,
                                                   extra_backends=[backend])
    daemon._capability_manager = CapabilityManager()
    daemon._load_profiles()
    daemon._stop_event.set()
    daemon._try_match_profile("SIM::1")
    assert backend.calls == []
    assert daemon._capability_manager.get_instrument_caps("SIM::1") is None


@pytest.fixture
async def stuck_io_call(daemon):
    """The I/O thread is inside a call that never checks the stop event (e.g. a hung vendor driver)."""
    daemon._io_drain_timeout_s = 0.2
    entered, release = threading.Event(), threading.Event()

    def call():
        entered.set()
        release.wait(5.0)

    work = asyncio.get_running_loop().run_in_executor(daemon._io_executor, call)
    assert await asyncio.to_thread(entered.wait, 1.0)
    yield
    release.set()
    await work


async def test_stop_waits_only_a_bounded_time_for_a_stuck_io_call(daemon, stuck_io_call, caplog):
    await asyncio.wait_for(daemon.stop(), timeout=1.5)
    assert "Instrument I/O thread still busy" in caplog.text


async def test_a_second_stop_waits_for_the_shutdown_in_progress(daemon, stuck_io_call, caplog):
    # main() calls stop() again in its finally as soon as the gRPC server has
    # terminated; that call must not return (and the loop close) mid-shutdown.
    caplog.set_level(logging.INFO, logger="galois_edge.main")
    first = asyncio.ensure_future(daemon.stop())        # e.g. the SIGTERM handler
    await asyncio.sleep(0)                              # yield: the first call starts shutting down
    await asyncio.wait_for(daemon.stop(), timeout=1.5)  # e.g. main()'s finally
    assert "Edge daemon stopped." in caplog.text
    await asyncio.wait_for(first, timeout=1.0)


async def test_a_trickle_scanner_started_during_the_io_drain_is_stopped(daemon, monkeypatch):
    # The initial GPIB scan is still on the I/O thread when stop() stops the trickle scanner
    # (step 1) and cancels its task (1c). It finishes while stop() drains the I/O thread and
    # then starts the trickle scanner; that one must not outlive stop() either.
    entered, release = threading.Event(), threading.Event()

    class _Gpib:   # a GPIBManager with one board, mid full-bus scan
        is_available = True
        boards = {0: None}

        def scan_all_boards(self):
            entered.set()
            release.wait(5.0)
            return []

        def disconnect_all(self):
            pass

    class _Server:   # stop() reaches step 2 (WebSocket server) after steps 1 and 1c
        async def stop(self):
            release.set()

    monkeypatch.setattr(daemon, "_cfg", dataclasses.replace(daemon._cfg, gpib_trickle_interval_s=60.0))
    daemon._instrument_manager = daemon._build_instrument_manager()
    monkeypatch.setattr(daemon._instrument_manager, "_gpib", _Gpib())
    daemon._ws_server = _Server()
    scan = asyncio.ensure_future(daemon._initial_gpib_scan_then_trickle())
    assert await asyncio.to_thread(entered.wait, 1.0)

    await asyncio.wait_for(daemon.stop(), timeout=1.5)
    await asyncio.wait_for(scan, timeout=1.0)

    assert daemon._trickle_task is not None   # the scan did start one, mid-shutdown
    assert daemon._trickle_task.done()
    assert not daemon._trickle_scanner.running
