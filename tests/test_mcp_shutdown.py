"""F3: shutting the daemon down leaves no MCP task pending for loop.close() to destroy.

Every daemon shutdown that had served an MCP SSE response logged, after "Edge daemon stopped.",

    asyncio - ERROR - Task was destroyed but it is pending!
    task: <Task pending ... coro=<_shutdown_watcher() running at .../sse_starlette/sse.py:140> ...>

sse-starlette starts one ``_shutdown_watcher`` task per thread on the first SSE response. It
polls for uvicorn's ``should_exit`` only while ``serve()`` runs (it finds the server through the
SIGTERM handler uvicorn installs), so once the server has stopped it sleeps forever and
``main()``'s ``loop.close()`` destroys it. These tests run main()'s loop sequence in-process
(new loop, run_until_complete, close) on loopback with port 0.
"""
from __future__ import annotations

import asyncio
import gc
import logging
from pathlib import Path
from typing import Any, List

import pytest

from galois_edge.config import Config
from galois_edge.main import EdgeDaemon
from galois_edge.mcp.server import MCPServer

pytestmark = pytest.mark.critical


@pytest.fixture(autouse=True)
def _loopback_without_proxy(monkeypatch):
    """httpx honours *_proxy variables; the MCP client talks to 127.0.0.1 directly (spec §10 rule 6)."""
    for var in ("no_proxy", "NO_PROXY"):
        monkeypatch.setenv(var, "127.0.0.1")


async def _one_mcp_session(url: str) -> None:
    """initialize + a tool call: both answered as SSE streams, which starts sse-starlette's watcher."""
    from mcp.client import streamable_http
    from mcp.client.session import ClientSession

    # streamable_http_client is the current name; older mcp 1.x only has streamablehttp_client.
    client = getattr(streamable_http, "streamable_http_client", None) or streamable_http.streamablehttp_client
    async with client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("get_status", {})
            assert result.isError is False


def _pending_tasks() -> List[str]:
    """Other pending tasks on this loop, as reprs: holding the tasks would keep them from collection."""
    current = asyncio.current_task()
    return [repr(t) for t in asyncio.all_tasks() if t is not current and not t.done()]


def _asyncio_errors(caplog) -> List[str]:
    return [r.getMessage() for r in caplog.records if r.name == "asyncio" and r.levelno >= logging.ERROR]


def _run_like_main(main_coro_factory, cleanup_coro_factory=None) -> Any:
    """main()'s loop handling: a fresh loop, run_until_complete, then close and collect."""
    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(main_coro_factory())
    finally:
        if cleanup_coro_factory is not None:
            loop.run_until_complete(cleanup_coro_factory())
        loop.close()
    gc.collect()   # a pending task dropped by close() is reported when it is collected
    return result


def test_mcp_server_stop_ends_the_tasks_it_spawned(caplog, mock_capability_manager, mock_command_handler,
                                                   mock_instrument_manager):
    caplog.set_level(logging.WARNING)
    server = MCPServer(capability_manager=mock_capability_manager, command_handler=mock_command_handler,
                       instrument_manager=mock_instrument_manager, host="127.0.0.1", port=0, path="/mcp",
                       dynamic_tools_max=200, mark_simulated=False, sim_control_tools=False)

    async def serve_then_stop() -> List[str]:
        await server.start()
        await _one_mcp_session(f"http://127.0.0.1:{server.port}/mcp")
        await server.stop()
        return _pending_tasks()

    left = _run_like_main(serve_then_stop)

    assert left == []
    assert _asyncio_errors(caplog) == []


@pytest.fixture
def mcp_daemon(tmp_path: Path, monkeypatch):
    """A hermetic EdgeDaemon with MCP on: loopback, port 0, no host instrument I/O, no stdin watcher."""
    monkeypatch.setattr("galois_edge.instrument_manager.PYVISA_AVAILABLE", False)
    monkeypatch.setattr("galois_edge.instrument_manager.USB_AVAILABLE", False)
    monkeypatch.setattr("galois_edge.main._configure_logging", lambda level: None)   # keeps caplog's handler
    daemon = EdgeDaemon(Config(
        gpib_enabled=False, usb_monitor_enabled=False, lan_instruments="", include_serial_ports=False,
        profile_dir=str(tmp_path / "bundled"), dynamic_profile_dir=str(tmp_path / "dynamic"),
        driver_profile_dir=str(tmp_path / "drivers"), demo=False, sim_mode=False, visa_backend="",
        trace_dir="", grpc_port=0, ws_port=0, mcp_port=0, mcp_enabled=True, grpc_bind_host="127.0.0.1",
        ws_bind_host="127.0.0.1", mcp_bind_host="127.0.0.1"))
    started = []

    async def watch_stdin(self) -> None:   # start() creates this task last, once every server is up
        started[0].set()

    monkeypatch.setattr(EdgeDaemon, "_watch_stdin", watch_stdin)
    return daemon, started


def test_daemon_shutdown_after_an_mcp_session_logs_no_destroyed_task(caplog, mcp_daemon):
    caplog.set_level(logging.INFO)
    daemon, started = mcp_daemon
    seen: List[List[str]] = []

    async def drive() -> None:
        await started[0].wait()
        await _one_mcp_session(f"http://127.0.0.1:{daemon._mcp_server.port}/mcp")
        await daemon.stop()                       # what the stdin watcher does on EOF

    async def start_and_drive() -> None:
        started.append(asyncio.Event())
        driver = asyncio.ensure_future(drive())
        await daemon.start()                      # returns once stop() has ended gRPC
        await driver
        seen.append(_pending_tasks())

    _run_like_main(start_and_drive, daemon.stop)  # main()'s finally: stop() again, then close()

    assert "Edge daemon stopped." in caplog.text
    assert seen == [[]]
    assert _asyncio_errors(caplog) == []
