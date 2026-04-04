"""
Waveform Assembly — decode SCPI oscilloscope binary waveforms into VectorData.

SCPI oscilloscopes (Keysight, Rigol, Tektronix, etc.) return waveform
data as IEEE 488.2 definite-length binary blocks.  This module provides:

1. ``decode_ieee_block`` — parse the ``#<n><length><bytes>`` header and
   extract raw bytes from an IEEE 488.2 binary block.
2. ``parse_preamble`` — parse the comma-separated preamble response into
   a ``WaveformPreamble`` dataclass.
3. ``assemble_waveform`` — orchestrate the full preamble-query → data-query
   → decode → scale pipeline and return a protobuf ``VectorData``.
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# IEEE 488.2 definite-length binary block decoder
# ---------------------------------------------------------------------------

def decode_ieee_block(raw: bytes) -> bytes:
    """Decode an IEEE 488.2 definite-length binary block.

    Format::

        #<n><length><data>

    Where ``<n>`` is a single ASCII digit giving the number of digits
    in ``<length>``, and ``<length>`` is a decimal string giving the
    byte count of ``<data>``.

    Args:
        raw: The complete response from the instrument, including the
            ``#`` header.

    Returns:
        The extracted binary payload (just the data bytes).

    Raises:
        ValueError: If the header is malformed.
    """
    if not raw:
        raise ValueError("Empty response — no IEEE block data")

    # Find the '#' character (some instruments prepend whitespace or
    # a header label like "WAVeform:DATA ")
    hash_idx = -1
    for i, b in enumerate(raw):
        ch = b if isinstance(b, int) else ord(b)
        if ch == ord('#'):
            hash_idx = i
            break

    if hash_idx < 0:
        raise ValueError("No '#' found in IEEE block response")

    # <n> — number of digits in the length field
    n_idx = hash_idx + 1
    if n_idx >= len(raw):
        raise ValueError("IEEE block truncated after '#'")

    n_char = raw[n_idx:n_idx + 1]
    if isinstance(n_char, int):
        n_char = bytes([n_char])
    n = int(n_char)

    if n == 0:
        # Indefinite-length block — read until the terminator.
        # We just return everything after "#0".
        return raw[n_idx + 1:]

    # <length> — byte count
    len_start = n_idx + 1
    len_end = len_start + n
    if len_end > len(raw):
        raise ValueError(
            f"IEEE block length field extends beyond response "
            f"(need {n} digits at offset {len_start}, have {len(raw) - len_start})"
        )

    length_str = raw[len_start:len_end]
    if isinstance(length_str, memoryview):
        length_str = bytes(length_str)
    byte_count = int(length_str)

    # <data>
    data_start = len_end
    data_end = data_start + byte_count
    if data_end > len(raw):
        logger.warning(
            "IEEE block claims %d bytes but only %d available — "
            "returning what we have",
            byte_count,
            len(raw) - data_start,
        )
        return raw[data_start:]

    return raw[data_start:data_end]


# ---------------------------------------------------------------------------
# Waveform preamble
# ---------------------------------------------------------------------------

@dataclass
class WaveformPreamble:
    """Decoded oscilloscope waveform preamble.

    The 10-field format is standard across Keysight, Rigol, and many
    other SCPI oscilloscopes::

        format, type, points, count,
        xincrement, xorigin, xreference,
        yincrement, yorigin, yreference
    """
    format: int = 0       # 0=BYTE, 1=WORD, 2=ASCII, 4=FLOAT32 (vendor-dependent)
    type: int = 0         # 0=NORMAL, 1=PEAK, 2=AVERAGE, 3=HRES
    points: int = 0       # number of data points
    count: int = 1        # number of averages
    xincrement: float = 1.0
    xorigin: float = 0.0
    xreference: float = 0.0
    yincrement: float = 1.0
    yorigin: float = 0.0
    yreference: float = 0.0


def parse_preamble(response: str) -> WaveformPreamble:
    """Parse a comma-separated preamble string into a ``WaveformPreamble``.

    Args:
        response: The raw string returned by ``:WAVeform:PREamble?``
            (or ``WFMOutpre?`` on Tektronix).

    Returns:
        A ``WaveformPreamble`` with all ten fields populated.

    Raises:
        ValueError: If fewer than 10 fields are present.
    """
    parts = [p.strip() for p in response.split(",")]
    if len(parts) < 10:
        raise ValueError(
            f"Preamble has {len(parts)} fields, expected at least 10: "
            f"{response!r}"
        )

    return WaveformPreamble(
        format=int(float(parts[0])),
        type=int(float(parts[1])),
        points=int(float(parts[2])),
        count=int(float(parts[3])),
        xincrement=float(parts[4]),
        xorigin=float(parts[5]),
        xreference=float(parts[6]),
        yincrement=float(parts[7]),
        yorigin=float(parts[8]),
        yreference=float(parts[9]),
    )


def decode_raw_samples(
    raw_bytes: bytes,
    preamble: WaveformPreamble,
) -> List[float]:
    """Decode raw ADC bytes into scaled voltage values.

    Applies the standard formula::

        y_value = (raw_sample - yreference) * yincrement + yorigin

    Args:
        raw_bytes: The binary payload (after IEEE block header removal).
        preamble: The parsed preamble with scaling factors.

    Returns:
        A list of float voltage values.
    """
    fmt = preamble.format

    if fmt == 0:
        # BYTE format — unsigned 8-bit
        samples = list(raw_bytes)
    elif fmt == 1:
        # WORD format — signed 16-bit (little-endian by default on
        # Keysight/Rigol; Tektronix may use big-endian but the
        # preamble format field will tell us)
        n_samples = len(raw_bytes) // 2
        # Try little-endian first (most common for Keysight/Rigol)
        samples = list(struct.unpack(f'<{n_samples}h', raw_bytes[:n_samples * 2]))
    elif fmt == 4:
        # FLOAT32 — some instruments support this
        n_samples = len(raw_bytes) // 4
        samples = list(struct.unpack(f'<{n_samples}f', raw_bytes[:n_samples * 4]))
        # Float data is already scaled — return directly
        return samples
    else:
        # Fallback: treat as unsigned bytes
        logger.warning("Unknown preamble format %d — treating as BYTE", fmt)
        samples = list(raw_bytes)

    # Apply scaling
    y_ref = preamble.yreference
    y_inc = preamble.yincrement
    y_orig = preamble.yorigin

    return [
        (sample - y_ref) * y_inc + y_orig
        for sample in samples
    ]


# ---------------------------------------------------------------------------
# WaveformAssemblyConfig — profile-level configuration
# ---------------------------------------------------------------------------

@dataclass
class WaveformAssemblyConfig:
    """Configuration for waveform assembly on a profile command.

    Stored on ``CommandConfig.waveform_assembly`` when a profile
    declares ``waveform_assembly: { ... }``.

    The assembly engine uses these SCPI templates to:
    1. Set the waveform source channel
    2. Set the data format
    3. Query the preamble
    4. Query the binary data
    """
    source_command: str = ":WAVeform:SOURce {channel}"
    format_command: str = ":WAVeform:FORMat BYTE"
    preamble_query: str = ":WAVeform:PREamble?"
    data_query: str = ":WAVeform:DATA?"
    # Data type hint: 'byte' (unsigned 8-bit), 'word' (signed 16-bit)
    data_format: str = "byte"
    # Byte order for WORD format
    big_endian: bool = False
    # Axis labels
    x_unit: str = "s"
    y_unit: str = "V"


def build_waveform_assembly_config(data: Dict[str, Any]) -> WaveformAssemblyConfig:
    """Build a ``WaveformAssemblyConfig`` from a raw dict (parsed YAML).

    Accepts either ``waveform_assembly: true`` (use all defaults) or a
    dict with overrides for the individual fields.
    """
    if isinstance(data, bool) and data:
        return WaveformAssemblyConfig()
    if not isinstance(data, dict):
        return WaveformAssemblyConfig()

    return WaveformAssemblyConfig(
        source_command=data.get("source_command", ":WAVeform:SOURce {channel}"),
        format_command=data.get("format_command", ":WAVeform:FORMat BYTE"),
        preamble_query=data.get("preamble_query", ":WAVeform:PREamble?"),
        data_query=data.get("data_query", ":WAVeform:DATA?"),
        data_format=data.get("data_format", "byte"),
        big_endian=data.get("big_endian", False),
        x_unit=data.get("x_unit", "s"),
        y_unit=data.get("y_unit", "V"),
    )


# ---------------------------------------------------------------------------
# High-level assembly function
# ---------------------------------------------------------------------------

def assemble_waveform_vector_data(
    preamble_response: str,
    raw_data: bytes,
    wf_config: WaveformAssemblyConfig,
) -> Dict[str, Any]:
    """Assemble a waveform from preamble + raw binary data.

    This is the core function called by the gRPC server after it has
    issued the preamble query and data query.

    Args:
        preamble_response: The string response from the preamble query.
        raw_data: The raw bytes response from the data query (including
            IEEE block header).
        wf_config: Assembly configuration from the profile.

    Returns:
        A dict with keys suitable for constructing a VectorData proto:
            y_data (bytes): packed float64 values
            y_dtype (str): "float64"
            y_length (int): number of samples
            x_start (float): time of first sample
            x_increment (float): time between samples
            x_unit (str): e.g. "s"
            y_unit (str): e.g. "V"
    """
    preamble = parse_preamble(preamble_response)

    # Decode the IEEE block to get raw sample bytes
    sample_bytes = decode_ieee_block(raw_data)

    # Override preamble format based on config data_format
    if wf_config.data_format == "word":
        preamble.format = 1
    elif wf_config.data_format == "byte":
        preamble.format = 0

    # Decode and scale
    voltages = decode_raw_samples(sample_bytes, preamble)

    # Pack as float64
    y_data = struct.pack(f'<{len(voltages)}d', *voltages)

    return {
        "y_data": y_data,
        "y_dtype": "float64",
        "y_length": len(voltages),
        "x_start": preamble.xorigin,
        "x_increment": preamble.xincrement,
        "x_unit": wf_config.x_unit,
        "y_unit": wf_config.y_unit,
    }
