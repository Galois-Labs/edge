"""Protocol-agnostic data point abstraction.

A Point represents a single named, typed, addressable datum on an instrument.
Protocol-specific addressing is stored in the ``addressing`` dict so the
same dataclass works for Modbus registers, OPC-UA nodes, CANopen object
dictionary entries, and future protocols without schema changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Point:
    """A named, typed, addressable datum on a device.

    Examples::

        # Modbus holding register
        Point(
            name="pv",
            data_type="float32",
            addressing={"address": 1, "register_type": "holding",
                        "length_words": 2, "byte_order": "big",
                        "word_order": "big"},
        )

        # OPC-UA node (future)
        Point(
            name="temperature",
            data_type="float64",
            addressing={"node_id": "ns=2;s=Temperature"},
        )
    """

    name: str
    data_type: str  # int16, uint16, int32, uint32, float32, float64, bool, string
    access: str = "read"  # read | read_write
    scale: float = 1.0
    unit: str = ""
    range: tuple[float, float] | None = None
    enum: dict[int, str] | None = None
    bitfield: dict[str, dict[str, Any]] | None = None
    description: str = ""
    addressing: dict[str, Any] = field(default_factory=dict)

    # -- Modbus convenience properties --

    @property
    def modbus_address(self) -> int:
        return self.addressing.get("address", 0)

    @property
    def register_type(self) -> str:
        return self.addressing.get("register_type", "holding")

    @property
    def length_words(self) -> int:
        return self.addressing.get("length_words", 1)

    @property
    def byte_order(self) -> str:
        return self.addressing.get("byte_order", "big")

    @property
    def word_order(self) -> str:
        return self.addressing.get("word_order", "big")

    @property
    def write_function_code(self) -> int | None:
        return self.addressing.get("write_function_code")

    def to_dict(self) -> dict[str, Any]:
        """Serialize to plain dict for capability advertisement."""
        d: dict[str, Any] = {
            "name": self.name,
            "data_type": self.data_type,
            "access": self.access,
            "unit": self.unit,
            "description": self.description,
        }
        if self.scale != 1.0:
            d["scale"] = self.scale
        if self.range is not None:
            d["range"] = list(self.range)
        if self.enum is not None:
            d["enum"] = self.enum
        if self.bitfield is not None:
            d["bitfield"] = self.bitfield
        return d
