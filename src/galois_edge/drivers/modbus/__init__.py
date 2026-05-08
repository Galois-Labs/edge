"""Modbus driver protocol package.

Self-registers the existing :class:`GenericModbusDriver` with
:class:`DriverRegistry` at import time.  The module-level driver and bus
manager files (``modbus_driver.py``, ``modbus_transport.py``) keep their
existing locations so direct imports in tests and call-sites still work.
"""

from __future__ import annotations

from galois_edge.drivers.modbus_driver import GenericModbusDriver
from galois_edge.drivers.modbus_transport import ModbusBusManager
from galois_edge.drivers.registry import DriverRegistry


def _modbus_kwargs_filter(kwargs: dict) -> dict:
    """Pass through Modbus-specific kwargs.

    Modbus' :class:`GenericModbusDriver` accepts an optional ``slave_id``
    kwarg on construction; the ConnectInstrument path forwards it from
    the request's ``connection_params``.
    """
    return kwargs


DriverRegistry.register(
    "modbus",
    GenericModbusDriver,
    bus_manager_factory=ModbusBusManager,
    extra_kwargs_filter=_modbus_kwargs_filter,
)


__all__ = ["GenericModbusDriver", "ModbusBusManager"]
