"""
Configuration for the galois-edge Python instrument engine.

Loads settings from environment variables with sensible defaults.
Supports .env files via python-dotenv. Platform-aware for Linux vs Windows.
"""

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # python-dotenv is optional; env vars still work without it
    pass


def _is_windows() -> bool:
    return sys.platform == "win32"


def _default_profile_dir() -> str:
    """Return the default profile directory path next to this file."""
    return str(Path(__file__).parent / "profiles")


def _default_config_dir() -> str:
    """Return the platform-appropriate configuration directory."""
    if _is_windows():
        base = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
        return os.path.join(base, "galois-edge")
    return os.path.join(os.path.expanduser("~"), ".config", "galois-edge")


def _bool_env(key: str, default: bool) -> bool:
    """Read a boolean from an environment variable (true/false string)."""
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("true", "1", "yes")


def _int_env(key: str, default: int) -> int:
    """Read an integer from an environment variable."""
    raw = os.environ.get(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float_env(key: str, default: float) -> float:
    """Read a float from an environment variable."""
    raw = os.environ.get(key)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _str_env(key: str, default: str) -> str:
    """Read a string from an environment variable."""
    return os.environ.get(key, default)


@dataclass(frozen=True)
class Config:
    """Immutable configuration loaded from environment variables."""

    # --- Server ports ---
    grpc_port: int = field(default_factory=lambda: _int_env("GRPC_PORT", 50052))
    grpc_max_workers: int = field(default_factory=lambda: _int_env("GRPC_MAX_WORKERS", 10))
    ws_port: int = field(default_factory=lambda: _int_env("WS_PORT", 8766))

    # --- Logging ---
    log_level: str = field(default_factory=lambda: _str_env("LOG_LEVEL", "INFO"))

    # --- Profile system ---
    profile_dir: str = field(
        default_factory=lambda: _str_env("PROFILE_DIR", _default_profile_dir())
    )

    # --- GPIB ---
    gpib_enabled: bool = field(
        default_factory=lambda: _bool_env(
            "GPIB_ENABLED", not _is_windows()
        )
    )

    # --- Serial ports ---
    include_serial_ports: bool = field(
        default_factory=lambda: _bool_env("INCLUDE_SERIAL_PORTS", False)
    )

    # --- LAN instruments ---
    lan_instruments: str = field(
        default_factory=lambda: _str_env("LAN_INSTRUMENTS", "")
    )

    # --- Periodic rescan ---
    scan_interval_s: int = field(
        default_factory=lambda: _int_env("SCAN_INTERVAL_S", 60)
    )

    # --- GPIB trickle scanning ---
    gpib_trickle_interval_s: float = field(
        default_factory=lambda: _float_env("GPIB_TRICKLE_INTERVAL_S", 2.0)
    )
    gpib_trickle_probe_timeout_ms: int = field(
        default_factory=lambda: _int_env("GPIB_TRICKLE_PROBE_TIMEOUT_MS", 1000)
    )

    # --- USB hotplug monitoring ---
    usb_monitor_enabled: bool = field(
        default_factory=lambda: _bool_env("USB_MONITOR_ENABLED", not _is_windows())
    )

    # --- ZMQ streaming ---
    zmq_enabled: bool = field(
        default_factory=lambda: _bool_env("ZMQ_ENABLED", False)
    )
    zmq_pub_port: int = field(
        default_factory=lambda: _int_env("ZMQ_PUB_PORT", 5556)
    )

    # --- Modbus / protocol drivers ---
    driver_profile_dir: str = field(
        default_factory=lambda: _str_env(
            "DRIVER_PROFILE_DIR",
            os.path.join(_default_config_dir(), "profiles") if not _is_windows()
            else os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"), "galois-edge", "profiles"),
        )
    )
    modbus_instruments: str = field(
        default_factory=lambda: _str_env("MODBUS_INSTRUMENTS", "")
    )

    # --- Config directory (platform-aware) ---
    config_dir: str = field(default_factory=_default_config_dir)

    @property
    def modbus_instrument_list(self) -> list[dict]:
        """Parse MODBUS_INSTRUMENTS JSON string into a list of configs.

        Each entry: {"profile": "eurotherm_3504", "id": "oven-1",
                     "uri": "rtu:///dev/ttyUSB0", "slave_id": 1}
        """
        if not self.modbus_instruments:
            return []
        import json
        try:
            return json.loads(self.modbus_instruments)
        except (json.JSONDecodeError, TypeError):
            return []

    @property
    def lan_instrument_list(self) -> list[str]:
        """Parse LAN_INSTRUMENTS comma-separated string into a list."""
        if not self.lan_instruments:
            return []
        return [addr.strip() for addr in self.lan_instruments.split(",") if addr.strip()]


def load_config() -> Config:
    """Create a Config instance from the current environment.

    This is the primary entry point. Calling code should use::

        cfg = load_config()

    rather than constructing Config directly.
    """
    return Config()
