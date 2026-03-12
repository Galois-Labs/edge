"""
Tests for command_handler.py -- query/write detection, timeout, error handling.
"""

import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "src")
)

from galois_edge.command_handler import CommandHandler


class TestQueryWriteDetection:
    """Test that commands ending with '?' are treated as queries."""

    def test_query_detected(self, mock_instrument_manager):
        handler = CommandHandler(mock_instrument_manager)
        mock_instrument_manager.set_query_response(
            "GPIB0::25::INSTR", "*IDN?", "KEITHLEY,2400,SN,v1"
        )
        result = handler.execute_command(
            "*IDN?", "GPIB0::25::INSTR"
        )
        assert result["success"] is True
        assert "KEITHLEY" in result["response"]

    def test_write_detected(self, mock_instrument_manager):
        handler = CommandHandler(mock_instrument_manager)
        result = handler.execute_command(
            "*RST", "GPIB0::25::INSTR"
        )
        assert result["success"] is True
        assert result["response"] == "OK"

    def test_force_query_overrides_write(self, mock_instrument_manager):
        handler = CommandHandler(mock_instrument_manager)
        mock_instrument_manager.set_query_response(
            "GPIB0::25::INSTR", "STATUS", "0"
        )
        result = handler.execute_command(
            "STATUS", "GPIB0::25::INSTR", force_query=True,
        )
        assert result["success"] is True
        assert result["response"] == "0"

    def test_whitespace_preserved_in_detection(
        self, mock_instrument_manager,
    ):
        handler = CommandHandler(mock_instrument_manager)
        mock_instrument_manager.set_query_response(
            "GPIB0::25::INSTR", "  *IDN?  ", "KEITHLEY"
        )
        result = handler.execute_command(
            "  *IDN?  ", "GPIB0::25::INSTR"
        )
        assert result["success"] is True


class TestTimeout:
    """Test timeout handling."""

    def test_result_has_execution_time(self, mock_instrument_manager):
        handler = CommandHandler(mock_instrument_manager)
        result = handler.execute_command(
            "*RST", "GPIB0::25::INSTR", timeout_ms=1000,
        )
        assert "execution_time_ms" in result
        assert isinstance(result["execution_time_ms"], float)

    def test_timeout_error_on_exception(self):
        """Simulate a timeout by raising TimeoutError."""
        mgr = MagicMock()
        mgr.is_connected.return_value = True
        mgr.write.side_effect = TimeoutError("VISA timeout")

        handler = CommandHandler(mgr)
        result = handler.execute_command(
            "*RST", "GPIB0::25::INSTR", timeout_ms=100,
        )
        assert result["success"] is False
        assert "Timeout" in result["error"]


class TestConnectionHandling:
    """Test auto-connect behaviour."""

    def test_auto_connects(self, mock_instrument_manager):
        handler = CommandHandler(mock_instrument_manager)
        # Disconnect first
        mock_instrument_manager.disconnect("GPIB0::25::INSTR")
        assert not mock_instrument_manager.is_connected("GPIB0::25::INSTR")

        result = handler.execute_command(
            "*RST", "GPIB0::25::INSTR"
        )
        # Should auto-connect
        assert result["success"] is True
        assert mock_instrument_manager.is_connected("GPIB0::25::INSTR")

    def test_connect_failure(self):
        """Test error when connection fails."""
        mgr = MagicMock()
        mgr.is_connected.return_value = False
        mgr.connect.return_value = None

        handler = CommandHandler(mgr)
        result = handler.execute_command(
            "*IDN?", "GPIB0::99::INSTR"
        )
        assert result["success"] is False
        assert "connect" in result["error"].lower()


class TestErrorWrapping:
    """Test that exceptions are wrapped in the result dict."""

    def test_visa_error_caught(self):
        mgr = MagicMock()
        mgr.is_connected.return_value = True
        mgr.query.side_effect = IOError("VISA communication error")

        handler = CommandHandler(mgr)
        result = handler.execute_command(
            "*IDN?", "GPIB0::25::INSTR"
        )
        assert result["success"] is False
        assert "error" in result["error"].lower() or "VISA" in result["error"]

    def test_result_keys_present(self, mock_instrument_manager):
        handler = CommandHandler(mock_instrument_manager)
        result = handler.execute_command(
            "*RST", "GPIB0::25::INSTR"
        )
        assert "success" in result
        assert "response" in result
        assert "error" in result
        assert "execution_time_ms" in result


class TestPerInstrumentLocking:
    """Test that per-instrument locks are created and can be released."""

    def test_lock_created(self, mock_instrument_manager):
        handler = CommandHandler(mock_instrument_manager)
        handler.execute_command("*RST", "GPIB0::25::INSTR")
        assert "GPIB0::25::INSTR" in handler._locks

    def test_release_lock(self, mock_instrument_manager):
        handler = CommandHandler(mock_instrument_manager)
        handler.execute_command("*RST", "GPIB0::25::INSTR")
        handler.release_lock("GPIB0::25::INSTR")
        assert "GPIB0::25::INSTR" not in handler._locks


class TestExecuteBinaryQuery:
    """Tests for execute_binary_query() — binary/vector data path."""

    def test_successful_binary_query(self):
        """Binary query returns success and data list."""
        mgr = MagicMock()
        mgr.is_connected.return_value = True
        mgr.query_binary_values.return_value = [1.0, 2.0, 3.0, 4.0]

        handler = CommandHandler(mgr)
        result = handler.execute_binary_query(
            ":WAV:DATA?", "TCPIP::192.168.1.1::INSTR",
        )

        assert result["success"] is True
        assert result["data"] == [1.0, 2.0, 3.0, 4.0]
        assert "execution_time_ms" in result
        mgr.query_binary_values.assert_called_once_with(
            "TCPIP::192.168.1.1::INSTR",
            ":WAV:DATA?",
            datatype='d',
            is_big_endian=False,
            timeout_ms=5000,
        )

    def test_binary_query_with_custom_datatype(self):
        """Binary query passes datatype and endianness through."""
        mgr = MagicMock()
        mgr.is_connected.return_value = True
        mgr.query_binary_values.return_value = [1.0, 2.0]

        handler = CommandHandler(mgr)
        result = handler.execute_binary_query(
            ":WAV:DATA?", "TCPIP::192.168.1.1::INSTR",
            datatype='f', is_big_endian=True, timeout_ms=10000,
        )

        assert result["success"] is True
        mgr.query_binary_values.assert_called_once_with(
            "TCPIP::192.168.1.1::INSTR",
            ":WAV:DATA?",
            datatype='f',
            is_big_endian=True,
            timeout_ms=10000,
        )

    def test_binary_query_error_returns_empty_data(self):
        """On VISA error, returns success=False and empty data list."""
        mgr = MagicMock()
        mgr.is_connected.return_value = True
        mgr.query_binary_values.side_effect = IOError("VISA transfer error")

        handler = CommandHandler(mgr)
        result = handler.execute_binary_query(
            ":WAV:DATA?", "TCPIP::192.168.1.1::INSTR",
        )

        assert result["success"] is False
        assert result["data"] == []
        assert "VISA transfer error" in result["error"]

    def test_binary_query_timeout_error(self):
        """TimeoutError returns success=False with timeout message."""
        mgr = MagicMock()
        mgr.is_connected.return_value = True
        mgr.query_binary_values.side_effect = TimeoutError("instrument timeout")

        handler = CommandHandler(mgr)
        result = handler.execute_binary_query(
            ":WAV:DATA?", "TCPIP::192.168.1.1::INSTR",
            timeout_ms=2000,
        )

        assert result["success"] is False
        assert result["data"] == []
        assert "Timeout" in result["error"]

    def test_binary_query_auto_connects(self):
        """If instrument is not connected, handler auto-connects before query."""
        mgr = MagicMock()
        mgr.is_connected.return_value = False
        mgr.connect.return_value = "TCPIP::192.168.1.1::INSTR"
        mgr.query_binary_values.return_value = [5.0, 6.0]

        handler = CommandHandler(mgr)
        result = handler.execute_binary_query(
            ":WAV:DATA?", "TCPIP::192.168.1.1::INSTR",
        )

        assert result["success"] is True
        mgr.connect.assert_called_once()

    def test_binary_query_connect_failure(self):
        """If auto-connect fails, returns error."""
        mgr = MagicMock()
        mgr.is_connected.return_value = False
        mgr.connect.return_value = None

        handler = CommandHandler(mgr)
        result = handler.execute_binary_query(
            ":WAV:DATA?", "TCPIP::192.168.1.1::INSTR",
        )

        assert result["success"] is False
        assert "Cannot connect" in result["error"]
        assert result["data"] == []

    def test_binary_query_creates_lock(self):
        """Binary query creates a per-instrument lock."""
        mgr = MagicMock()
        mgr.is_connected.return_value = True
        mgr.query_binary_values.return_value = [1.0]

        handler = CommandHandler(mgr)
        handler.execute_binary_query(
            ":WAV:DATA?", "TCPIP::192.168.1.1::INSTR",
        )

        assert "TCPIP::192.168.1.1::INSTR" in handler._locks
