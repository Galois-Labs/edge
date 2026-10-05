"""
Unit tests for the GPIB trickle address scanner.

Tests use a mock GPIBManager to verify:
  - Correct board/address round-robin rotation
  - Callback invocation on instrument discovery
  - Priority yielding when executor is busy
  - Reset behaviour
  - Graceful stop
"""

from __future__ import annotations

import asyncio
import inspect
import sys
import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

# Ensure source tree is importable
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "src")
)

from galois_edge.trickle_scanner import TrickleScanScheduler


async def _until(predicate, timeout: float = 5.0, step: float = 0.005) -> None:
    """Await *predicate()* instead of sleeping a fixed time (spec §10 rule 5)."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"condition not met within {timeout}s")
        await asyncio.sleep(step)


def _sleeping_between_probes(task: asyncio.Task) -> bool:
    """True while TrickleScanScheduler.run() awaits its inter-probe sleep.

    run() reads the next address only after that sleep, so a reset() made now
    applies to the next probe. While run() awaits the executor instead, a probe
    is in flight and its _advance() overwrites the reset.
    """
    awaiting = task.get_coro().cr_await
    return inspect.iscoroutine(awaiting) and awaiting.__name__ == "sleep"


# ---------------------------------------------------------------------------
# Mock GPIBManager
# ---------------------------------------------------------------------------


class MockGPIBManager:
    """Minimal mock of GPIBManager for trickle scanner tests."""

    def __init__(
        self,
        boards: Optional[dict] = None,
        respond_at: Optional[set] = None,
    ) -> None:
        """
        Parameters
        ----------
        boards:
            Dict of board_index -> object (simulates _boards).
            Pass an empty dict for "no boards" scenario.
        respond_at:
            Set of (board, addr) tuples that should return a VISA address
            when probed.
        """
        self._boards = boards if boards is not None else {0: MagicMock()}
        self._respond_at = respond_at or set()
        self.probed: list[tuple[int, int]] = []

    @property
    def boards(self) -> dict:
        return self._boards

    def scan_single_address(
        self,
        board: int,
        addr: int,
        timeout_code: int = 11,
    ) -> Optional[str]:
        """Mock probe: returns a VISA string if (board, addr) is in respond_at."""
        self.probed.append((board, addr))
        if (board, addr) in self._respond_at:
            return f"GPIB{board}::{addr}::INSTR"
        return None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.fixture
def io_executor():
    """A single-thread executor for tests."""
    executor = ThreadPoolExecutor(max_workers=1)
    yield executor
    executor.shutdown(wait=False)


@pytest.mark.asyncio
async def test_trickle_scanner_probes_all_addresses(io_executor):
    """Verify the scanner cycles through all 30 addresses on a board."""
    gpib = MockGPIBManager(boards={0: MagicMock()})
    found = []

    scanner = TrickleScanScheduler(
        gpib_manager=gpib,
        io_executor=io_executor,
        interval_s=0.01,  # fast for testing
        on_instrument_found=lambda addr: found.append(addr),
    )

    # Run until one full cycle (30+ probes) has been covered
    task = asyncio.create_task(scanner.run())
    await _until(lambda: {a for _, a in gpib.probed} >= set(range(1, 31)))
    await scanner.stop()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # Should have probed at least 30 addresses (one full cycle)
    assert len(gpib.probed) >= 30

    # All addresses 1-30 should have been probed
    probed_addrs = {addr for _, addr in gpib.probed}
    expected_addrs = set(range(1, 31))
    assert probed_addrs == expected_addrs


@pytest.mark.asyncio
async def test_trickle_scanner_finds_instrument(io_executor):
    """Verify callback is called when an instrument is discovered."""
    gpib = MockGPIBManager(
        boards={0: MagicMock()},
        respond_at={(0, 5)},
    )
    found = []

    scanner = TrickleScanScheduler(
        gpib_manager=gpib,
        io_executor=io_executor,
        interval_s=0.01,
        on_instrument_found=lambda addr: found.append(addr),
    )

    task = asyncio.create_task(scanner.run())
    await _until(lambda: "GPIB0::5::INSTR" in found)
    await scanner.stop()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # Should have found the instrument at address 5
    assert "GPIB0::5::INSTR" in found


@pytest.mark.asyncio
async def test_trickle_scanner_multiple_boards(io_executor):
    """Verify round-robin across multiple boards."""
    gpib = MockGPIBManager(
        boards={0: MagicMock(), 1: MagicMock()},
        respond_at={(1, 1)},
    )
    found = []

    scanner = TrickleScanScheduler(
        gpib_manager=gpib,
        io_executor=io_executor,
        interval_s=0.01,
        on_instrument_found=lambda addr: found.append(addr),
    )

    task = asyncio.create_task(scanner.run())
    # Run until both boards have been covered
    await _until(lambda: "GPIB1::1::INSTR" in found and {b for b, _ in gpib.probed} >= {0, 1})
    await scanner.stop()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # Should have probed addresses on both boards
    boards_probed = {board for board, _ in gpib.probed}
    assert 0 in boards_probed
    assert 1 in boards_probed

    # Should have found the instrument on board 1
    assert "GPIB1::1::INSTR" in found


@pytest.mark.asyncio
async def test_trickle_scanner_reset(io_executor):
    """Verify reset restarts from address 1."""
    gpib = MockGPIBManager(boards={0: MagicMock()})
    scanner = TrickleScanScheduler(
        gpib_manager=gpib,
        io_executor=io_executor,
        interval_s=0.01,
    )

    task = asyncio.create_task(scanner.run())
    # Reset after at least one probe, while run() sleeps between probes (a
    # reset during an in-flight probe is undone by that probe's _advance()).
    await _until(lambda: len(gpib.probed) >= 1 and _sleeping_between_probes(task))

    # Reset should work without error
    scanner.reset()

    # Bound the wait by probe count, not time: without a working reset the
    # next probes continue at address 2 and up, so address 1 is not re-probed.
    probes_at_reset = len(gpib.probed)
    await _until(lambda: len(gpib.probed) >= probes_at_reset + 3)
    await scanner.stop()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # After reset, should continue probing (address 1 should appear
    # more than once since we reset)
    addr_1_count = sum(1 for _, addr in gpib.probed if addr == 1)
    assert addr_1_count >= 2


@pytest.mark.asyncio
async def test_trickle_scanner_no_boards(io_executor):
    """Verify scanner handles empty board list gracefully."""
    gpib = MockGPIBManager(boards={})

    scanner = TrickleScanScheduler(
        gpib_manager=gpib,
        io_executor=io_executor,
        interval_s=0.01,
    )

    task = asyncio.create_task(scanner.run())
    # Wait for run() itself to return. `not scanner.running` is already true
    # before the task first runs, so it cannot be the wait condition.
    await _until(task.done)

    # Should have exited run() since no boards were available at init
    assert task.done() or not scanner.running

    # Clean up
    await scanner.stop()
    if not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_trickle_scanner_stop(io_executor):
    """Verify stop() terminates the scanner."""
    gpib = MockGPIBManager(boards={0: MagicMock()})

    scanner = TrickleScanScheduler(
        gpib_manager=gpib,
        io_executor=io_executor,
        interval_s=0.01,
    )

    task = asyncio.create_task(scanner.run())
    # Wait for the task to actually start running
    await _until(lambda: scanner.running)
    assert scanner.running

    await scanner.stop()

    # Wait for run() itself to return. stop() clears `running` synchronously,
    # so `not scanner.running` would hold even if run() ignored the stop.
    await _until(task.done)
    assert not scanner.running

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_trickle_scanner_executor_busy_yields(io_executor):
    """Verify the scanner skips probes when the executor is busy."""
    gpib = MockGPIBManager(boards={0: MagicMock()})

    scanner = TrickleScanScheduler(
        gpib_manager=gpib,
        io_executor=io_executor,
        interval_s=0.01,
    )

    # Occupy the executor with a blocking task
    import time
    blocker = io_executor.submit(time.sleep, 0.3)

    task = asyncio.create_task(scanner.run())
    await asyncio.sleep(0.1)

    # While executor is busy, few or no probes should complete
    probes_while_busy = len(gpib.probed)

    # Wait for blocker to finish, then for probes to resume
    await asyncio.wrap_future(blocker)
    await _until(lambda: len(gpib.probed) > probes_while_busy)

    # After blocker finishes, probes should resume
    probes_after = len(gpib.probed)
    assert probes_after > probes_while_busy

    await scanner.stop()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
