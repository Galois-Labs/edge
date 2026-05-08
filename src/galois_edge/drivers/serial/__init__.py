"""Serial driver protocol package.

Self-registers the existing :class:`GenericSerialDriver` with
:class:`DriverRegistry` at import time.
"""

from __future__ import annotations

from galois_edge.drivers.registry import DriverRegistry
from galois_edge.drivers.serial_driver import GenericSerialDriver
from galois_edge.drivers.serial_transport import SerialBusManager


DriverRegistry.register(
    "serial",
    GenericSerialDriver,
    bus_manager_factory=SerialBusManager,
)


__all__ = ["GenericSerialDriver", "SerialBusManager"]
