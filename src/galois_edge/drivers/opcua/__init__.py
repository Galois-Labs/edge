"""OPC-UA protocol driver package.

Self-registers with ``DriverRegistry`` at import time when the registry's
class-method ``register()`` API is available (Phase 0 foundation work).
Until then it's a no-op and the existing per-protocol switch in
``drivers/registry.py`` remains the active dispatcher.
"""

from __future__ import annotations

import logging

from galois_edge.drivers.registry import DriverRegistry

from .driver import GenericOpcuaDriver
from .transport import OPCUABusManager, OPCUA_AVAILABLE

logger = logging.getLogger(__name__)

__all__ = ["GenericOpcuaDriver", "OPCUABusManager", "OPCUA_AVAILABLE"]


# Self-registration. Guarded by hasattr so we don't crash on the legacy
# instance-only registry shape; the integration phase wires the decorator.
if hasattr(DriverRegistry, "register"):
    try:
        DriverRegistry.register(  # type: ignore[attr-defined]
            "opcua", GenericOpcuaDriver, OPCUABusManager(),
        )
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("OPC-UA driver self-registration skipped: %s", exc)
