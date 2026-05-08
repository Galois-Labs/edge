"""Driver profile discovery and instance management.

Discovers YAML register profiles from the profiles directory and
instantiates the matching protocol driver via a class-method
registration mechanism (``DriverRegistry.register``).

Phase 0 refactor (foundation): the per-protocol ``if/elif`` chain in
``instantiate()`` is replaced by a class-level registry of protocol →
``DriverSpec``.  Each protocol package's ``__init__.py`` calls
``DriverRegistry.register(...)`` at import time so adding a protocol is
"create a directory and import it" — no central edits needed beyond
``galois_edge.drivers.__init__``.

Backward compatibility: every public attribute and method that existed
before the refactor (``discover``, ``reload``, ``list_profiles``,
``instantiate``, ``get_instance``, ``bus_manager``, ``can_bus_manager``,
``profiles_dir``) keeps the same signature.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from galois_edge.drivers.base import BaseProtocolDriver

logger = logging.getLogger(__name__)


# Module-level guard so we only attempt the protocol-package imports
# once even if many DriverRegistry instances get created.
_protocols_imported = False


def _ensure_protocols_imported() -> None:
    """Import the shipping protocol packages on first use.

    Each protocol package's ``__init__.py`` calls
    ``DriverRegistry.register(...)`` as an import side effect.  Importing
    them lazily (rather than at the top of this module) keeps
    ``galois_edge.drivers.registry`` free of cycles with the protocol
    packages, which themselves import this module to call
    ``DriverRegistry.register``.
    """
    global _protocols_imported
    if _protocols_imported:
        return
    _protocols_imported = True

    # Each import is wrapped because optional protocols (SPI / I2C /
    # OPC-UA) may have missing system libraries on some daemon hosts.
    # The four currently-shipping protocols have no optional deps but
    # we use the same shape so adding new ones is mechanical.
    for module_name in (
        "galois_edge.drivers.modbus",
        "galois_edge.drivers.can",
        "galois_edge.drivers.serial",
    ):
        try:
            __import__(module_name)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "DriverRegistry: failed to import %s: %s",
                module_name,
                exc,
            )


@dataclass
class DriverSpec:
    """A single protocol driver registration.

    Attributes
    ----------
    protocol:
        Discriminator value matching ``profile["protocol"]``.
    driver_class:
        Concrete :class:`BaseProtocolDriver` subclass.
    bus_manager_factory:
        Zero-arg callable returning the bus manager instance to share
        across all drivers of this protocol.  Lazily invoked the first
        time the protocol is instantiated, so failed imports (e.g. SPI
        on x86) don't crash the daemon at import time.
    bus_manager_kwarg:
        Kwarg name to pass the bus manager under when constructing the
        driver.  Defaults to ``"bus_manager"`` to match the existing
        Modbus / CAN / Serial drivers.
    extra_kwargs_filter:
        Optional callable that lets the protocol massage the kwargs that
        ``ConnectInstrument`` provides (e.g. translate ``slave_id`` →
        Modbus-specific kwarg).  Defaults to a passthrough.
    """

    protocol: str
    driver_class: type[BaseProtocolDriver]
    bus_manager_factory: Callable[[], Any] | None = None
    bus_manager_kwarg: str = "bus_manager"
    extra_kwargs_filter: Callable[[dict[str, Any]], dict[str, Any]] = field(
        default=lambda kw: kw
    )


class DriverRegistry:
    """Manages protocol driver profiles and running instances."""

    # Class-level registration table.  Populated by each protocol
    # package's ``__init__.py`` at import time.
    _drivers: dict[str, DriverSpec] = {}

    # ── Class-method registration API ──────────────────────────────

    @classmethod
    def register(
        cls,
        protocol: str,
        driver_class: type[BaseProtocolDriver],
        bus_manager_factory: Callable[[], Any] | None = None,
        *,
        bus_manager_kwarg: str = "bus_manager",
        extra_kwargs_filter: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        """Register a protocol driver class.

        Idempotent on the protocol name.  Registering twice replaces the
        previous spec (with a debug log) so test fixtures can swap
        drivers without leaking state across tests.
        """
        if not protocol:
            raise ValueError("protocol name must be a non-empty string")
        if protocol in cls._drivers:
            logger.debug(
                "DriverRegistry.register: replacing existing entry for %r",
                protocol,
            )
        spec = DriverSpec(
            protocol=protocol,
            driver_class=driver_class,
            bus_manager_factory=bus_manager_factory,
            bus_manager_kwarg=bus_manager_kwarg,
            extra_kwargs_filter=extra_kwargs_filter or (lambda kw: kw),
        )
        cls._drivers[protocol] = spec

    @classmethod
    def registered_protocols(cls) -> list[str]:
        """Return the list of currently-registered protocol names."""
        return sorted(cls._drivers)

    @classmethod
    def get_spec(cls, protocol: str) -> DriverSpec:
        """Look up a protocol spec; raises ``KeyError`` if missing."""
        try:
            return cls._drivers[protocol]
        except KeyError:
            raise KeyError(
                f"protocol {protocol!r} is not registered "
                f"(known: {cls.registered_protocols()!r})",
            )

    # ── Instance lifecycle ─────────────────────────────────────────

    def __init__(self, profiles_dir: str | None = None) -> None:
        # Ensure every shipping protocol has self-registered.  This
        # tolerates direct imports of ``DriverRegistry`` (i.e. without
        # going through ``galois_edge.drivers.__init__``) — necessary
        # because tests do exactly that.
        _ensure_protocols_imported()

        self._profiles: dict[str, dict[str, Any]] = {}
        self._instances: dict[str, BaseProtocolDriver] = {}
        # Bus managers are constructed on demand from each protocol's
        # ``bus_manager_factory`` and cached here.  The factory may
        # return ``None`` for protocols that don't share a bus.
        self._bus_managers: dict[str, Any] = {}
        self.profiles_dir = profiles_dir or str(
            Path(__file__).parent.parent / "profiles"
        )

    # ── Profile discovery ──────────────────────────────────────────

    def discover(self) -> int:
        """Scan profiles directories for YAML driver profiles."""
        self._profiles.clear()
        profiles_path = Path(self.profiles_dir)

        if not profiles_path.is_dir():
            logger.warning(
                "Profiles directory does not exist: %s", self.profiles_dir
            )
            return 0

        for protocol_dir in sorted(profiles_path.iterdir()):
            if not protocol_dir.is_dir():
                continue
            # SCPI profiles still flow through the legacy profile_loader.
            if protocol_dir.name == "scpi":
                continue

            for yaml_file in sorted(protocol_dir.glob("*.yaml")):
                try:
                    with open(yaml_file) as f:
                        profile = yaml.safe_load(f)
                    if (
                        profile
                        and isinstance(profile, dict)
                        and "protocol" in profile
                    ):
                        name = yaml_file.stem
                        self._profiles[name] = profile
                        logger.debug(
                            "Loaded driver profile: %s (%s)",
                            name,
                            protocol_dir.name,
                        )
                except Exception as exc:
                    logger.warning("Failed to load %s: %s", yaml_file, exc)

        logger.info("Discovered %d driver profile(s)", len(self._profiles))
        return len(self._profiles)

    def reload(self) -> int:
        """Re-scan profiles directory (for hot-reload on new profiles)."""
        return self.discover()

    # ── Instantiation ──────────────────────────────────────────────

    def _bus_manager_for(self, protocol: str) -> Any:
        if protocol in self._bus_managers:
            return self._bus_managers[protocol]
        spec = self.get_spec(protocol)
        if spec.bus_manager_factory is None:
            self._bus_managers[protocol] = None
            return None
        manager = spec.bus_manager_factory()
        self._bus_managers[protocol] = manager
        return manager

    def instantiate(
        self,
        profile_name: str,
        instrument_id: str,
        transport_uri: str,
        **kwargs: Any,
    ) -> BaseProtocolDriver:
        """Create a driver instance from a named profile.

        The protocol-specific driver class is looked up via the
        class-level registration table.  ``kwargs`` are passed verbatim
        to the driver after the spec's ``extra_kwargs_filter`` runs (used
        by Modbus to keep its ``slave_id`` parameter on the connect path).
        """
        profile = self._profiles.get(profile_name)
        if not profile:
            raise KeyError(f"No profile found: {profile_name}")

        protocol = profile.get("protocol", "modbus")
        spec = self.get_spec(protocol)

        bus_manager = self._bus_manager_for(protocol)
        filtered_kwargs = spec.extra_kwargs_filter(dict(kwargs))

        driver_kwargs: dict[str, Any] = {
            "instrument_id": instrument_id,
            "transport_uri": transport_uri,
            "profile": profile,
        }
        if bus_manager is not None:
            driver_kwargs[spec.bus_manager_kwarg] = bus_manager
        driver_kwargs.update(filtered_kwargs)

        instance = spec.driver_class(**driver_kwargs)
        self._instances[instrument_id] = instance
        return instance

    def get_instance(self, instrument_id: str) -> BaseProtocolDriver | None:
        """Return running driver instance, or None."""
        return self._instances.get(instrument_id)

    def list_profiles(self) -> list[dict[str, Any]]:
        """Return summary of all available profiles."""
        result = []
        for name, profile in self._profiles.items():
            identity = profile.get("identity", {})
            protocol = profile.get("protocol")
            summary: dict[str, Any] = {
                "name": name,
                "protocol": protocol,
                "manufacturer": identity.get("manufacturer"),
                "model": identity.get("model"),
                "description": identity.get("description"),
            }
            if protocol == "modbus":
                summary["register_count"] = len(profile.get("registers", {}))
            else:
                summary["command_count"] = len(profile.get("commands", {}))
                summary["point_count"] = len(profile.get("points", {}))
            result.append(summary)
        return result

    # ── Backward-compatible legacy properties ──────────────────────

    @property
    def bus_manager(self) -> Any:
        """Modbus bus manager (legacy accessor)."""
        return self._bus_manager_for("modbus") if "modbus" in self._drivers else None

    @property
    def can_bus_manager(self) -> Any:
        """CAN bus manager (legacy accessor)."""
        return self._bus_manager_for("can") if "can" in self._drivers else None
