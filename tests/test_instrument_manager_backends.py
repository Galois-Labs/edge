"""edge-api.md §2: InstrumentManager routes addresses to extra backends before GPIB/USB/VISA."""
from __future__ import annotations

import pytest

from galois_edge.backends.base import InstrumentBackend
from galois_edge.instrument_manager import InstrumentManager

pytestmark = pytest.mark.critical


class FakeBackend:
    name = "fake"
    simulated = True

    def __init__(self, addrs, prefix=""):
        self.addrs = list(addrs)
        self.prefix = prefix
        self.connected: set[str] = set()
        self.calls: list[tuple] = []
        self.closed = False

    def handles(self, address): return address in self.addrs
    def list_resources(self): return tuple(self.addrs)
    def connect(self, address, timeout=5000):
        self.calls.append(("connect", address, timeout)); self.connected.add(address); return self.prefix + address
    def disconnect(self, address): self.calls.append(("disconnect", address)); self.connected.discard(address)
    def is_connected(self, address): return address in self.connected
    def write(self, address, command): self.calls.append(("write", address, command))
    def read(self, address): return "r"
    def query(self, address, command): return f"q:{command}"
    def query_raw(self, address, command): return b"#10"
    def read_binary(self, address, num_bytes): return b"\x00" * num_bytes
    def query_binary_values(self, address, command, datatype="d", is_big_endian=False, container=list, timeout_ms=None):
        return [1.0]
    def identify(self, address): return "FAKE,1"
    def profile_hint(self, address): return None
    def profile_dirs(self): return ()
    def close(self): self.closed = True


@pytest.fixture
def mgr(monkeypatch):
    # Hermetic and fast (spec §10): without a PyVISA resource manager, listing never probes
    # USB/serial or broadcasts VXI-11 discovery on the network (~1 s per list_resources()).
    monkeypatch.setattr("galois_edge.instrument_manager.PYVISA_AVAILABLE", False)
    a, b =FakeBackend(["SIM::1", "SIM::2"], prefix="canon-"), FakeBackend(["SIM::2", "SIM::3"])
    m = InstrumentManager(gpib_enabled=False, usb_raw_enabled=False, extra_backends=[a, b])
    return m, a, b


def test_fake_conforms_to_the_protocol():
    assert isinstance(FakeBackend([]), InstrumentBackend)


def test_first_handler_wins_and_unclaimed_falls_through(mgr):
    m, a, b = mgr
    assert m.extra_backends == (a, b)
    assert m.backend_for("SIM::2") is a and m.backend_for("SIM::3") is b
    assert m.backend_for("TCPIP0::10.0.0.1::5025::SOCKET") is None


def test_every_address_method_routes_to_the_backend(mgr):
    m, a, _ = mgr
    assert m.connect("SIM::1", timeout=1234, max_attempts=3, retry_delay=2.0) == "canon-SIM::1"
    assert ("connect", "SIM::1", 1234) in a.calls
    assert m.canonical_id("SIM::1") == "canon-SIM::1"
    assert m.is_connected("SIM::1") and m.get_instrument("SIM::1") is a
    assert m.query("SIM::1", "*IDN?") == "q:*IDN?"
    m.write("SIM::1", "*RST")
    assert ("write", "SIM::1", "*RST") in a.calls
    assert m.read("SIM::1") == "r" and m.identify("SIM::1") == "FAKE,1"
    assert m.query_raw("SIM::1", ":D?") == b"#10" and m.read_binary("SIM::1", 2) == b"\x00\x00"
    assert m.query_binary_values("SIM::1", ":T?", datatype="f") == [1.0]
    m.mark_absent("SIM::1")
    assert not m.is_connected("SIM::1") and m.get_instrument("SIM::1") is None


def test_listing_appends_backends_deduplicated(mgr):
    m, _, _ = mgr
    listed = m.list_resources()
    assert listed[-3:] == ("SIM::1", "SIM::2", "SIM::3") and len(set(listed)) == len(listed)
    assert m.rescan_all()[-3:] == ("SIM::1", "SIM::2", "SIM::3")


def test_disconnect_all_and_close(mgr):
    m, a, b = mgr
    m.connect("SIM::1")
    m.connect("SIM::3")
    m.disconnect_all()
    assert not a.connected and not b.connected
    m.close_backends()
    assert a.closed and b.closed


def test_gpib_accessor(mgr):
    m, _, _ = mgr
    assert m.gpib is None
    sentinel = object()
    m._gpib = sentinel
    assert m.gpib is sentinel
