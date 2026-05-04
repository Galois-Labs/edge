"""Tests for the Pi UART startup diagnostics.

Each check is unit-tested with mocked filesystem / subprocess so the suite
runs identically on macOS, Linux, and CI.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from galois_edge import pi_diagnostics


# ---------------------------------------------------------------------------
# is_raspberry_pi
# ---------------------------------------------------------------------------

class TestIsRaspberryPi:
    def test_returns_false_on_non_linux(self):
        with patch.object(pi_diagnostics.sys, "platform", "darwin"):
            assert pi_diagnostics.is_raspberry_pi() is False

    def test_returns_false_when_device_tree_missing(self):
        with patch("builtins.open", side_effect=OSError("nope")):
            with patch.object(pi_diagnostics.sys, "platform", "linux"):
                assert pi_diagnostics.is_raspberry_pi() is False

    def test_returns_true_when_device_tree_says_pi(self, tmp_path):
        from unittest.mock import mock_open
        m = mock_open(read_data=b"Raspberry Pi Zero 2 W Rev 1.0\x00")
        with patch("builtins.open", m), patch.object(pi_diagnostics.sys, "platform", "linux"):
            assert pi_diagnostics.is_raspberry_pi() is True

    def test_returns_false_for_other_arm_boards(self):
        from unittest.mock import mock_open
        m = mock_open(read_data=b"BeagleBone Black\x00")
        with patch("builtins.open", m), patch.object(pi_diagnostics.sys, "platform", "linux"):
            assert pi_diagnostics.is_raspberry_pi() is False


# ---------------------------------------------------------------------------
# serial_console_active
# ---------------------------------------------------------------------------

class TestSerialConsoleActive:
    def test_systemctl_says_active(self):
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b"")
        with patch("subprocess.run", return_value=completed):
            assert pi_diagnostics.serial_console_active() is True

    def test_falls_back_to_cmdline_when_no_systemctl(self):
        from unittest.mock import mock_open
        with patch("subprocess.run", side_effect=FileNotFoundError):
            m = mock_open(read_data="root=/dev/mmcblk0p2 console=serial0,115200 quiet")
            with patch("builtins.open", m):
                assert pi_diagnostics.serial_console_active() is True

    def test_returns_false_when_neither_console_nor_unit(self):
        completed = subprocess.CompletedProcess(args=[], returncode=3, stdout=b"", stderr=b"")
        from unittest.mock import mock_open
        m = mock_open(read_data="root=/dev/mmcblk0p2 quiet rootwait")
        with patch("subprocess.run", return_value=completed), patch("builtins.open", m):
            assert pi_diagnostics.serial_console_active() is False


# ---------------------------------------------------------------------------
# bluetooth_on_pl011
# ---------------------------------------------------------------------------

class TestBluetoothOnPl011:
    def _set_model(self, model: str):
        from unittest.mock import mock_open
        return patch("builtins.open", mock_open(read_data=model.encode("utf-8")))

    def test_returns_false_on_pi_without_onboard_bt(self):
        with self._set_model("Raspberry Pi 2 Model B Rev 1.1"):
            assert pi_diagnostics.bluetooth_on_pl011() is False

    def test_returns_false_when_disable_bt_overlay_present(self, tmp_path):
        cfg = tmp_path / "config.txt"
        cfg.write_text("[all]\ndtoverlay=disable-bt\n")

        # mock_open chained: first call reads model (Pi 4), second reads config.txt
        opens = iter([
            b"Raspberry Pi 4 Model B Rev 1.4\x00",
            cfg.read_text(),
        ])

        def fake_open(path, *args, **kwargs):
            from io import BytesIO, StringIO
            data = next(opens)
            return BytesIO(data) if isinstance(data, bytes) else StringIO(data)

        with patch("builtins.open", side_effect=fake_open), \
             patch.object(pi_diagnostics.Path, "read_text", return_value=cfg.read_text()):
            assert pi_diagnostics.bluetooth_on_pl011() is False

    def test_returns_true_on_bt_pi_without_disable(self, tmp_path):
        cfg = tmp_path / "config.txt"
        cfg.write_text("[all]\ngpu_mem=128\n")  # no disable-bt

        from unittest.mock import mock_open
        with patch("builtins.open", mock_open(read_data=b"Raspberry Pi 4 Model B\x00")), \
             patch.object(pi_diagnostics.Path, "read_text", return_value=cfg.read_text()):
            assert pi_diagnostics.bluetooth_on_pl011() is True

    def test_returns_true_when_only_commented_out(self, tmp_path):
        cfg = tmp_path / "config.txt"
        cfg.write_text("[all]\n# dtoverlay=disable-bt\n")

        from unittest.mock import mock_open
        with patch("builtins.open", mock_open(read_data=b"Raspberry Pi Zero 2 W\x00")), \
             patch.object(pi_diagnostics.Path, "read_text", return_value=cfg.read_text()):
            assert pi_diagnostics.bluetooth_on_pl011() is True


# ---------------------------------------------------------------------------
# in_dialout_group
# ---------------------------------------------------------------------------

class TestInDialoutGroup:
    def test_returns_true_when_no_dialout_group(self):
        with patch("grp.getgrnam", side_effect=KeyError("dialout")):
            assert pi_diagnostics.in_dialout_group() is True

    def test_returns_true_when_root(self):
        import types
        fake_grp = types.SimpleNamespace(gr_gid=20)
        with patch("grp.getgrnam", return_value=fake_grp), \
             patch("os.geteuid", return_value=0):
            assert pi_diagnostics.in_dialout_group() is True

    def test_returns_true_when_user_is_in_group(self):
        import types
        fake_grp = types.SimpleNamespace(gr_gid=20)
        with patch("grp.getgrnam", return_value=fake_grp), \
             patch("os.geteuid", return_value=1000), \
             patch("os.getgroups", return_value=[100, 20, 1000]):
            assert pi_diagnostics.in_dialout_group() is True

    def test_returns_false_when_user_not_in_group(self):
        import types
        fake_grp = types.SimpleNamespace(gr_gid=20)
        with patch("grp.getgrnam", return_value=fake_grp), \
             patch("os.geteuid", return_value=1000), \
             patch("os.getgroups", return_value=[100, 1000]):
            assert pi_diagnostics.in_dialout_group() is False


# ---------------------------------------------------------------------------
# run_diagnostics aggregator
# ---------------------------------------------------------------------------

class TestRunDiagnostics:
    def test_empty_when_not_a_pi(self):
        with patch.object(pi_diagnostics, "is_raspberry_pi", return_value=False):
            assert pi_diagnostics.run_diagnostics() == []

    def test_collects_all_three_issues(self):
        with patch.object(pi_diagnostics, "is_raspberry_pi", return_value=True), \
             patch.object(pi_diagnostics, "serial_console_active", return_value=True), \
             patch.object(pi_diagnostics, "bluetooth_on_pl011", return_value=True), \
             patch.object(pi_diagnostics, "in_dialout_group", return_value=False):
            issues = pi_diagnostics.run_diagnostics()
            assert len(issues) == 3
            # Each entry is (description, fix_command)
            for issue, fix in issues:
                assert issue and fix
                assert "sudo" in fix or "log out" in fix

    def test_only_collects_present_issues(self):
        with patch.object(pi_diagnostics, "is_raspberry_pi", return_value=True), \
             patch.object(pi_diagnostics, "serial_console_active", return_value=False), \
             patch.object(pi_diagnostics, "bluetooth_on_pl011", return_value=True), \
             patch.object(pi_diagnostics, "in_dialout_group", return_value=True):
            issues = pi_diagnostics.run_diagnostics()
            assert len(issues) == 1
            assert "Bluetooth" in issues[0][0]


class TestLogDiagnostics:
    def test_silent_on_non_pi(self, caplog):
        with patch.object(pi_diagnostics, "is_raspberry_pi", return_value=False):
            pi_diagnostics.log_diagnostics()
        # No warnings should be emitted
        assert not [r for r in caplog.records if r.levelname == "WARNING"]

    def test_emits_warning_block_when_issues_found(self, caplog):
        import logging
        caplog.set_level(logging.WARNING, logger="galois_edge.pi_diagnostics")
        with patch.object(pi_diagnostics, "run_diagnostics", return_value=[
            ("test issue", "test fix command"),
        ]), patch.object(pi_diagnostics, "is_raspberry_pi", return_value=True):
            pi_diagnostics.log_diagnostics()
        msgs = [r.message for r in caplog.records]
        assert any("test issue" in m for m in msgs)
        assert any("test fix command" in m for m in msgs)
        assert any("pi-setup" in m for m in msgs)

    def test_clean_pi_emits_info(self, caplog):
        import logging
        caplog.set_level(logging.INFO, logger="galois_edge.pi_diagnostics")
        with patch.object(pi_diagnostics, "is_raspberry_pi", return_value=True), \
             patch.object(pi_diagnostics, "run_diagnostics", return_value=[]):
            pi_diagnostics.log_diagnostics()
        assert any("all checks passed" in r.message for r in caplog.records)
