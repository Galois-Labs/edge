"""Tests for DriverRegistry profile discovery and loading."""

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from galois_edge.drivers.registry import DriverRegistry


SAMPLE_PROFILE = {
    "protocol": "modbus",
    "identity": {
        "manufacturer": "TestCo",
        "model": "TM100",
        "description": "Test device",
    },
    "connection": {
        "transport": "tcp",
        "default_slave_id": 1,
    },
    "registers": {
        "temp": {
            "address": 0,
            "register_type": "holding",
            "data_type": "int16",
            "length_words": 1,
            "scale": 0.1,
            "unit": "°C",
        },
    },
    "commands": {
        "get_temp": {
            "type": "query",
            "reads": ["temp"],
        },
    },
}


@pytest.fixture
def profiles_dir(tmp_path):
    """Create a temp profiles directory with one Modbus profile."""
    modbus_dir = tmp_path / "modbus"
    modbus_dir.mkdir()

    profile_file = modbus_dir / "test_device.yaml"
    with open(profile_file, "w") as f:
        yaml.dump(SAMPLE_PROFILE, f)

    # Also create a scpi dir that should be ignored
    scpi_dir = tmp_path / "scpi"
    scpi_dir.mkdir()
    with open(scpi_dir / "keithley.yaml", "w") as f:
        yaml.dump({"instrument": {"manufacturer": "Keithley"}}, f)

    return str(tmp_path)


class TestDiscovery:
    def test_discovers_modbus_profile(self, profiles_dir):
        registry = DriverRegistry(profiles_dir)
        count = registry.discover()
        assert count == 1

    def test_ignores_scpi_dir(self, profiles_dir):
        registry = DriverRegistry(profiles_dir)
        registry.discover()
        profiles = registry.list_profiles()
        names = [p["name"] for p in profiles]
        assert "keithley" not in names
        assert "test_device" in names

    def test_list_profiles(self, profiles_dir):
        registry = DriverRegistry(profiles_dir)
        registry.discover()
        profiles = registry.list_profiles()
        assert len(profiles) == 1
        p = profiles[0]
        assert p["name"] == "test_device"
        assert p["protocol"] == "modbus"
        assert p["manufacturer"] == "TestCo"
        assert p["model"] == "TM100"
        assert p["register_count"] == 1

    def test_empty_dir(self, tmp_path):
        registry = DriverRegistry(str(tmp_path))
        count = registry.discover()
        assert count == 0

    def test_nonexistent_dir(self, tmp_path):
        registry = DriverRegistry(str(tmp_path / "nope"))
        count = registry.discover()
        assert count == 0

    def test_reload(self, profiles_dir):
        registry = DriverRegistry(profiles_dir)
        registry.discover()
        assert len(registry.list_profiles()) == 1

        # Add another profile
        modbus_dir = Path(profiles_dir) / "modbus"
        with open(modbus_dir / "new_device.yaml", "w") as f:
            yaml.dump({**SAMPLE_PROFILE, "identity": {"model": "NEW"}}, f)

        count = registry.reload()
        assert count == 2


class TestInstantiate:
    @patch("galois_edge.drivers.modbus_driver.ModbusBusManager")
    def test_instantiate_driver(self, mock_bus_cls, profiles_dir):
        registry = DriverRegistry(profiles_dir)
        registry.discover()

        driver = registry.instantiate(
            "test_device", "inst-1", "tcp://127.0.0.1:502"
        )
        assert driver.instrument_id == "inst-1"
        assert driver.transport_uri == "tcp://127.0.0.1:502"

    def test_instantiate_unknown_profile(self, profiles_dir):
        registry = DriverRegistry(profiles_dir)
        registry.discover()
        with pytest.raises(KeyError, match="No profile found"):
            registry.instantiate("nonexistent", "x", "tcp://x")

    @patch("galois_edge.drivers.modbus_driver.ModbusBusManager")
    def test_get_instance(self, mock_bus_cls, profiles_dir):
        registry = DriverRegistry(profiles_dir)
        registry.discover()

        driver = registry.instantiate(
            "test_device", "inst-1", "tcp://127.0.0.1:502"
        )
        assert registry.get_instance("inst-1") is driver
        assert registry.get_instance("unknown") is None
