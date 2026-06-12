"""
Dataclass models for YAML instrument profiles.

These models define the schema for instrument profile YAML files.
Each profile describes an instrument's identity, commands, sequences,
settings, and (optionally) SDK call mappings.

The design uses plain dataclasses for simplicity and zero external
dependencies.  Validation helpers raise ``ValueError`` on bad data
so callers get clear error messages during profile loading.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Parameter / return-type config
# ---------------------------------------------------------------------------

@dataclass
class ParameterConfig:
    """Single parameter for a command or sequence."""

    type: str = "string"  # float | int | string | enum | bool
    unit: Optional[str] = None
    min: Optional[float] = None
    max: Optional[float] = None
    default: Optional[Any] = None
    description: Optional[str] = None
    options: Optional[List[str]] = None  # for enum type
    map: Optional[Dict[str, Any]] = None  # label → wire-value mapping

    def validate(self) -> None:
        """Check internal consistency."""
        allowed = ("float", "int", "string", "enum", "bool")
        if self.type not in allowed:
            raise ValueError(
                f"Invalid parameter type '{self.type}'. "
                f"Must be one of {allowed}"
            )
        if self.type == "enum" and not self.options:
            raise ValueError("Enum parameters must define 'options'")


#: Sample dtypes a profile may declare inside ``returns.binary``.
#: ``int8`` is accepted but widened to ``int16`` before emission — the
#: cloud decodes only float64|float32|int32|int16|uint8 (doc §2.4).
ALLOWED_BINARY_DTYPES = ("int8", "int16", "uint8", "float32", "float64")

#: Byte orders a profile may declare inside ``returns.binary``.
ALLOWED_BYTE_ORDERS = ("little", "big")

#: ``returns.format`` spellings that mean "IEEE 488.2 definite-length
#: block". ``ieee_block`` is canonical; ``ieee_binary`` is the legacy
#: alias normalised at YAML load time.
IEEE_BLOCK_FORMATS = ("ieee_block", "ieee_binary")


@dataclass
class PreambleMap:
    """Maps CSV indices of a preamble response to waveform scaling fields.

    The map is **index-only** by design (doc §2.3): anything requiring
    arithmetic across preamble fields — the Keysight reference-point
    composition — lives in daemon code
    (``waveform_assembly.compose_block_scaling``), not in YAML.

    Example (Keysight DSOX3000 ``:WAVeform:PREamble?``): the CSV reply
    is ``format,type,points,count,xincrement,xorigin,xreference,
    yincrement,yorigin,yreference`` so ``x_increment=4, x_start=5,
    x_reference=6, y_scale=7, y_offset=8, y_reference=9``.  When the
    reference indices are mapped, the daemon composes::

        x_start  = xorigin - xreference * xincrement
        y_offset = yorigin - yreference * yincrement

    Omit them for instruments whose origin already includes the
    reference.
    """

    x_increment: Optional[int] = None
    x_start: Optional[int] = None
    y_scale: Optional[int] = None
    y_offset: Optional[int] = None
    x_reference: Optional[int] = None
    y_reference: Optional[int] = None

    _FIELDS = (
        "x_increment", "x_start", "y_scale", "y_offset",
        "x_reference", "y_reference",
    )

    def to_index_dict(self) -> Dict[str, int]:
        """Return ``{field: csv_index}`` for every mapped field."""
        return {
            name: getattr(self, name)
            for name in self._FIELDS
            if getattr(self, name) is not None
        }

    def validate(self) -> None:
        for name in self._FIELDS:
            value = getattr(self, name)
            if value is None:
                continue
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(
                    f"preamble_map.{name} must be a non-negative CSV "
                    f"index, got {value!r}"
                )


@dataclass
class BinaryConfig:
    """Decode configuration for ``returns.type == binary`` commands whose
    response is a definite-length IEEE 488.2 block (``#<n><len><payload>``).
    """

    dtype: str = "uint8"            # int8 | int16 | uint8 | float32 | float64
    byte_order: str = "little"      # little | big
    preamble_command: Optional[str] = None  # sibling command name or raw SCPI
    preamble_map: Optional[PreambleMap] = None

    def validate(self) -> None:
        if self.dtype not in ALLOWED_BINARY_DTYPES:
            raise ValueError(
                f"binary.dtype must be one of {ALLOWED_BINARY_DTYPES}, "
                f"got {self.dtype!r}"
            )
        if self.byte_order not in ALLOWED_BYTE_ORDERS:
            raise ValueError(
                f"binary.byte_order must be one of {ALLOWED_BYTE_ORDERS}, "
                f"got {self.byte_order!r}"
            )
        if self.preamble_map is not None:
            self.preamble_map.validate()
            if not self.preamble_command:
                raise ValueError(
                    "binary.preamble_map requires binary.preamble_command"
                )


@dataclass
class ReturnConfig:
    """Return value description for a query command."""

    type: str = "string"  # float | int | string | array | binary | bool | vector
    unit: Optional[str] = None
    element_type: Optional[str] = None  # for array
    separator: Optional[str] = None     # for array
    format: Optional[str] = None        # "ieee_block" (canonical; "ieee_binary"
                                        # is a legacy alias), "ascii" — how to
                                        # read trace data
    fields: Optional[List[Dict[str, Any]]] = None  # named multi-value fields
    parser: Optional[Dict[str, Any]] = None  # response parser config
    binary: Optional[BinaryConfig] = None  # block decode config for binary types
    # Vector/trace fields
    x_name: Optional[str] = None          # e.g. "Time"
    x_unit: Optional[str] = None          # e.g. "s"
    x_start_query: Optional[str] = None   # SCPI query to fetch x-axis start
    x_increment_query: Optional[str] = None  # SCPI query to fetch x-axis increment

    @property
    def is_ieee_block(self) -> bool:
        """True when this command returns an IEEE 488.2 definite-length
        block that must be read through the raw byte path
        (``InstrumentManager.query_raw``), never the text path.

        A ``type: binary`` command with no explicit format defaults to
        the block format.
        """
        if self.type != "binary":
            return False
        return (self.format or "ieee_block") in IEEE_BLOCK_FORMATS

    @property
    def effective_binary(self) -> Optional[BinaryConfig]:
        """The :class:`BinaryConfig` for an ``ieee_block`` command,
        defaulting to ``uint8``/``little`` when the profile omits the
        ``binary`` sub-block. ``None`` for non-block commands."""
        if not self.is_ieee_block:
            return None
        return self.binary if self.binary is not None else BinaryConfig()

    def parse_response(self, raw: str) -> str:
        """Apply parser rules to raw instrument response. Falls back to raw on no match."""
        if not self.parser:
            return raw
        ptype = self.parser.get("type", "regex")
        if ptype == "regex":
            pattern = self.parser.get("pattern", "")
            group = self.parser.get("group", 0)
            m = re.search(pattern, raw)
            if m:
                return m.group(group)
        elif ptype == "strip":
            result = raw
            prefix = self.parser.get("prefix", "")
            suffix = self.parser.get("suffix", "")
            if prefix and result.startswith(prefix):
                result = result[len(prefix):]
            if suffix and result.endswith(suffix):
                result = result[:-len(suffix)]
            return result
        elif ptype == "split":
            delimiter = self.parser.get("delimiter", ",")
            index = self.parser.get("index", 0)
            parts = raw.split(delimiter)
            if index < len(parts):
                return parts[index].strip()
        return raw

    def validate(self) -> None:
        allowed = ("float", "int", "string", "array", "binary", "bool", "vector")
        if self.type not in allowed:
            raise ValueError(
                f"Invalid return type '{self.type}'. "
                f"Must be one of {allowed}"
            )
        if self.binary is not None:
            if self.type != "binary":
                raise ValueError(
                    "returns.binary is only valid with returns.type == 'binary'"
                )
            self.binary.validate()


# ---------------------------------------------------------------------------
# SDK call config
# ---------------------------------------------------------------------------

@dataclass
class SDKCallConfig:
    """Maps a profile command to a Python SDK method or property."""

    method: Optional[str] = None
    getter: Optional[str] = None
    setter: Optional[str] = None
    args_map: Optional[Dict[str, str]] = None
    result_field: Optional[str] = None
    is_property: bool = False


# ---------------------------------------------------------------------------
# Sweep config (for hardware ramp/sweep on a command)
# ---------------------------------------------------------------------------

@dataclass
class SweepConfig:
    """Configuration for hardware sweep/ramp on a command."""

    rate_param: str = "sweep_rate"
    command: str = ""           # SCPI template with {value} and {sweep_rate}
    check_command: str = ""     # SCPI query to poll status
    check_idle_match: str = ""  # Exact string or regex that means "sweep done"
    stop_command: str = ""      # Emergency abort SCPI
    poll_interval_ms: int = 1000


# ---------------------------------------------------------------------------
# CAN signal / command config
# ---------------------------------------------------------------------------

@dataclass
class CANSignalConfig:
    """Single signal packed inside a CAN frame."""
    start_bit: int = 0
    bit_length: int = 8
    byte_order: str = "little_endian"  # little_endian | big_endian
    signed: bool = False
    scale: float = 1.0
    offset: float = 0.0

    def validate(self) -> None:
        if self.start_bit < 0 or self.start_bit > 63:
            raise ValueError(f"start_bit must be 0-63, got {self.start_bit}")
        if self.bit_length < 1 or self.bit_length > 64:
            raise ValueError(f"bit_length must be 1-64, got {self.bit_length}")
        if self.byte_order not in ("little_endian", "big_endian"):
            raise ValueError(f"byte_order must be 'little_endian' or 'big_endian', got '{self.byte_order}'")
        if self.scale == 0:
            raise ValueError("scale must not be zero")


@dataclass
class CANCommandConfig:
    """CAN frame definition for a command."""
    message_id: int = 0
    direction: str = "rx"  # rx | tx | tx_rx
    signals: Optional[Dict[str, CANSignalConfig]] = None
    response_id: Optional[int] = None  # for tx_rx: expected response arbitration ID
    payload: Optional[List[int]] = None  # for tx_rx: fixed request bytes (e.g. UDS)
    dlc: int = 8  # data length code

    def validate(self) -> None:
        if self.message_id < 0 or self.message_id > 0x1FFFFFFF:
            raise ValueError(f"message_id out of range: 0x{self.message_id:X}")
        if self.direction not in ("rx", "tx", "tx_rx"):
            raise ValueError(f"direction must be 'rx', 'tx', or 'tx_rx', got '{self.direction}'")
        if self.dlc < 0 or self.dlc > 64:
            raise ValueError(f"dlc must be 0-64, got {self.dlc}")
        if self.direction == "tx_rx" and self.response_id is None:
            raise ValueError("tx_rx direction requires response_id")
        if self.signals:
            for name, sig in self.signals.items():
                sig.validate()


# ---------------------------------------------------------------------------
# Command config
# ---------------------------------------------------------------------------

@dataclass
class CommandConfig:
    """Configuration for a single SCPI or SDK command."""

    scpi: Optional[str] = None
    getter: Optional[str] = None       # for property-type commands
    setter: Optional[str] = None
    type: Optional[str] = None         # query | write | property
    description: Optional[str] = None
    enabled: bool = True
    streamable: bool = False
    is_dangerous: bool = False
    params: Optional[Dict[str, ParameterConfig]] = None
    returns: Optional[ReturnConfig] = None
    sdk_call: Optional[SDKCallConfig] = None
    force_query: bool = False  # send getter as-is (no trailing '?')
    requires_sweep: bool = False  # safety interlock: must use StartSweep RPC
    sweep: Optional[SweepConfig] = None  # sweep/ramp configuration
    waveform_assembly: Optional[Any] = None  # WaveformAssemblyConfig or None
    can: Optional[CANCommandConfig] = None  # CAN frame definition

    # ---- helpers -----------------------------------------------------------

    @property
    def is_sdk_command(self) -> bool:
        return self.sdk_call is not None

    @property
    def is_can_command(self) -> bool:
        return self.can is not None

    def get_scpi_string(self, is_query: bool = True) -> Optional[str]:
        """Return the SCPI string for this command."""
        if self.type == "property":
            return self.getter if is_query else self.setter
        return self.scpi

    def format_scpi(
        self,
        params: Optional[Dict[str, Any]] = None,
        is_query: bool = True,
    ) -> str:
        """Build the final SCPI string with parameter substitution."""
        scpi = self.get_scpi_string(is_query)
        if scpi is None:
            raise ValueError("No SCPI string available for this command")

        def _substitute(template: str) -> str:
            if params and self.params:
                for key, value in params.items():
                    # Apply map transformation if available (forward-map only:
                    # label -> wire value on writes)
                    pc = self.params.get(key)
                    if pc and pc.map and str(value) in pc.map:
                        value = pc.map[str(value)]
                    template = template.replace(f"{{{key}}}", str(value))
            elif params:
                for key, value in params.items():
                    template = template.replace(f"{{{key}}}", str(value))
            return template

        resolved = _substitute(scpi)

        # Property-command fallback: if the setter still has unresolved
        # placeholders AND we have a getter, the caller almost certainly
        # meant to read. Fall back to the getter template.
        if (
            self.type == "property"
            and not is_query
            and "{" in resolved
            and self.getter is not None
        ):
            logger.info(
                "Property command setter has unresolved placeholders; "
                "falling back to getter template (likely a read with is_query=False)"
            )
            return _substitute(self.getter)

        return resolved

    def validate(self) -> None:
        """Check internal consistency."""
        if self.sdk_call is not None:
            return  # SDK commands only need sdk_call
        if self.can is not None:
            self.can.validate()
            return  # CAN commands only need the can field
        if self.type == "property":
            if not self.getter and not self.setter:
                raise ValueError(
                    "Property commands require at least 'getter' or 'setter'"
                )
        elif self.scpi is None and self.getter is None and self.setter is None:
            raise ValueError(
                "Command must define 'scpi', 'getter', or 'setter'"
            )
        if self.params:
            for name, pc in self.params.items():
                pc.validate()
        if self.returns:
            self.returns.validate()


# ---------------------------------------------------------------------------
# Sequence config
# ---------------------------------------------------------------------------

@dataclass
class SequenceStepConfig:
    """A single step in a measurement sequence."""

    command: Optional[str] = None   # references a named command
    scpi: Optional[str] = None      # or a raw SCPI string
    args: Optional[Dict[str, str]] = None
    capture: Optional[str] = None   # variable name to capture result

    def validate(self) -> None:
        if not self.command and not self.scpi:
            raise ValueError("Sequence step must define 'command' or 'scpi'")


@dataclass
class SequenceConfig:
    """Multi-step measurement sequence."""

    steps: List[SequenceStepConfig] = field(default_factory=list)
    description: Optional[str] = None
    parameters: Optional[Dict[str, ParameterConfig]] = None
    returns: Optional[str] = None   # captured variable to return
    enabled: bool = True

    def validate(self) -> None:
        if not self.steps:
            raise ValueError("Sequence must have at least one step")
        for step in self.steps:
            step.validate()
        if self.parameters:
            for name, pc in self.parameters.items():
                pc.validate()


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@dataclass
class SettingsConfig:
    """Instrument communication settings."""

    timeout_ms: int = 5000
    terminator: str = "\n"
    opc_query: bool = False
    init_commands: Optional[List[str]] = None
    cleanup_commands: Optional[List[str]] = None


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

@dataclass
class IdentityConfig:
    """Identity query and regex pattern matching."""

    query: str = "*IDN?"
    pattern: Optional[str] = None           # single regex
    patterns: Optional[List[str]] = None    # multiple regexes (preferred)

    def __post_init__(self) -> None:
        self._compiled: Optional[List[re.Pattern[str]]] = None

    @property
    def all_patterns(self) -> List[str]:
        """Collect all patterns into a single list."""
        result: List[str] = []
        if self.patterns:
            result.extend(self.patterns)
        if self.pattern and self.pattern not in result:
            result.append(self.pattern)
        return result

    def _compile(self) -> List[re.Pattern[str]]:
        if self._compiled is None:
            compiled: List[re.Pattern[str]] = []
            for pat in self.all_patterns:
                try:
                    compiled.append(re.compile(pat, re.IGNORECASE))
                except re.error as exc:
                    raise ValueError(f"Invalid regex pattern '{pat}': {exc}")
            self._compiled = compiled
        return self._compiled

    def matches(self, idn_response: str) -> bool:
        """Return True if *any* pattern matches the IDN response."""
        for regex in self._compile():
            if regex.search(idn_response):
                return True
        return False

    def validate(self) -> None:
        if not self.all_patterns:
            raise ValueError(
                "Identity must define at least one pattern "
                "(via 'pattern' or 'patterns')"
            )
        # force compilation to surface bad regexes early
        self._compile()


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------

@dataclass
class InterfaceConfig:
    """Interface configuration for instrument connection."""

    type: str = "gpib"  # gpib | usb | ethernet | serial
    port: Optional[int] = None
    default_address: Optional[int] = None
    # Serial-specific fields (only meaningful when type == "serial")
    baud_rate: Optional[int] = None
    parity: Optional[str] = None       # "none", "even", "odd"
    data_bits: Optional[int] = None
    stop_bits: Optional[float] = None   # 1, 1.5, 2
    # USB VID/PID for serial-over-USB auto-discovery (hex strings, e.g. "2E3C")
    usb_vid: Optional[str] = None
    usb_pid: Optional[str] = None
    # CAN-specific fields (only meaningful when type == "can")
    bus: Optional[str] = None            # "can0", "vcan0"
    bitrate: Optional[int] = None       # 500000
    can_protocol: Optional[str] = None  # "can20a" | "can20b" | "canfd"


# ---------------------------------------------------------------------------
# SDK driver config (top-level, for non-SCPI instruments)
# ---------------------------------------------------------------------------

@dataclass
class SDKConnectConfig:
    """Connection parameters for SDK instruments."""

    method: Optional[str] = None
    args: Optional[Dict[str, str]] = None
    defaults: Optional[Dict[str, Any]] = None
    constructor_args: Optional[Dict[str, Any]] = None


@dataclass
class SDKDisconnectConfig:
    """Disconnection parameters for SDK instruments."""

    method: Optional[str] = None


@dataclass
class SDKIdentityConfig:
    """Identity query parameters for SDK instruments."""

    method: Optional[str] = None
    property: Optional[str] = None
    pattern: Optional[str] = None


@dataclass
class SDKConfig:
    """Top-level SDK driver configuration for non-SCPI instruments."""

    package: str = ""
    import_path: str = ""
    class_name: str = ""
    is_async: bool = False
    connect: SDKConnectConfig = field(default_factory=SDKConnectConfig)
    disconnect: SDKDisconnectConfig = field(default_factory=SDKDisconnectConfig)
    identity: Optional[SDKIdentityConfig] = None


# ---------------------------------------------------------------------------
# Instrument metadata
# ---------------------------------------------------------------------------

@dataclass
class InstrumentMetadata:
    """Top-level identification metadata."""

    manufacturer: str = ""
    model: str = ""
    instrument_class: str = ""   # smu, dmm, oscilloscope, ...
    description: Optional[str] = None


# ---------------------------------------------------------------------------
# InstrumentProfile — the top-level model
# ---------------------------------------------------------------------------

@dataclass
class InstrumentProfile:
    """Complete instrument profile loaded from a YAML file."""

    instrument: InstrumentMetadata = field(default_factory=InstrumentMetadata)
    identity: IdentityConfig = field(default_factory=IdentityConfig)
    interfaces: List[InterfaceConfig] = field(default_factory=list)
    settings: SettingsConfig = field(default_factory=SettingsConfig)
    commands: Dict[str, CommandConfig] = field(default_factory=dict)
    sequences: Optional[Dict[str, SequenceConfig]] = None
    sdk: Optional[SDKConfig] = None

    # ---- derived keys ------------------------------------------------------

    @property
    def profile_key(self) -> str:
        """Unique key for this profile: ``manufacturer_model`` (lower, no spaces)."""
        return (
            f"{self.instrument.manufacturer}_{self.instrument.model}"
            .lower()
            .replace(" ", "_")
        )

    @property
    def is_sdk_instrument(self) -> bool:
        return self.sdk is not None

    # ---- query helpers -----------------------------------------------------

    @property
    def enabled_commands(self) -> Dict[str, CommandConfig]:
        return {n: c for n, c in self.commands.items() if c.enabled}

    @property
    def enabled_sequences(self) -> Dict[str, SequenceConfig]:
        if not self.sequences:
            return {}
        return {n: s for n, s in self.sequences.items() if s.enabled}

    def matches_idn(self, idn_response: str) -> bool:
        """Check whether an ``*IDN?`` response matches this profile."""
        return self.identity.matches(idn_response)

    def get_command(self, name: str) -> Optional[CommandConfig]:
        return self.commands.get(name)

    def get_sequence(self, name: str) -> Optional[SequenceConfig]:
        if self.sequences:
            return self.sequences.get(name)
        return None

    def resolve_scpi_ref(
        self,
        ref: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Resolve a command reference to a query SCPI string.

        ``binary.preamble_command`` may name a sibling profile command
        (doc §2.3, e.g. ``waveform_preamble``) or carry a raw SCPI
        string (e.g. ``":WAVeform:PREamble?"``).  When *ref* names a
        sibling command, its SCPI template is formatted with *params*
        (same channel parameters as the block read); otherwise *ref* is
        returned as-is.
        """
        cmd = self.commands.get(ref)
        if cmd is not None:
            try:
                return cmd.format_scpi(params, is_query=True)
            except ValueError:
                logger.warning(
                    "preamble command '%s' has no usable SCPI template; "
                    "treating the reference as raw SCPI", ref,
                )
        return ref

    # ---- export ------------------------------------------------------------

    @staticmethod
    def _param_to_dict(name: str, pc: ParameterConfig) -> Dict[str, Any]:
        """Serialise a single ParameterConfig to a plain dict."""
        d: Dict[str, Any] = {
            "name": name,
            "type": pc.type,
            "description": pc.description or "",
            "unit": pc.unit or "",
            "required": pc.default is None,
            "default": pc.default,
            "options": pc.options or [],
        }
        return d

    def to_capability_dict(self) -> Dict[str, Any]:
        """Export profile as a capability dictionary for gRPC responses."""
        cmds = []
        for name, cmd in self.enabled_commands.items():
            entry: Dict[str, Any] = {
                "name": name,
                "type": cmd.type or "write",
                "description": cmd.description or "",
                "parameters": [
                    self._param_to_dict(pname, pc)
                    for pname, pc in cmd.params.items()
                ] if cmd.params else [],
                "return_type": cmd.returns.type if cmd.returns else "",
                "unit": cmd.returns.unit if cmd.returns and cmd.returns.unit else "",
                "is_streamable": cmd.streamable,
                "is_dangerous": cmd.is_dangerous,
            }
            if cmd.returns and cmd.returns.fields:
                entry["fields"] = cmd.returns.fields
            cmds.append(entry)

        seqs = []
        for name, seq in self.enabled_sequences.items():
            seqs.append({
                "name": name,
                "description": seq.description or "",
                "parameters": [
                    self._param_to_dict(pname, pc)
                    for pname, pc in seq.parameters.items()
                ] if seq.parameters else [],
            })

        return {
            "has_profile": True,
            "profile_key": self.profile_key,
            "manufacturer": self.instrument.manufacturer,
            "model": self.instrument.model,
            "instrument_class": self.instrument.instrument_class,
            "commands": cmds,
            "sequences": seqs,
            "settings": {
                "timeout_ms": self.settings.timeout_ms,
                "opc_query": self.settings.opc_query,
            },
        }

    # ---- validation --------------------------------------------------------

    def validate(self) -> None:
        """Run validation checks across all sub-models."""
        self.identity.validate()
        for name, cmd in self.commands.items():
            try:
                cmd.validate()
            except ValueError as exc:
                raise ValueError(f"Command '{name}': {exc}") from exc
        if self.sequences:
            for name, seq in self.sequences.items():
                try:
                    seq.validate()
                except ValueError as exc:
                    raise ValueError(f"Sequence '{name}': {exc}") from exc


# ---------------------------------------------------------------------------
# Factory: build an InstrumentProfile from a raw dict (parsed YAML)
# ---------------------------------------------------------------------------

def _build_parameter_config(data: Dict[str, Any]) -> ParameterConfig:
    return ParameterConfig(
        type=data.get("type", "string"),
        unit=data.get("unit"),
        min=data.get("min"),
        max=data.get("max"),
        default=data.get("default"),
        description=data.get("description"),
        options=data.get("options"),
        map=data.get("map"),
    )


def _build_preamble_map(data: Dict[str, Any]) -> PreambleMap:
    return PreambleMap(
        x_increment=data.get("x_increment"),
        x_start=data.get("x_start"),
        y_scale=data.get("y_scale"),
        y_offset=data.get("y_offset"),
        x_reference=data.get("x_reference"),
        y_reference=data.get("y_reference"),
    )


def _build_binary_config(data: Dict[str, Any]) -> BinaryConfig:
    preamble_map = None
    if data.get("preamble_map"):
        preamble_map = _build_preamble_map(data["preamble_map"])
    return BinaryConfig(
        dtype=data.get("dtype", "uint8"),
        byte_order=data.get("byte_order", "little"),
        preamble_command=data.get("preamble_command"),
        preamble_map=preamble_map,
    )


def _build_return_config(data: Dict[str, Any]) -> ReturnConfig:
    fmt = data.get("format")
    if fmt == "ieee_binary":
        # Legacy alias — "ieee_block" is canonical (doc §2.3).
        fmt = "ieee_block"

    binary = None
    if data.get("binary"):
        binary = _build_binary_config(data["binary"])

    return ReturnConfig(
        type=data.get("type", "string"),
        unit=data.get("unit"),
        element_type=data.get("element_type"),
        separator=data.get("separator"),
        format=fmt,
        fields=data.get("fields"),
        parser=data.get("parser"),
        binary=binary,
        x_name=data.get("x_name"),
        x_unit=data.get("x_unit"),
        x_start_query=data.get("x_start_query"),
        x_increment_query=data.get("x_increment_query"),
    )


def _build_sdk_call(data: Dict[str, Any]) -> SDKCallConfig:
    return SDKCallConfig(
        method=data.get("method"),
        getter=data.get("getter"),
        setter=data.get("setter"),
        args_map=data.get("args_map"),
        result_field=data.get("result_field"),
        is_property=data.get("is_property", False),
    )


def _build_command(data: Dict[str, Any]) -> CommandConfig:
    params = None
    if "params" in data and data["params"]:
        params = {
            k: _build_parameter_config(v) if isinstance(v, dict) else _build_parameter_config({"type": str(v)})
            for k, v in data["params"].items()
        }

    returns = None
    if "returns" in data and data["returns"]:
        returns = _build_return_config(data["returns"])

    sdk_call = None
    if "sdk_call" in data and data["sdk_call"]:
        sdk_call = _build_sdk_call(data["sdk_call"])

    sweep = None
    if "sweep" in data and data["sweep"]:
        sweep = SweepConfig(**data["sweep"])

    waveform_assembly = None
    if "waveform_assembly" in data and data["waveform_assembly"]:
        from .waveform_assembly import build_waveform_assembly_config
        waveform_assembly = build_waveform_assembly_config(data["waveform_assembly"])

    can = None
    if "can" in data and data["can"]:
        can_data = data["can"]
        signals = None
        if "signals" in can_data and can_data["signals"]:
            signals = {}
            for sig_name, sig_data in can_data["signals"].items():
                if isinstance(sig_data, dict):
                    signals[sig_name] = CANSignalConfig(
                        start_bit=sig_data.get("start_bit", 0),
                        bit_length=sig_data.get("bit_length", 8),
                        byte_order=sig_data.get("byte_order", "little_endian"),
                        signed=sig_data.get("signed", False),
                        scale=sig_data.get("scale", 1.0),
                        offset=sig_data.get("offset", 0.0),
                    )
        can = CANCommandConfig(
            message_id=can_data.get("message_id", 0),
            direction=can_data.get("direction", "rx"),
            signals=signals,
            response_id=can_data.get("response_id"),
            payload=can_data.get("payload"),
            dlc=can_data.get("dlc", 8),
        )

    return CommandConfig(
        scpi=data.get("scpi"),
        getter=data.get("getter"),
        setter=data.get("setter"),
        type=data.get("type"),
        description=data.get("description"),
        enabled=data.get("enabled", True),
        streamable=data.get("streamable", False),
        is_dangerous=data.get("is_dangerous", False),
        params=params,
        returns=returns,
        sdk_call=sdk_call,
        force_query=data.get("force_query", False),
        requires_sweep=data.get("requires_sweep", False),
        sweep=sweep,
        waveform_assembly=waveform_assembly,
        can=can,
    )


def _build_sequence_step(data: Dict[str, Any]) -> SequenceStepConfig:
    return SequenceStepConfig(
        command=data.get("command"),
        scpi=data.get("scpi"),
        args=data.get("args"),
        capture=data.get("capture"),
    )


def _build_sequence(data: Dict[str, Any]) -> SequenceConfig:
    steps_data = data.get("steps", [])
    steps = [_build_sequence_step(s) for s in steps_data]
    params = None
    raw_params = data.get("parameters") or data.get("params")
    if raw_params:
        params = {
            k: _build_parameter_config(v) if isinstance(v, dict) else _build_parameter_config({"type": str(v)})
            for k, v in raw_params.items()
        }
    return SequenceConfig(
        steps=steps,
        description=data.get("description"),
        parameters=params,
        returns=data.get("returns"),
        enabled=data.get("enabled", True),
    )


def profile_from_dict(data: Dict[str, Any]) -> InstrumentProfile:
    """Build an ``InstrumentProfile`` from a raw dict (e.g. parsed YAML).

    This factory handles the mapping from loosely-typed dicts to the
    strongly-typed dataclass tree.  It intentionally tolerates missing
    optional keys so that minimal YAML files are valid.
    """
    # -- instrument metadata -------------------------------------------------
    inst_data = data.get("instrument", {})
    instrument = InstrumentMetadata(
        manufacturer=inst_data.get("manufacturer", ""),
        model=str(inst_data.get("model", "")),
        instrument_class=inst_data.get("class", inst_data.get("instrument_class", "")),
        description=inst_data.get("description"),
    )

    # -- identity ------------------------------------------------------------
    id_data = data.get("identity", {})
    identity = IdentityConfig(
        query=id_data.get("query", "*IDN?"),
        pattern=id_data.get("pattern"),
        patterns=id_data.get("patterns"),
    )

    # -- interfaces ----------------------------------------------------------
    interfaces = []
    for iface in data.get("interfaces", []):
        interfaces.append(InterfaceConfig(
            type=iface.get("type", "gpib"),
            port=iface.get("port"),
            default_address=iface.get("default_address"),
            baud_rate=iface.get("baud_rate"),
            parity=iface.get("parity"),
            data_bits=iface.get("data_bits"),
            stop_bits=iface.get("stop_bits"),
            usb_vid=iface.get("usb_vid"),
            usb_pid=iface.get("usb_pid"),
            bus=iface.get("bus"),
            bitrate=iface.get("bitrate"),
            can_protocol=iface.get("can_protocol"),
        ))

    # -- settings ------------------------------------------------------------
    s_data = data.get("settings", {})
    settings = SettingsConfig(
        timeout_ms=s_data.get("timeout_ms", 5000),
        terminator=s_data.get("terminator", "\n"),
        opc_query=s_data.get("opc_query", False),
        init_commands=s_data.get("init_commands"),
        cleanup_commands=s_data.get("cleanup_commands"),
    )

    # -- commands ------------------------------------------------------------
    commands: Dict[str, CommandConfig] = {}
    for name, cmd_data in data.get("commands", {}).items():
        if isinstance(cmd_data, dict):
            commands[name] = _build_command(cmd_data)

    # -- sequences -----------------------------------------------------------
    sequences: Optional[Dict[str, SequenceConfig]] = None
    if "sequences" in data and data["sequences"]:
        sequences = {}
        for name, seq_data in data["sequences"].items():
            if isinstance(seq_data, dict):
                sequences[name] = _build_sequence(seq_data)

    # -- SDK config ----------------------------------------------------------
    sdk = None
    if "sdk" in data and data["sdk"]:
        sdk_data = data["sdk"]
        connect_data = sdk_data.get("connect", {}) or {}
        disconnect_data = sdk_data.get("disconnect", {}) or {}
        identity_data = sdk_data.get("identity")

        sdk = SDKConfig(
            package=sdk_data.get("package", ""),
            import_path=sdk_data.get("import_path", ""),
            class_name=sdk_data.get("class_name", ""),
            is_async=sdk_data.get("is_async", False),
            connect=SDKConnectConfig(
                method=connect_data.get("method"),
                args=connect_data.get("args"),
                defaults=connect_data.get("defaults"),
                constructor_args=connect_data.get("constructor_args"),
            ),
            disconnect=SDKDisconnectConfig(
                method=disconnect_data.get("method"),
            ),
            identity=SDKIdentityConfig(
                method=identity_data.get("method"),
                property=identity_data.get("property"),
                pattern=identity_data.get("pattern"),
            ) if identity_data else None,
        )

    profile = InstrumentProfile(
        instrument=instrument,
        identity=identity,
        interfaces=interfaces,
        settings=settings,
        commands=commands,
        sequences=sequences,
        sdk=sdk,
    )
    return profile
