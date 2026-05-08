"""SPI protocol driver package — self-registers on import.

Per Phase 4 of the multi-protocol-driver-pipeline-spec, each protocol
package self-registers with ``DriverRegistry`` at import time. The
existing registry in ``drivers/registry.py`` is instance-based and does
not yet expose a class-level ``register`` method; this package therefore
guards the registration call with ``hasattr(DriverRegistry, 'register')``
so the import is a no-op until Phase 0 / Phase F land the registry
refactor described in section 7.5 of the spec.

Public surface:

- :class:`GenericSpiDriver` — profile-driven SPI driver.
- :class:`SPIBusManager` — bus handle pool with capability gating.
- :class:`MockSPI` — non-Linux test fake.
- :func:`is_available` — capability probe.
"""

from __future__ import annotations

from galois_edge.drivers.registry import DriverRegistry

from .driver import GenericSpiDriver
from .transport import MockSPI, SPIBusManager, is_available

__all__ = [
    "GenericSpiDriver",
    "SPIBusManager",
    "MockSPI",
    "is_available",
]


# Phase-0-compatible self-registration: only fires once the registry
# exposes a class-level ``register`` hook. Today's registry is
# instance-based, so this is intentionally a no-op until the foundation
# refactor lands. See spec section 7.5.
if hasattr(DriverRegistry, "register"):
    DriverRegistry.register("spi", GenericSpiDriver, SPIBusManager())  # type: ignore[attr-defined]
