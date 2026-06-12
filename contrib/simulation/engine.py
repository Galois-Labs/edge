"""
SimulatedInstrumentManager — drop-in replacement for InstrumentManager.

Responds to real Quantifi Photonics SCPI commands with deterministic,
physics-based values. Implements the same interface as InstrumentManager
and MockInstrumentManager so all downstream subsystems work unchanged.

Usage:
    from contrib.simulation.engine import SimulatedInstrumentManager
    mgr = SimulatedInstrumentManager()
    mgr.connect("TCPIP::192.168.1.10::5025::SOCKET")
    print(mgr.query("TCPIP::192.168.1.10::5025::SOCKET", "*IDN?"))
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from .bench import IL_SWITCH, SimulationBench

logger = logging.getLogger(__name__)


# Regex for tokens like "1550", "1550NM", "0.1S", "-3.5DB", "1e-6", "1.55012e-06M".
_NUMERIC_WITH_UNIT = re.compile(
    r'^([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)([A-Z]+)?$'
)


def _parse_value_with_glued_unit(token: str) -> Optional[float]:
    """Parse '1550', '1550NM', '0.1S', '-3.5DB' into a float.

    Returns None if the token cannot be parsed as a numeric value
    (optionally followed by a unit suffix).
    """
    m = _NUMERIC_WITH_UNIT.match(token)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Virtual instrument VISA addresses
# ---------------------------------------------------------------------------

INSTRUMENTS = {
    "TCPIP::192.168.1.10::5025::SOCKET": {
        "type": "laser",
        "idn": "Quantifi Photonics,LASER 1000,SN00001,1.0.0",
    },
    "TCPIP::192.168.1.11::5025::SOCKET": {
        "type": "switch",
        "idn": "Quantifi Photonics,SWITCH,SN00002,1.0.0",
    },
    "TCPIP::192.168.1.12::5025::SOCKET": {
        "type": "voa",
        "idn": "Quantifi Photonics,VOA,SN00003,1.0.0",
    },
    "TCPIP::192.168.1.13::5025::SOCKET": {
        "type": "power_meter",
        "idn": "Quantifi Photonics,POWER-1400,SN00004,1.0.0",
    },
    "TCPIP::192.168.1.14::5025::SOCKET": {
        "type": "osa",
        "idn": "Quantifi Photonics,OSA 1000,SN00005,1.0.0",
    },
}


# ---------------------------------------------------------------------------
# SCPI parsing helpers
# ---------------------------------------------------------------------------

def _parse_indices(cmd: str) -> Dict[str, int]:
    """Extract numeric indices from SCPI command.

    e.g. ":SOURce1:CHANnel2:POWer?" -> {"source": 1, "channel": 2}
         ":SENSe1:CHANnel3:POWer? ACT" -> {"sense": 1, "channel": 3}
         ":ROUTe1:CHANnel1:STATE 3" -> {"route": 1, "channel": 1}
    """
    indices = {}
    # Match keyword followed by optional digits
    for m in re.finditer(r'([A-Za-z]+?)(\d+)', cmd):
        key = m.group(1).lower()
        # Normalize common prefixes
        for prefix in ("source", "sourc", "sour"):
            if key.startswith(prefix):
                key = "source"
                break
        for prefix in ("channel", "chann", "chan"):
            if key.startswith(prefix):
                key = "channel"
                break
        for prefix in ("input", "inp"):
            if key.startswith(prefix):
                key = "slot"
                break
        for prefix in ("sense", "sens"):
            if key.startswith(prefix):
                key = "slot"
                break
        for prefix in ("output", "outp"):
            if key.startswith(prefix):
                key = "output"
                break
        for prefix in ("route", "rout"):
            if key.startswith(prefix):
                key = "route"
                break
        for prefix in ("initiate", "init"):
            if key.startswith(prefix):
                key = "slot"
                break
        for prefix in ("slot",):
            if key.startswith(prefix):
                key = "slot"
                break
        for prefix in ("trigger", "trig"):
            if key.startswith(prefix):
                key = "trigger"
                break
        for prefix in ("marker", "mark"):
            if key.startswith(prefix):
                key = "marker"
                break
        for prefix in ("calculate", "calc"):
            if key.startswith(prefix):
                key = "calc"
                break
        for prefix in ("category", "categ"):
            if key.startswith(prefix):
                key = "category"
                break
        indices[key] = int(m.group(2))
    return indices


def _normalize_cmd(cmd: str) -> str:
    """Normalize SCPI command for matching.

    Strips leading/trailing whitespace, collapses whitespace,
    and uppercases for case-insensitive matching.
    """
    return " ".join(cmd.strip().split()).upper()


# ---------------------------------------------------------------------------
# Per-instrument SCPI handlers
# ---------------------------------------------------------------------------

class _LaserHandler:
    """Handle SCPI commands for the simulated laser."""

    def __init__(self, bench: SimulationBench) -> None:
        self.bench = bench

    def handle(self, cmd: str) -> Optional[str]:
        n = _normalize_cmd(cmd)
        idx = _parse_indices(cmd)

        # *IDN?
        if n == "*IDN?":
            return INSTRUMENTS["TCPIP::192.168.1.10::5025::SOCKET"]["idn"]
        if n in ("*CLS", "*RST"):
            return None
        if n == "*OPC?":
            return "1"
        if n == "*OPT?":
            return "0"
        if n == "*ESR?":
            return "0"

        # Output state
        if "STATE?" in n:
            return "ON" if self.bench.laser.output else "OFF"
        if "STATE" in n and "?" not in n:
            val = n.split()[-1]
            self.bench.laser.output = val in ("ON", "1")
            return None

        # Power query
        if "POW" in n and "?" in n:
            return str(self.bench.laser.power)
        if "POW" in n and "?" not in n:
            val = n.split()[-1]
            try:
                self.bench.laser.power = float(val)
            except ValueError:
                pass
            return None

        # Wavelength query (profile uses meters)
        if "WAV" in n and "?" in n:
            return str(self.bench.laser.wavelength)
        if "WAV" in n and "?" not in n:
            val = n.split()[-1]
            try:
                self.bench.laser.wavelength = float(val)
            except ValueError:
                pass
            return None

        # Frequency fine offset — MUST come before the plain FREQ branches,
        # otherwise "FREQ:FINE 100e6" would be interpreted as a plain
        # frequency set and corrupt the stored wavelength.
        if "FREQ" in n and "FINE" in n and "?" in n:
            return str(self.bench.laser.frequency_fine)
        if "FREQ" in n and "FINE" in n and "?" not in n:
            try:
                self.bench.laser.frequency_fine = float(n.split()[-1])
            except ValueError:
                pass
            return None

        # Frequency grid spacing — just acknowledge, no physical effect.
        if ":GRID" in n and "?" in n:
            return "50e9"
        if ":GRID" in n:
            return None

        # Whisper (low-noise) mode — acknowledge as OFF.
        if "WHIS" in n and "?" in n:
            return "OFF"
        if "WHIS" in n:
            return None

        # Frequency
        if "FREQ" in n and "?" in n:
            # Convert wavelength to frequency: f = c / lambda
            c = 299792458.0
            freq = c / self.bench.laser.wavelength if self.bench.laser.wavelength > 0 else 0
            return str(freq)
        if "FREQ" in n and "?" not in n:
            val = n.split()[-1]
            try:
                c = 299792458.0
                self.bench.laser.wavelength = c / float(val)
            except (ValueError, ZeroDivisionError):
                pass
            return None

        # Temperature
        if "TEMP" in n and "?" in n:
            return "25.3"

        # Slot queries
        if "SLOT" in n and "IDN?" in n:
            return INSTRUMENTS["TCPIP::192.168.1.10::5025::SOCKET"]["idn"]
        if "SLOT" in n and "OPC?" in n:
            return "1"
        if "SLOT" in n and "OPT" in n:
            return "LASER-1002-1-FA"

        logger.debug("Laser: unhandled command: %s", cmd)
        return ""


class _SwitchHandler:
    """Handle SCPI commands for the simulated optical switch."""

    def __init__(self, bench: SimulationBench) -> None:
        self.bench = bench

    def handle(self, cmd: str) -> Optional[str]:
        n = _normalize_cmd(cmd)
        idx = _parse_indices(cmd)

        if n == "*IDN?":
            return INSTRUMENTS["TCPIP::192.168.1.11::5025::SOCKET"]["idn"]
        if n in ("*CLS", "*RST"):
            return None
        if n == "*OPC?":
            return "1"
        if n == "*OPT?":
            return "0"
        if n == "*ESR?":
            return "0"

        route = idx.get("route", 1)
        channel = idx.get("channel", 1)

        # Channel state query
        if "STATE?" in n:
            return str(self.bench.switch.get_channel(route, channel))
        # Channel state set
        if "STATE" in n and "?" not in n:
            val = n.split()[-1]
            try:
                self.bench.switch.set_channel(int(val), route, channel)
            except ValueError:
                pass
            return None

        # Error query
        if "ERROR?" in n:
            return "0"

        # Slot queries
        if "SLOT" in n and "IDN?" in n:
            return INSTRUMENTS["TCPIP::192.168.1.11::5025::SOCKET"]["idn"]
        if "SLOT" in n and "OPC?" in n:
            return "1"
        if "SLOT" in n and "OPT" in n:
            return "SWITCH-1X8"

        logger.debug("Switch: unhandled command: %s", cmd)
        return ""


class _VOAHandler:
    """Handle SCPI commands for the simulated VOA."""

    def __init__(self, bench: SimulationBench) -> None:
        self.bench = bench

    def handle(self, cmd: str) -> Optional[str]:
        n = _normalize_cmd(cmd)
        idx = _parse_indices(cmd)

        if n == "*IDN?":
            return INSTRUMENTS["TCPIP::192.168.1.12::5025::SOCKET"]["idn"]
        if n in ("*CLS", "*RST"):
            return None
        if n == "*OPC?":
            return "1"
        if n == "*OPT?":
            return "0"
        if n == "*ESR?":
            return "0"

        # Attenuation mode (AMODE) — MUST come before the MODE check (which
        # is a substring of AMODE) and before the ATT check (since ":AMODE"
        # also contains "A").
        if "AMODE?" in n:
            return self.bench.voa.amode
        if "AMODE" in n and "?" not in n:
            val = n.split()[-1]
            if val in ("ABS", "REL", "OFFSET"):
                self.bench.voa.amode = val
            return None

        # Control mode — MUST come before the ATT branches, otherwise a
        # setter like ":CONTrol1:CHANnel1:MODE ATT" is caught by the ATT
        # set branch which then fails to parse "ATT" as a float.
        if "MODE?" in n:
            return self.bench.voa.mode
        if "MODE" in n and "?" not in n:
            val = n.split()[-1]
            if val in ("ATT", "POW"):
                self.bench.voa.mode = val
            return None

        # Attenuation query
        if "ATT" in n and "?" in n:
            return str(self.bench.voa.attenuation)
        # Attenuation set
        if "ATT" in n and "?" not in n:
            parts = n.split()
            # Value is after ATTenuation, might have unit suffix
            for p in reversed(parts):
                if p in ("DB", "MDB"):
                    continue
                try:
                    self.bench.voa.attenuation = float(p)
                    break
                except ValueError:
                    continue
            return None

        # Input power query — pre-attenuation (laser - switch IL). MUST
        # come before the bare POW? check.
        if "INPUT" in n and "POW" in n and "?" in n:
            if not self.bench.laser.output:
                return "-60.0"
            return str(round(self.bench.laser.power - IL_SWITCH, 3))

        # Output power query — post-attenuation (laser - switch IL - VOA att).
        if "OUTPUT" in n and "POW" in n and "?" in n:
            return str(round(self.bench.compute_voa_output_power(), 3))

        # Bare power query fallback (built-in monitor)
        if "POW" in n and "?" in n:
            return str(round(self.bench.compute_voa_output_power(), 3))
        # Output power set
        if "POW" in n and "?" not in n:
            # Power control mode — just acknowledge
            return None

        # Wavelength
        if "WAV" in n and "?" in n:
            return str(self.bench.voa.wavelength)
        if "WAV" in n and "?" not in n:
            parts = n.split()
            for p in reversed(parts):
                if p in ("NM", "M", "MM", "UM", "PM"):
                    continue
                try:
                    self.bench.voa.wavelength = float(p)
                    break
                except ValueError:
                    continue
            return None

        # Offset
        if "OFFS" in n and "?" in n:
            return "0.0"
        if "OFFS" in n:
            return None

        # Nulling
        if "NULL" in n:
            return None

        # Averaging time
        if "AVER" in n and "?" in n:
            return "0.1"
        if "AVER" in n:
            return None

        # Trace
        if "TRACE" in n and "COMP" in n:
            return "1"
        if "TRACE" in n and "POINT" in n and "?" in n:
            return "1024"
        if "TRACE" in n and "POINT" in n:
            return None
        if "TRACE" in n and "RATE" in n and "?" in n:
            return "1000.0"
        if "TRACE" in n and "RATE" in n:
            return None
        if "TRACE" in n and "TRIG" in n:
            return None
        if "TRACE" in n and "?" in n:
            # Return comma-separated power values
            p = self.bench.compute_voa_output_power()
            return ",".join([str(round(p, 2))] * 10)

        # Trigger
        if "TRIG" in n and "ARM?" in n:
            return "ENABLE"
        if "TRIG" in n and "ARM" in n:
            return None

        # Slot queries
        if "SLOT" in n and "IDN?" in n:
            return INSTRUMENTS["TCPIP::192.168.1.12::5025::SOCKET"]["idn"]
        if "SLOT" in n and "OPC?" in n:
            return "1"
        if "SLOT" in n and "OPT" in n:
            return "VOA-1002-2-FC"
        if "SLOT" in n and "TEST" in n:
            return "0"
        if "SLOT" in n and "RESET" in n.upper():
            return None

        logger.debug("VOA: unhandled command: %s", cmd)
        return ""


class _PowerMeterHandler:
    """Handle SCPI commands for the simulated power meter."""

    def __init__(self, bench: SimulationBench) -> None:
        self.bench = bench

    def handle(self, cmd: str) -> Optional[str]:
        n = _normalize_cmd(cmd)
        idx = _parse_indices(cmd)

        if n == "*IDN?":
            return INSTRUMENTS["TCPIP::192.168.1.13::5025::SOCKET"]["idn"]
        if n in ("*CLS", "*RST"):
            return None
        if n == "*OPC?":
            return "1"
        if n == "*OPT?":
            return "0"
        if n == "*ESR?":
            return "0"

        channel = idx.get("channel", 1)

        # Power offset — MUST be checked BEFORE the bare "POW in n and ? in n"
        # branch, otherwise ":POWer:OFFSet?" would return the computed power
        # reading instead of the stored offset.
        if "POW" in n and "OFFS" in n and "?" in n:
            return str(self.bench.power_meter.offset)
        if "POW" in n and "OFFS" in n:
            parts = n.split()
            for p in reversed(parts):
                if p in ("DB", "MDB"):
                    continue
                parsed = _parse_value_with_glued_unit(p)
                if parsed is not None:
                    self.bench.power_meter.offset = parsed
                    break
            return None

        # Averaging time — must also precede the bare POW? check, and the
        # profile renders the value/unit glued together ("0.1S").
        if "POW" in n and "AVER" in n and "?" in n:
            return str(self.bench.power_meter.averaging_time)
        if "POW" in n and "AVER" in n:
            parts = n.split()
            for p in reversed(parts):
                if p in ("S", "MS", "US", "NS"):
                    continue
                parsed = _parse_value_with_glued_unit(p)
                if parsed is not None:
                    self.bench.power_meter.averaging_time = parsed
                    break
            return None

        # Time-nulling query — must precede POW?.
        if "POW" in n and "TIME" in n and "NULL" in n and "?" in n:
            return "0.0"

        # Nulling write (no ? so not caught by POW+?)
        if "POW" in n and "NULL" in n:
            return None

        # Power measurement (bare query — comes AFTER the specific sub-command
        # checks above).
        if "POW?" in n or ("POW" in n and "?" in n):
            power = self.bench.compute_received_power(channel)
            return str(round(power, 3))

        # Power offset (legacy OFFS-only branch, now also used for non-POW
        # offset commands if any exist).
        if "OFFS" in n and "?" in n:
            return str(self.bench.power_meter.offset)
        if "OFFS" in n:
            parts = n.split()
            for p in reversed(parts):
                if p in ("DB", "MDB"):
                    continue
                parsed = _parse_value_with_glued_unit(p)
                if parsed is not None:
                    self.bench.power_meter.offset = parsed
                    break
            return None

        # Nulling
        if "NULL" in n:
            return None

        # Averaging time (bare fallback)
        if "AVER" in n and "?" in n:
            return str(self.bench.power_meter.averaging_time)
        if "AVER" in n:
            return None

        # Wavelength — profile renders "{value}{unit}" (glued, e.g. "1550NM").
        if "WAV" in n and "?" in n:
            return str(self.bench.power_meter.wavelength)
        if "WAV" in n:
            val_str = n.split()[-1]
            parsed = _parse_value_with_glued_unit(val_str)
            if parsed is not None:
                self.bench.power_meter.wavelength = parsed
            else:
                # Fallback for space-separated value/unit
                parts = n.split()
                for p in reversed(parts):
                    if p in ("NM", "M", "MM", "UM", "PM"):
                        continue
                    try:
                        self.bench.power_meter.wavelength = float(p)
                        break
                    except ValueError:
                        continue
            return None

        # Trace
        if "TRACE" in n and "COMP" in n:
            return "1"
        if "TRACE" in n and "POINT" in n and "?" in n:
            return "1024"
        if "TRACE" in n and "POINT" in n:
            return None
        if "TRACE" in n and "RATE" in n and "?" in n:
            return "1000.0"
        if "TRACE" in n and "RATE" in n:
            return None
        if "TRACE" in n and "TRIG" in n:
            return None
        if "TRACE" in n and "?" in n:
            # Return comma-separated power values
            p = self.bench.compute_received_power(channel)
            return ",".join([str(round(p, 2))] * 10)

        # Trigger
        if "TRIG" in n and "ARM?" in n:
            return "ENABLE"
        if "TRIG" in n and "ARM" in n:
            return None

        # Slot queries
        if "SLOT" in n and "IDN?" in n:
            return INSTRUMENTS["TCPIP::192.168.1.13::5025::SOCKET"]["idn"]
        if "SLOT" in n and "OPC?" in n:
            return "1"
        if "SLOT" in n and "OPT" in n:
            return "POWER-1401-4-FC"
        if "SLOT" in n and "TEST" in n:
            return "0"
        if "SLOT" in n and "RESET" in n.upper():
            return None

        logger.debug("Power meter: unhandled command: %s", cmd)
        return ""


class _OSAHandler:
    """Handle SCPI commands for the simulated OSA."""

    def __init__(self, bench: SimulationBench) -> None:
        self.bench = bench

    def handle(self, cmd: str) -> Optional[str]:
        n = _normalize_cmd(cmd)
        idx = _parse_indices(cmd)

        if n == "*IDN?":
            return INSTRUMENTS["TCPIP::192.168.1.14::5025::SOCKET"]["idn"]
        if n in ("*CLS", "*RST"):
            return None
        if n == "*OPC?":
            return "1"
        if n == "*OPT?":
            return "0"
        if n == "*ESR?":
            return "0"

        # Initiate sweep
        if "INIT" in n and "SWEEP" in n.upper().replace(":", "").replace(" ", ""):
            self.bench.osa.sweep_complete = True
            return None

        # Sweep mode
        if "SMOD" in n and "?" in n:
            return "SINGLE"
        if "SMOD" in n:
            return None

        # Wavelength start
        if "START?" in n and "WAV" in n:
            return str(self.bench.osa.wavelength_start)
        if "START" in n and "WAV" in n and "?" not in n:
            val = n.split()[-1]
            try:
                self.bench.osa.wavelength_start = float(val)
            except ValueError:
                pass
            return None

        # Wavelength stop
        if "STOP?" in n and "WAV" in n:
            return str(self.bench.osa.wavelength_stop)
        if "STOP" in n and "WAV" in n and "?" not in n:
            val = n.split()[-1]
            try:
                self.bench.osa.wavelength_stop = float(val)
            except ValueError:
                pass
            return None

        # Frequency start/stop
        if "START?" in n and "FREQ" in n:
            return "186000.0"  # GHz
        if "STOP?" in n and "FREQ" in n:
            return "196000.0"
        if ("START" in n or "STOP" in n) and "FREQ" in n:
            return None

        # Sweep data
        if "SWEEP" in n.upper().replace(":", "") and "WAV" in n and "?" in n:
            wavelengths, powers = self.bench.generate_spectrum()
            # Determine what to return based on data_type param
            parts = n.split()
            data_type = "FULL"
            for p in parts:
                if p in ("X", "Y", "FULL"):
                    data_type = p
            if data_type == "X":
                return ",".join(str(round(w, 4)) for w in wavelengths)
            elif data_type == "Y":
                return ",".join(str(p) for p in powers)
            else:
                # FULL: interleaved wavelength,power pairs
                full = []
                for w, p in zip(wavelengths, powers):
                    full.append(str(round(w, 4)))
                    full.append(str(p))
                return ",".join(full)

        # Sweep frequency data
        if "SWEEP" in n.upper().replace(":", "") and "FREQ" in n and "?" in n:
            wavelengths, powers = self.bench.generate_spectrum()
            c = 299792458.0
            freqs = [c / (w * 1e-9) / 1e9 for w in wavelengths]  # GHz
            return ",".join(str(round(f, 4)) for f in freqs)

        # Sweep points
        if "POINT" in n and "?" in n:
            return str(self.bench.osa.sweep_points)
        if "POINT" in n:
            val = n.split()[-1]
            try:
                self.bench.osa.sweep_points = int(val)
            except ValueError:
                pass
            return None

        # OSNR calculation
        if "OSNR?" in n:
            return "35.0,1550.12,0.5"  # OSNR, peak wavelength, noise BW

        # Power calculation
        if "CALC" in n and "POW" in n and "?" in n:
            return str(round(self.bench.laser.power - 0.8, 2))

        # SMSR calculation
        if "SMSR?" in n:
            return "45.0,1550.12,1550.45"  # SMSR, main mode, side mode

        # Spectral width
        if "SWTH" in n and "?" in n:
            return "0.08,1550.08,1550.16"  # width, left, right

        # Marker search
        if "MARK" in n and "MSEARCH" in n and "?" in n:
            return str(round(self.bench.laser.wavelength_nm, 4))

        # Temperature
        if "TEMP" in n and "?" in n:
            return "25.1"

        # Slot queries
        if "SLOT" in n and "IDN?" in n:
            return INSTRUMENTS["TCPIP::192.168.1.14::5025::SOCKET"]["idn"]
        if "SLOT" in n and "OPC?" in n:
            return "1"
        if "SLOT" in n and "OPT" in n:
            return "OSA-1004-1-FC"
        if "SLOT" in n and "TEST" in n:
            return "0"
        if "SLOT" in n and "RESET" in n.upper():
            return None

        logger.debug("OSA: unhandled command: %s", cmd)
        return ""


# ---------------------------------------------------------------------------
# SimulatedInstrumentManager
# ---------------------------------------------------------------------------

class SimulatedInstrumentManager:
    """Drop-in replacement for InstrumentManager using virtual instruments.

    Implements the same interface as InstrumentManager and
    MockInstrumentManager (from tests/conftest.py).
    """

    def __init__(self) -> None:
        self._bench = SimulationBench()
        self._connected: set[str] = set()
        self._resources = list(INSTRUMENTS.keys())

        # Map VISA address -> handler
        self._handlers: Dict[str, Any] = {
            "TCPIP::192.168.1.10::5025::SOCKET": _LaserHandler(self._bench),
            "TCPIP::192.168.1.11::5025::SOCKET": _SwitchHandler(self._bench),
            "TCPIP::192.168.1.12::5025::SOCKET": _VOAHandler(self._bench),
            "TCPIP::192.168.1.13::5025::SOCKET": _PowerMeterHandler(self._bench),
            "TCPIP::192.168.1.14::5025::SOCKET": _OSAHandler(self._bench),
        }

        logger.info(
            "SimulatedInstrumentManager initialized with %d virtual instruments",
            len(self._resources),
        )

    # -- Resource listing --

    def list_resources(self) -> tuple[str, ...]:
        return tuple(self._resources)

    def discover_resources(
        self,
        max_attempts: int = 5,
        initial_delay: float = 2.0,
        backoff_factor: float = 2.0,
    ) -> tuple[str, ...]:
        return self.list_resources()

    def rescan_all(self) -> tuple[str, ...]:
        return self.list_resources()

    def rescan_gpib(self) -> list[str]:
        return []

    # -- Properties --

    @property
    def gpib_available(self) -> bool:
        return False

    @property
    def usb_available(self) -> bool:
        return False

    @property
    def lan_available(self) -> bool:
        return True

    @property
    def visa_available(self) -> bool:
        return True

    # -- Connection --

    def connect(
        self,
        visa_address: str,
        timeout: int = 5000,
        max_attempts: int = 1,
        retry_delay: float = 2.0,
        serial_config: Any = None,
    ) -> Optional[str]:
        if visa_address in INSTRUMENTS:
            self._connected.add(visa_address)
            logger.info("SIM: Connected to %s", visa_address)
            return visa_address
        logger.warning("SIM: Unknown instrument address: %s", visa_address)
        return None

    def disconnect(self, instrument_id: str) -> None:
        self._connected.discard(instrument_id)

    def disconnect_all(self) -> None:
        self._connected.clear()

    def is_connected(self, instrument_id: str) -> bool:
        return instrument_id in self._connected

    def canonical_id(self, instrument_id: str) -> str:
        return instrument_id

    def mark_absent(self, visa_address: str) -> None:
        self._connected.discard(visa_address)

    def get_instrument(self, instrument_id: str) -> Optional[object]:
        return None  # No underlying transport

    # -- I/O --

    def query(self, instrument_id: str, command: str) -> str:
        handler = self._handlers.get(instrument_id)
        if handler is None:
            return ""
        result = handler.handle(command)
        if result is None:
            return ""
        return result

    def write(self, instrument_id: str, command: str) -> None:
        handler = self._handlers.get(instrument_id)
        if handler is not None:
            handler.handle(command)

    def read(self, instrument_id: str) -> str:
        return ""

    def identify(self, instrument_id: str) -> str:
        info = INSTRUMENTS.get(instrument_id)
        if info:
            return info["idn"]
        return ""

    def query_raw(self, instrument_id: str, command: str) -> bytes:
        """Raw binary-safe read — not supported for simulated instruments.

        Mirrors the transport-unsupported behaviour of the real
        InstrumentManager so the binary command path surfaces a clean
        error instead of an AttributeError.
        """
        raise ValueError(
            f"Binary (raw) reads are not supported on this transport: {instrument_id}"
        )

    def query_binary_values(
        self,
        instrument_id: str,
        command: str,
        datatype: str = "d",
        is_big_endian: bool = False,
        container: type = list,
        timeout_ms: Optional[int] = None,
    ) -> list:
        """Return binary trace data (used for OSA spectrum)."""
        handler = self._handlers.get(instrument_id)
        if handler is None:
            return []

        # For OSA, generate spectrum and return power values
        if isinstance(handler, _OSAHandler):
            _, powers = self._bench.generate_spectrum()
            return powers

        # For power meter trace, return repeated power values
        if isinstance(handler, _PowerMeterHandler):
            p = self._bench.compute_received_power()
            return [p] * 10

        return []

    def set_gpib_identity_probes(self, probes: list) -> None:
        pass
