"""edge-api.md §9 / CI-9: uint16 binary blocks decode via struct and widen to int32 on the wire."""
from __future__ import annotations

import struct

import pytest

from galois_edge.profile_schema import ALLOWED_BINARY_DTYPES, BinaryConfig
from galois_edge.waveform_assembly import WIRE_DTYPES, decode_block_samples, decode_ieee_block

pytestmark = pytest.mark.critical
VALUES = [0, 1, 32768, 65535]


def _block(payload: bytes) -> bytes:
    n = str(len(payload))
    return b"#" + str(len(n)).encode() + n.encode() + payload + b"\n"


@pytest.mark.parametrize("order,fmt", [("little", "<4H"), ("big", ">4H")])
def test_uint16_block_widens_to_int32_on_the_wire(order, fmt):
    payload = decode_ieee_block(_block(struct.pack(fmt, *VALUES)))
    data, count, wire = decode_block_samples(payload, "uint16", order)
    assert (count, wire) == (4, "int32")
    assert wire in WIRE_DTYPES
    assert list(struct.unpack("<4i", data)) == VALUES


def test_profile_schema_accepts_uint16():
    assert "uint16" in ALLOWED_BINARY_DTYPES
    BinaryConfig(dtype="uint16").validate()
