"""edge-api.md §9: CAN registry factory; CSafeLoader in DriverRegistry.discover."""
from __future__ import annotations

import pytest
import yaml

pytestmark = pytest.mark.critical


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


def test_discover_uses_the_c_safe_loader_when_available(tmp_path):
    from galois_edge.drivers import registry

    assert registry._YAML_LOADER is getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    (tmp_path / "modbus").mkdir()
    doc = "protocol: modbus\nname: x\nscale: 1.5e3\nflag: yes\n"
    (tmp_path / "modbus" / "x.yaml").write_text(doc)
    reg = registry.DriverRegistry(str(tmp_path))
    assert reg.discover() == 1
    assert reg._profiles["x"] == yaml.safe_load(doc)  # identical YAML 1.1 semantics, faster parser
