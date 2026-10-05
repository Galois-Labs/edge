"""Pluggable instrument backends checked before built-in GPIB/USB/VISA routing (edge-api.md §2)."""
from .base import InstrumentBackend

__all__ = ["InstrumentBackend"]
