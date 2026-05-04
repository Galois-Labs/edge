"""
Tests for config.py -- environment variable loading and defaults.
"""

import os
import sys

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "src")
)


class TestConfigDefaults:
    """Verify that Config loads sensible defaults."""

    def test_default_grpc_port(self):
        from galois_edge.config import Config
        cfg = Config()
        assert cfg.grpc_port == 50052

    def test_default_ws_port(self):
        from galois_edge.config import Config
        cfg = Config()
        assert cfg.ws_port == 8766

    def test_default_log_level(self):
        from galois_edge.config import Config
        cfg = Config()
        assert cfg.log_level == "INFO"

    def test_default_scan_interval(self):
        from galois_edge.config import Config
        cfg = Config()
        assert cfg.scan_interval_s == 60

    def test_zmq_disabled_by_default(self):
        from galois_edge.config import Config
        cfg = Config()
        assert cfg.zmq_enabled is False

    def test_zmq_pub_port_default(self):
        from galois_edge.config import Config
        cfg = Config()
        assert cfg.zmq_pub_port == 5556


class TestConfigEnvOverride:
    """Verify that environment variables override defaults."""

    def test_grpc_port_env(self, monkeypatch):
        monkeypatch.setenv("GRPC_PORT", "55555")
        from galois_edge.config import Config
        cfg = Config()
        assert cfg.grpc_port == 55555

    def test_ws_port_env(self, monkeypatch):
        monkeypatch.setenv("WS_PORT", "9999")
        from galois_edge.config import Config
        cfg = Config()
        assert cfg.ws_port == 9999

    def test_log_level_env(self, monkeypatch):
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        from galois_edge.config import Config
        cfg = Config()
        assert cfg.log_level == "DEBUG"

    def test_gpib_enabled_env(self, monkeypatch):
        monkeypatch.setenv("GPIB_ENABLED", "false")
        from galois_edge.config import Config
        cfg = Config()
        assert cfg.gpib_enabled is False

    def test_gpib_enabled_true(self, monkeypatch):
        monkeypatch.setenv("GPIB_ENABLED", "true")
        from galois_edge.config import Config
        cfg = Config()
        assert cfg.gpib_enabled is True

    def test_scan_interval_env(self, monkeypatch):
        monkeypatch.setenv("SCAN_INTERVAL_S", "120")
        from galois_edge.config import Config
        cfg = Config()
        assert cfg.scan_interval_s == 120

    def test_zmq_enabled_env(self, monkeypatch):
        monkeypatch.setenv("ZMQ_ENABLED", "true")
        from galois_edge.config import Config
        cfg = Config()
        assert cfg.zmq_enabled is True

    def test_invalid_int_falls_to_default(self, monkeypatch):
        monkeypatch.setenv("GRPC_PORT", "not_a_number")
        from galois_edge.config import Config
        cfg = Config()
        assert cfg.grpc_port == 50052


class TestSerialInstruments:
    """Verify SERIAL_INSTRUMENTS JSON parsing."""

    def test_empty_string_returns_empty_list(self):
        from galois_edge.config import Config
        cfg = Config(serial_instruments="")
        assert cfg.serial_instrument_list == []

    def test_single_entry(self):
        import json
        from galois_edge.config import Config
        entries = [{"profile": "example_ascii_psu", "id": "psu-1", "uri": "/dev/ttyUSB0"}]
        cfg = Config(serial_instruments=json.dumps(entries))
        assert cfg.serial_instrument_list == entries

    def test_multiple_entries(self):
        import json
        from galois_edge.config import Config
        entries = [
            {"profile": "example_ascii_psu", "id": "psu-1", "uri": "/dev/ttyUSB0"},
            {"profile": "example_binary_sensor", "id": "sensor-1", "uri": "COM3"},
        ]
        cfg = Config(serial_instruments=json.dumps(entries))
        assert cfg.serial_instrument_list == entries

    def test_invalid_json_returns_empty_list(self):
        from galois_edge.config import Config
        cfg = Config(serial_instruments="not valid json {")
        assert cfg.serial_instrument_list == []

    def test_env_override(self, monkeypatch):
        import json
        monkeypatch.setenv(
            "SERIAL_INSTRUMENTS",
            json.dumps([{"profile": "p", "id": "i", "uri": "/dev/serial0"}]),
        )
        from galois_edge.config import Config
        cfg = Config()
        assert len(cfg.serial_instrument_list) == 1
        assert cfg.serial_instrument_list[0]["uri"] == "/dev/serial0"


class TestConfigLanInstruments:
    """Verify LAN_INSTRUMENTS parsing."""

    def test_empty_string(self):
        from galois_edge.config import Config
        cfg = Config(lan_instruments="")
        assert cfg.lan_instrument_list == []

    def test_single_address(self):
        from galois_edge.config import Config
        cfg = Config(lan_instruments="192.168.1.100")
        assert cfg.lan_instrument_list == ["192.168.1.100"]

    def test_multiple_addresses(self):
        from galois_edge.config import Config
        cfg = Config(lan_instruments="192.168.1.100, 192.168.1.101")
        assert cfg.lan_instrument_list == [
            "192.168.1.100", "192.168.1.101"
        ]

    def test_strips_whitespace(self):
        from galois_edge.config import Config
        cfg = Config(lan_instruments="  a , b , c  ")
        assert cfg.lan_instrument_list == ["a", "b", "c"]

    def test_filters_empty_entries(self):
        from galois_edge.config import Config
        cfg = Config(lan_instruments="a,,b,")
        assert cfg.lan_instrument_list == ["a", "b"]


class TestLoadConfig:
    """Verify the load_config() factory."""

    def test_returns_config(self):
        from galois_edge.config import load_config
        cfg = load_config()
        assert isinstance(cfg, object)
        assert hasattr(cfg, "grpc_port")

    def test_frozen(self):
        from galois_edge.config import Config
        cfg = Config()
        with pytest.raises(Exception):
            cfg.grpc_port = 9999  # type: ignore[misc]
