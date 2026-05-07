"""
Spec G — Environment-variable reconciliation tests.

Covers all 13 rows of the test plan in spec §6.

Tests are pure unit tests: they manipulate environment variables directly
via monkeypatch and inspect the resulting Config dataclass fields.  No live
Go supervisor or daemon process is required.
"""

from __future__ import annotations

import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


# ---------------------------------------------------------------------------
# Row 1: Rescan rename — new key works
# ---------------------------------------------------------------------------


class TestRescanRenameNewKey:
    """Row 1: RESCAN_INTERVAL_SEC=120 → cfg.scan_interval_s == 120."""

    def test_new_key_works(self, monkeypatch):
        monkeypatch.setenv("RESCAN_INTERVAL_SEC", "120")
        # Remove deprecated key to ensure it's not involved
        monkeypatch.delenv("SCAN_INTERVAL_S", raising=False)

        # Re-import to pick up fresh env state via the factory function
        import importlib
        import galois_edge.config as cfg_mod
        importlib.reload(cfg_mod)

        cfg = cfg_mod.Config()
        assert cfg.scan_interval_s == 120


# ---------------------------------------------------------------------------
# Row 2 & 3: Rescan rename — deprecated key behaviour
# ---------------------------------------------------------------------------


class TestRescanRenameDeprecatedKey:
    """Rows 2 & 3: SCAN_INTERVAL_S compat-window behaviour."""

    def test_deprecated_key_returns_value_and_warns(self, monkeypatch, caplog):
        """Row 3: SCAN_INTERVAL_S=30 (only) → scan_interval_s == 30 + warning."""
        monkeypatch.delenv("RESCAN_INTERVAL_SEC", raising=False)
        monkeypatch.setenv("SCAN_INTERVAL_S", "30")

        import importlib
        import galois_edge.config as cfg_mod
        importlib.reload(cfg_mod)

        with caplog.at_level(logging.WARNING, logger="galois_edge.config"):
            cfg = cfg_mod.Config()

        assert cfg.scan_interval_s == 30
        assert any(
            "SCAN_INTERVAL_S" in r.message and "deprecated" in r.message
            for r in caplog.records
        ), "Expected a deprecation warning for SCAN_INTERVAL_S"

    def test_deprecated_key_absent_gives_default(self, monkeypatch):
        """Row 2 (post-migration): neither key set → default 60."""
        monkeypatch.delenv("RESCAN_INTERVAL_SEC", raising=False)
        monkeypatch.delenv("SCAN_INTERVAL_S", raising=False)

        import importlib
        import galois_edge.config as cfg_mod
        importlib.reload(cfg_mod)

        cfg = cfg_mod.Config()
        assert cfg.scan_interval_s == 60


# ---------------------------------------------------------------------------
# Row 4: New key wins over old key
# ---------------------------------------------------------------------------


class TestRescanRenameNewKeyWins:
    """Row 4: RESCAN_INTERVAL_SEC=90, SCAN_INTERVAL_S=30 → 90."""

    def test_new_key_wins_over_old(self, monkeypatch):
        monkeypatch.setenv("RESCAN_INTERVAL_SEC", "90")
        monkeypatch.setenv("SCAN_INTERVAL_S", "30")

        import importlib
        import galois_edge.config as cfg_mod
        importlib.reload(cfg_mod)

        cfg = cfg_mod.Config()
        assert cfg.scan_interval_s == 90


# ---------------------------------------------------------------------------
# Rows 5 & 6: GPIB enable / disable
# ---------------------------------------------------------------------------


class TestGPIBToggle:
    """Rows 5 & 6: GPIB_ENABLED=true/false."""

    def test_gpib_enable(self, monkeypatch):
        """Row 5: GPIB_ENABLED=true → cfg.gpib_enabled == True."""
        monkeypatch.setenv("GPIB_ENABLED", "true")

        import importlib
        import galois_edge.config as cfg_mod
        importlib.reload(cfg_mod)

        cfg = cfg_mod.Config()
        assert cfg.gpib_enabled is True

    def test_gpib_disable(self, monkeypatch):
        """Row 6: GPIB_ENABLED=false → cfg.gpib_enabled == False."""
        monkeypatch.setenv("GPIB_ENABLED", "false")

        import importlib
        import galois_edge.config as cfg_mod
        importlib.reload(cfg_mod)

        cfg = cfg_mod.Config()
        assert cfg.gpib_enabled is False


# ---------------------------------------------------------------------------
# Row 7: LAN instruments
# ---------------------------------------------------------------------------


class TestLanInstruments:
    """Row 7: LAN_INSTRUMENTS=TCPIP::192.0.2.1::INSTR → lan_instrument_list."""

    def test_lan_instruments_parsed(self, monkeypatch):
        monkeypatch.setenv("LAN_INSTRUMENTS", "TCPIP::192.0.2.1::INSTR")

        import importlib
        import galois_edge.config as cfg_mod
        importlib.reload(cfg_mod)

        cfg = cfg_mod.Config()
        assert cfg.lan_instrument_list == ["TCPIP::192.0.2.1::INSTR"]


# ---------------------------------------------------------------------------
# Rows 8 & 9: USB monitor default + disable
# ---------------------------------------------------------------------------


class TestUSBMonitor:
    """Rows 8 & 9: USB_MONITOR_ENABLED default and override."""

    def test_usb_monitor_default_non_windows(self, monkeypatch):
        """Row 8: On non-Windows, usb_monitor_enabled defaults to True."""
        monkeypatch.delenv("USB_MONITOR_ENABLED", raising=False)
        monkeypatch.setattr("sys.platform", "linux")

        import importlib
        import galois_edge.config as cfg_mod
        importlib.reload(cfg_mod)

        cfg = cfg_mod.Config()
        # Default is True on Linux (not Windows)
        assert cfg.usb_monitor_enabled is True

    def test_usb_monitor_disable(self, monkeypatch):
        """Row 9: USB_MONITOR_ENABLED=false → usb_monitor_enabled == False."""
        monkeypatch.setenv("USB_MONITOR_ENABLED", "false")

        import importlib
        import galois_edge.config as cfg_mod
        importlib.reload(cfg_mod)

        cfg = cfg_mod.Config()
        assert cfg.usb_monitor_enabled is False


# ---------------------------------------------------------------------------
# Row 10: ZMQ enable + port
# ---------------------------------------------------------------------------


class TestZMQ:
    """Row 10: ZMQ_ENABLED=true, ZMQ_PUB_PORT=5557."""

    def test_zmq_enable_with_custom_port(self, monkeypatch):
        monkeypatch.setenv("ZMQ_ENABLED", "true")
        monkeypatch.setenv("ZMQ_PUB_PORT", "5557")

        import importlib
        import galois_edge.config as cfg_mod
        importlib.reload(cfg_mod)

        cfg = cfg_mod.Config()
        assert cfg.zmq_enabled is True
        assert cfg.zmq_pub_port == 5557


# ---------------------------------------------------------------------------
# Row 11: Unknown-var guard fires on typo
# ---------------------------------------------------------------------------


class TestUnknownVarGuardFires:
    """Row 11: TYPO_SCAN_INTERVAL=60 → WARNING in startup log."""

    def test_unknown_var_triggers_warning(self, monkeypatch, caplog):
        monkeypatch.setenv("TYPO_SCAN_INTERVAL", "60")

        import importlib
        import galois_edge.config as cfg_mod
        importlib.reload(cfg_mod)

        with caplog.at_level(logging.WARNING, logger="galois_edge.config"):
            cfg_mod._warn_unknown_galois_vars()

        assert any(
            "TYPO_SCAN_INTERVAL" in r.message and "unrecognized" in r.message
            for r in caplog.records
        ), "Expected an unrecognized-var warning for TYPO_SCAN_INTERVAL"

    def test_rscan_interval_typo_triggers_warning(self, monkeypatch, caplog):
        """RSCAN_INTERVAL=60 (missing underscore prefix) should warn."""
        monkeypatch.setenv("RSCAN_INTERVAL", "60")

        import importlib
        import galois_edge.config as cfg_mod
        importlib.reload(cfg_mod)

        with caplog.at_level(logging.WARNING, logger="galois_edge.config"):
            cfg_mod._warn_unknown_galois_vars()

        assert any(
            "RSCAN_INTERVAL" in r.message and "unrecognized" in r.message
            for r in caplog.records
        )


# ---------------------------------------------------------------------------
# Row 12: Unknown-var guard is silent on known system vars
# ---------------------------------------------------------------------------


class TestUnknownVarGuardSilentOnSystemVars:
    """Row 12: PATH, HOME, LANG, BACKEND_URL, TAILSCALE_AUTH_KEY do NOT warn."""

    def test_no_warning_for_system_vars(self, monkeypatch, caplog):
        # These are standard system vars — should not trigger the guard.
        # Use exact-match: check that none of the warning messages reference
        # the key in single quotes (the format is: "unrecognized env var 'KEY'").
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.setenv("HOME", "/home/user")
        monkeypatch.setenv("LANG", "en_US.UTF-8")

        import importlib
        import galois_edge.config as cfg_mod
        importlib.reload(cfg_mod)

        with caplog.at_level(logging.WARNING, logger="galois_edge.config"):
            cfg_mod._warn_unknown_galois_vars()

        # Match the quoted key as it appears in the warning message format:
        # "galois-edge: unrecognized env var 'KEY' — ..."
        system_var_warnings = [
            r for r in caplog.records
            if any(f"'{sv}'" in r.message for sv in ("PATH", "HOME", "LANG"))
               and "unrecognized" in r.message
        ]
        assert not system_var_warnings, (
            f"Got unexpected warnings for system vars: {system_var_warnings}"
        )

    def test_no_warning_for_known_galois_vars(self, monkeypatch, caplog):
        """BACKEND_URL, TAILSCALE_AUTH_KEY, REGISTRATION_TOKEN are in the allow-list."""
        monkeypatch.setenv("BACKEND_URL", "https://app.galois.dev")
        monkeypatch.setenv("TAILSCALE_AUTH_KEY", "tskey-abc123")
        monkeypatch.setenv("REGISTRATION_TOKEN", "tok-xyz")

        import importlib
        import galois_edge.config as cfg_mod
        importlib.reload(cfg_mod)

        with caplog.at_level(logging.WARNING, logger="galois_edge.config"):
            cfg_mod._warn_unknown_galois_vars()

        known_var_warnings = [
            r for r in caplog.records
            if any(kv in r.message for kv in (
                "BACKEND_URL", "TAILSCALE_AUTH_KEY", "REGISTRATION_TOKEN"
            )) and "unrecognized" in r.message
        ]
        assert not known_var_warnings, (
            f"Got unexpected warnings for known Galois vars: {known_var_warnings}"
        )


# ---------------------------------------------------------------------------
# Acceptance gate: end-to-end load_config() with RESCAN_INTERVAL_SEC
# ---------------------------------------------------------------------------


class TestLoadConfigEndToEnd:
    """Verify load_config() respects RESCAN_INTERVAL_SEC end-to-end."""

    def test_load_config_rescan(self, monkeypatch):
        """RESCAN_INTERVAL_SEC=120 → cfg.scan_interval_s == 120 via load_config()."""
        monkeypatch.setenv("RESCAN_INTERVAL_SEC", "120")
        monkeypatch.delenv("SCAN_INTERVAL_S", raising=False)

        import importlib
        import galois_edge.config as cfg_mod
        importlib.reload(cfg_mod)

        cfg = cfg_mod.load_config()
        assert cfg.scan_interval_s == 120

    def test_load_config_deprecated_key(self, monkeypatch, caplog):
        """SCAN_INTERVAL_S=30 → cfg.scan_interval_s == 30 + deprecation warning."""
        monkeypatch.delenv("RESCAN_INTERVAL_SEC", raising=False)
        monkeypatch.setenv("SCAN_INTERVAL_S", "30")

        import importlib
        import galois_edge.config as cfg_mod
        importlib.reload(cfg_mod)

        with caplog.at_level(logging.WARNING, logger="galois_edge.config"):
            cfg = cfg_mod.load_config()

        assert cfg.scan_interval_s == 30
        assert any("SCAN_INTERVAL_S" in r.message for r in caplog.records)
