from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class OperatingMode(str, Enum):
    CC = "CC"
    CV = "CV"


class ProtectionState(str, Enum):
    NONE = ""
    OVP = "OVP"
    OCP = "OCP"
    OPP = "OPP"
    OTP = "OTP"
    LVP = "LVP"
    REP = "REP"


@dataclass
class DeviceState:
    input_voltage: float = 0.0
    set_voltage: float = 0.0
    set_current: float = 0.0
    output_voltage: float = 0.0
    output_current: float = 0.0
    output_power: float = 0.0
    temperature: float = 0.0

    group1_set_voltage: float = 0.0
    group1_set_current: float = 0.0
    group2_set_voltage: float = 0.0
    group2_set_current: float = 0.0
    group3_set_voltage: float = 0.0
    group3_set_current: float = 0.0
    group4_set_voltage: float = 0.0
    group4_set_current: float = 0.0
    group5_set_voltage: float = 0.0
    group5_set_current: float = 0.0
    group6_set_voltage: float = 0.0
    group6_set_current: float = 0.0

    over_voltage_protection: float = 0.0
    over_current_protection: float = 0.0
    over_power_protection: float = 0.0
    over_temperature_protection: float = 0.0
    low_voltage_protection: float = 0.0

    brightness: int = 0
    volume: int = 0
    metering_closed: bool = True

    output_capacity: float = 0.0
    output_energy: float = 0.0

    output_closed: bool = False
    protection_state: str = ""
    mode: str = "CV"

    upper_limit_voltage: float = 0.0
    upper_limit_current: float = 0.0

    model_name: str = ""
    hardware_version: str = ""
    firmware_version: str = ""
