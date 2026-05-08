"""
Protocol drivers for non-SCPI instruments.

Provides a YAML-interpreted driver framework for industrial protocols
(Modbus, CAN, Serial, future: SPI/I2C/OPC-UA) that coexists with the
daemon's existing SCPI profile system.

Importing this package triggers each protocol's registration with
:class:`DriverRegistry`.  The integration agent (Phase F) appends new
protocol-package imports to the "Phase 1+ protocols" block as they ship.
"""

from galois_edge.drivers.point import Point
from galois_edge.drivers.base import BaseProtocolDriver

# Currently-shipping protocols.  Each module's import side effect is a
# single ``DriverRegistry.register(...)`` call.
from galois_edge.drivers import can  # noqa: F401
from galois_edge.drivers import modbus  # noqa: F401
from galois_edge.drivers import serial  # noqa: F401

# Phase 1+ protocols register here as they ship:
# from galois_edge.drivers import i2c    # noqa: F401
# from galois_edge.drivers import opcua  # noqa: F401
# from galois_edge.drivers import spi    # noqa: F401

# Re-export legacy names so existing call-sites (tests, grpc_server) keep
# working without churn.  These are convenience re-exports; new code
# should import from the protocol package or via DriverRegistry.
from galois_edge.drivers.can import CANBusManager, GenericCANDriver  # noqa: E402

__all__ = [
    "Point",
    "BaseProtocolDriver",
    "CANBusManager",
    "GenericCANDriver",
]
