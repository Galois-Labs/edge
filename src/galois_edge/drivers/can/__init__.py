"""CAN driver protocol package.

Self-registers the existing :class:`GenericCANDriver` with
:class:`DriverRegistry` at import time.
"""

from __future__ import annotations

from galois_edge.drivers.can_driver import GenericCANDriver
from galois_edge.drivers.can_transport import CANBusManager
from galois_edge.drivers.registry import DriverRegistry


DriverRegistry.register(
    "can",
    GenericCANDriver,
    bus_manager_factory=CANBusManager,
)


__all__ = ["GenericCANDriver", "CANBusManager"]
