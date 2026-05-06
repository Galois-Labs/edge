"""Driver profile discovery and loading.

Discovers YAML register profiles from the profiles directory and
creates GenericModbusDriver instances. Supports hot-reload via
``reload()``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from galois_edge.drivers.base import BaseProtocolDriver
from galois_edge.drivers.modbus_driver import GenericModbusDriver
from galois_edge.drivers.modbus_transport import ModbusBusManager
from galois_edge.drivers.can_driver import GenericCANDriver
from galois_edge.drivers.can_transport import CANBusManager

logger = logging.getLogger(__name__)


class DriverRegistry:
    """Manages protocol driver profiles and running instances."""

    def __init__(self, profiles_dir: str | None = None) -> None:
        self._profiles: dict[str, dict[str, Any]] = {}
        self._instances: dict[str, BaseProtocolDriver] = {}
        self._bus_manager = ModbusBusManager()
        self._can_bus_manager = CANBusManager()
        self.profiles_dir = profiles_dir or str(
            Path(__file__).parent.parent / "profiles"
        )

    def discover(self) -> int:
        """Scan profiles directories for YAML driver profiles.

        Returns the number of profiles found.
        """
        self._profiles.clear()
        profiles_path = Path(self.profiles_dir)

        if not profiles_path.is_dir():
            logger.warning("Profiles directory does not exist: %s", self.profiles_dir)
            return 0

        for protocol_dir in sorted(profiles_path.iterdir()):
            if not protocol_dir.is_dir():
                continue
            # Skip SCPI profiles (handled by existing profile_loader)
            if protocol_dir.name == "scpi":
                continue

            for yaml_file in sorted(protocol_dir.glob("*.yaml")):
                try:
                    with open(yaml_file) as f:
                        profile = yaml.safe_load(f)
                    if profile and isinstance(profile, dict) and "protocol" in profile:
                        name = yaml_file.stem
                        self._profiles[name] = profile
                        logger.debug("Loaded driver profile: %s (%s)", name, protocol_dir.name)
                except Exception as exc:
                    logger.warning("Failed to load %s: %s", yaml_file, exc)

        logger.info("Discovered %d driver profile(s)", len(self._profiles))
        return len(self._profiles)

    def reload(self) -> int:
        """Re-scan profiles directory (for hot-reload on new profiles)."""
        return self.discover()

    def instantiate(
        self,
        profile_name: str,
        instrument_id: str,
        transport_uri: str,
        **kwargs: Any,
    ) -> BaseProtocolDriver:
        """Create a driver instance from a named profile."""
        profile = self._profiles.get(profile_name)
        if not profile:
            raise KeyError(f"No profile found: {profile_name}")

        protocol = profile.get("protocol", "modbus")

        if protocol == "modbus":
            instance = GenericModbusDriver(
                instrument_id=instrument_id,
                transport_uri=transport_uri,
                profile=profile,
                bus_manager=self._bus_manager,
                **kwargs,
            )
        elif protocol == "can":
            instance = GenericCANDriver(
                instrument_id=instrument_id,
                transport_uri=transport_uri,
                profile=profile,
                bus_manager=self._can_bus_manager,
                **kwargs,
            )
        else:
            raise ValueError(f"Unsupported protocol: {protocol}")

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

    @property
    def bus_manager(self) -> ModbusBusManager:
        return self._bus_manager

    @property
    def can_bus_manager(self) -> CANBusManager:
        return self._can_bus_manager
