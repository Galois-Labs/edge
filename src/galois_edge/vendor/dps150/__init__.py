"""Python driver for the FNIRSI DPS-150 programmable DC power supply."""

from .device import DPS150, SweepPoint
from .discovery import find_dps150_port, list_dps150_ports
from .exceptions import ChecksumError, DPS150Error, ValueOutOfRange
from .protocol import (
    ALL,
    BRIGHTNESS,
    CURRENT_SET,
    GROUP1_CURRENT_SET,
    GROUP1_VOLTAGE_SET,
    GROUP2_CURRENT_SET,
    GROUP2_VOLTAGE_SET,
    GROUP3_CURRENT_SET,
    GROUP3_VOLTAGE_SET,
    GROUP4_CURRENT_SET,
    GROUP4_VOLTAGE_SET,
    GROUP5_CURRENT_SET,
    GROUP5_VOLTAGE_SET,
    GROUP6_CURRENT_SET,
    GROUP6_VOLTAGE_SET,
    LVP,
    OCP,
    OPP,
    OTP,
    OVP,
    VOLTAGE_SET,
    VOLUME,
)
from .types import DeviceState, OperatingMode, ProtectionState

__version__ = "0.1.0"

__all__ = [
    "DPS150",
    "find_dps150_port",
    "list_dps150_ports",
    "DPS150Error",
    "ChecksumError",
    "ValueOutOfRange",
    "SweepPoint",
    "DeviceState",
    "OperatingMode",
    "ProtectionState",
    "VOLTAGE_SET",
    "CURRENT_SET",
    "GROUP1_VOLTAGE_SET",
    "GROUP1_CURRENT_SET",
    "GROUP2_VOLTAGE_SET",
    "GROUP2_CURRENT_SET",
    "GROUP3_VOLTAGE_SET",
    "GROUP3_CURRENT_SET",
    "GROUP4_VOLTAGE_SET",
    "GROUP4_CURRENT_SET",
    "GROUP5_VOLTAGE_SET",
    "GROUP5_CURRENT_SET",
    "GROUP6_VOLTAGE_SET",
    "GROUP6_CURRENT_SET",
    "OVP",
    "OCP",
    "OPP",
    "OTP",
    "LVP",
    "BRIGHTNESS",
    "VOLUME",
    "ALL",
]
