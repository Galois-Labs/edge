"""Central parameter validation for profile commands (contracts/edge-api.md §4).

Decoding follows contracts/semantics.md §3.3 so that edge and edgesim's session
parser accept and reject the same inputs. Every command surface (gRPC
ExecuteCommand, the stream paths, static MCP execute_command, dynamic MCP
tools) reaches this module through CapabilityManager.resolve_command, so they
reject identical inputs with identical messages.
"""

from __future__ import annotations

import re
from typing import Any, Optional

DATA_TYPE_ERROR = -104
MISSING_PARAMETER = -109
DATA_OUT_OF_RANGE = -222
ILLEGAL_PARAMETER_VALUE = -224


class ParamValidationError(ValueError):
    """A command parameter failed validation. ``str(exc) == exc.message``."""

    def __init__(self, field: str, message: str, code: int = ILLEGAL_PARAMETER_VALUE) -> None:
        super().__init__(message)
        self.field = field
        self.message = message
        self.code = code


_SI = {
    "EX": 1e18, "PE": 1e15, "T": 1e12, "G": 1e9, "MA": 1e6, "K": 1e3,
    "M": 1e-3, "U": 1e-6, "N": 1e-9, "P": 1e-12, "F": 1e-15, "A": 1e-18,
}
_MEGA_UNITS = {"MHZ": 1e6, "MOHM": 1e6}  # SCPI: "M" means mega only in these two
_NUMBER = re.compile(r"^\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*([A-Za-z/%]*)\s*$")
_KEYWORDS = {"MIN": "MIN", "MINIMUM": "MIN", "MAX": "MAX", "MAXIMUM": "MAX", "DEF": "DEF", "DEFAULT": "DEF"}
_BOOL = {"ON": True, "OFF": False, "1": True, "0": False, "TRUE": True, "FALSE": False}


def decode_float(text: str, unit: Optional[str]) -> float:
    """Number with an optional SI multiplier and unit suffix. Raises ValueError."""
    m = _NUMBER.match(text)
    if m is None:
        raise ValueError(f"not a number: {text!r}")
    value = float(m.group(1))
    suffix = m.group(2).upper()
    if not suffix:
        return value
    u = (unit or "").upper()
    if suffix in _MEGA_UNITS and u and suffix == "M" + u:
        return value * _MEGA_UNITS[suffix]
    if u and suffix == u:
        return value
    if u and suffix.endswith(u):
        prefix = suffix[: -len(u)]
        if prefix in _SI:
            return value * _SI[prefix]
        raise ValueError(f"unknown multiplier {prefix!r} in {text!r}")
    if suffix in _SI:
        return value * _SI[suffix]
    raise ValueError(f"unknown suffix {m.group(2)!r} in {text!r}")


def _bound(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _check_range(name: str, pc: Any, value: Any) -> None:
    lo, hi = _bound(pc.min), _bound(pc.max)
    if (lo is not None and value < lo) or (hi is not None and value > hi):
        lo_s = "-inf" if pc.min is None else repr(pc.min)
        hi_s = "inf" if pc.max is None else repr(pc.max)
        raise ParamValidationError(name, f"{name}: {value!r} is out of range [{lo_s}, {hi_s}]", DATA_OUT_OF_RANGE)


def _type_error(name: str, expected: str, raw: Any) -> ParamValidationError:
    return ParamValidationError(name, f"{name}: expected {expected}, got {raw!r}", DATA_TYPE_ERROR)


def _keyword_value(name: str, pc: Any, keyword: str) -> float:
    source = {"MIN": pc.min, "MAX": pc.max, "DEF": pc.default}[keyword]
    if source is None:
        raise ParamValidationError(name, f"{name}: {keyword} is not defined for this parameter")
    return float(source)


def _num_text(raw: Any) -> str:
    if isinstance(raw, float) and raw.is_integer():
        return str(int(raw))
    return str(raw)


def _short_form(option: str) -> str:
    if not any(c.islower() for c in option):
        return option
    return "".join(c for c in option if c.isupper() or c.isdigit())


def _same(mapped: Any, text: str) -> bool:
    try:
        return float(mapped) == float(text)
    except (TypeError, ValueError):
        return str(mapped).upper() == text.upper()


def _decode_enum(name: str, pc: Any, raw: Any) -> str:
    options = list(pc.options or [])
    if isinstance(raw, bool):
        text = "1" if raw else "0"
    elif isinstance(raw, (int, float)):
        text = _num_text(raw)
    elif isinstance(raw, str):
        text = raw.strip()
    else:
        raise _type_error(name, "enum", raw)
    up = text.upper()
    for option in options:
        if option.upper() == up:
            return option
    for option in options:
        if _short_form(option).upper() == up:
            return option
    for option in options:
        if pc.map and option in pc.map and _same(pc.map[option], text):
            return option
    raise ParamValidationError(name, f"{name}: {raw!r} is not one of {options!r}")


def decode_value(name: str, pc: Any, raw: Any) -> Any:
    """Decode and check one declared parameter value (semantics.md §3.3)."""
    ptype = (pc.type or "string").lower()
    if ptype == "float":
        if isinstance(raw, bool):
            raise _type_error(name, "float", raw)
        if isinstance(raw, (int, float)):
            value = float(raw)
        elif isinstance(raw, str):
            keyword = _KEYWORDS.get(raw.strip().upper())
            if keyword is not None:
                value = _keyword_value(name, pc, keyword)
            else:
                try:
                    value = decode_float(raw, pc.unit)
                except ValueError:
                    raise _type_error(name, "float", raw) from None
        else:
            raise _type_error(name, "float", raw)
        _check_range(name, pc, value)
        return value
    if ptype == "int":
        if isinstance(raw, bool):
            raise _type_error(name, "int", raw)
        if isinstance(raw, int):
            value = raw
        elif isinstance(raw, float) and raw.is_integer():
            value = int(raw)
        elif isinstance(raw, str):
            text = raw.strip()
            try:
                value = int(text, 10)
            except ValueError:
                try:
                    f = float(text)
                except ValueError:
                    raise _type_error(name, "int", raw) from None
                if not f.is_integer():
                    raise _type_error(name, "int", raw) from None
                value = int(f)
        else:
            raise _type_error(name, "int", raw)
        _check_range(name, pc, value)
        return value
    if ptype == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)) and raw in (0, 1):
            return bool(raw)
        if isinstance(raw, str) and raw.strip().upper() in _BOOL:
            return _BOOL[raw.strip().upper()]
        raise ParamValidationError(name, f"{name}: {raw!r} is not a boolean (ON|OFF|1|0|TRUE|FALSE)")
    if ptype == "enum":
        return _decode_enum(name, pc, raw)
    # string
    if isinstance(raw, str):
        text = raw.strip()
        if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
            return text[1:-1]
        return raw
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return str(raw)
    raise _type_error(name, "string", raw)
