"""
Tests for InstrumentManager.query_raw() — the binary-safe raw read path.

IEEE 488.2 definite-length blocks can never survive the text query()
path: the str decode corrupts arbitrary bytes and the read termination
character (0x0A) terminates the read inside the payload. query_raw()
must (a) bypass text decoding entirely, (b) clear read_termination for
the read, and (c) restore it afterwards so text queries keep working.
"""

from unittest.mock import MagicMock

import pytest

from galois_edge.instrument_manager import InstrumentManager


PAYLOAD_WITH_LF = b"#212\x00\x01\x0a\x03\x04\x0a\x06\x07\x08\x09\x0a\x0b\n"


def _make_manager_with_resource(resource, address="TCPIP0::1.2.3.4::5025::SOCKET"):
    """Build an InstrumentManager with all backends disabled and a fake
    pyvisa resource pre-registered."""
    mgr = InstrumentManager(
        gpib_enabled=False,
        usb_raw_enabled=False,
        lan_instruments="",
        lan_mdns_enabled=False,
    )
    mgr._instruments[address] = resource
    return mgr, address


class _FakeResource:
    """Minimal pyvisa.Resource double tracking read_termination state."""

    def __init__(self, raw_response: bytes, read_termination="\n"):
        self.raw_response = raw_response
        self.read_termination = read_termination
        self.written = []
        self.termination_at_read = "UNSET"

    def write(self, command):
        self.written.append(command)

    def read_raw(self):
        # Capture what the termination was at the moment of the read.
        self.termination_at_read = self.read_termination
        return self.raw_response


class TestQueryRaw:
    def test_returns_raw_bytes_with_embedded_lf(self):
        resource = _FakeResource(PAYLOAD_WITH_LF)
        mgr, addr = _make_manager_with_resource(resource)

        result = mgr.query_raw(addr, ":WAVeform:DATA?")

        assert isinstance(result, bytes)
        assert result == PAYLOAD_WITH_LF
        assert b"\x0a" in result  # the byte that breaks the text path
        assert resource.written == [":WAVeform:DATA?"]

    def test_read_termination_cleared_during_read(self):
        resource = _FakeResource(b"#13abc\n", read_termination="\n")
        mgr, addr = _make_manager_with_resource(resource)

        mgr.query_raw(addr, "CURV?")

        assert resource.termination_at_read is None

    def test_read_termination_restored_after_read(self):
        resource = _FakeResource(b"#13abc\n", read_termination="\n")
        mgr, addr = _make_manager_with_resource(resource)

        mgr.query_raw(addr, "CURV?")

        assert resource.read_termination == "\n"

    def test_read_termination_restored_on_error(self):
        resource = _FakeResource(b"", read_termination="\n")
        resource.read_raw = MagicMock(side_effect=IOError("VISA timeout"))
        mgr, addr = _make_manager_with_resource(resource)

        with pytest.raises(IOError):
            mgr.query_raw(addr, "CURV?")

        assert resource.read_termination == "\n"

    def test_no_termination_left_untouched(self):
        # Resources without read_termination (e.g. USB-TMC INSTR) must
        # not have one assigned by the raw read.
        resource = _FakeResource(b"#13abc\n", read_termination=None)
        mgr, addr = _make_manager_with_resource(resource, address="USB0::1::2::3::INSTR")

        result = mgr.query_raw("USB0::1::2::3::INSTR", "CURV?")

        assert result == b"#13abc\n"
        assert resource.read_termination is None
        assert resource.termination_at_read is None

    def test_not_connected_raises(self):
        mgr = InstrumentManager(
            gpib_enabled=False,
            usb_raw_enabled=False,
            lan_instruments="",
            lan_mdns_enabled=False,
        )
        with pytest.raises(ValueError, match="not connected"):
            mgr.query_raw("TCPIP0::9.9.9.9::INSTR", "CURV?")

    def test_gpib_transport_rejected(self):
        mgr = InstrumentManager(
            gpib_enabled=False,
            usb_raw_enabled=False,
            lan_instruments="",
            lan_mdns_enabled=False,
        )
        # Simulate an available GPIB backend that owns the address.
        gpib = MagicMock()
        gpib.is_available = True
        gpib.is_gpib_address.return_value = True
        mgr._gpib = gpib

        with pytest.raises(ValueError, match="not supported on this transport"):
            mgr.query_raw("GPIB0::7::INSTR", "CURV?")

    def test_raw_usb_transport_rejected(self):
        mgr = InstrumentManager(
            gpib_enabled=False,
            usb_raw_enabled=False,
            lan_instruments="",
            lan_mdns_enabled=False,
        )
        usb = MagicMock()
        usb.is_available = True
        usb.is_usb_resource.return_value = True
        mgr._usb = usb

        with pytest.raises(ValueError, match="not supported on this transport"):
            mgr.query_raw("RAWUSB::1234::5678::SN1", "CURV?")

    def test_text_query_still_works_after_raw_read(self):
        resource = _FakeResource(b"#13abc\n", read_termination="\n")
        resource.query = MagicMock(return_value="KEYSIGHT,DSOX3024T,SN,1.0\n")
        mgr, addr = _make_manager_with_resource(resource)

        mgr.query_raw(addr, ":WAVeform:DATA?")
        text = mgr.query(addr, "*IDN?")

        assert text == "KEYSIGHT,DSOX3024T,SN,1.0"
        assert resource.read_termination == "\n"
