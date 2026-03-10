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

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


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


@dataclass
class ReturnConfig:
    """Return value description for a query command."""

    type: str = "string"  # float | int | string | array | binary | bool
    unit: Optional[str] = None
    element_type: Optional[str] = None  # for array
    separator: Optional[str] = None     # for array
    format: Optional[str] = None        # for binary
    fields: Optional[List[Dict[str, Any]]] = None  # named multi-value fields

    def validate(self) -> None:
        allowed = ("float", "int", "string", "array", "binary", "bool")
        if self.type not in allowed:
            raise ValueError(
                f"Invalid return type '{self.type}'. "
                f"Must be one of {allowed}"
            )


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

    # ---- helpers -----------------------------------------------------------

    @property
    def is_sdk_command(self) -> bool:
        return self.sdk_call is not None

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
        if params:
            for key, value in params.items():
                scpi = scpi.replace(f"{{{key}}}", str(value))
        return scpi

    def validate(self) -> None:
        """Check internal consistency."""
        if self.sdk_call is not None:
            return  # SDK commands only need sdk_call
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


# ---------------------------------------------------------------------------
# SDK driver config (top-level, for non-SCPI instruments)
# ---------------------------------------------------------------------------

@dataclass
class SDKConfig:
    """Top-level SDK driver configuration for non-SCPI instruments."""

    package: str = ""
    import_path: str = ""
    class_name: str = ""
    is_async: bool = False


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
    )


def _build_return_config(data: Dict[str, Any]) -> ReturnConfig:
    return ReturnConfig(
        type=data.get("type", "string"),
        unit=data.get("unit"),
        element_type=data.get("element_type"),
        separator=data.get("separator"),
        format=data.get("format"),
        fields=data.get("fields"),
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
        ))

    # -- settings ------------------------------------------------------------
    s_data = data.get("settings", {})
    settings = SettingsConfig(
        timeout_ms=s_data.get("timeout_ms", 5000),
        terminator=s_data.get("terminator", "\n"),
        opc_query=s_data.get("opc_query", False),
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
        sdk = SDKConfig(
            package=sdk_data.get("package", ""),
            import_path=sdk_data.get("import_path", ""),
            class_name=sdk_data.get("class_name", ""),
            is_async=sdk_data.get("is_async", False),
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
