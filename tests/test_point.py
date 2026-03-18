"""Tests for the Point dataclass."""

from galois_edge.drivers.point import Point


def test_basic_construction():
    p = Point(name="pv", data_type="int16", unit="°C", scale=0.1)
    assert p.name == "pv"
    assert p.data_type == "int16"
    assert p.unit == "°C"
    assert p.scale == 0.1
    assert p.access == "read"  # default


def test_modbus_addressing():
    p = Point(
        name="temperature",
        data_type="float32",
        addressing={
            "address": 100,
            "register_type": "holding",
            "length_words": 2,
            "byte_order": "big",
            "word_order": "little",
        },
    )
    assert p.modbus_address == 100
    assert p.register_type == "holding"
    assert p.length_words == 2
    assert p.byte_order == "big"
    assert p.word_order == "little"


def test_addressing_defaults():
    p = Point(name="x", data_type="uint16")
    assert p.modbus_address == 0
    assert p.register_type == "holding"
    assert p.length_words == 1
    assert p.byte_order == "big"
    assert p.word_order == "big"
    assert p.write_function_code is None


def test_range_and_enum():
    p = Point(
        name="mode",
        data_type="uint16",
        range=(0, 10),
        enum={0: "auto", 1: "manual"},
    )
    assert p.range == (0, 10)
    assert p.enum[0] == "auto"
    assert p.enum[1] == "manual"


def test_bitfield():
    p = Point(
        name="status",
        data_type="uint16",
        bitfield={
            "alarm": {"bit": 0, "description": "Alarm active"},
            "ready": {"bit": 7, "description": "Ready"},
        },
    )
    assert p.bitfield["alarm"]["bit"] == 0
    assert p.bitfield["ready"]["bit"] == 7


def test_to_dict():
    p = Point(
        name="sp",
        data_type="int16",
        access="read_write",
        scale=0.1,
        unit="°C",
        range=(0, 100),
        enum={0: "off"},
        description="Setpoint",
    )
    d = p.to_dict()
    assert d["name"] == "sp"
    assert d["access"] == "read_write"
    assert d["scale"] == 0.1
    assert d["range"] == [0, 100]
    assert d["enum"] == {0: "off"}
    assert d["description"] == "Setpoint"


def test_to_dict_minimal():
    p = Point(name="x", data_type="uint16")
    d = p.to_dict()
    assert "scale" not in d  # default 1.0 is omitted
    assert "range" not in d
    assert "enum" not in d
    assert "bitfield" not in d
