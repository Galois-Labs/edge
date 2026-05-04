"""Raspberry Pi UART diagnostics.

The daemon can talk to instruments over the Pi's GPIO UART (``/dev/serial0``,
``/dev/ttyAMA0``), but three Pi-specific gotchas silently break this on a fresh
install:

1. The kernel attaches a login console to ``/dev/ttyAMA0`` by default — any
   bytes you read are mixed with the login banner.
2. On Pi 3+, Pi 4, Pi 5, and Zero 2 W the Bluetooth chip claims the high-quality
   PL011 UART, leaving ``/dev/serial0`` aliased to the mini-UART (smaller FIFO,
   core-clock-tied baud rate, more jitter).
3. The daemon process must be in the ``dialout`` group to open the device
   without root.

This module runs at daemon startup, detects these conditions on Pi hardware,
and logs a one-line warning + the exact remediation command for each. It does
not modify the system; the ``galois-edge pi-setup`` subcommand on the Go side
applies fixes with consent.
"""

from __future__ import annotations

import getpass
import grp
import logging
import os
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Hardware detection
# ---------------------------------------------------------------------------

def is_raspberry_pi() -> bool:
    """True when running on Raspberry Pi hardware (any model)."""
    if sys.platform != "linux":
        return False
    try:
        with open("/proc/device-tree/model", "rb") as f:
            return b"Raspberry Pi" in f.read()
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Individual checks — each returns True when the situation is *problematic*
# ---------------------------------------------------------------------------

def serial_console_active() -> bool:
    """True if the kernel login console is attached to a UART tty.

    Checks systemd ``serial-getty@`` units first; falls back to ``/proc/cmdline``.
    """
    for unit in ("serial-getty@ttyAMA0.service", "serial-getty@ttyS0.service"):
        try:
            r = subprocess.run(
                ["systemctl", "is-active", "--quiet", unit],
                timeout=2,
                check=False,
                capture_output=True,
            )
            if r.returncode == 0:
                return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            break  # no systemctl on this system; fall through

    try:
        with open("/proc/cmdline") as f:
            cmdline = f.read()
    except OSError:
        return False
    return any(token in cmdline for token in ("console=serial0", "console=ttyAMA0", "console=ttyS0"))


def bluetooth_on_pl011() -> bool:
    """True when Bluetooth is claiming the PL011 UART on a Pi that has BT.

    On models without on-board BT (Pi 1, Pi 2, Pi Zero v1) this returns False.
    """
    # Models with on-board Bluetooth that ship with BT bound to PL011
    bt_models = ("Pi 3", "Pi 4", "Pi 5", "Pi Zero 2", "Pi Zero W 2")
    try:
        with open("/proc/device-tree/model", "rb") as f:
            model = f.read().decode("utf-8", errors="replace")
    except OSError:
        return False
    if not any(m in model for m in bt_models):
        return False

    # Look for a config.txt overlay that frees the PL011 from BT
    for path in ("/boot/firmware/config.txt", "/boot/config.txt"):
        try:
            content = Path(path).read_text()
        except OSError:
            continue
        for raw in content.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "disable-bt" in line or "miniuart-bt" in line:
                return False
        return True  # config exists but has no BT-disabling overlay
    return False  # can't read config.txt; don't false-alarm


def in_dialout_group() -> bool:
    """True if the current process can already open /dev/serial0 without sudo."""
    try:
        dialout = grp.getgrnam("dialout")
    except KeyError:
        return True  # no dialout group on this system → not relevant
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return True  # root bypasses group membership
    try:
        return dialout.gr_gid in os.getgroups()
    except OSError:
        return True


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------

def run_diagnostics() -> list[tuple[str, str]]:
    """Return a list of (issue, fix_command) tuples for any detected problems.

    Empty list means "no Pi-specific issues" (or "not on a Pi").
    """
    if not is_raspberry_pi():
        return []

    issues: list[tuple[str, str]] = []

    if serial_console_active():
        issues.append((
            "Pi serial console is attached to /dev/ttyAMA0 — the kernel login "
            "banner will be mixed into anything you read.",
            "sudo raspi-config nonint do_serial_cons 1 && sudo reboot",
        ))

    if bluetooth_on_pl011():
        issues.append((
            "Bluetooth is using the PL011 UART. /dev/serial0 will route to "
            "the mini-UART, which has a smaller FIFO and more jitter at high baud.",
            "echo 'dtoverlay=disable-bt' | sudo tee -a /boot/firmware/config.txt && "
            "sudo systemctl disable hciuart && sudo reboot",
        ))

    if not in_dialout_group():
        try:
            user = getpass.getuser()
        except Exception:
            user = "$USER"
        issues.append((
            f"User {user!r} is not in the 'dialout' group — opening /dev/serial0 "
            f"will fail with PermissionError.",
            f"sudo usermod -aG dialout {user}  # then log out and back in",
        ))

    return issues


def log_diagnostics() -> None:
    """Run diagnostics and emit warnings. Returns nothing; safe on non-Pi systems."""
    issues = run_diagnostics()
    if not issues:
        if is_raspberry_pi():
            logger.info("Pi UART diagnostics: all checks passed.")
        return

    logger.warning("=" * 70)
    logger.warning("Raspberry Pi UART diagnostics detected %d issue(s):", len(issues))
    for issue, fix in issues:
        logger.warning("  • %s", issue)
        logger.warning("    fix: %s", fix)
    logger.warning(
        "Run 'galois-edge pi-setup' to apply these fixes interactively, "
        "or address them manually with the commands above."
    )
    logger.warning("=" * 70)
