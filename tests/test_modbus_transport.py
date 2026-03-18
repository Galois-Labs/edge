"""Tests for ModbusBusManager shared transport."""

from unittest.mock import MagicMock, patch

from galois_edge.drivers.modbus_transport import ModbusBusManager


@patch("galois_edge.drivers.modbus_transport.ModbusTcpClient")
def test_shared_client_same_bus(mock_tcp_cls):
    """Two get_client calls for the same URI return the same client."""
    mock_client = MagicMock()
    mock_client.connect.return_value = True
    mock_tcp_cls.return_value = mock_client

    mgr = ModbusBusManager()
    client1, lock1 = mgr.get_client("tcp://192.168.1.10:502")
    client2, lock2 = mgr.get_client("tcp://192.168.1.10:502")

    assert client1 is client2
    assert lock1 is lock2
    # Only one client should have been created
    assert mock_tcp_cls.call_count == 1


@patch("galois_edge.drivers.modbus_transport.ModbusTcpClient")
def test_different_uris_different_clients(mock_tcp_cls):
    """Different URIs get different clients."""
    # side_effect returns a new mock for each call
    mock_tcp_cls.side_effect = [
        MagicMock(connect=MagicMock(return_value=True)),
        MagicMock(connect=MagicMock(return_value=True)),
    ]

    mgr = ModbusBusManager()
    client1, _ = mgr.get_client("tcp://192.168.1.10:502")
    client2, _ = mgr.get_client("tcp://192.168.1.20:502")

    assert client1 is not client2
    assert mock_tcp_cls.call_count == 2


@patch("galois_edge.drivers.modbus_transport.ModbusTcpClient")
def test_ref_counting_release(mock_tcp_cls):
    """Client is closed only when all references are released."""
    mock_client = MagicMock()
    mock_client.connect.return_value = True
    mock_tcp_cls.return_value = mock_client

    mgr = ModbusBusManager()
    mgr.get_client("tcp://10.0.0.1:502")
    mgr.get_client("tcp://10.0.0.1:502")

    # First release — still 1 ref
    mgr.release("tcp://10.0.0.1:502")
    mock_client.close.assert_not_called()

    # Second release — ref_count = 0 → close
    mgr.release("tcp://10.0.0.1:502")
    mock_client.close.assert_called_once()


@patch("galois_edge.drivers.modbus_transport.ModbusSerialClient")
def test_rtu_uri_parsing(mock_serial_cls):
    """RTU URIs are parsed correctly."""
    mock_client = MagicMock()
    mock_client.connect.return_value = True
    mock_serial_cls.return_value = mock_client

    mgr = ModbusBusManager()
    mgr.get_client("rtu:///dev/ttyUSB0", baudrate=19200, parity="E", stopbits=1)

    mock_serial_cls.assert_called_once_with(
        "/dev/ttyUSB0",
        baudrate=19200,
        parity="E",
        stopbits=1,
        timeout=1.0,
    )


@patch("galois_edge.drivers.modbus_transport.ModbusSerialClient")
def test_windows_com_port_above_9(mock_serial_cls):
    """COM ports above 9 are converted to \\\\.\\COM format."""
    mock_client = MagicMock()
    mock_client.connect.return_value = True
    mock_serial_cls.return_value = mock_client

    mgr = ModbusBusManager()
    mgr.get_client("rtu://COM12")

    # Should have been converted to \\.\COM12
    call_args = mock_serial_cls.call_args
    assert call_args[0][0] == "\\\\.\\COM12"


def test_bus_key_rtu():
    mgr = ModbusBusManager()
    key = mgr._bus_key("rtu:///dev/ttyUSB0", baudrate=9600, parity="N", stopbits=1)
    assert "rtu" in key
    assert "/dev/ttyUSB0" in key
    assert "9600" in key


def test_bus_key_tcp():
    mgr = ModbusBusManager()
    key = mgr._bus_key("tcp://192.168.1.10:502")
    assert "tcp" in key
    assert "192.168.1.10" in key
    assert "502" in key
