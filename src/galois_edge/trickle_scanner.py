"""
GPIB trickle address scanner.

Probes one GPIB address at a time with configurable inter-probe delay,
yielding the I/O executor to command traffic between probes. Achieves
full bus coverage (~30 addresses) in approximately 60 seconds without
ever blocking commands for more than ~1 second.

Architecture:
  - Runs as an asyncio Task in the event loop.
  - Each probe is submitted to the shared single-thread ``_io_executor``
    via ``loop.run_in_executor()``.
  - Before submitting a probe, checks the executor's work queue. If
    commands are pending, the trickle scanner yields (skips this cycle)
    to avoid delaying user commands.
  - Cycles through all boards in round-robin, probing addresses 1-30
    on each board before moving to the next.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .gpib_manager import GPIBManager

logger = logging.getLogger(__name__)

# GPIB primary address range
_ADDR_START = 1
_ADDR_END = 30


class TrickleScanScheduler:
    """Background GPIB address trickle scanner.

    Probes one GPIB address per cycle, sleeping ``interval_s`` seconds
    between probes. When a new instrument is discovered, the
    ``on_instrument_found`` callback is invoked with the VISA address.

    Parameters
    ----------
    gpib_manager:
        The GPIBManager instance to probe through.
    io_executor:
        The shared single-thread I/O executor. Probes are submitted
        here to maintain the thread-safety invariant.
    interval_s:
        Seconds to sleep between probe cycles. Default 2.0.
    on_instrument_found:
        Callback invoked (in the event loop) when a new instrument is
        found. Receives the VISA address string.
    probe_timeout_code:
        linux-gpib timeout code for probes. If not set, the default
        in ``scan_single_address()`` (T1s) is used.
    """

    def __init__(
        self,
        gpib_manager: GPIBManager,
        io_executor: ThreadPoolExecutor,
        interval_s: float = 2.0,
        on_instrument_found: Optional[Callable[[str], None]] = None,
        probe_timeout_code: Optional[int] = None,
    ) -> None:
        self._gpib_manager = gpib_manager
        self._io_executor = io_executor
        self._interval_s = interval_s
        self._on_instrument_found = on_instrument_found
        self._probe_timeout_code = probe_timeout_code

        # Scan state
        self._boards_to_scan: list[int] = sorted(
            gpib_manager.boards.keys()
        )
        self._current_board_idx: int = 0
        self._current_addr: int = _ADDR_START
        self._running: bool = False

        # Cycle counter for logging
        self._probes_done: int = 0
        self._instruments_found: int = 0

    @property
    def running(self) -> bool:
        return self._running

    async def run(self) -> None:
        """Main trickle scan loop.

        Runs until ``stop()`` is called or the task is cancelled.
        """
        self._running = True
        logger.info(
            "Trickle scanner started: %d board(s), interval=%.1fs, "
            "addresses %d-%d",
            len(self._boards_to_scan),
            self._interval_s,
            _ADDR_START,
            _ADDR_END,
        )

        if not self._boards_to_scan:
            logger.warning("Trickle scanner: no GPIB boards to scan")
            self._running = False
            return

        loop = asyncio.get_running_loop()

        while self._running:
            try:
                await asyncio.sleep(self._interval_s)
            except asyncio.CancelledError:
                break

            if not self._running:
                break

            # Priority yielding: skip this cycle if commands are queued
            if self._executor_busy():
                logger.debug("Trickle scanner: executor busy, yielding")
                continue

            # Refresh board list in case of hotplug
            self._boards_to_scan = sorted(
                self._gpib_manager.boards.keys()
            )
            if not self._boards_to_scan:
                continue

            # Ensure board index is valid
            if self._current_board_idx >= len(self._boards_to_scan):
                self._current_board_idx = 0

            board = self._boards_to_scan[self._current_board_idx]
            addr = self._current_addr

            # Submit ONE probe to the I/O executor
            try:
                kwargs = {"board": board, "addr": addr}
                if self._probe_timeout_code is not None:
                    kwargs["timeout_code"] = self._probe_timeout_code

                visa_addr = await loop.run_in_executor(
                    self._io_executor,
                    lambda: self._gpib_manager.scan_single_address(**kwargs),
                )

                self._probes_done += 1

                if visa_addr is not None:
                    self._instruments_found += 1
                    logger.info(
                        "Trickle scanner: new instrument at %s "
                        "(probe #%d, total found: %d)",
                        visa_addr,
                        self._probes_done,
                        self._instruments_found,
                    )
                    if self._on_instrument_found is not None:
                        self._on_instrument_found(visa_addr)
                else:
                    logger.debug(
                        "Trickle probe GPIB%d::%d -> no instrument "
                        "(probe #%d)",
                        board, addr, self._probes_done,
                    )

            except Exception as exc:
                logger.warning(
                    "Trickle scan probe error at GPIB%d::%d: %s",
                    board, addr, exc,
                )

            # Advance position
            self._advance()

        self._running = False
        logger.info(
            "Trickle scanner stopped after %d probes, %d instruments found",
            self._probes_done,
            self._instruments_found,
        )

    async def stop(self) -> None:
        """Signal the trickle scanner to stop."""
        self._running = False

    def reset(self, board: Optional[int] = None) -> None:
        """Restart scanning from address 1.

        Parameters
        ----------
        board:
            If specified, reset to scan this specific board from
            address 1. Otherwise, restart from the beginning of
            the board rotation.
        """
        self._current_addr = _ADDR_START
        if board is not None and board in self._boards_to_scan:
            self._current_board_idx = self._boards_to_scan.index(board)
        else:
            self._current_board_idx = 0
            # Refresh board list
            self._boards_to_scan = sorted(
                self._gpib_manager.boards.keys()
            )
        logger.info(
            "Trickle scanner reset (board=%s, addr=%d)",
            board if board is not None else "all",
            self._current_addr,
        )

    def _advance(self) -> None:
        """Advance to the next address/board in round-robin order."""
        self._current_addr += 1
        if self._current_addr > _ADDR_END:
            self._current_addr = _ADDR_START
            self._current_board_idx += 1
            if self._current_board_idx >= len(self._boards_to_scan):
                self._current_board_idx = 0

    def _executor_busy(self) -> bool:
        """Check if the I/O executor has pending work items.

        Uses ThreadPoolExecutor._work_queue (CPython internal but stable
        since Python 3.2). Returns False if the attribute is unavailable.
        """
        try:
            queue = getattr(self._io_executor, "_work_queue", None)
            if queue is not None:
                return queue.qsize() > 0
        except Exception:
            pass
        return False
