"""I²C protocol driver package.

Exports `GenericI2cDriver` and `I2CBusManager`. Self-registers with
`DriverRegistry` if the registry exposes a `register()` classmethod (the
Phase 0 foundation work). Until that lands, importing this package is a
no-op apart from making the classes available.
"""

from __future__ import annotations

from galois_edge.drivers.i2c.driver import GenericI2cDriver
from galois_edge.drivers.i2c.transport import I2CBusManager

__all__ = ["GenericI2cDriver", "I2CBusManager"]


def _self_register() -> None:
    """Register with the daemon's DriverRegistry if it accepts plugins."""
    try:
        from galois_edge.drivers.registry import DriverRegistry  # noqa: WPS433
    except Exception:  # pragma: no cover - registry not importable
        return
    if hasattr(DriverRegistry, "register"):
        try:
            DriverRegistry.register(  # type: ignore[attr-defined]
                "i2c", GenericI2cDriver, I2CBusManager()
            )
        except Exception:  # pragma: no cover - registration is best-effort
            pass


_self_register()
