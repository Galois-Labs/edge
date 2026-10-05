"""DEMO_MODE virtual instruments as an InstrumentBackend (contracts/edge-api.md §2).

Adapts contrib.simulation.engine.SimulatedInstrumentManager with identical
behaviour. The contrib import (formerly main.py:263) lives here, so edge's
core never imports contrib unless DEMO_MODE asks for it.
"""

from __future__ import annotations

from typing import Optional

#: Formerly main.py PROFILE_OVERRIDES: the generic Quantifi profile pattern is too broad.
DEMO_PROFILE_HINTS = {
    "TCPIP::192.168.1.10::5025::SOCKET": "quantifi_photonics_laser_1000",
    "TCPIP::192.168.1.11::5025::SOCKET": "quantifi_photonics_switch",
    "TCPIP::192.168.1.12::5025::SOCKET": "quantifi_photonics_voa",
    "TCPIP::192.168.1.13::5025::SOCKET": "quantifi_photonics_power_1400",
    "TCPIP::192.168.1.14::5025::SOCKET": "quantifi_photonics_osa_1000",
}


class DemoBackend:
    name = "demo"
    simulated = True

    def __init__(self, manager: object | None = None) -> None:
        if manager is None:
            from contrib.simulation.engine import SimulatedInstrumentManager  # ImportError ⇒ caller warns
            manager = SimulatedInstrumentManager()
        self._sim = manager
        self._addresses = frozenset(manager.list_resources())

    def handles(self, address: str) -> bool: return address in self._addresses
    def list_resources(self) -> tuple[str, ...]: return tuple(self._sim.list_resources())
    def connect(self, address: str, timeout: int = 5000) -> Optional[str]: return self._sim.connect(address, timeout=timeout)
    def disconnect(self, address: str) -> None: self._sim.disconnect(address)
    def is_connected(self, address: str) -> bool: return self._sim.is_connected(address)
    def write(self, address: str, command: str) -> None: self._sim.write(address, command)
    def read(self, address: str) -> str: return self._sim.read(address)
    def query(self, address: str, command: str) -> str: return self._sim.query(address, command)
    def query_raw(self, address: str, command: str) -> bytes: return self._sim.query_raw(address, command)

    def read_binary(self, address: str, num_bytes: int) -> bytes:
        raise ValueError(f"Binary (raw) reads are not supported on this transport: {address}")

    def query_binary_values(self, address: str, command: str, datatype: str = "d", is_big_endian: bool = False,
                            container: type = list, timeout_ms: Optional[int] = None) -> list:
        return self._sim.query_binary_values(address, command, datatype=datatype, is_big_endian=is_big_endian,
                                             container=container, timeout_ms=timeout_ms)

    def identify(self, address: str) -> str: return self._sim.identify(address)
    def profile_hint(self, address: str) -> Optional[str]: return DEMO_PROFILE_HINTS.get(address)
    def profile_dirs(self) -> tuple[str, ...]: return ()
    def close(self) -> None: self._sim.disconnect_all()
