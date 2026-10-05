"""tests/e2e: process-level end-to-end tests of the M1 success criteria (spec §1, §10 "Cross-repo E2E").

Every scenario runs real OS processes on loopback, the way a user or an agent meets them:
- `edgesim world serve` (a remote World on a Unix-domain socket);
- `edgesim serve` (SCPI socket listeners);
- the edge daemon, `python -m galois_edge`, in SIM_MODE against a remote World;
- the `galois-profiles` CLI.

Parallel safety (spec §10):
- every TCP port is 0, and each process reports the port it got (the daemon's log line, the address map);
- files live under per-test temp dirs, and HOME is one of them;
- seeds derive from the test node id;
- every wait is a deadline loop, and every process is stopped in the fixture's finally.

Tests here are `e2e` + `slow` (the collection hook below enforces both), so neither the critical tier nor
the default tier runs them. Run them
with `make test-e2e`. They need the sim extra (cargo + maturin build edgesim's Rust extension) and fail
loudly without it, as tests/sim does (edge-api.md §7).
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Optional

import pytest

try:
    import edgesim.remote  # noqa: F401
except ImportError as exc:  # never skip silently (edge-api.md §7)
    raise pytest.UsageError(
        "tests/e2e needs the sim extra: uv pip install -e '.[dev,test,sim]' with cargo + maturin on PATH"
    ) from exc

ROOT = Path(__file__).resolve().parents[2]
EDGESIM = ROOT / "third_party/edgesim"
CONTRACTS = EDGESIM / "contracts"
BENCHES = CONTRACTS / "examples/benches"
PSU_BENCH = BENCHES / "psu_resistor_dmm.bench.json"
SCOPE_BENCH = BENCHES / "awg_rc_scope.bench.yaml"
TRACE_SCHEMA = CONTRACTS / "schemas/trace-v1.schema.json"
BENCH_SCHEMA = CONTRACTS / "schemas/bench-topology.schema.json"

#: The venv running pytest also holds the console scripts (`edgesim`, `galois-profiles`).
BIN = Path(sys.executable).parent

STARTUP_S = 30.0
"""Bound for any process to report ready, and for the daemon to register its bench."""
SHUTDOWN_GRACE_S = 10.0
"""How long the Go supervisor waits after closing the daemon's stdin before it escalates
(`shutdownGrace`, internal/supervisor/supervisor.go). "Promptly" means within it."""
CLI_TIMEOUT_S = 60.0
POLL_S = 0.05
SOCKET_PATH_MAX = 100
"""Under the 107-byte Unix-domain socket limit on Linux (103 on macOS) that `edgesim world serve` checks."""

NO_VISA_BACKEND = "@none"
"""VISA_BACKEND naming a PyVISA wrapper that does not exist. InstrumentManager then logs "PyVISA
initialisation failed" and runs without PyVISA. With the default `@py`, discovery broadcasts a VXI-11
portmap query to 255.255.255.255 (pyvisa-py's TCPIP listing, without psutil). That breaks the loopback-only
rule (spec §10 rule 6). Every bench address belongs to the sim backend, so the daemon never needs PyVISA here."""


E2E_DIR = Path(__file__).resolve().parent


@pytest.hookimpl(tryfirst=True)   # before `-m` deselects anything
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark every test under this directory `e2e` and `slow`, even if its module forgot to. `make test-python`
    deselects only `slow`, so an unmarked module would otherwise spawn real daemons in the default tier.
    The hook sees the whole session's items, so it filters by path."""
    for item in items:
        if item.path.resolve().is_relative_to(E2E_DIR):
            for mark in (pytest.mark.e2e, pytest.mark.slow):
                if item.get_closest_marker(mark.name) is None:
                    item.add_marker(mark)


def node_seed(nodeid: str) -> int:
    """The bench seed for a test (spec §10 rule 4; the pytest plugin's derivation, semantics §10.1)."""
    return int.from_bytes(hashlib.blake2b(nodeid.encode(), digest_size=8).digest(), "little")


def console_script(name: str) -> str:
    path = BIN / name
    if not path.exists():
        raise pytest.UsageError(f"tests/e2e needs the `{name}` console script beside {sys.executable}")
    return str(path)


def base_env(home: Path) -> dict[str, str]:
    """A minimal environment: no ambient Galois or SIM_* variable reaches a child (spec §10 rule 7)."""
    env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT") if key in os.environ}
    env["HOME"] = str(home)
    env["PYTHONUNBUFFERED"] = "1"
    return env


def wait_until(predicate: Callable[[], Any], what: str, *, timeout: float = STARTUP_S,
               alive: Iterable["Proc"] = ()) -> Any:
    """Poll `predicate` until it returns a truthy value. Fails at the deadline, or as soon as a process in
    `alive` exits."""
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        for proc in alive:
            proc.check_alive(what)
        if time.monotonic() >= deadline:
            details = "".join(f"\n--- {proc.name} ---\n{proc.output_tail()}" for proc in alive)
            raise AssertionError(f"timed out after {timeout:.0f}s waiting for {what}{details}")
        time.sleep(POLL_S)


@dataclass
class Proc:
    """One child process. stdout and stderr go to files, so a chatty child can never block on a full pipe."""

    name: str
    popen: subprocess.Popen
    stdout: Path
    stderr: Path

    def output(self) -> str:
        return self.stdout.read_text(errors="replace") + self.stderr.read_text(errors="replace")

    def output_tail(self, chars: int = 4000) -> str:
        return self.output()[-chars:]

    def check_alive(self, waiting_for: str = "") -> None:
        rc = self.popen.poll()
        if rc is not None:
            raise AssertionError(f"{self.name} exited with {rc} while waiting for {waiting_for}:\n{self.output_tail()}")

    def first_line(self, timeout: float = STARTUP_S) -> str:
        """The first complete stdout line (the ready report of the edgesim hosts)."""
        def line() -> Optional[str]:
            text = self.stdout.read_text(errors="replace")
            return text.split("\n", 1)[0] if "\n" in text else None
        return wait_until(line, f"{self.name}'s first stdout line", timeout=timeout, alive=[self])

    def wait(self, timeout: float = SHUTDOWN_GRACE_S) -> int:
        try:
            return self.popen.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            raise AssertionError(f"{self.name} did not exit within {timeout:.0f}s:\n{self.output_tail()}") from None

    def terminate(self, timeout: float = SHUTDOWN_GRACE_S) -> int:
        """SIGTERM, the documented stop for the edgesim hosts, then wait for the exit code."""
        self.popen.send_signal(signal.SIGTERM)
        return self.wait(timeout)

    def close_stdin(self, timeout: float = SHUTDOWN_GRACE_S) -> tuple[int, float]:
        """Close stdin, the Go supervisor's graceful stop, and return (exit code, seconds to exit)."""
        assert self.popen.stdin is not None, f"{self.name} was not started with a stdin pipe"
        start = time.monotonic()
        self.popen.stdin.close()
        rc = self.wait(timeout)
        return rc, time.monotonic() - start


@dataclass
class World:
    proc: Proc
    socket: Path
    bench_id: str


@dataclass
class ScpiServer:
    proc: Proc
    address_map: dict[str, dict[str, str]]


@dataclass
class EdgeDaemon:
    proc: Proc
    mcp_url: str


class Processes:
    """Starts the E2E processes and stops every one that is still running, in reverse order."""

    def __init__(self, tmp_path: Path, nodeid: str, mktemp: Callable[[str], Path]) -> None:
        self._tmp = tmp_path
        self._mktemp = mktemp
        self.seed = node_seed(nodeid)
        self.home = tmp_path / "home"
        self.home.mkdir()
        self._procs: list[Proc] = []
        self._dirs: list[Path] = []

    # ---- generic ----------------------------------------------------------------------------------------
    def spawn(self, name: str, argv: list[str], *, env: Optional[dict[str, str]] = None,
              stdin_pipe: bool = False) -> Proc:
        cwd = self._tmp / name
        cwd.mkdir()
        stdout, stderr = cwd / "stdout.log", cwd / "stderr.log"
        with stdout.open("wb") as out, stderr.open("wb") as err:
            popen = subprocess.Popen(argv, cwd=cwd, env=env if env is not None else base_env(self.home),
                                     stdin=subprocess.PIPE if stdin_pipe else subprocess.DEVNULL,
                                     stdout=out, stderr=err)
        proc = Proc(name, popen, stdout, stderr)
        self._procs.append(proc)
        return proc

    def run(self, argv: list[str], *, timeout: float = CLI_TIMEOUT_S) -> subprocess.CompletedProcess:
        """A one-shot CLI process, bounded by `timeout`."""
        return subprocess.run(argv, cwd=self._tmp, env=base_env(self.home), capture_output=True, text=True,
                              timeout=timeout, check=False)

    def socket_path(self, name: str) -> Path:
        """A Unix-domain socket path in a per-test dir that fits the platform limit."""
        path = self._mktemp("s") / name
        if len(os.fsencode(str(path))) > SOCKET_PATH_MAX:   # a long basetemp: fall back to a short temp dir
            short = Path(tempfile.mkdtemp(prefix="e2e-"))
            self._dirs.append(short)
            path = short / name
        return path

    def close(self) -> None:
        for proc in reversed(self._procs):
            if proc.popen.poll() is None:
                proc.popen.terminate()
                try:
                    proc.popen.wait(timeout=SHUTDOWN_GRACE_S)
                except subprocess.TimeoutExpired:
                    proc.popen.kill()
                    proc.popen.wait(timeout=SHUTDOWN_GRACE_S)
            if proc.popen.stdin is not None:
                with contextlib.suppress(OSError):
                    proc.popen.stdin.close()
        for path in self._dirs:
            shutil.rmtree(path, ignore_errors=True)

    # ---- edgesim hosts ----------------------------------------------------------------------------------
    def world_serve(self, bench: Path, *, trace_dir: Optional[Path] = None) -> World:
        """`edgesim world serve`: a remote World. Clock: the bench's. Ready when it prints its JSON line."""
        sock = self.socket_path("world.sock")
        argv = [console_script("edgesim"), "world", "serve", "--socket", str(sock), "--seed", str(self.seed)]
        if trace_dir is not None:
            argv += ["--trace-dir", str(trace_dir)]
        proc = self.spawn("world", [*argv, str(bench)])
        ready = json.loads(proc.first_line())
        assert ready["socket"] == str(sock), ready
        return World(proc, sock, ready["bench_id"])

    def scpi_serve(self, bench: Path) -> ScpiServer:
        """`edgesim serve --base-port 0`. Its first stdout line is the authoritative address map (semantics §8.3)."""
        proc = self.spawn("scpi", [console_script("edgesim"), "serve", "--base-port", "0",
                                   "--seed", str(self.seed), str(bench)])
        return ScpiServer(proc, json.loads(proc.first_line()))

    # ---- the edge daemon --------------------------------------------------------------------------------
    def edge_daemon(self, *, remote_socket: Path, trace_dir: Path) -> EdgeDaemon:
        """`python -m galois_edge` in SIM_MODE against a remote World, every server on port 0, scanning off.

        stdin is a pipe, as under the Go supervisor, so closing it stops the daemon. Ready when the MCP
        server logs the URL it listens on (the daemon's only port-0 read-back). The match needs the line's
        newline, so a read that catches the line half-written never yields a truncated URL."""
        env = base_env(self.home) | {
            "SIM_MODE": "true",
            "SIM_REMOTE_SOCKET": str(remote_socket),
            "SIM_MARK_INSTRUMENTS": "true",
            "GRPC_PORT": "0", "WS_PORT": "0", "MCP_PORT": "0", "MCP_ENABLED": "true",
            "GRPC_BIND_HOST": "127.0.0.1", "WS_BIND_HOST": "127.0.0.1", "MCP_BIND_HOST": "127.0.0.1",
            "TRACE_DIR": str(trace_dir),
            "GPIB_ENABLED": "false", "USB_MONITOR_ENABLED": "false", "LAN_INSTRUMENTS": "",
            "VISA_BACKEND": NO_VISA_BACKEND,
            "DEMO_MODE": "false",
            "DYNAMIC_PROFILE_DIR": str(self._tmp / "dynamic-profiles"),
            "DRIVER_PROFILE_DIR": str(self._tmp / "driver-profiles"),
            "LOG_LEVEL": "INFO",
        }
        proc = self.spawn("edge", [sys.executable, "-m", "galois_edge"], env=env, stdin_pipe=True)
        found = wait_until(lambda: re.search(r"MCP server listening on (http://127\.0\.0\.1:[1-9]\d*/\S*)\n",
                                             proc.output()),
                           "the edge daemon's MCP server", alive=[proc])
        assert "PyVISA initialisation failed" in proc.output(), "VISA scanning must be off (loopback only)"
        return EdgeDaemon(proc, found.group(1))


@pytest.fixture
def e2e(tmp_path, request, tmp_path_factory):
    """The test's process starter; every process still running at teardown is stopped (then killed)."""
    procs = Processes(tmp_path, request.node.nodeid, lambda prefix: tmp_path_factory.mktemp(prefix))
    try:
        yield procs
    finally:
        procs.close()


# ---- the agent side: an MCP streamable-HTTP client ---------------------------------------------------------
class Agent:
    """Calls edge's MCP tools the way an LLM agent does, and decodes their JSON results."""

    def __init__(self, session: Any) -> None:
        self.session = session

    async def call(self, tool: str, **arguments: Any) -> Any:
        result = await self.session.call_tool(tool, arguments)
        text = "".join(getattr(block, "text", "") for block in result.content)
        assert not result.isError, f"{tool}({arguments}) failed: {text}"
        structured = result.structuredContent
        if structured is not None:   # FastMCP wraps non-object returns as {"result": ...}
            return structured["result"] if list(structured) == ["result"] else structured
        return json.loads(text)

    async def execute(self, instrument_id: str, command: str, parameters: Optional[dict[str, str]] = None,
                      *, is_query: bool = False) -> dict[str, Any]:
        out = await self.call("execute_command", instrument_id=instrument_id, command_name=command,
                              parameters=parameters, is_query=is_query)
        assert out["success"], out
        return out

    async def wait_for_instruments(self, profiles: set[str], timeout: float = STARTUP_S) -> dict[str, dict]:
        """Poll list_instruments until every profile in `profiles` has a registered instrument.
        Returns {profile_name: instrument}."""
        deadline = time.monotonic() + timeout
        while True:
            listed = {i["profile_name"]: i for i in await self.call("list_instruments")}
            if profiles <= listed.keys():
                return listed
            if time.monotonic() >= deadline:
                raise AssertionError(f"after {timeout:.0f}s list_instruments has {sorted(listed)}, "
                                     f"not {sorted(profiles)}")
            await asyncio.sleep(POLL_S)


@contextlib.asynccontextmanager
async def mcp_agent(url: str) -> AsyncIterator[Agent]:
    """An initialized MCP session to edge over streamable HTTP. It ignores ambient proxies (loopback only)."""
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(STARTUP_S)) as http:
        async with streamable_http_client(url, http_client=http) as (read, write, _):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=STARTUP_S)) as session:
                await session.initialize()
                yield Agent(session)


def trace_records(trace_dir: Path) -> list[dict[str, Any]]:
    """The records of the single edgesim.trace/1 JSONL file in `trace_dir`."""
    files = sorted(trace_dir.glob("*.jsonl"))
    assert len(files) == 1, f"expected one trace file in {trace_dir}, found {[f.name for f in files]}"
    return [json.loads(line) for line in files[0].read_text().splitlines() if line.strip()]


def trace_validator():
    import jsonschema
    return jsonschema.Draft202012Validator(json.loads(TRACE_SCHEMA.read_text()))
