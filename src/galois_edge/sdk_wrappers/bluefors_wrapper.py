"""BlueFors Logging wrapper — reads temperature and pressure log files.

BlueFors dilution refrigerators write temperature/pressure data to log
files on disk (typically ``/var/log/bluefors/`` or a network share).
This wrapper reads those log files — there is no VISA, SCPI, or direct
network connection to the instrument.

Log directory layout::

    <log_dir>/<YY-MM-DD>/CH1 T <YY-MM-DD>.log
    <log_dir>/<YY-MM-DD>/CH2 T <YY-MM-DD>.log
    ...
    <log_dir>/<YY-MM-DD>/maxigauge <YY-MM-DD>.log

Temperature log line format::

    DD-MM-YY,HH:MM:SS,<temperature_in_kelvin>

Pressure (maxigauge) log line format::

    DD-MM-YY,HH:MM:SS,CH<n>,<name>,<status>,<pressure>

Common temperature channels:
    1 = 50 K plate, 2 = 4 K plate, 5 = Still, 6 = MXC (mixing chamber)
"""

from __future__ import annotations

import os
import glob
import json
from datetime import date
from typing import Dict, Optional


class BlueForsClient:
    """Reads BlueFors dilution refrigerator log files."""

    def __init__(self, log_dir: str = "/var/log/bluefors") -> None:
        self.log_dir = log_dir
        self._connected = False

    # -- lifecycle -----------------------------------------------------------

    def connect(self) -> None:
        """Verify that the log directory exists."""
        if not os.path.isdir(self.log_dir):
            raise FileNotFoundError(
                f"BlueFors log directory not found: {self.log_dir}"
            )
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def get_identity(self) -> str:
        return "BlueFors,Logging,N/A,1.0"

    # -- temperature ---------------------------------------------------------

    def get_temperature(self, channel: int = 6) -> float:
        """Read latest temperature from a channel log file.

        Default channel 6 = MXC (mixing chamber plate).
        Common channels: 1=50K, 2=4K, 5=Still, 6=MXC, 8=magnet
        """
        today = date.today().strftime("%y-%m-%d")
        log_pattern = os.path.join(
            self.log_dir, today,
            f"CH{channel} T {today}.log",
        )
        files = glob.glob(log_pattern)
        if not files:
            raise FileNotFoundError(
                f"No log file for CH{channel} on {today}"
            )

        with open(files[0], "r") as f:
            lines = f.readlines()

        if not lines:
            raise ValueError(f"Empty log file for CH{channel}")

        # Last line: "DD-MM-YY,HH:MM:SS,temperature"
        last_line = lines[-1].strip()
        parts = last_line.split(",")
        if len(parts) >= 3:
            return float(parts[2])
        raise ValueError(f"Cannot parse temperature from: {last_line}")

    # -- pressure ------------------------------------------------------------

    def get_pressure(self, channel: int = 1) -> float:
        """Read latest pressure from the maxigauge log file.

        The maxigauge log uses a multi-field CSV format.  Each logical
        entry spans one line per gauge channel.  The format is::

            DD-MM-YY,HH:MM:SS,CH<n>,<name>,<status>,<pressure>

        We scan backwards to find the most recent line matching the
        requested channel number.
        """
        today = date.today().strftime("%y-%m-%d")
        log_file = os.path.join(
            self.log_dir, today,
            f"maxigauge {today}.log",
        )
        if not os.path.exists(log_file):
            raise FileNotFoundError(f"No pressure log for {today}")

        with open(log_file, "r") as f:
            lines = f.readlines()

        # Find last line for the requested channel
        for line in reversed(lines):
            parts = line.strip().split(",")
            if len(parts) >= 6:
                try:
                    ch_field = parts[2].strip()
                    # Handle both "CH1" and bare "1" formats
                    ch_num = int(ch_field.replace("CH", "").strip())
                    if ch_num == channel:
                        return float(parts[5])
                except (ValueError, IndexError):
                    continue

        raise ValueError(f"No pressure data for channel {channel}")

    # -- bulk reads ----------------------------------------------------------

    def get_all_temperatures(self) -> str:
        """Read latest temperature from all available channels.

        Returns a JSON-encoded dict mapping channel number to temperature
        in Kelvin, e.g. ``{"1": 45.2, "2": 3.8, "6": 0.012}``.
        """
        result: Dict[str, float] = {}
        for ch in range(1, 13):
            try:
                result[str(ch)] = self.get_temperature(ch)
            except (FileNotFoundError, ValueError):
                pass
        return json.dumps(result)
