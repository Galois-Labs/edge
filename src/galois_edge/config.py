"""
Configuration for the galois-edge Python instrument engine.

Loads settings from environment variables with sensible defaults.
Supports .env files via python-dotenv. Platform-aware for Linux vs Windows.
"""

import os
import re
import sys
import logging
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # python-dotenv is optional; env vars still work without it
    pass


# ---------------------------------------------------------------------------
# G2 — Allow-list of known Galois environment variables
# ---------------------------------------------------------------------------
# Derived from the union of:
#   - Every fieldMapping key in internal/config/config.go (32 entries)
#   - INBOUND_AUTH_TOKEN (Spec C — additive, not yet merged)
#   - Python-only reads not yet in Go fieldMapping
#   - Documented passthroughs
#
# The CI drift test in tests/test_known_vars_drift.py asserts that this set
# is a superset of the Go fieldMapping keys, so it can never silently shrink.
_KNOWN_GALOIS_VARS: frozenset[str] = frozenset({
    # --- config.go fieldMapping (32 entries) ---
    "EDGE_NAME",
    "PYTHON_BIN",
    "GRPC_PORT",
    "GRPC_INTERNAL_PORT",
    "GRPC_MAX_WORKERS",
    "WS_PORT",
    "WS_INTERNAL_PORT",
    "BACKEND_URL",
    "RELAY_URL",
    "REGISTRATION_TOKEN",
    "HEARTBEAT_INTERVAL_SEC",
    "TAILSCALE_AUTH_KEY",
    "HEADSCALE_URL",
    "TSNET_STATE_DIR",
    "PROFILES_ENABLED",
    "PROFILE_DIR",
    "GPIB_ENABLED",
    "GPIB_BOARD",
    "GPIB_SCAN_ON_INIT",
    "LAN_ENABLED",
    "LAN_MDNS_ENABLED",
    "LAN_INSTRUMENTS",
    "USB_RAW_ENABLED",
    "WS_ENABLED",
    "ZMQ_ENABLED",
    "ZMQ_PUB_PORT",
    "RESCAN_INTERVAL_SEC",
    "VISA_BACKEND",
    "CONNECTION_INITIAL_BACKOFF",
    "CONNECTION_MAX_BACKOFF",
    "CONNECTION_FAILURE_THRESHOLD",
    "LOG_LEVEL",
    # --- Spec C (INBOUND_AUTH_TOKEN) — additive, lands after G ---
    "INBOUND_AUTH_TOKEN",
    # --- Python-only reads not yet in Go fieldMapping ---
    "INCLUDE_SERIAL_PORTS",
    "USB_MONITOR_ENABLED",
    "GPIB_TRICKLE_INTERVAL_S",
    "GPIB_TRICKLE_PROBE_TIMEOUT_MS",
    "DRIVER_PROFILE_DIR",
    # --- Documented passthroughs ---
    "MODBUS_INSTRUMENTS",
    "SERIAL_INSTRUMENTS",
    "DEMO_MODE",
})

# ---------------------------------------------------------------------------
# G3 — System-environment exact-match and prefix skip-lists
# ---------------------------------------------------------------------------
# Intentionally narrow: better to log a spurious warning on an unusual
# all-caps env var than to silently swallow a real config typo.
_SYSTEM_ENV_EXACT: frozenset[str] = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "PWD", "OLDPWD", "SHELL",
    "TERM", "DISPLAY", "LANG", "SHLVL", "TMPDIR", "TZ",
    "EDITOR", "VISUAL", "PAGER", "MANPATH", "INFOPATH",
    "HOSTNAME", "HOST",
    # Windows
    "PROGRAMDATA", "PROGRAMFILES", "APPDATA", "LOCALAPPDATA",
    "USERPROFILE", "USERNAME", "SYSTEMROOT", "WINDIR", "COMSPEC",
})

_SYSTEM_ENV_PREFIXES: tuple[str, ...] = (
    "LC_",       # locale family — LC_ALL, LC_CTYPE, ...
    "SSH_",      # SSH agent / connection
    "XDG_",      # XDG base-dir spec
    "GIT_",      # git internals
    "DBUS_",     # session bus
    "GNOME_", "KDE_", "QT_",
    "PYTHON",    # PYTHONPATH, PYTHONHOME, PYTHONDONTWRITEBYTECODE, etc.
    "PIP_",      # pip internals
    "VIRTUAL_ENV",  # virtualenv activation
    "CONDA_",    # conda environment
    "PYTEST_",   # pytest internals
)

# Heuristic: all-caps with underscores, no lowercase, 4+ chars, no leading digit.
_GALOIS_VAR_RE = re.compile(r'^[A-Z][A-Z0-9_]{3,}$')

_cfg_logger = logging.getLogger(__name__)


def _is_system_var(key: str) -> bool:
    """Return True if key is a known system/runtime env var, not a Galois key."""
    if key in _SYSTEM_ENV_EXACT:
        return True
    return any(key.startswith(p) for p in _SYSTEM_ENV_PREFIXES)


def _warn_unknown_galois_vars() -> None:
    """Warn at startup about env vars that look like Galois keys but aren't known.

    Called once from load_config() before any subsystem starts.  The operator
    sees these alongside the startup banner, catching typos like
    ``RSCAN_INTERVAL=60`` or stale keys left in config.env after a rename.
    """
    for key in os.environ:
        if key in _KNOWN_GALOIS_VARS:
            continue
        if not _GALOIS_VAR_RE.match(key):
            continue
        if _is_system_var(key):
            continue
        _cfg_logger.warning(
            "galois-edge: unrecognized env var '%s' — possible typo or stale config key",
            key,
        )


# ---------------------------------------------------------------------------
# Platform helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Environment-variable readers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# G1 — Compat shim: RESCAN_INTERVAL_SEC (new) with SCAN_INTERVAL_S fallback
# ---------------------------------------------------------------------------


def _rescan_interval_sec() -> int:
    """Read the rescan interval, preferring the new key over the deprecated one.

    Go canonical name: RESCAN_INTERVAL_SEC
    Deprecated Python name: SCAN_INTERVAL_S  (removed next minor release)

    If only SCAN_INTERVAL_S is set, returns its value and logs a
    DeprecationWarning so operators know to rename the key.
    """
    new_val = os.environ.get("RESCAN_INTERVAL_SEC")
    old_val = os.environ.get("SCAN_INTERVAL_S")

    if new_val is not None:
        # New key wins unconditionally; ignore old key even if both are set.
        try:
            return int(new_val)
        except ValueError:
            return 60

    if old_val is not None:
        _cfg_logger.warning(
            "galois-edge: env var 'SCAN_INTERVAL_S' is deprecated; "
            "rename to 'RESCAN_INTERVAL_SEC'"
        )
        try:
            return int(old_val)
        except ValueError:
            return 60

    return 60


# ---------------------------------------------------------------------------
# Config dataclass
# ---------------------------------------------------------------------------


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
    # G1: reads RESCAN_INTERVAL_SEC first; falls back to deprecated SCAN_INTERVAL_S.
    # The internal Python attribute name (scan_interval_s) is unchanged.
    scan_interval_s: int = field(default_factory=_rescan_interval_sec)

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
    serial_instruments: str = field(
        default_factory=lambda: _str_env("SERIAL_INSTRUMENTS", "")
    )

    # --- Demo mode (virtual instruments) ---
    demo: bool = field(
        default_factory=lambda: _bool_env("DEMO_MODE", False)
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
    def serial_instrument_list(self) -> list[dict]:
        """Parse SERIAL_INSTRUMENTS JSON string into a list of configs.

        Each entry: {"profile": "example_ascii_psu", "id": "psu-1",
                     "uri": "/dev/ttyUSB0"}.

        URIs may be a bare port path (``/dev/ttyUSB0``, ``COM3``,
        ``/dev/serial0`` for Pi GPIO UART) or a ``serial://<port>`` URI.
        """
        if not self.serial_instruments:
            return []
        import json
        try:
            return json.loads(self.serial_instruments)
        except (json.JSONDecodeError, TypeError):
            return []

    @property
    def lan_instrument_list(self) -> list[str]:
        """Parse LAN_INSTRUMENTS comma-separated string into a list."""
        if not self.lan_instruments:
            return []
        return [addr.strip() for addr in self.lan_instruments.split(",") if addr.strip()]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def load_config() -> Config:
    """Create a Config instance from the current environment.

    This is the primary entry point. Calling code should use::

        cfg = load_config()

    rather than constructing Config directly.

    Calls _warn_unknown_galois_vars() before constructing Config so that
    any typo-vars or stale config keys are surfaced alongside the startup
    banner, before any subsystem starts.
    """
    _warn_unknown_galois_vars()
    return Config()
