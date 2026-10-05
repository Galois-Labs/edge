"""edge-api.md §9: CAN registry factory; CSafeLoader in DriverRegistry.discover.

CI-10: the SPI, I²C and OPC-UA packages had the same instance-instead-of-factory
registration bug, so they are pinned here too.
"""
from __future__ import annotations

import importlib

import pytest
import yaml

pytestmark = pytest.mark.critical

# protocol -> (package that self-registers on import, bus-manager class name)
FACTORY_PROTOCOLS = {
    "can": ("galois_edge.drivers.can", "CANBusManager"),
    "spi": ("galois_edge.drivers.spi", "SPIBusManager"),
    "i2c": ("galois_edge.drivers.i2c", "I2CBusManager"),
    "opcua": ("galois_edge.drivers.opcua", "OPCUABusManager"),
}


def test_can_registers_a_factory_not_an_instance():
    import galois_edge.drivers.can  # noqa: F401  (self-registers)
    from galois_edge.drivers.can import CANBusManager
    from galois_edge.drivers.registry import DriverRegistry

    spec = DriverRegistry.get_spec("can")
    assert spec.bus_manager_factory is CANBusManager


def test_can_bus_manager_is_built_lazily(tmp_path):
    import galois_edge.drivers.can  # noqa: F401
    from galois_edge.drivers.can import CANBusManager
    from galois_edge.drivers.registry import DriverRegistry

    reg = DriverRegistry(str(tmp_path))
    mgr = reg._bus_manager_for("can")
    assert isinstance(mgr, CANBusManager)
    assert reg._bus_manager_for("can") is mgr  # shared per registry


@pytest.mark.parametrize("protocol", sorted(FACTORY_PROTOCOLS))
def test_protocol_registers_its_bus_manager_class_as_the_factory(protocol, tmp_path):
    """CI-10: every self-registering package passes the class, never an instance."""
    module_name, class_name = FACTORY_PROTOCOLS[protocol]
    package = importlib.import_module(module_name)  # self-registers on import
    manager_cls = getattr(package, class_name)
    from galois_edge.drivers.registry import DriverRegistry

    assert DriverRegistry.get_spec(protocol).bus_manager_factory is manager_cls
    reg = DriverRegistry(str(tmp_path))
    mgr = reg._bus_manager_for(protocol)  # raised TypeError when an instance was registered
    assert isinstance(mgr, manager_cls)
    assert reg._bus_manager_for(protocol) is mgr  # one shared manager per registry
    assert DriverRegistry(str(tmp_path))._bus_manager_for(protocol) is not mgr  # never shared across registries


def test_discover_uses_the_c_safe_loader_when_available(tmp_path, monkeypatch):
    from galois_edge.drivers import registry

    real_loader = registry._YAML_LOADER
    assert real_loader is getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    parsed_with = []

    class _SpyLoader(real_loader):
        def __init__(self, stream):
            parsed_with.append(real_loader)
            super().__init__(stream)

    # Proves discover() parses through _YAML_LOADER rather than yaml.safe_load,
    # which would never instantiate the spy.
    monkeypatch.setattr(registry, "_YAML_LOADER", _SpyLoader)
    (tmp_path / "modbus").mkdir()
    doc = "protocol: modbus\nname: x\nscale: 1.5e3\nflag: yes\n"
    (tmp_path / "modbus" / "x.yaml").write_text(doc)
    reg = registry.DriverRegistry(str(tmp_path))
    assert reg.discover() == 1
    assert parsed_with == [real_loader]
    assert reg._profiles["x"] == yaml.safe_load(doc)  # identical YAML 1.1 semantics, faster parser
