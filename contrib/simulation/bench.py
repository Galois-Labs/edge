"""
Optical bench physics model for the Quantifi Photonics simulation.

Models a transceiver insertion loss validation bench:
    LASER -> SWITCH -> VOA -> DUT -> POWER METER
                                  -> OSA

All instruments share state through a SimulationBench instance.
The power meter computes received power from upstream state.
The OSA generates a Gaussian spectrum trace.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List


# ---------------------------------------------------------------------------
# Insertion loss constants (dB)
# ---------------------------------------------------------------------------

IL_SWITCH = 0.8       # Switch insertion loss
IL_PATH: Dict[int, float] = {
    1: 1.3,           # Path A — normal
    2: 4.5,           # Path B — degraded (3.2 dB excess = the demo anomaly)
}
IL_PATH_DEFAULT = 1.3  # For channels 3-8
IL_DUT = 0.5          # Device under test


# ---------------------------------------------------------------------------
# Simulated instrument state classes
# ---------------------------------------------------------------------------

@dataclass
class LaserState:
    """Tunable laser source state."""
    wavelength: float = 1550.12e-9  # meters (profile uses meters)
    power: float = 6.0              # dBm
    output: bool = False
    source: int = 1
    channel: int = 1

    @property
    def wavelength_nm(self) -> float:
        return self.wavelength * 1e9


@dataclass
class SwitchState:
    """Optical switch state."""
    # channel_state[route][channel] = port value
    channel_states: Dict[tuple, int] = field(default_factory=dict)

    def get_channel(self, route: int = 1, channel: int = 1) -> int:
        return self.channel_states.get((route, channel), 1)

    def set_channel(self, value: int, route: int = 1, channel: int = 1) -> None:
        self.channel_states[(route, channel)] = value


@dataclass
class VOAState:
    """Variable optical attenuator state."""
    attenuation: float = 0.0   # dB
    slot: int = 1
    channel: int = 1
    mode: str = "ATT"          # ATT or POW
    amode: str = "ABS"         # ABS, REL, OFFSET
    wavelength: float = 1550.0  # nm


@dataclass
class PowerMeterState:
    """Optical power meter state (stateless — computes from upstream)."""
    slot: int = 1
    wavelength: float = 1550.0  # nm
    offset: float = 0.0        # dB


@dataclass
class OSAState:
    """Optical spectrum analyzer state."""
    slot: int = 1
    channel: int = 1
    wavelength_start: float = 1525.0  # nm
    wavelength_stop: float = 1575.0   # nm
    sweep_points: int = 401
    sweep_complete: bool = False


# ---------------------------------------------------------------------------
# SimulationBench — shared state + physics
# ---------------------------------------------------------------------------

class SimulationBench:
    """Shared optical bench state with physics model.

    All simulated instruments hold a reference to this bench.
    When the power meter is queried, it reads the current laser power,
    switch channel, and VOA attenuation to compute the result.
    """

    def __init__(self) -> None:
        self.laser = LaserState()
        self.switch = SwitchState()
        self.voa = VOAState()
        self.power_meter = PowerMeterState()
        self.osa = OSAState()

    def compute_received_power(self, channel: int = 1) -> float:
        """Compute optical power at the power meter (dBm).

        P_received = P_laser - IL_switch - A_VOA - IL_path - IL_DUT

        The path IL depends on which switch channel is selected.
        """
        if not self.laser.output:
            return -60.0  # Below noise floor when laser is off

        switch_channel = self.switch.get_channel()
        path_il = IL_PATH.get(switch_channel, IL_PATH_DEFAULT)

        p_received = (
            self.laser.power
            - IL_SWITCH
            - self.voa.attenuation
            - path_il
            - IL_DUT
            + self.power_meter.offset
        )
        # Clamp to realistic range
        return max(p_received, -60.0)

    def compute_voa_output_power(self) -> float:
        """Compute power at VOA built-in monitor (dBm)."""
        if not self.laser.output:
            return -60.0
        return self.laser.power - IL_SWITCH - self.voa.attenuation

    def generate_spectrum(self) -> tuple[List[float], List[float]]:
        """Generate a Gaussian spectrum trace.

        Returns (wavelengths_nm, powers_dBm) arrays.
        Center wavelength = laser wavelength.
        FWHM = 0.08 nm, peak power = laser power - upstream losses.
        """
        n_points = self.osa.sweep_points
        wl_start = self.osa.wavelength_start
        wl_stop = self.osa.wavelength_stop
        step = (wl_stop - wl_start) / (n_points - 1) if n_points > 1 else 1.0

        center = self.laser.wavelength_nm
        fwhm = 0.08  # nm
        sigma = fwhm / (2.0 * math.sqrt(2.0 * math.log(2.0)))

        if not self.laser.output:
            peak_power = -60.0
        else:
            peak_power = self.laser.power - IL_SWITCH

        wavelengths = []
        powers = []
        noise_floor = -55.0

        for i in range(n_points):
            wl = wl_start + i * step
            wavelengths.append(wl)

            # Gaussian peak
            exponent = -((wl - center) ** 2) / (2.0 * sigma ** 2)
            if exponent < -50:
                power = noise_floor
            else:
                signal = peak_power * math.exp(0) + (peak_power - noise_floor) * (math.exp(exponent) - 1)
                # Simpler: Gaussian on linear scale, convert
                linear_peak = 10 ** (peak_power / 10.0)
                linear_noise = 10 ** (noise_floor / 10.0)
                linear = linear_noise + (linear_peak - linear_noise) * math.exp(exponent)
                power = 10.0 * math.log10(max(linear, 1e-10))

            powers.append(round(power, 2))

        return wavelengths, powers
