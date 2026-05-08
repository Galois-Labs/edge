"""Production-grade CAN driver package.

Phase 1 of the multi-protocol driver pipeline (see
``docs/multi-protocol-driver-pipeline-spec.md``).  This package replaces
the legacy ``drivers/can_driver.py`` + ``drivers/can_transport.py`` once
Phase F integration moves the registry over.

Self-registration uses ``DriverRegistry.register``, which Phase 0
(foundation) added.  The ``hasattr`` guard remains as defense in depth
in case this package is imported in a stripped-down context where the
registry refactor hasn't been picked up.
"""

from __future__ import annotations

import logging

from galois_edge.drivers.can.driver import GenericCANDriver
from galois_edge.drivers.can.transport import CAN_AVAILABLE, CANBusManager

logger = logging.getLogger(__name__)

__all__ = ["GenericCANDriver", "CANBusManager", "CAN_AVAILABLE"]


# Self-registration. Phase 0 introduced DriverRegistry.register;
# the hasattr guard stays for defense in depth.
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
