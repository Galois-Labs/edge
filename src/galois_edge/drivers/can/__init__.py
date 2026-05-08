"""Production-grade CAN driver package.

Phase 1 of the multi-protocol driver pipeline (see
``docs/multi-protocol-driver-pipeline-spec.md``).  This package replaces
the legacy ``drivers/can_driver.py`` + ``drivers/can_transport.py`` once
Phase F integration moves the registry over.

Self-registration is intentionally guarded.  Phase 0 (foundation) adds a
``DriverRegistry.register`` classmethod for the registry/plugin pattern;
until then the legacy registry instantiates this package's classes via
the explicit ``protocol == "can"`` branch and we do not attempt to
register at import time.
"""

from __future__ import annotations

import logging

from galois_edge.drivers.can.driver import GenericCANDriver
from galois_edge.drivers.can.transport import CAN_AVAILABLE, CANBusManager

logger = logging.getLogger(__name__)

__all__ = ["GenericCANDriver", "CANBusManager", "CAN_AVAILABLE"]


# Guarded self-registration.  When Phase 0 lands, ``DriverRegistry`` will
# expose a ``register`` classmethod; until then this is a no-op.
try:
    from galois_edge.drivers.registry import DriverRegistry  # type: ignore[import-not-found]

    if hasattr(DriverRegistry, "register"):
        try:
            DriverRegistry.register("can", GenericCANDriver, CANBusManager())  # type: ignore[attr-defined]
            logger.debug("Registered CAN driver via DriverRegistry.register()")
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("CAN driver self-registration failed: %s", exc)
except Exception:  # pragma: no cover — registry import broken
    pass
