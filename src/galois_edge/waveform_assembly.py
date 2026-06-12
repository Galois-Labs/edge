"""
Waveform Assembly — decode SCPI oscilloscope binary waveforms into VectorData.

SCPI oscilloscopes (Keysight, Rigol, Tektronix, etc.) return waveform
data as IEEE 488.2 definite-length binary blocks.  This module provides:

1. ``decode_ieee_block`` — parse the ``#<n><length><bytes>`` header and
   extract raw bytes from an IEEE 488.2 binary block (strict: malformed
   blocks raise ``IEEEBlockError``, never return partial data).
2. ``decode_block_samples`` — validate and normalise a block payload to
   the little-endian wire format used by ``VectorData.y_data``.
3. ``parse_preamble`` — parse the comma-separated preamble response into
   a ``WaveformPreamble`` dataclass.
4. ``compose_block_scaling`` — apply the SCPI reference-point formulas
   in code (``preamble_map`` is index-only by design).
5. ``build_vector_data`` / ``build_spectrum_info`` /
   ``populate_point_vectors`` — normative ``VectorData`` population per
   docs/daemon-clean-required-changes.md §3.0–§3.5 (explicit ``y_scale``,
   little-endian ``y_data``, pairs, spectra, multi-channel back-compat).
6. ``assemble_waveform_vector_data`` — orchestrate the full
   preamble-query → data-query → decode → scale pipeline.
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import edge_pb2

logger = logging.getLogger(__name__)


class IEEEBlockError(ValueError):
    """Raised when an IEEE 488.2 definite-length block is malformed.

    Subclasses ``ValueError`` so existing catch-all error handling in
    the poll loop and command handler keeps working; producers convert
    it into an error result (``status:"error"`` point for streams,
    ``success:false`` for one-shots) — never a crash, never a partial
    vector emitted as good data.
    """


#: Bytes per sample for each binary dtype a profile may declare.
DTYPE_SIZES = {
    "int8": 1,
    "int16": 2,
    "uint8": 1,
    "float32": 4,
    "float64": 8,
}

#: Dtypes allowed on the wire (``VectorData.y_dtype``). The cloud client
#: decodes only these five; anything else is silently reinterpreted as
#: float64 garbage (doc §2.4). ``int8`` payloads are widened to int16
#: before emission; ``uint16`` is rejected outright.
WIRE_DTYPES = {
    "float64": 8,
    "float32": 4,
    "int32": 4,
    "int16": 2,
    "uint8": 1,
}


# ---------------------------------------------------------------------------
# IEEE 488.2 definite-length binary block decoder
# ---------------------------------------------------------------------------

def decode_ieee_block(raw: bytes) -> bytes:
    """Decode an IEEE 488.2 definite-length binary block.

    Format::

        #<n><length><data>

    Where ``<n>`` is a single ASCII digit (1–9) giving the number of
    digits in ``<length>``, and ``<length>`` is a decimal string giving
    the byte count of ``<data>``. A trailing terminator (CR/LF) after
    the payload is tolerated and discarded; any other trailing bytes
    are an error (long read). The payload is byte-transparent: bytes
    such as ``0x0A``, ``0x00`` or ``#`` pass through untouched.

    Args:
        raw: The complete response from the instrument, including the
            ``#`` header. Leading ASCII whitespace before ``#`` is
            tolerated (some instruments prepend it); anything else is
            a malformed block.

    Returns:
        The extracted binary payload (exactly the declared data bytes).

    Raises:
        IEEEBlockError: On any malformed block — missing/garbled ``#``
            header, the indefinite-length ``#0`` form (unsupported),
            non-digit length field, declared length != received bytes
            (short and long reads are both errors).
    """
    if not raw:
        raise IEEEBlockError("empty response, expected IEEE 488.2 block")

    # Tolerate leading ASCII whitespace before the '#' header only —
    # never scan into arbitrary bytes for a '#'.
    start = 0
    while start < len(raw) and raw[start:start + 1] in (b" ", b"\t", b"\r", b"\n"):
        start += 1
    raw = raw[start:]

    if raw[0:1] != b"#":
        raise IEEEBlockError(
            f"bad IEEE block header: expected '#', got {raw[0:1]!r}"
        )
    if len(raw) < 2:
        raise IEEEBlockError("truncated IEEE block: missing digit count")

    digit_count_byte = raw[1:2]
    if not digit_count_byte.isdigit():
        raise IEEEBlockError(
            f"bad IEEE block digit count: {digit_count_byte!r}"
        )
    n_digits = int(digit_count_byte)
    if n_digits == 0:
        # '#0' is the indefinite-length form — explicitly unsupported
        # (a polled definite-length read cannot know where it ends).
        raise IEEEBlockError("indefinite-length IEEE block (#0) is not supported")

    header_end = 2 + n_digits
    if len(raw) < header_end:
        raise IEEEBlockError("truncated IEEE block: incomplete length field")

    length_field = raw[2:header_end]
    if not length_field.isdigit():
        raise IEEEBlockError(f"non-numeric IEEE block length: {length_field!r}")
    declared = int(length_field)

    payload = raw[header_end:header_end + declared]
    if len(payload) != declared:
        raise IEEEBlockError(
            f"IEEE block length mismatch: declared {declared} bytes, "
            f"received {len(payload)}"
        )

    trailer = raw[header_end + declared:]
    if trailer.strip(b"\r\n"):
        raise IEEEBlockError(
            f"unexpected {len(trailer)} trailing bytes after IEEE block payload"
        )

    return payload


def decode_block_samples(
    payload: bytes, dtype: str, byte_order: str = "little"
) -> Tuple[bytes, int, str]:
    """Validate and normalise a block payload for VectorData transport.

    The wire format (``VectorData.y_data``) is always little-endian —
    big-endian instrument payloads are byte-swapped here so consumers
    never need to care.  ``int8`` payloads are widened to ``int16``
    (the cloud decodes only float64|float32|int32|int16|uint8, doc
    §2.4); the raw counts are preserved so scale/offset still apply.

    Args:
        payload: Raw sample bytes from the IEEE block.
        dtype: Instrument-side sample dtype, one of
            ``int8|int16|uint8|float32|float64``.
        byte_order: ``"little"`` or ``"big"`` — order of the
            *instrument's* samples.

    Returns:
        ``(little-endian sample bytes, sample count, wire dtype)``.
        ``wire dtype`` differs from *dtype* only for ``int8`` (widened
        to ``"int16"``).

    Raises:
        IEEEBlockError: If the dtype or byte order is unknown, or the
            payload length is not a multiple of the sample size.
    """
    size = DTYPE_SIZES.get(dtype)
    if size is None:
        raise IEEEBlockError(f"unsupported binary dtype: {dtype!r}")
    if byte_order not in ("little", "big"):
        raise IEEEBlockError(f"unsupported byte order: {byte_order!r}")
    if len(payload) % size != 0:
        raise IEEEBlockError(
            f"binary payload length {len(payload)} is not a multiple of "
            f"{size}-byte {dtype} samples"
        )
    count = len(payload) // size

    if byte_order == "big" and size > 1:
        swapped = bytearray(len(payload))
        for i in range(0, len(payload), size):
            swapped[i:i + size] = payload[i:i + size][::-1]
        payload = bytes(swapped)

    if dtype == "int8":
        # Widen to int16 — int8 is not a wire dtype (doc §2.4).
        samples = struct.unpack(f"{count}b", payload)
        payload = struct.pack(f"<{count}h", *samples)
        return payload, count, "int16"

    return payload, count, dtype


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


def compose_block_scaling(values: Dict[str, float]) -> Dict[str, float]:
    """Compose SCPI reference-point preamble values into the wire model.

    ``preamble_map`` is index-only by design (doc §2.3) — the arithmetic
    lives here.  Keysight (and most SCPI scopes) define::

        time(i)  = (i - xreference) * xincrement + xorigin
        volts(i) = (raw(i) - yreference) * yincrement + yorigin

    while the ``VectorData`` wire model is ``y = raw*y_scale + y_offset``
    and ``x = x_start + i*x_increment``.  Composition (doc §2.4)::

        x_start  = xorigin - xreference * xincrement
        y_offset = yorigin - yreference * yincrement
        y_scale  = yincrement

    Args:
        values: Mapped preamble fields. Recognised keys: ``x_increment``,
            ``x_start`` (= xorigin), ``y_scale`` (= yincrement),
            ``y_offset`` (= yorigin), and the optional reference points
            ``x_reference`` / ``y_reference``.  Omit the references for
            instruments whose origin already includes them.

    Returns:
        ``{"x_start", "x_increment", "y_scale", "y_offset"}`` with
        explicit defaults (``y_scale=1.0``, ``x_increment=1.0``) when a
        field is unmapped — never a proto3 zero-default.

    Raises:
        IEEEBlockError: If the composed ``y_scale`` or ``x_increment``
            is zero — emitting either would collapse every sample on
            the client (doc §3.0).
    """
    scaling = {
        "x_start": 0.0,
        "x_increment": 1.0,
        "y_scale": 1.0,
        "y_offset": 0.0,
    }
    values = dict(values)
    y_ref = values.pop("y_reference", None)
    x_ref = values.pop("x_reference", None)
    for key in ("x_start", "x_increment", "y_scale", "y_offset"):
        if key in values:
            scaling[key] = float(values[key])
    if y_ref is not None:
        scaling["y_offset"] -= float(y_ref) * scaling["y_scale"]
    if x_ref is not None:
        scaling["x_start"] -= float(x_ref) * scaling["x_increment"]

    if scaling["y_scale"] == 0.0:
        raise IEEEBlockError(
            "preamble produced y_scale == 0; refusing to emit a vector "
            "that would collapse every sample to y_offset"
        )
    if scaling["x_increment"] == 0.0:
        raise IEEEBlockError(
            "preamble produced x_increment == 0 for a uniform sweep"
        )
    return scaling


# ---------------------------------------------------------------------------
# VectorData population (doc §3.0–§3.5)
# ---------------------------------------------------------------------------

#: Valid VectorData.pair_kind values (doc §3.3).
PAIR_KINDS = ("iq", "magphase", "xy")

#: Valid SpectrumInfo.amplitude values (doc §1.1).
SPECTRUM_AMPLITUDES = ("dbm", "dbv", "vrms", "vpk", "v2", "psd")

#: Valid SpectrumInfo.scale values (doc §1.1).
SPECTRUM_SCALES = ("log", "linear")


def build_vector_data(
    *,
    y_data: bytes,
    y_dtype: str = "float64",
    y_scale: float = 1.0,
    y_offset: float = 0.0,
    x_start: float = 0.0,
    x_increment: float = 1.0,
    x_unit: str = "",
    y_unit: str = "",
    x_name: str = "",
    x_data: bytes = b"",
    x_dtype: str = "",
    y2_data: bytes = b"",
    pair_kind: str = "",
    channel: str = "",
    y2_unit: str = "",
    spectrum: Optional[edge_pb2.SpectrumInfo] = None,
) -> edge_pb2.VectorData:
    """Build a validated ``VectorData`` message (doc §3.0–§3.4).

    This is the single producer path for vectors; its guards make the
    normative population rules unskippable:

    - ``y_scale`` is **explicitly 1.0** by default and may never be 0 —
      proto3 zero-defaults would collapse every sample to ``y_offset``
      on the client (``y = raw*y_scale + y_offset``).
    - ``x_increment`` may never be 0 on a uniform sweep (``x_data``
      empty) for the same reason on the x axis.
    - ``y_data`` (and ``x_data``/``y2_data``) are little-endian on the
      wire; callers must pass little-endian bytes (use
      ``decode_block_samples`` for instrument payloads).
    - ``y_dtype`` must be one of the five wire dtypes.
    - Pairs: ``y2_data`` and ``pair_kind`` come together or not at all;
      ``y2_data`` shares dtype, length, and the scale pair with
      ``y_data``.
    - Non-uniform x: ``x_data`` sample count must equal ``y_length``;
      ``x_start``/``x_increment`` are zeroed (consumers ignore them).

    Raises:
        ValueError: On any rule violation. Producers convert this into
            an error result; they never emit a partially valid vector.
    """
    dtype = y_dtype or "float64"
    size = WIRE_DTYPES.get(dtype)
    if size is None:
        raise ValueError(
            f"y_dtype {dtype!r} is not a wire dtype "
            f"(must be one of {sorted(WIRE_DTYPES)})"
        )
    if len(y_data) % size != 0:
        raise ValueError(
            f"y_data length {len(y_data)} is not a multiple of "
            f"{size}-byte {dtype} samples"
        )
    y_length = len(y_data) // size

    if y_scale == 0.0:
        raise ValueError(
            "y_scale must never be 0 — set it explicitly to 1.0 when no "
            "scaling applies (doc §3.0)"
        )

    if pair_kind and pair_kind not in PAIR_KINDS:
        raise ValueError(
            f"pair_kind {pair_kind!r} must be one of {PAIR_KINDS} or empty"
        )
    if y2_data and not pair_kind:
        raise ValueError("y2_data requires a non-empty pair_kind (doc §3.3)")
    if pair_kind and not y2_data:
        raise ValueError("pair_kind requires y2_data (doc §3.3)")
    if y2_data and len(y2_data) != len(y_data):
        raise ValueError(
            f"y2_data length {len(y2_data)} != y_data length {len(y_data)} "
            "(same dtype and sample count required, doc §3.3)"
        )

    if x_data:
        x_dt = x_dtype or "float64"
        x_size = WIRE_DTYPES.get(x_dt)
        if x_size is None:
            raise ValueError(
                f"x_dtype {x_dt!r} is not a wire dtype "
                f"(must be one of {sorted(WIRE_DTYPES)})"
            )
        if len(x_data) != y_length * x_size:
            raise ValueError(
                f"x_data carries {len(x_data) // x_size} samples but "
                f"y_length is {y_length} (doc §3.2)"
            )
        # Consumers ignore x_start/x_increment when x_data is present;
        # zero them so nothing downstream mistakes them for a timebase.
        x_start = 0.0
        x_increment = 0.0
    else:
        if x_increment == 0.0:
            raise ValueError(
                "x_increment must never be 0 on a uniform sweep — every "
                "sample would land at x_start (doc §3.0)"
            )

    vector = edge_pb2.VectorData(
        y_data=y_data,
        y_dtype=dtype,
        y_length=y_length,
        x_start=x_start,
        x_increment=x_increment,
        x_unit=x_unit,
        y_unit=y_unit,
        x_name=x_name,
        y_scale=y_scale,
        y_offset=y_offset,
        x_data=x_data,
        x_dtype=x_dtype if x_data else "",
        y2_data=y2_data,
        pair_kind=pair_kind,
        channel=channel,
        y2_unit=y2_unit,
    )
    if spectrum is not None:
        vector.spectrum.CopyFrom(spectrum)
    return vector


def build_spectrum_info(
    *,
    amplitude: str,
    scale: str,
    rbw_hz: float = 0.0,
    vbw_hz: float = 0.0,
    ref_level: Optional[float] = None,
    window: str = "",
    averages: int = 0,
) -> edge_pb2.SpectrumInfo:
    """Build a validated ``SpectrumInfo`` message (doc §1.1, §3.4).

    ``amplitude``/``scale`` declare what the y values *are as sent* —
    the cloud renders with no client transformation.  ``ref_level``
    uses explicit proto3 presence: pass it only when known (0 dBm is a
    valid ref level, so a zero-sentinel cannot mean "unknown").
    ``rbw_hz``/``vbw_hz``/``averages`` use 0 as the unknown sentinel.

    Raises:
        ValueError: If ``amplitude`` or ``scale`` is not one of the
            coordinated lowercase enums.
    """
    if amplitude not in SPECTRUM_AMPLITUDES:
        raise ValueError(
            f"spectrum amplitude {amplitude!r} must be one of "
            f"{SPECTRUM_AMPLITUDES}"
        )
    if scale not in SPECTRUM_SCALES:
        raise ValueError(
            f"spectrum scale {scale!r} must be one of {SPECTRUM_SCALES}"
        )
    info = edge_pb2.SpectrumInfo(
        amplitude=amplitude,
        scale=scale,
        rbw_hz=rbw_hz,
        vbw_hz=vbw_hz,
        window=window,
        averages=averages,
    )
    if ref_level is not None:
        info.ref_level = ref_level
    return info


def populate_point_vectors(
    point: edge_pb2.MeasurementDataPoint,
    vectors: Sequence[edge_pb2.VectorData],
) -> edge_pb2.MeasurementDataPoint:
    """Fill a multi-channel frame onto a ``MeasurementDataPoint`` (§3.5).

    Fills ``vectors`` (field 9) with one ``VectorData`` per channel AND
    — the back-compat producer rule — ``vector_data`` (field 8) with the
    **first** channel, so pre-``vectors`` clouds keep rendering one
    trace instead of nothing.  New clouds ignore field 8 when
    ``vectors`` is non-empty.

    Args:
        point: The point to populate (mutated in place).
        vectors: One validated ``VectorData`` per channel, each with its
            ``channel`` label set (single-channel frames may pass one).

    Returns:
        The same *point*, for chaining.

    Raises:
        ValueError: If *vectors* is empty.
    """
    if not vectors:
        raise ValueError("populate_point_vectors requires at least one vector")
    point.vector_data.CopyFrom(vectors[0])
    if len(vectors) > 1:
        del point.vectors[:]
        point.vectors.extend(vectors)
    return point


def vector_data_from_block(
    block: Dict[str, Any],
    *,
    x_unit: str = "s",
    y_unit: str = "V",
    x_name: str = "Time",
    channel: str = "",
) -> edge_pb2.VectorData:
    """Build a ``VectorData`` from a command-handler ``block`` dict.

    The block dict is the success payload of
    ``CommandHandler.execute_binary_block_query``: little-endian
    ``y_data``, wire ``y_dtype``, ``y_length``, and the composed
    ``x_start``/``x_increment``/``y_scale``/``y_offset`` scaling.
    """
    vector = build_vector_data(
        y_data=block["y_data"],
        y_dtype=block.get("y_dtype", "float64"),
        y_scale=block.get("y_scale", 1.0),
        y_offset=block.get("y_offset", 0.0),
        x_start=block.get("x_start", 0.0),
        x_increment=block.get("x_increment", 1.0),
        x_unit=x_unit,
        y_unit=y_unit,
        x_name=x_name,
        channel=channel,
    )
    expected = block.get("y_length")
    if expected is not None and vector.y_length != expected:
        raise ValueError(
            f"block declares {expected} samples but y_data carries "
            f"{vector.y_length}"
        )
    return vector


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
        if len(raw_bytes) % 2 != 0:
            raise IEEEBlockError(
                f"WORD payload length {len(raw_bytes)} is not a multiple "
                f"of 2 bytes"
            )
        n_samples = len(raw_bytes) // 2
        samples = list(struct.unpack(f'<{n_samples}h', raw_bytes))
    elif fmt == 4:
        # FLOAT32 — some instruments support this
        if len(raw_bytes) % 4 != 0:
            raise IEEEBlockError(
                f"FLOAT32 payload length {len(raw_bytes)} is not a "
                f"multiple of 4 bytes"
            )
        n_samples = len(raw_bytes) // 4
        samples = list(struct.unpack(f'<{n_samples}f', raw_bytes))
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
            y_data (bytes): packed float64 values (pre-scaled, little-endian)
            y_dtype (str): "float64"
            y_length (int): number of samples
            x_start (float): time of first sample (reference-composed:
                xorigin - xreference * xincrement)
            x_increment (float): time between samples
            x_unit (str): e.g. "s"
            y_unit (str): e.g. "V"
            y_scale (float): always 1.0 — samples are pre-scaled; the
                explicit 1.0 prevents the proto3 zero-default collapse
            y_offset (float): always 0.0 (same rule)

    Raises:
        IEEEBlockError: On a malformed block or a preamble that would
            produce a zero x_increment.
        ValueError: On a malformed preamble.
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

    if preamble.points and len(voltages) != preamble.points:
        raise IEEEBlockError(
            f"preamble declares {preamble.points} points but block "
            f"decoded to {len(voltages)} samples"
        )
    if preamble.xincrement == 0.0:
        raise IEEEBlockError(
            "preamble xincrement is 0 — refusing to emit a uniform "
            "waveform with x_increment == 0"
        )

    # Pack as float64
    y_data = struct.pack(f'<{len(voltages)}d', *voltages)

    return {
        "y_data": y_data,
        "y_dtype": "float64",
        "y_length": len(voltages),
        # Reference-point composition (doc §2.4):
        # x(i) = (i - xreference)*xincrement + xorigin
        #      = x_start + i*x_increment
        "x_start": preamble.xorigin - preamble.xreference * preamble.xincrement,
        "x_increment": preamble.xincrement,
        "x_unit": wf_config.x_unit,
        "y_unit": wf_config.y_unit,
        # Samples are pre-scaled physical values: y_scale is EXPLICITLY
        # 1.0 (never the proto3 zero-default — doc §3.0).
        "y_scale": 1.0,
        "y_offset": 0.0,
    }
