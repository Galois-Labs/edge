"""edgesim.trace/1 writer for real-hardware commands (contracts/edge-api.md §5).

Record encoding follows contracts/semantics.md §11 and
contracts/schemas/trace-v1.schema.json. The data types here are shared by
capability_manager (which builds CommandContext and returns ResolvedSCPI) and
command_handler (which emits CommandEvent to observers).

M1 scope (plan CI-24): only commands that pass through CommandHandler are
traced. SDK (sdk_executor) and protocol-driver (Modbus, CAN, ...) commands
never reach CommandHandler, so they produce no transition.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import platform
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from .validation import argument_placeholders, coerce_response


@dataclass(frozen=True)
class WriteTarget:
    """One ``writes:`` entry of a leaf, resolved against the profile's state schema (CI-6).

    ``path`` is the state path (may already end in an explicit ``[k]``);
    ``index_name``/``index_min``/``index_max`` describe its governing ``_index``.
    """

    path: str
    index_name: Optional[str] = None
    index_min: Optional[int] = None
    index_max: Optional[int] = None
    state_type: Optional[str] = None          # bool | int | float | enum | string
    options: Optional[Tuple[str, ...]] = None


@dataclass(frozen=True)
class CommandContext:
    """How a profile command was resolved; carried on ResolvedSCPI (CI-5)."""

    instrument_id: str
    path: str                                  # leaf path (== v1 name for v1 profiles)
    params: Mapping[str, Any]                  # validated, JSON-safe
    command: Any                               # profile_schema.CommandConfig
    is_query: bool
    form: str                                  # getter | setter | query | write | none
    writes: Tuple[WriteTarget, ...] = ()


class ResolvedSCPI(str):
    """A formatted SCPI string that remembers how it was resolved. Behaves exactly as ``str``."""

    context: Optional[CommandContext]

    def __new__(cls, text: str, context: Optional[CommandContext] = None) -> "ResolvedSCPI":
        obj = super().__new__(cls, text)
        obj.context = context
        return obj

    def __reduce__(self):
        return (str, (str(self),))


@dataclass(frozen=True)
class CommandEvent:
    """One executed command, as seen by CommandHandler observers."""

    instrument_id: str
    scpi: str                                  # wire string, terminator stripped
    context: Optional[CommandContext]
    is_query: bool
    success: bool
    response: Optional[str]                    # text response of a query; None for writes/binary/failure
    response_bytes: Optional[bytes]            # raw IEEE block or packed float64 values
    error: str
    t_wall_ns: int                             # wall clock at command start
    latency_ns: int
    timed_out: bool = False
    simulated: bool = False                    # owning backend is simulated (CI-8)

    @property
    def kind(self) -> str:
        return "command" if self.context is not None else "scpi"


def json_safe(value: Any) -> Any:
    """Make *value* JSON-encodable per semantics.md §11.5 (NaN/±inf as strings)."""
    if isinstance(value, bool) or value is None or isinstance(value, (int, str)):
        return value
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        return value
    if isinstance(value, Mapping):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return str(value)


# ---------------------------------------------------------------------------
# TraceWriter (edge-api.md §5; semantics.md §11)
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)
SCHEMA = "edgesim.trace/1"


def _edge_version() -> str:
    try:
        from importlib.metadata import version
        return version("galois-edge")
    except Exception:
        return "0.0.0+unknown"


def _short_form(option: str) -> str:
    if not any(c.islower() for c in option):
        return option
    return "".join(c for c in option if c.isupper() or c.isdigit())


_BOOLS = {"ON": True, "OFF": False, "TRUE": True, "FALSE": False, "1": True, "0": False}


def _coerce_state(value: Any, target: WriteTarget) -> Any:
    """semantics.md §3.4 auto-store coercion; raises ValueError."""
    t = target.state_type
    if t is None:
        return value
    if t == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        if isinstance(value, str) and value.strip().upper() in _BOOLS:
            return _BOOLS[value.strip().upper()]
        raise ValueError(value)
    if t == "int":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            value = float(value)
        return int(round(value))
    if t == "float":
        return float(value)
    if t == "enum":
        text = str(value).strip().upper()
        for option in target.options or ():
            if option.upper() == text or _short_form(option).upper() == text:
                return option
        raise ValueError(value)
    return str(value)


def _delta(ctx: CommandContext) -> Dict[str, List[Any]]:
    """``{canonical key: [None, value]}`` for a setter whose leaf declares ``writes`` (CI-6)."""
    if ctx.form not in ("setter", "write") or not ctx.writes:
        return {}
    template = ctx.command.setter if ctx.form == "setter" else ctx.command.scpi
    args = argument_placeholders(template or "")
    if len(args) != 1 or args[0] not in ctx.params:
        return {}
    value = ctx.params[args[0]]
    pc = (getattr(ctx.command, "params", None) or {}).get(args[0])
    if pc is not None and pc.map and str(value) in pc.map:
        value = pc.map[str(value)]
    target = ctx.writes[0]
    try:
        value = _coerce_state(value, target)
    except (TypeError, ValueError, OverflowError):
        return {}
    if target.index_name is None:
        keys = [target.path]
    elif target.index_name in ctx.params:
        try:
            index = int(ctx.params[target.index_name])
        except (TypeError, ValueError, OverflowError):
            # An index that does not bind (e.g. 'CH1') drops only the delta,
            # never the transition (one transition per executed command).
            return {}
        keys = [f"{target.path}[{index}]"]
    elif target.index_min is not None and target.index_max is not None:
        keys = [f"{target.path}[{i}]" for i in range(target.index_min, target.index_max + 1)]
    else:
        keys = [target.path]
    return {k: [None, value] for k in keys}


class TraceWriter:
    """Writes one ``<trace_dir>/<run_id>.jsonl`` per daemon run (edge-api.md §5).

    ``start()`` writes ``run_start``; ``observe(event)`` (a CommandHandler
    observer) writes one ``transition`` per executed command and never
    raises; ``close()`` writes ``run_end``. ``start`` and ``close`` are
    idempotent, and events after ``close`` are ignored.
    """

    def __init__(self, trace_dir: "str | os.PathLike[str]", *, run_id: Optional[str] = None,
                 clock_ns: Callable[[], int] = time.time_ns, engine_version: Optional[str] = None) -> None:
        self._dir = Path(trace_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._clock_ns = clock_ns
        self.run_id = run_id or (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(clock_ns() / 1e9))
                                 + "-" + uuid.uuid4().hex[:8])
        self.path = self._dir / f"{self.run_id}.jsonl"
        self._engine_version = engine_version or _edge_version()
        self._lock = threading.Lock()
        self._fh = open(self.path, "a", encoding="utf-8")
        self._seq = 0
        self._t0_ns: Optional[int] = None
        self._closed = False
        self._write_failed = False

    def _io_failed(self, what: str, exc: OSError) -> None:
        """Warn on the first trace I/O failure, debug thereafter (Review Focus 5)."""
        log = logger.debug if self._write_failed else logger.warning
        log("trace %s under %s failed: %s", what, self._dir, exc)
        self._write_failed = True

    def _write(self, record: Dict[str, Any]) -> None:
        line = json.dumps(json_safe(record), separators=(",", ":"), allow_nan=False, ensure_ascii=False)
        try:
            self._fh.write(line + "\n")
            self._fh.flush()
        except OSError as exc:
            self._io_failed(f"write to {self.path.name}", exc)

    def _start_locked(self) -> None:
        if self._t0_ns is not None:
            return
        self._t0_ns = self._clock_ns()
        self._write({"schema": SCHEMA, "kind": "run_start", "run_id": self.run_id, "t_wall_ns": self._t0_ns,
                     "provenance": "real", "engine": {"name": "galois-edge", "version": self._engine_version},
                     "arch": platform.machine() or "unknown", "instruments": {}})

    def start(self) -> None:
        with self._lock:
            if not self._closed:
                self._start_locked()

    def _blob(self, data: bytes) -> Dict[str, Any]:
        sha = hashlib.sha256(data).hexdigest()
        blobs = self._dir / "blobs"
        blobs.mkdir(exist_ok=True)
        target = blobs / f"{sha}.bin"
        if not target.exists():
            tmp = blobs / f".{sha}.{uuid.uuid4().hex}.tmp"
            tmp.write_bytes(data)
            os.replace(tmp, target)
        return {"kind": "blob", "uri": f"blobs/{sha}.bin", "sha256": sha, "nbytes": len(data)}

    def observe(self, event: CommandEvent) -> None:
        try:
            ctx = event.context
            action: Dict[str, Any] = {"kind": event.kind, "raw": event.scpi}
            if ctx is not None:
                action["path"] = ctx.path
                action["params"] = dict(ctx.params)
            observation: Dict[str, Any] = {"errors": [], "latency_ns": max(int(event.latency_ns), 0)}
            if event.response_bytes is not None:
                observation["response"] = None
                try:
                    observation["data_ref"] = self._blob(event.response_bytes)
                except OSError as exc:
                    self._io_failed("blob write", exc)
            else:
                observation["response"] = event.response
                if ctx is not None and event.is_query and event.response is not None:
                    returns = getattr(ctx.command, "returns", None)
                    text = returns.parse_response(event.response) if returns is not None else event.response
                    typed = coerce_response(returns, text)
                    if typed is not None:
                        observation["typed"] = typed
            delta = _delta(ctx) if (ctx is not None and event.success) else {}
            status = "ok" if event.success else ("timeout" if event.timed_out else "error")
            with self._lock:
                if self._closed:
                    return
                self._start_locked()
                record = {"schema": SCHEMA, "kind": "transition", "run_id": self.run_id, "seq": self._seq,
                          "t_virtual_ns": max(event.t_wall_ns - self._t0_ns, 0), "t_wall_ns": event.t_wall_ns,
                          "instrument_id": event.instrument_id, "action": action, "delta": delta,
                          "observation": observation, "fidelity": "exact", "status": status}
                if event.simulated:
                    # CI-8: a simulated backend's instrument overrides run_start's "real";
                    # absent means inherit (trace-v1 transition.provenance).
                    record["provenance"] = "sim"
                self._seq += 1
                self._write(record)
        except Exception:
            logger.exception("trace observe failed")

    def close(self, status: str = "ok") -> None:
        with self._lock:
            if self._closed:
                return
            self._start_locked()
            self._write({"schema": SCHEMA, "kind": "run_end", "run_id": self.run_id, "seq": self._seq,
                         "t_wall_ns": self._clock_ns(), "status": status})
            self._closed = True
            try:
                self._fh.close()
            except OSError:
                pass
