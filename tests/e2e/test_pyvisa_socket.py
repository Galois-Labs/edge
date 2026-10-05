"""M1 criterion 2, as processes: an existing PyVISA script drives the bench over loopback SCPI sockets.

`edgesim serve --base-port 0` gives every instrument of the PSU -> 1 kΩ -> DMM bench an OS-assigned port and
prints the address map, which is authoritative (semantics §8.3). Plain pyvisa-py (`@py`) opens
`TCPIP::127.0.0.1::<port>::SOCKET` sessions with no edgesim code in the client.
"""
from __future__ import annotations

import re

import pytest

from tests.e2e.conftest import PSU_BENCH

pytestmark = [pytest.mark.e2e, pytest.mark.slow]

LISTEN = re.compile(r"TCPIP0::127\.0\.0\.1::(?P<port>[1-9]\d*)::SOCKET")


def socket_resource(listen: str) -> str:
    """The map's listen address as an existing script spells it: `TCPIP::127.0.0.1::<port>::SOCKET`."""
    match = LISTEN.fullmatch(listen)
    assert match, f"not a loopback SOCKET address with an assigned port: {listen}"
    return f"TCPIP::127.0.0.1::{match['port']}::SOCKET"


def test_pyvisa_py_configures_the_psu_and_reads_the_dmm(e2e):
    import pyvisa

    server = e2e.scpi_serve(PSU_BENCH)
    assert set(server.address_map) == {"sim-psu-1", "sim-dmm-1"}
    psu_addr, dmm_addr = (socket_resource(server.address_map[i]["listen"]) for i in ("sim-psu-1", "sim-dmm-1"))
    assert psu_addr != dmm_addr

    rm = pyvisa.ResourceManager("@py")
    sessions = []
    try:
        def open_session(address: str):
            session = rm.open_resource(address, read_termination="\n", write_termination="\n", timeout=5000)
            sessions.append(session)
            return session

        psu, dmm = open_session(psu_addr), open_session(dmm_addr)
        assert psu.query("*IDN?").startswith("GALOIS,SIM-PSU-2,")
        assert dmm.query("*IDN?").startswith("GALOIS,SIM-DMM,")
        assert float(dmm.query(":MEASure:VOLTage:DC?")) == pytest.approx(0.0, abs=1e-3)   # output still off

        psu.write(":SOURce1:VOLTage 5")
        psu.write(":SOURce1:CURRent 0.1")
        psu.write(":OUTPut1:STATe ON")
        assert psu.query("*OPC?") == "1"

        assert float(dmm.query(":MEASure:VOLTage:DC?")) == pytest.approx(5.0, rel=0.01)
        assert float(psu.query(":MEASure1:CURRent?")) == pytest.approx(0.005, rel=0.02)
        for session in (psu, dmm):
            assert session.query(":SYSTem:ERRor?") == '0,"No error"'
    finally:
        for session in sessions:
            session.close()
        rm.close()

    assert server.proc.terminate() == 0, server.proc.output_tail()
