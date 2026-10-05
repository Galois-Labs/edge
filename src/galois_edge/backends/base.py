"""InstrumentBackend: the pluggable-backend Protocol (contracts/edge-api.md §2).

This is a copy of the edgesim contract (``contracts/python/edge_instrument_backend.py``);
a critical test keeps them identical (``tests/test_backend_protocol.py``). edge's runtime never
imports from the edgesim submodule, so the Protocol lives here as well.

Semantics (normative text is in the contract):
- Addresses are VISA-style resource strings. ``handles()`` is the routing predicate: the first
  registered backend whose ``handles()`` returns True owns the address, and InstrumentManager's
  built-in GPIB/USB/VISA routing runs only when no extra backend claims it.
- Method names and defaults mirror InstrumentManager, so the manager delegates 1:1.
- ``simulated`` is a class attribute: True for every backend whose instruments are not real
  hardware (edge's DemoBackend, edgesim's EdgeSimBackend).
- ``profile_hint()`` names a profile key to use instead of ``*IDN?`` regex matching.
- ``profile_dirs()`` lists profile directories edge's ProfileLoader scans after the bundled dir
  and before the dynamic (deployed) dir; ``()`` when edge's own profiles cover the backend.
- There is no ``set_timeout``; per-call timeouts travel as arguments.
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable


@runtime_checkable
class InstrumentBackend(Protocol):
    name: str
    simulated: bool  # class attribute; see module docstring

    def handles(self, address: str) -> bool: ...
    def list_resources(self) -> tuple[str, ...]: ...
    def connect(self, address: str, timeout: int = 5000) -> Optional[str]:
        """Return the canonical instrument id (usually the address) or None on failure."""
    def disconnect(self, address: str) -> None: ...
    def is_connected(self, address: str) -> bool: ...
    def write(self, address: str, command: str) -> None: ...
    def read(self, address: str) -> str: ...
    def query(self, address: str, command: str) -> str: ...
    def query_raw(self, address: str, command: str) -> bytes: ...
    def read_binary(self, address: str, num_bytes: int) -> bytes: ...
    def query_binary_values(
        self,
        address: str,
        command: str,
        datatype: str = "d",
        is_big_endian: bool = False,
        container: type = list,
        timeout_ms: Optional[int] = None,
    ) -> list: ...
    def identify(self, address: str) -> str: ...
    def profile_hint(self, address: str) -> Optional[str]:
        """Profile key (e.g. 'galois_sim-psu-2') for an owned address, or None to fall back to *IDN? matching."""
    def profile_dirs(self) -> tuple[str, ...]:
        """Absolute paths of profile directories edge must scan for this backend's instruments; () if none."""
    def close(self) -> None: ...
