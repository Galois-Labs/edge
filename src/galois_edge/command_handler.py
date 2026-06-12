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

from .waveform_assembly import (
    IEEEBlockError,
    compose_block_scaling,
    decode_block_samples,
    decode_ieee_block,
)

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

    def execute_binary_query(
        self,
        scpi_cmd: str,
        instrument_id: str,
        datatype: str = 'd',
        is_big_endian: bool = False,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> Dict[str, Any]:
        """Execute a SCPI query that returns IEEE 488.2 binary block data.

        Uses the same per-instrument locking as ``execute_command()``.

        Args:
            scpi_cmd: The SCPI query string (e.g. ``":WAV:DATA?"``).
            instrument_id: Target instrument identifier (VISA address).
            datatype: Format character for ``struct``: ``'d'`` = float64,
                ``'f'`` = float32, ``'h'`` = int16, etc.
            is_big_endian: If True, data is big-endian.
            timeout_ms: Maximum time (in ms) to wait.

        Returns:
            A dict with keys:

            * ``success`` (bool) -- whether the query succeeded.
            * ``data`` (list) -- the decoded numeric values, or ``[]``
              on failure.
            * ``error`` (str) -- an error message, or empty on success.
            * ``execution_time_ms`` (float) -- wall-clock time spent.
        """
        lock = self._get_lock(instrument_id)
        with lock:
            return self._execute_binary_locked(
                scpi_cmd, instrument_id, datatype, is_big_endian, timeout_ms,
            )

    def execute_binary_block_query(
        self,
        scpi_cmd: str,
        instrument_id: str,
        binary_config: Any,
        preamble_scpi: Optional[str] = None,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        command_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Execute a query whose response is an IEEE 488.2 definite-length
        block (``returns: {type: binary, format: ieee_block}``).

        Routes through ``InstrumentManager.query_raw()`` — the text
        ``query()`` path corrupts arbitrary binary payloads at decode
        and terminates early on any ``0x0A`` byte inside the block, so
        binary must never touch it.

        When the profile declares a preamble (e.g. DSOX3000
        ``:WAVeform:PREamble?``), it is queried first over the text path
        and its CSV fields are mapped by index per ``preamble_map``;
        the SCPI reference-point composition (doc §2.4) is applied in
        code by ``waveform_assembly.compose_block_scaling``.

        Uses the same per-instrument locking as ``execute_command()``
        so the preamble query and block read form one uninterrupted
        SCPI transaction.

        Args:
            scpi_cmd: The formatted SCPI query (e.g. ``":WAVeform:DATA?"``).
            instrument_id: Target instrument identifier (VISA address).
            binary_config: ``profile_schema.BinaryConfig`` (dtype,
                byte order, optional preamble command + index map).
            preamble_scpi: Optional pre-resolved preamble SCPI string
                (use ``InstrumentProfile.resolve_scpi_ref`` when
                ``binary.preamble_command`` names a sibling command).
                Defaults to ``binary_config.preamble_command`` as-is.
            timeout_ms: Maximum time (in ms) to wait.
            command_id: Optional caller-supplied identifier for logging.

        Returns:
            A dict with keys:

            * ``success`` (bool)
            * ``response`` (str) -- a short summary on success.
            * ``error`` (str) -- error message, or empty on success.
            * ``execution_time_ms`` (float)
            * ``block`` (dict, success only) -- ``{y_data
              (little-endian bytes), y_dtype (wire dtype), y_length,
              x_start, x_increment, y_scale, y_offset}`` ready for
              ``VectorData`` population
              (``waveform_assembly.vector_data_from_block``).
              ``y_scale`` is never 0.

            Malformed blocks return ``success=False`` with a message —
            never raise, never return partial data (doc §2.2 rule 4).
        """
        lock = self._get_lock(instrument_id)
        with lock:
            return self._execute_binary_block_locked(
                scpi_cmd, instrument_id, binary_config,
                preamble_scpi, timeout_ms, command_id,
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _execute_binary_block_locked(
        self,
        scpi_cmd: str,
        instrument_id: str,
        binary_config: Any,
        preamble_scpi: Optional[str],
        timeout_ms: int,
        command_id: Optional[str],
    ) -> Dict[str, Any]:
        """Run the IEEE-block query while holding the per-instrument lock."""
        tag = f"[{command_id}] " if command_id else ""
        dtype = getattr(binary_config, "dtype", "uint8") or "uint8"
        byte_order = getattr(binary_config, "byte_order", "little") or "little"
        logger.info(
            "%sBinary block query '%s' on %s (dtype=%s, byte_order=%s, "
            "timeout=%dms)",
            tag, scpi_cmd, instrument_id, dtype, byte_order, timeout_ms,
        )

        start = time.monotonic()

        def _fail(message: str) -> Dict[str, Any]:
            elapsed_ms = (time.monotonic() - start) * 1000.0
            logger.error("%s%s", tag, message)
            return {
                "success": False,
                "response": "",
                "error": message,
                "execution_time_ms": round(elapsed_ms, 2),
            }

        try:
            # Ensure the instrument is connected.
            if not self._instruments.is_connected(instrument_id):
                connected = self._instruments.connect(
                    instrument_id, timeout=timeout_ms
                )
                if not connected:
                    return _fail(
                        f"Cannot connect to instrument: {instrument_id}"
                    )

            self._try_set_timeout(instrument_id, timeout_ms)

            # --- Preamble (text path) + reference-point composition ---
            mapped_values: Dict[str, float] = {}
            preamble_cmd = preamble_scpi or getattr(
                binary_config, "preamble_command", None
            )
            preamble_map = getattr(binary_config, "preamble_map", None)
            if preamble_cmd:
                preamble_raw = self._instruments.query(
                    instrument_id, preamble_cmd
                )
                if preamble_map is not None:
                    index_map = (
                        preamble_map.to_index_dict()
                        if hasattr(preamble_map, "to_index_dict")
                        else dict(preamble_map)
                    )
                    parts = [p.strip() for p in preamble_raw.split(",")]
                    for field, idx in index_map.items():
                        if idx < 0 or idx >= len(parts):
                            raise IEEEBlockError(
                                f"preamble index {idx} for '{field}' out of "
                                f"range ({len(parts)} fields in "
                                f"{preamble_raw!r})"
                            )
                        try:
                            mapped_values[field] = float(parts[idx])
                        except ValueError:
                            raise IEEEBlockError(
                                f"non-numeric preamble field '{field}' at "
                                f"index {idx}: {parts[idx]!r}"
                            )
            scaling = compose_block_scaling(mapped_values)

            # --- Block read (raw byte path — never the text path) ---
            raw = self._instruments.query_raw(instrument_id, scpi_cmd)
            payload = decode_ieee_block(raw)
            y_data, y_length, wire_dtype = decode_block_samples(
                payload, dtype, byte_order
            )

            elapsed_ms = (time.monotonic() - start) * 1000.0
            summary = f"<binary block: {y_length} {wire_dtype} samples>"
            logger.info(
                "%sBinary block query completed in %.1fms: %s",
                tag, elapsed_ms, summary,
            )

            return {
                "success": True,
                "response": summary,
                "error": "",
                "execution_time_ms": round(elapsed_ms, 2),
                "block": {
                    "y_data": y_data,
                    "y_dtype": wire_dtype,
                    "y_length": y_length,
                    **scaling,
                },
            }

        except IEEEBlockError as exc:
            return _fail(f"Malformed binary block: {exc}")

        except TimeoutError as exc:
            return _fail(f"Timeout after {timeout_ms}ms: {exc}")

        except Exception as exc:
            # Catch-all for VISA errors, transport errors, etc. — the
            # poll loop must never crash on a bad read (doc §2.2).
            return _fail(f"Binary block query error: {exc}")

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

    def _execute_binary_locked(
        self,
        scpi_cmd: str,
        instrument_id: str,
        datatype: str,
        is_big_endian: bool,
        timeout_ms: int,
    ) -> Dict[str, Any]:
        """Run a binary query while holding the per-instrument lock."""
        logger.info(
            "Binary query '%s' on %s (dtype=%s, big_endian=%s, timeout=%dms)",
            scpi_cmd, instrument_id, datatype, is_big_endian, timeout_ms,
        )

        start = time.monotonic()

        try:
            # Ensure the instrument is connected.
            if not self._instruments.is_connected(instrument_id):
                connected = self._instruments.connect(
                    instrument_id, timeout=timeout_ms
                )
                if not connected:
                    return {
                        "success": False,
                        "data": [],
                        "error": f"Cannot connect to instrument: {instrument_id}",
                        "execution_time_ms": round(
                            (time.monotonic() - start) * 1000, 2
                        ),
                    }

            data = self._instruments.query_binary_values(
                instrument_id,
                scpi_cmd,
                datatype=datatype,
                is_big_endian=is_big_endian,
                timeout_ms=timeout_ms,
            )

            elapsed_ms = (time.monotonic() - start) * 1000.0
            logger.info(
                "Binary query completed in %.1fms: %d values",
                elapsed_ms, len(data),
            )

            return {
                "success": True,
                "data": data,
                "error": "",
                "execution_time_ms": round(elapsed_ms, 2),
            }

        except TimeoutError as exc:
            elapsed_ms = (time.monotonic() - start) * 1000.0
            logger.error("Binary query timeout after %dms: %s", timeout_ms, exc)
            return {
                "success": False,
                "data": [],
                "error": f"Timeout after {timeout_ms}ms: {exc}",
                "execution_time_ms": round(elapsed_ms, 2),
            }

        except Exception as exc:
            elapsed_ms = (time.monotonic() - start) * 1000.0
            logger.error("Binary query error: %s", exc)
            return {
                "success": False,
                "data": [],
                "error": f"Binary query error: {exc}",
                "execution_time_ms": round(elapsed_ms, 2),
            }

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
