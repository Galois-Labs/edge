"""
Tests for the Quantifi Photonics simulation engine.

Verifies:
- Power budget math (Path A vs Path B)
- Instrument state management (set/query round-trips)
- Spectrum generation (Gaussian peak at laser wavelength)
- IDN responses match Quantifi format
- SimulatedInstrumentManager interface compatibility
"""

from __future__ import annotations

import sys
import os
import pytest

# Ensure source and contrib are importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contrib.simulation.engine import SimulatedInstrumentManager, INSTRUMENTS
from contrib.simulation.bench import SimulationBench


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

LASER = "TCPIP::192.168.1.10::5025::SOCKET"
SWITCH = "TCPIP::192.168.1.11::5025::SOCKET"
VOA = "TCPIP::192.168.1.12::5025::SOCKET"
POWER = "TCPIP::192.168.1.13::5025::SOCKET"
OSA = "TCPIP::192.168.1.14::5025::SOCKET"


@pytest.fixture
def mgr() -> SimulatedInstrumentManager:
    m = SimulatedInstrumentManager()
    # Connect all instruments
    for addr in INSTRUMENTS:
        m.connect(addr)
    # Turn laser on for power measurements
    m.write(LASER, ":OUTPut1:CHANnel1:STATE ON")
    return m


@pytest.fixture
def bench() -> SimulationBench:
    return SimulationBench()


# ---------------------------------------------------------------------------
# Resource listing
# ---------------------------------------------------------------------------

class TestResourceListing:
    def test_list_resources_returns_5(self):
        mgr = SimulatedInstrumentManager()
        resources = mgr.list_resources()
        assert len(resources) == 5
        assert all("TCPIP" in r for r in resources)

    def test_discover_resources_same_as_list(self):
        mgr = SimulatedInstrumentManager()
        assert mgr.discover_resources() == mgr.list_resources()

    def test_rescan_all_same_as_list(self):
        mgr = SimulatedInstrumentManager()
        assert mgr.rescan_all() == mgr.list_resources()


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

class TestConnection:
    def test_connect_returns_address(self):
        mgr = SimulatedInstrumentManager()
        result = mgr.connect(LASER)
        assert result == LASER

    def test_connect_unknown_returns_none(self):
        mgr = SimulatedInstrumentManager()
        result = mgr.connect("GPIB0::99::INSTR")
        assert result is None

    def test_is_connected(self):
        mgr = SimulatedInstrumentManager()
        assert not mgr.is_connected(LASER)
        mgr.connect(LASER)
        assert mgr.is_connected(LASER)
        mgr.disconnect(LASER)
        assert not mgr.is_connected(LASER)

    def test_disconnect_all(self):
        mgr = SimulatedInstrumentManager()
        mgr.connect(LASER)
        mgr.connect(SWITCH)
        mgr.disconnect_all()
        assert not mgr.is_connected(LASER)
        assert not mgr.is_connected(SWITCH)


# ---------------------------------------------------------------------------
# IDN responses
# ---------------------------------------------------------------------------

class TestIdentity:
    def test_all_instruments_have_quantifi_idn(self, mgr):
        for addr in INSTRUMENTS:
            idn = mgr.identify(addr)
            assert "Quantifi Photonics" in idn, f"{addr} IDN missing manufacturer"

    def test_laser_idn(self, mgr):
        assert mgr.query(LASER, "*IDN?") == "Quantifi Photonics,LASER 1000,SN00001,1.0.0"

    def test_switch_idn(self, mgr):
        assert mgr.query(SWITCH, "*IDN?") == "Quantifi Photonics,SWITCH,SN00002,1.0.0"

    def test_voa_idn(self, mgr):
        assert mgr.query(VOA, "*IDN?") == "Quantifi Photonics,VOA,SN00003,1.0.0"

    def test_power_meter_idn(self, mgr):
        assert mgr.query(POWER, "*IDN?") == "Quantifi Photonics,POWER-1400,SN00004,1.0.0"

    def test_osa_idn(self, mgr):
        assert mgr.query(OSA, "*IDN?") == "Quantifi Photonics,OSA 1000,SN00005,1.0.0"


# ---------------------------------------------------------------------------
# Power budget
# ---------------------------------------------------------------------------

class TestPowerBudget:
    """Test the core physics model:
    P_received = P_laser - IL_switch - A_VOA - IL_path - IL_DUT

    P_laser = 6.0 dBm, IL_switch = 0.8 dB, IL_DUT = 0.5 dB
    Path A (ch1): IL_path = 1.3 dB -> P = 6.0 - 0.8 - 0 - 1.3 - 0.5 = 3.4 dBm
    Path B (ch2): IL_path = 4.5 dB -> P = 6.0 - 0.8 - 0 - 4.5 - 0.5 = 0.2 dBm
    """

    def test_path_a_zero_attenuation(self, mgr):
        # Switch to path A (channel 1) — default
        mgr.write(SWITCH, ":ROUTe1:CHANnel1:STATE 1")
        mgr.write(VOA, ":INPut1:CHANnel1:ATTenuation 0 DB")
        result = float(mgr.query(POWER, ":SENSe1:CHANnel1:POWer? ACT"))
        assert abs(result - 3.4) < 0.01, f"Path A at 0dB should be 3.4 dBm, got {result}"

    def test_path_b_zero_attenuation(self, mgr):
        mgr.write(SWITCH, ":ROUTe1:CHANnel1:STATE 2")
        mgr.write(VOA, ":INPut1:CHANnel1:ATTenuation 0 DB")
        result = float(mgr.query(POWER, ":SENSe1:CHANnel1:POWer? ACT"))
        assert abs(result - 0.2) < 0.01, f"Path B at 0dB should be 0.2 dBm, got {result}"

    def test_path_b_excess_loss(self, mgr):
        """Path B has 3.2 dB excess insertion loss compared to Path A."""
        mgr.write(SWITCH, ":ROUTe1:CHANnel1:STATE 1")
        mgr.write(VOA, ":INPut1:CHANnel1:ATTenuation 0 DB")
        path_a = float(mgr.query(POWER, ":SENSe1:CHANnel1:POWer? ACT"))

        mgr.write(SWITCH, ":ROUTe1:CHANnel1:STATE 2")
        path_b = float(mgr.query(POWER, ":SENSe1:CHANnel1:POWer? ACT"))

        excess = path_a - path_b
        assert abs(excess - 3.2) < 0.01, f"Excess IL should be 3.2 dB, got {excess}"

    def test_path_b_fails_threshold_at_high_attenuation(self, mgr):
        """Path B at 26 dB VOA should be below -25 dBm threshold."""
        mgr.write(SWITCH, ":ROUTe1:CHANnel1:STATE 2")
        mgr.write(VOA, ":INPut1:CHANnel1:ATTenuation 26 DB")
        result = float(mgr.query(POWER, ":SENSe1:CHANnel1:POWer? ACT"))
        assert result < -25.0, f"Path B at 26dB should be below -25 dBm, got {result}"
        assert abs(result - (-25.8)) < 0.01

    def test_path_a_passes_threshold_at_high_attenuation(self, mgr):
        """Path A at 26 dB VOA should still be above -25 dBm."""
        mgr.write(SWITCH, ":ROUTe1:CHANnel1:STATE 1")
        mgr.write(VOA, ":INPut1:CHANnel1:ATTenuation 26 DB")
        result = float(mgr.query(POWER, ":SENSe1:CHANnel1:POWer? ACT"))
        assert result > -25.0, f"Path A at 26dB should be above -25 dBm, got {result}"

    def test_laser_off_returns_noise_floor(self, mgr):
        mgr.write(LASER, ":OUTPut1:CHANnel1:STATE OFF")
        result = float(mgr.query(POWER, ":SENSe1:CHANnel1:POWer? ACT"))
        assert result == -60.0, f"Laser off should return -60 dBm, got {result}"

    def test_attenuation_sweep(self, mgr):
        """Power should decrease linearly with attenuation in dB."""
        mgr.write(SWITCH, ":ROUTe1:CHANnel1:STATE 1")
        powers = []
        for att in range(0, 21, 2):
            mgr.write(VOA, f":INPut1:CHANnel1:ATTenuation {att} DB")
            p = float(mgr.query(POWER, ":SENSe1:CHANnel1:POWer? ACT"))
            powers.append(p)

        # Each 2 dB step should decrease power by ~2 dB
        for i in range(1, len(powers)):
            diff = powers[i - 1] - powers[i]
            assert abs(diff - 2.0) < 0.01, f"Step {i}: expected 2.0 dB drop, got {diff}"


# ---------------------------------------------------------------------------
# Instrument state round-trips
# ---------------------------------------------------------------------------

class TestStateManagement:
    def test_voa_attenuation_roundtrip(self, mgr):
        mgr.write(VOA, ":INPut1:CHANnel1:ATTenuation 15.5 DB")
        result = mgr.query(VOA, ":INPut1:CHANnel1:ATTenuation? SET")
        assert result == "15.5"

    def test_switch_channel_roundtrip(self, mgr):
        mgr.write(SWITCH, ":ROUTe1:CHANnel1:STATE 3")
        result = mgr.query(SWITCH, ":ROUTe1:CHANnel1:STATE? SET")
        assert result == "3"

    def test_laser_output_roundtrip(self, mgr):
        mgr.write(LASER, ":OUTPut1:CHANnel1:STATE OFF")
        result = mgr.query(LASER, ":OUTPut1:CHANnel1:STATE?")
        assert result == "OFF"
        mgr.write(LASER, ":OUTPut1:CHANnel1:STATE ON")
        result = mgr.query(LASER, ":OUTPut1:CHANnel1:STATE?")
        assert result == "ON"

    def test_laser_wavelength_roundtrip(self, mgr):
        mgr.write(LASER, ":SOURce1:CHANnel1:WAVelength 1.55012e-06")
        result = mgr.query(LASER, ":SOURce1:CHANnel1:WAVelength? ACT")
        assert float(result) == pytest.approx(1.55012e-06, rel=1e-6)


# ---------------------------------------------------------------------------
# Spectrum generation
# ---------------------------------------------------------------------------

class TestSpectrum:
    def test_spectrum_length(self, mgr):
        data = mgr.query_binary_values(OSA, ":SENSe1:CHANnel1:SWEep:WAVelength? Y")
        assert len(data) == 401

    def test_spectrum_peak_near_laser_wavelength(self, bench):
        bench.laser.output = True
        bench.laser.wavelength = 1550.12e-9
        wavelengths, powers = bench.generate_spectrum()
        # Find peak
        max_idx = powers.index(max(powers))
        peak_wl = wavelengths[max_idx]
        assert abs(peak_wl - 1550.12) < 0.5, f"Peak at {peak_wl}, expected near 1550.12"

    def test_spectrum_stable_across_paths(self, mgr):
        """Spectrum should look the same on both paths (anomaly is IL, not laser)."""
        mgr.write(SWITCH, ":ROUTe1:CHANnel1:STATE 1")
        data_a = mgr.query(OSA, ":SENSe1:CHANnel1:SWEep:WAVelength? Y")

        mgr.write(SWITCH, ":ROUTe1:CHANnel1:STATE 2")
        data_b = mgr.query(OSA, ":SENSe1:CHANnel1:SWEep:WAVelength? Y")

        # Spectrum doesn't change with switch channel (it's before the switch in the chain)
        assert data_a == data_b


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

class TestProperties:
    def test_gpib_not_available(self):
        mgr = SimulatedInstrumentManager()
        assert mgr.gpib_available is False

    def test_lan_available(self):
        mgr = SimulatedInstrumentManager()
        assert mgr.lan_available is True

    def test_canonical_id(self):
        mgr = SimulatedInstrumentManager()
        assert mgr.canonical_id(LASER) == LASER

    def test_common_commands(self, mgr):
        for addr in INSTRUMENTS:
            assert mgr.query(addr, "*OPC?") == "1"
