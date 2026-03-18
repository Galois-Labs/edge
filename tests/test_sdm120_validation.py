"""Validate SDM120 Galois driver profile against sdm_modbus library.

Compares our hand-written YAML profile with the register definitions
from the sdm_modbus Python library (the reference implementation) to
ensure our pipeline would produce a correct driver.
"""

import yaml
import pytest
from pathlib import Path
from unittest.mock import MagicMock

from galois_edge.drivers.modbus_driver import GenericModbusDriver
from galois_edge.drivers.modbus_transport import ModbusBusManager


# Reference data from sdm_modbus library (sdm.py, SDM120 class, lines 149-201)
# Format: (address_hex, length, register_type, data_type, description)
SDM_MODBUS_INPUT_REGISTERS = {
    "voltage":           (0x0000, 2, "input", "float32", "V"),
    "current":           (0x0006, 2, "input", "float32", "A"),
    "power_active":      (0x000C, 2, "input", "float32", "W"),
    "power_apparent":    (0x0012, 2, "input", "float32", "VA"),
    "power_reactive":    (0x0018, 2, "input", "float32", "VAr"),
    "power_factor":      (0x001E, 2, "input", "float32", ""),
    "frequency":         (0x0046, 2, "input", "float32", "Hz"),
    "import_energy_active":  (0x0048, 2, "input", "float32", "kWh"),
    "export_energy_active":  (0x004A, 2, "input", "float32", "kWh"),
    "import_energy_reactive": (0x004C, 2, "input", "float32", "kVArh"),
    "export_energy_reactive": (0x004E, 2, "input", "float32", "kVArh"),
    "total_demand_power_active": (0x0054, 2, "input", "float32", "W"),
    "maximum_total_demand_power_active": (0x0056, 2, "input", "float32", "W"),
    "import_demand_power_active": (0x0058, 2, "input", "float32", "W"),
    "maximum_import_demand_power_active": (0x005A, 2, "input", "float32", "W"),
    "export_demand_power_active": (0x005C, 2, "input", "float32", "W"),
    "maximum_export_demand_power_active": (0x005E, 2, "input", "float32", "W"),
    "current_demand":    (0x0102, 2, "input", "float32", "A"),
    "maximum_current_demand": (0x0108, 2, "input", "float32", "A"),
    "total_energy_active":   (0x0156, 2, "input", "float32", "kWh"),
    "total_energy_reactive": (0x0158, 2, "input", "float32", "kVArh"),
}

SDM_MODBUS_HOLDING_REGISTERS = {
    "relay_pulse_width":    (0x000C, 2, "holding", "float32", "ms"),
    "network_parity_stop":  (0x0012, 2, "holding", "float32", ""),
    "meter_id":             (0x0014, 2, "holding", "float32", ""),
    "baud_rate":            (0x001C, 2, "holding", "float32", ""),
    "pulse_1_output_mode":  (0x0056, 2, "holding", "float32", ""),
}


@pytest.fixture
def sdm120_profile():
    """Load the hand-written SDM120 YAML profile."""
    profile_path = Path(__file__).parent.parent / "src" / "galois_edge" / "profiles" / "modbus" / "eastron_sdm120.yaml"
    with open(profile_path) as f:
        return yaml.safe_load(f)


@pytest.fixture
def sdm120_driver(sdm120_profile):
    """Create a GenericModbusDriver from the SDM120 profile."""
    bus_mgr = MagicMock(spec=ModbusBusManager)
    mock_client = MagicMock()
    mock_lock = MagicMock()
    mock_lock.__enter__ = MagicMock(return_value=None)
    mock_lock.__exit__ = MagicMock(return_value=False)
    bus_mgr.get_client.return_value = (mock_client, mock_lock)

    driver = GenericModbusDriver(
        instrument_id="sdm120-test",
        transport_uri="rtu:///dev/ttyUSB0",
        profile=sdm120_profile,
        bus_manager=bus_mgr,
    )
    driver.connect()
    return driver


class TestProfileStructure:
    """Verify the YAML profile has correct structure."""

    def test_protocol(self, sdm120_profile):
        assert sdm120_profile["protocol"] == "modbus"

    def test_identity(self, sdm120_profile):
        assert sdm120_profile["identity"]["manufacturer"] == "Eastron"
        assert sdm120_profile["identity"]["model"] == "SDM120"

    def test_connection_defaults(self, sdm120_profile):
        conn = sdm120_profile["connection"]
        assert conn["default_baudrate"] == 2400  # SDM120 default is 2400, not 9600
        assert conn["byte_order"] == "big"
        assert conn["word_order"] == "big"
        assert conn["addressing_mode"] == "modicon_3x"

    def test_has_registers(self, sdm120_profile):
        assert len(sdm120_profile["registers"]) >= 20  # At least 20 input + 5 holding

    def test_has_commands(self, sdm120_profile):
        assert "get_voltage" in sdm120_profile["commands"]
        assert "get_measurements" in sdm120_profile["commands"]


class TestInputRegisterAddresses:
    """Verify input register addresses match sdm_modbus library exactly."""

    @pytest.mark.parametrize("name,expected", SDM_MODBUS_INPUT_REGISTERS.items())
    def test_input_register_address(self, sdm120_profile, name, expected):
        """Each input register address must match the sdm_modbus reference."""
        addr, length, reg_type, dtype, unit = expected

        # Find matching register in our profile (name may differ slightly)
        found = False
        for reg_name, reg_def in sdm120_profile["registers"].items():
            if reg_def.get("register_type") == "input" and reg_def["address"] == addr:
                assert reg_def["data_type"] == dtype, f"Register at 0x{addr:04X}: expected {dtype}, got {reg_def['data_type']}"
                assert reg_def["length_words"] == length, f"Register at 0x{addr:04X}: expected {length} words"
                found = True
                break

        assert found, f"Input register '{name}' at address 0x{addr:04X} not found in profile"


class TestHoldingRegisterAddresses:
    """Verify holding register addresses match sdm_modbus library."""

    @pytest.mark.parametrize("name,expected", SDM_MODBUS_HOLDING_REGISTERS.items())
    def test_holding_register_address(self, sdm120_profile, name, expected):
        addr, length, reg_type, dtype, unit = expected

        found = False
        for reg_name, reg_def in sdm120_profile["registers"].items():
            if reg_def.get("register_type") == "holding" and reg_def["address"] == addr:
                assert reg_def["data_type"] == dtype
                assert reg_def["length_words"] == length
                found = True
                break

        assert found, f"Holding register '{name}' at address 0x{addr:04X} not found in profile"


class TestDriverInstantiation:
    """Verify the profile loads correctly into GenericModbusDriver."""

    def test_point_count(self, sdm120_driver):
        # 20 input + 5 holding = 25 minimum
        assert len(sdm120_driver._points) >= 25

    def test_command_count(self, sdm120_driver):
        assert len(sdm120_driver._commands) >= 5

    def test_identify(self, sdm120_driver):
        ident = sdm120_driver.identify()
        assert "Eastron" in ident
        assert "SDM120" in ident

    def test_capabilities(self, sdm120_driver):
        caps = sdm120_driver.get_capabilities()
        assert caps["protocol"] == "modbus"
        assert caps["registers"] >= 25
        assert "get_voltage" in caps["commands"]

    def test_input_registers_readonly(self, sdm120_driver):
        """All input registers should be read-only."""
        for name, point in sdm120_driver._points.items():
            if point.register_type == "input":
                assert point.access == "read", f"Input register '{name}' should be read-only"

    def test_holding_registers_writable(self, sdm120_driver):
        """Holding registers should be read_write."""
        writable_count = sum(
            1 for p in sdm120_driver._points.values()
            if p.register_type == "holding" and p.access == "read_write"
        )
        assert writable_count >= 4  # At least relay_pulse_width, parity, meter_id, baud

    def test_all_float32(self, sdm120_driver):
        """SDM120 uses float32 for ALL registers (per the datasheet)."""
        for name, point in sdm120_driver._points.items():
            assert point.data_type == "float32", f"Register '{name}' should be float32, got {point.data_type}"

    def test_all_2_words(self, sdm120_driver):
        """All float32 registers need 2 words."""
        for name, point in sdm120_driver._points.items():
            if point.data_type == "float32":
                assert point.length_words == 2, f"Register '{name}' should be 2 words"


class TestReadSimulation:
    """Simulate reading registers to verify the driver works end-to-end."""

    def test_read_voltage(self, sdm120_driver):
        """Simulate reading voltage — should call read_input_registers(0, 2)."""
        from pymodbus.constants import Endian
        from pymodbus.payload import BinaryPayloadBuilder

        # Build a response for 230.2V
        builder = BinaryPayloadBuilder(byteorder=Endian.BIG, wordorder=Endian.BIG)
        builder.add_32bit_float(230.2)
        registers = builder.to_registers()

        resp = MagicMock()
        resp.isError.return_value = False
        resp.registers = registers
        sdm120_driver.client.read_input_registers.return_value = resp

        result = sdm120_driver.execute_command("get_voltage")
        assert result == pytest.approx(230.2, rel=1e-4)

        # Verify correct Modbus call
        sdm120_driver.client.read_input_registers.assert_called_with(0x0000, 2, slave=1)

    def test_read_frequency(self, sdm120_driver):
        """Simulate reading frequency at address 0x0046."""
        from pymodbus.constants import Endian
        from pymodbus.payload import BinaryPayloadBuilder

        builder = BinaryPayloadBuilder(byteorder=Endian.BIG, wordorder=Endian.BIG)
        builder.add_32bit_float(50.0)
        registers = builder.to_registers()

        resp = MagicMock()
        resp.isError.return_value = False
        resp.registers = registers
        sdm120_driver.client.read_input_registers.return_value = resp

        point = sdm120_driver._points["frequency"]
        result = sdm120_driver.read_point(point)
        assert result == pytest.approx(50.0, rel=1e-4)

        sdm120_driver.client.read_input_registers.assert_called_with(0x0046, 2, slave=1)

    def test_read_enum_baud_rate(self, sdm120_driver):
        """Holding register with enum should return string label."""
        from pymodbus.constants import Endian
        from pymodbus.payload import BinaryPayloadBuilder

        # Baud rate enum: 2 = 9600
        builder = BinaryPayloadBuilder(byteorder=Endian.BIG, wordorder=Endian.BIG)
        builder.add_32bit_float(2.0)
        registers = builder.to_registers()

        resp = MagicMock()
        resp.isError.return_value = False
        resp.registers = registers
        sdm120_driver.client.read_holding_registers.return_value = resp

        point = sdm120_driver._points["baud_rate"]
        result = sdm120_driver.read_point(point)
        assert result == "9600"
