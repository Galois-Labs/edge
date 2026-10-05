"""edge-api.md §2: DemoBackend adapts contrib.simulation with identical behaviour."""
from __future__ import annotations

import pytest

from contrib.simulation.engine import INSTRUMENTS, SimulatedInstrumentManager
from galois_edge.backends.base import InstrumentBackend
from galois_edge.backends.demo import DEMO_PROFILE_HINTS, DemoBackend

pytestmark = pytest.mark.critical
LASER, SWITCH, VOA, POWER, OSA = list(INSTRUMENTS)
SCRIPT = [
    ("write", LASER, ":OUTPut1:CHANnel1:STATE ON"), ("write", SWITCH, ":ROUTe1:CHANnel1:STATE 2"),
    ("write", VOA, ":INPut1:CHANnel1:ATTenuation 26 DB"), ("query", POWER, ":SENSe1:CHANnel1:POWer? ACT"),
    ("query", OSA, "*IDN?"), ("query", "TCPIP::10.9.9.9::5025::SOCKET", "*IDN?"),
]


def test_shape():
    b = DemoBackend()
    assert isinstance(b, InstrumentBackend)
    assert (b.name, b.simulated, b.profile_dirs()) == ("demo", True, ())
    assert b.list_resources() == tuple(INSTRUMENTS)
    assert b.handles(LASER) and not b.handles("GPIB0::1::INSTR")


def test_profile_hints_are_the_old_overrides():
    assert DEMO_PROFILE_HINTS == {
        "TCPIP::192.168.1.10::5025::SOCKET": "quantifi_photonics_laser_1000",
        "TCPIP::192.168.1.11::5025::SOCKET": "quantifi_photonics_switch",
        "TCPIP::192.168.1.12::5025::SOCKET": "quantifi_photonics_voa",
        "TCPIP::192.168.1.13::5025::SOCKET": "quantifi_photonics_power_1400",
        "TCPIP::192.168.1.14::5025::SOCKET": "quantifi_photonics_osa_1000",
    }
    assert DemoBackend().profile_hint(LASER) == "quantifi_photonics_laser_1000"
    assert DemoBackend().profile_hint("GPIB0::1::INSTR") is None


def test_behaviour_is_identical_to_the_simulated_manager():
    sim, demo = SimulatedInstrumentManager(), DemoBackend()
    for addr in INSTRUMENTS:
        assert demo.connect(addr) == sim.connect(addr) == addr
        assert demo.identify(addr) == sim.identify(addr)
    for op, addr, cmd in SCRIPT:
        got = getattr(demo, op)(addr, cmd)
        want = getattr(sim, op)(addr, cmd)
        assert got == want, (op, addr, cmd)
    assert demo.query_binary_values(OSA, ":TRACe?") == sim.query_binary_values(OSA, ":TRACe?")
    with pytest.raises(ValueError):
        demo.query_raw(LASER, ":X?")
    with pytest.raises(ValueError):
        demo.read_binary(LASER, 4)
    demo.close()
    assert not any(demo.is_connected(a) for a in INSTRUMENTS)
