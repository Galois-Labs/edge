"""
SCPI Command Handler.

Routes raw SCPI commands to the correct instrument via an injected
InstrumentManager. Handles query vs. write detection, timeout
enforcement, and error wrapping.

The handler is protocol-agnostic: it receives SCPI strings and an
instrument identifier, and returns a uniform result dictionary. The
gRPC server and WebSocket server both delegate to this handler.
"""

from __future__ import annotations

import logging
import time
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Default timeout for SCPI commands (milliseconds).
DEFAULT_TIMEOUT_MS = 5000


class CommandHandler:
    """Executes SCPI commands on instruments.

    The handler does NOT import ``InstrumentManager`` directly; it
    receives one via constructor injection. This keeps the module
    decoupled and testable with mocks.

    Thread safety: each command execution acquires a per-instrument
    lock so that only one SCPI transaction is in flight per instrument
    at a time. This matches GPIB bus semantics (one talker/listener
    pair active on the bus).
    """

    def __init__(self, instrument_manager: Any) -> None:
        """Initialise the command handler.

        Args:
            instrument_manager: An object that exposes ``is_connected``,
                ``connect``, ``query``, and ``write`` methods matching
                the ``InstrumentManager`` interface.
        """
        self._instruments = instrument_manager
        # Per-instrument locks to serialise SCPI access.
        self._locks: Dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def execute_command(
        self,
        scpi_cmd: str,
        instrument_id: str,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        command_id: Optional[str] = None,
        force_query: bool = False,
    ) -> Dict[str, Any]:
        """Execute a single SCPI command on an instrument.

        Query vs. write detection: if the (stripped) command string ends
        with ``?`` it is treated as a query and a response is read back.
        Otherwise it is treated as a write and no read is attempted.
        The ``force_query`` flag overrides this heuristic.

        Args:
            scpi_cmd: The SCPI command string (e.g. ``*IDN?`` or
                ``*RST``).
            instrument_id: Target instrument identifier (typically the
                VISA address).
            timeout_ms: Maximum time (in ms) to wait for the command to
                complete. Defaults to 5000 ms.
            command_id: Optional caller-supplied identifier for logging
                and correlation.
            force_query: When ``True``, always read a response even if
                the command does not end with ``?``. Useful for non-SCPI
                instruments whose query commands lack the ``?`` suffix.

        Returns:
            A dict with three keys:

            * ``success`` (bool) -- whether the command succeeded.
            * ``response`` (str) -- the instrument response (queries)
              or ``"OK"`` (writes).
            * ``error`` (str) -- an error message, or empty string on
              success.
            * ``execution_time_ms`` (float) -- wall-clock time spent.
        """
        lock = self._get_lock(instrument_id)
        with lock:
            return self._execute_locked(
                scpi_cmd, instrument_id, timeout_ms, command_id, force_query
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _execute_locked(
        self,
        scpi_cmd: str,
        instrument_id: str,
        timeout_ms: int,
        command_id: Optional[str],
        force_query: bool,
    ) -> Dict[str, Any]:
        """Run the command while holding the per-instrument lock."""
        tag = f"[{command_id}] " if command_id else ""
        logger.info(
            "%sExecuting '%s' on %s (timeout=%dms)",
            tag, scpi_cmd, instrument_id, timeout_ms,
        )

        start = time.monotonic()

        try:
            # Ensure the instrument is connected.
            if not self._instruments.is_connected(instrument_id):
                connected = self._instruments.connect(
                    instrument_id, timeout=timeout_ms
                )
                if not connected:
                    return self._error_result(
                        f"Cannot connect to instrument: {instrument_id}",
                        start,
                    )

            # Apply the timeout to the underlying VISA resource if the
            # instrument manager exposes a set_timeout helper; otherwise
            # we rely on the default configured on the resource.
            self._try_set_timeout(instrument_id, timeout_ms)

            # Determine query vs. write.
            is_query = force_query or scpi_cmd.strip().endswith("?")

            if is_query:
                response = self._instruments.query(instrument_id, scpi_cmd)
            else:
                self._instruments.write(instrument_id, scpi_cmd)
                response = "OK"

            elapsed_ms = (time.monotonic() - start) * 1000.0
            logger.info(
                "%sCommand completed in %.1fms: %s",
                tag, elapsed_ms, _truncate(response, 120),
            )

            return {
                "success": True,
                "response": response,
                "error": "",
                "execution_time_ms": round(elapsed_ms, 2),
            }

        except TimeoutError as exc:
            return self._error_result(
                f"Timeout after {timeout_ms}ms: {exc}", start
            )

        except Exception as exc:
            # Catch-all for VISA errors, connection errors, etc.
            return self._error_result(
                f"Command error: {exc}", start
            )

    def _get_lock(self, instrument_id: str) -> threading.Lock:
        """Return (or create) a per-instrument lock."""
        with self._locks_guard:
            if instrument_id not in self._locks:
                self._locks[instrument_id] = threading.Lock()
            return self._locks[instrument_id]

    def _try_set_timeout(self, instrument_id: str, timeout_ms: int) -> None:
        """Attempt to set the VISA timeout on the instrument resource.

        If the instrument manager does not expose a ``set_timeout``
        method this is a silent no-op.
        """
        setter = getattr(self._instruments, "set_timeout", None)
        if setter is not None and callable(setter):
            try:
                setter(instrument_id, timeout_ms)
            except Exception:
                # Non-critical; the default timeout will apply.
                pass

    @staticmethod
    def _error_result(
        message: str, start: float
    ) -> Dict[str, Any]:
        """Build an error result dict."""
        elapsed_ms = (time.monotonic() - start) * 1000.0
        logger.error(message)
        return {
            "success": False,
            "response": "",
            "error": message,
            "execution_time_ms": round(elapsed_ms, 2),
        }

    def release_lock(self, instrument_id: str) -> None:
        """Remove the per-instrument lock when an instrument is removed.

        This is optional cleanup; locks are lightweight and can be left
        in place without harm.
        """
        with self._locks_guard:
            self._locks.pop(instrument_id, None)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _truncate(text: str, max_len: int = 120) -> str:
    """Truncate a string for log display."""
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."
