"""Tests for GenericModbusDriver using pymodbus simulator."""

import struct
from unittest.mock import MagicMock, PropertyMock, patch

import pytest
from pymodbus.constants import Endian
from pymodbus.payload import BinaryPayloadBuilder

from galois_edge.drivers.modbus_driver import GenericModbusDriver
from galois_edge.drivers.modbus_transport import ModbusBusManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SAMPLE_PROFILE = {
    "protocol": "modbus",
    "identity": {
        "manufacturer": "TestCo",
        "model": "TM100",
        "description": "Test temperature monitor",
    },
    "connection": {
        "transport": "tcp",
        "default_slave_id": 1,
        "byte_order": "big",
        "word_order": "big",
        "default_timeout": 1.0,
    },
    "registers": {
        "temperature": {
            "address": 0,
            "register_type": "holding",
            "access": "read",
            "data_type": "int16",
            "length_words": 1,
            "scale": 0.1,
            "unit": "°C",
            "description": "Current temperature",
        },
        "setpoint": {
            "address": 1,
            "register_type": "holding",
            "access": "read_write",
            "data_type": "int16",
            "length_words": 1,
            "scale": 0.1,
            "unit": "°C",
            "range": [0, 100],
            "write_function_code": 6,
            "description": "Temperature setpoint",
        },
        "pressure_f32": {
            "address": 10,
            "register_type": "holding",
            "access": "read",
            "data_type": "float32",
            "length_words": 2,
            "byte_order": "big",
            "word_order": "big",
            "unit": "bar",
            "description": "Pressure (float32, 2 registers)",
        },
        "pressure_f32_swapped": {
            "address": 12,
            "register_type": "holding",
            "access": "read",
            "data_type": "float32",
            "length_words": 2,
            "byte_order": "big",
            "word_order": "little",
            "unit": "bar",
            "description": "Pressure (float32, word-swapped CDAB)",
        },
        "mode": {
            "address": 20,
            "register_type": "holding",
            "access": "read_write",
            "data_type": "uint16",
            "length_words": 1,
            "enum": {0: "auto", 1: "manual", 2: "standby"},
            "description": "Control mode",
        },
        "status_bits": {
            "address": 30,
            "register_type": "holding",
            "access": "read",
            "data_type": "uint16",
            "length_words": 1,
            "bitfield": {
                "alarm": {"bit": 0, "description": "Alarm"},
                "ready": {"bit": 1, "description": "Ready"},
                "running": {"bit": 7, "description": "Running"},
            },
            "description": "Status word",
        },
        "heater_on": {
            "address": 0,
            "register_type": "coil",
            "access": "read",
            "data_type": "bool",
            "length_words": 0,
            "description": "Heater coil",
        },
        "valve_open": {
            "address": 1,
            "register_type": "coil",
            "access": "read_write",
            "data_type": "bool",
            "length_words": 0,
            "description": "Valve coil",
        },
        "pid_setpoint_f32": {
            "address": 40,
            "register_type": "holding",
            "access": "read_write",
            "data_type": "float32",
            "length_words": 2,
            "byte_order": "big",
            "word_order": "big",
            "write_function_code": 16,
            "unit": "°C",
            "description": "PID setpoint (float32 write)",
        },
    },
    "commands": {
        "get_temperature": {
            "type": "query",
            "reads": ["temperature"],
        },
        "set_temperature": {
            "type": "action",
            "writes": [{"register": "setpoint", "value": "{temp}"}],
        },
        "get_status": {
            "type": "query",
            "reads": ["temperature", "setpoint", "mode"],
        },
    },
}


def _make_holding_response(registers):
    """Create a mock pymodbus read response."""
    resp = MagicMock()
    resp.isError.return_value = False
    resp.registers = registers
    return resp


def _make_coil_response(bits):
    resp = MagicMock()
    resp.isError.return_value = False
    resp.bits = bits
    return resp


def _make_error_response():
    resp = MagicMock()
    resp.isError.return_value = True
    resp.__str__ = lambda self: "ModbusException(IllegalAddress)"
    return resp


def _make_write_response(success=True):
    resp = MagicMock()
    resp.isError.return_value = not success
    return resp


def _float32_to_registers(value, byteorder=Endian.BIG, wordorder=Endian.BIG):
    """Encode a float32 into 2 Modbus registers."""
    builder = BinaryPayloadBuilder(byteorder=byteorder, wordorder=wordorder)
    builder.add_32bit_float(value)
    return builder.to_registers()


@pytest.fixture
def driver():
    """Create a GenericModbusDriver with mocked transport."""
    bus_mgr = MagicMock(spec=ModbusBusManager)
    mock_client = MagicMock()
    mock_lock = MagicMock()
    # Make lock work as context manager
    mock_lock.__enter__ = MagicMock(return_value=None)
    mock_lock.__exit__ = MagicMock(return_value=False)
    bus_mgr.get_client.return_value = (mock_client, mock_lock)

    drv = GenericModbusDriver(
        instrument_id="test-1",
        transport_uri="tcp://127.0.0.1:5020",
        profile=SAMPLE_PROFILE,
        bus_manager=bus_mgr,
    )
    drv.connect()
    return drv


# ---------------------------------------------------------------------------
# Read tests
# ---------------------------------------------------------------------------

class TestReadInt16:
    def test_read_scaled(self, driver):
        driver.client.read_holding_registers.return_value = _make_holding_response([250])
        result = driver.read_point(driver._points["temperature"])
        assert result == pytest.approx(25.0)  # 250 * 0.1

    def test_read_negative_int16(self, driver):
        # -10°C = raw -100 → stored as unsigned 65436
        driver.client.read_holding_registers.return_value = _make_holding_response([65436])
        result = driver.read_point(driver._points["temperature"])
        assert result == pytest.approx(-10.0)  # -100 * 0.1


class TestReadFloat32:
    def test_read_big_endian(self, driver):
        regs = _float32_to_registers(3.14, Endian.BIG, Endian.BIG)
        driver.client.read_holding_registers.return_value = _make_holding_response(regs)
        result = driver.read_point(driver._points["pressure_f32"])
        assert result == pytest.approx(3.14, rel=1e-5)

    def test_read_word_swapped(self, driver):
        regs = _float32_to_registers(2.718, Endian.BIG, Endian.LITTLE)
        driver.client.read_holding_registers.return_value = _make_holding_response(regs)
        result = driver.read_point(driver._points["pressure_f32_swapped"])
        assert result == pytest.approx(2.718, rel=1e-5)


class TestReadEnum:
    def test_enum_mapping(self, driver):
        driver.client.read_holding_registers.return_value = _make_holding_response([1])
        result = driver.read_point(driver._points["mode"])
        assert result == "manual"

    def test_enum_unknown_value(self, driver):
        driver.client.read_holding_registers.return_value = _make_holding_response([99])
        result = driver.read_point(driver._points["mode"])
        assert result == "99"  # Fallback to string


class TestReadBitfield:
    def test_bitfield_extraction(self, driver):
        # Bits: alarm=1, ready=1, running=0 → value = 0b00000011 = 3
        driver.client.read_holding_registers.return_value = _make_holding_response([3])
        result = driver.read_point(driver._points["status_bits"])
        assert result["alarm"] is True
        assert result["ready"] is True
        assert result["running"] is False

    def test_bitfield_bit7(self, driver):
        # Bit 7 set → value = 128
        driver.client.read_holding_registers.return_value = _make_holding_response([128])
        result = driver.read_point(driver._points["status_bits"])
        assert result["alarm"] is False
        assert result["running"] is True


class TestReadCoil:
    def test_coil_true(self, driver):
        driver.client.read_coils.return_value = _make_coil_response([True])
        result = driver.read_point(driver._points["heater_on"])
        assert result is True

    def test_coil_false(self, driver):
        driver.client.read_coils.return_value = _make_coil_response([False])
        result = driver.read_point(driver._points["heater_on"])
        assert result is False


class TestReadErrors:
    def test_modbus_error_raises(self, driver):
        driver.client.read_holding_registers.return_value = _make_error_response()
        with pytest.raises(IOError, match="Modbus error"):
            driver.read_point(driver._points["temperature"])

    def test_coil_error_raises(self, driver):
        driver.client.read_coils.return_value = _make_error_response()
        with pytest.raises(IOError, match="Modbus error"):
            driver.read_point(driver._points["heater_on"])


# ---------------------------------------------------------------------------
# Write tests
# ---------------------------------------------------------------------------

class TestWriteInt16:
    def test_write_scaled(self, driver):
        driver.client.write_register.return_value = _make_write_response()
        driver.write_point(driver._points["setpoint"], 50.0)
        # 50.0 / 0.1 = 500
        driver.client.write_register.assert_called_once_with(1, 500, slave=1)

    def test_write_out_of_range(self, driver):
        with pytest.raises(ValueError, match="out of range"):
            driver.write_point(driver._points["setpoint"], 150.0)

    def test_write_read_only_rejected(self, driver):
        with pytest.raises(PermissionError, match="read-only"):
            driver.write_point(driver._points["temperature"], 25.0)


class TestWriteEnum:
    def test_write_enum_string(self, driver):
        driver.client.write_register.return_value = _make_write_response()
        driver.write_point(driver._points["mode"], "manual")
        driver.client.write_register.assert_called_once_with(20, 1, slave=1)

    def test_write_enum_int(self, driver):
        driver.client.write_register.return_value = _make_write_response()
        driver.write_point(driver._points["mode"], 2)
        driver.client.write_register.assert_called_once_with(20, 2, slave=1)


class TestWriteFloat32:
    def test_write_fc16_multi_register(self, driver):
        driver.client.write_registers.return_value = _make_write_response()
        driver.write_point(driver._points["pid_setpoint_f32"], 42.5)

        # Should use write_registers (FC16) for 2-register float32
        driver.client.write_registers.assert_called_once()
        call_args = driver.client.write_registers.call_args
        assert call_args[0][0] == 40  # address
        assert len(call_args[0][1]) == 2  # 2 registers
        assert call_args[1]["slave"] == 1


class TestWriteCoil:
    def test_write_coil(self, driver):
        driver.client.write_coil.return_value = _make_write_response()
        driver.write_point(driver._points["valve_open"], True)
        driver.client.write_coil.assert_called_once_with(1, True, slave=1)


class TestWriteErrors:
    def test_write_error_response(self, driver):
        driver.client.write_register.return_value = _make_write_response(success=False)
        with pytest.raises(IOError, match="Modbus error"):
            driver.write_point(driver._points["setpoint"], 50.0)


# ---------------------------------------------------------------------------
# Command execution tests
# ---------------------------------------------------------------------------

class TestExecuteCommand:
    def test_query_single(self, driver):
        driver.client.read_holding_registers.return_value = _make_holding_response([300])
        result = driver.execute_command("get_temperature")
        assert result == pytest.approx(30.0)

    def test_action_with_params(self, driver):
        driver.client.write_register.return_value = _make_write_response()
        result = driver.execute_command("set_temperature", {"temp": 75.0})
        assert result == {"status": "ok"}
        driver.client.write_register.assert_called_once_with(1, 750, slave=1)

    def test_query_multiple(self, driver):
        # get_status reads temperature, setpoint, mode
        driver.client.read_holding_registers.side_effect = [
            _make_holding_response([200]),   # temperature
            _make_holding_response([500]),   # setpoint
            _make_holding_response([0]),     # mode → "auto"
        ]
        result = driver.execute_command("get_status")
        assert result["temperature"] == pytest.approx(20.0)
        assert result["setpoint"] == pytest.approx(50.0)
        assert result["mode"] == "auto"

    def test_unknown_command(self, driver):
        with pytest.raises(ValueError, match="Unknown command"):
            driver.execute_command("nonexistent_command")


# ---------------------------------------------------------------------------
# Capabilities and identity
# ---------------------------------------------------------------------------

class TestCapabilities:
    def test_identify(self, driver):
        ident = driver.identify()
        assert "TestCo" in ident
        assert "TM100" in ident

    def test_get_capabilities(self, driver):
        caps = driver.get_capabilities()
        assert caps["protocol"] == "modbus"
        assert "get_temperature" in caps["commands"]
        assert caps["registers"] > 0
        assert caps["writable"] > 0
