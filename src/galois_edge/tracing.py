"""edgesim.trace/1 writer for real-hardware commands (contracts/edge-api.md §5).

Record encoding follows contracts/semantics.md §11 and
contracts/schemas/trace-v1.schema.json. The data types here are shared by
capability_manager (which builds CommandContext and returns ResolvedSCPI) and
command_handler (which emits CommandEvent to observers).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Tuple


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
