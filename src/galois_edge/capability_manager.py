"""
Capability Manager for per-instrument feature tracking.

Tracks which commands, sequences, and settings are available for each
connected instrument based on its matched profile. Supports runtime
enable/disable of individual commands and sequences.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Dict, FrozenSet, List, Mapping, Optional, Set, Tuple, TYPE_CHECKING

from .tracing import CommandContext, ResolvedSCPI, WriteTarget, json_safe
from .validation import select_template, validate_params, wire_params

if TYPE_CHECKING:
    from .profile_schema import (
        CommandConfig,
        InstrumentProfile,
        SDKCallConfig,
        SequenceConfig,
    )

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-instrument capability record
# ---------------------------------------------------------------------------

@dataclass
class InstrumentCapabilities:
    """Tracks capabilities for a single instrument.

    Stores the instrument's identifier, VISA address, matched profile
    (if any), and runtime enable/disable sets for commands and sequences.
    """

    instrument_id: str
    visa_address: str
    idn_response: str = ""
    profile: Optional[InstrumentProfile] = None
    _disabled_commands: Set[str] = field(default_factory=set)
    _disabled_sequences: Set[str] = field(default_factory=set)
    _enabled_cache: Optional[tuple] = field(default=None, init=False, repr=False, compare=False)
    _toggle_version: int = field(default=0, init=False, repr=False, compare=False)

    @property
    def has_profile(self) -> bool:
        """Whether this instrument has a matched profile."""
        return self.profile is not None

    @property
    def profile_key(self) -> str:
        """Profile key string, or empty string when no profile."""
        if self.profile is not None:
            return self.profile.profile_key
        return ""

    @property
    def manufacturer(self) -> str:
        """Manufacturer from profile, or parsed from *IDN? response."""
        if self.profile is not None:
            return self.profile.instrument.manufacturer
        if self.idn_response:
            parts = self.idn_response.split(",")
            if parts:
                return parts[0].strip()
        return ""

    @property
    def model(self) -> str:
        """Model from profile, or parsed from *IDN? response."""
        if self.profile is not None:
            return self.profile.instrument.model
        if self.idn_response:
            parts = self.idn_response.split(",")
            if len(parts) > 1:
                return parts[1].strip()
        return ""

    @property
    def instrument_class(self) -> str:
        """Instrument class from profile (e.g. 'smu', 'dmm')."""
        if self.profile is not None:
            return self.profile.instrument.instrument_class
        return ""

    # -- Enabled / disabled tracking ---

    def _path_of(self, name_or_alias: str) -> str:
        """Command path for a path or alias; exact keys for profiles without ``path_of``. KeyError."""
        path_of = getattr(self.profile, "path_of", None)
        if path_of is not None:
            return path_of(name_or_alias)
        if name_or_alias in self.profile.commands:
            return name_or_alias
        raise KeyError(name_or_alias)

    @property
    def enabled_commands(self) -> FrozenSet[str]:
        """Paths of commands that are profile-enabled and not runtime-disabled (cached, edge-api.md §6)."""
        if self.profile is None:
            return frozenset()
        commands = self.profile.commands
        key = (id(self.profile), id(commands), len(commands), self._toggle_version, len(self._disabled_commands))
        if self._enabled_cache is not None and self._enabled_cache[0] == key:
            return self._enabled_cache[1]
        names = frozenset(n for n, c in commands.items() if c.enabled and n not in self._disabled_commands)
        self._enabled_cache = (key, names)
        return names

    @property
    def disabled_commands(self) -> Set[str]:
        """Names of all commands that are currently disabled."""
        if self.profile is None:
            return set()
        return {
            name
            for name, cmd in self.profile.commands.items()
            if not cmd.enabled or name in self._disabled_commands
        }

    @property
    def enabled_sequences(self) -> Set[str]:
        """Names of all sequences that are currently enabled."""
        if self.profile is None or self.profile.sequences is None:
            return set()
        return {
            name
            for name, seq in self.profile.sequences.items()
            if seq.enabled and name not in self._disabled_sequences
        }

    @property
    def disabled_sequences(self) -> Set[str]:
        """Names of all sequences that are currently disabled."""
        if self.profile is None or self.profile.sequences is None:
            return set()
        return {
            name
            for name, seq in self.profile.sequences.items()
            if not seq.enabled or name in self._disabled_sequences
        }

    # -- Runtime toggles ---

    def disable_command(self, command_name: str) -> bool:
        """Disable a command (path or alias) at runtime. Returns True if it existed."""
        if self.profile is None:
            return False
        try:
            path = self._path_of(command_name)
        except KeyError:
            return False
        self._disabled_commands.add(path)
        self._toggle_version += 1
        logger.info("Disabled command '%s' for %s", path, self.instrument_id)
        return True

    def enable_command(self, command_name: str) -> bool:
        """Re-enable a runtime-disabled command (path or alias). Returns True if it was disabled."""
        path = command_name
        if self.profile is not None:
            try:
                path = self._path_of(command_name)
            except KeyError:
                path = command_name
        if path in self._disabled_commands:
            self._disabled_commands.discard(path)
            self._toggle_version += 1
            logger.info("Re-enabled command '%s' for %s", path, self.instrument_id)
            return True
        return False

    def disable_sequence(self, sequence_name: str) -> bool:
        """Disable a sequence at runtime."""
        if self.profile is None or self.profile.sequences is None:
            return False
        if sequence_name not in self.profile.sequences:
            return False
        self._disabled_sequences.add(sequence_name)
        logger.info("Disabled sequence '%s' for %s", sequence_name, self.instrument_id)
        return True

    def enable_sequence(self, sequence_name: str) -> bool:
        """Re-enable a runtime-disabled sequence."""
        if sequence_name in self._disabled_sequences:
            self._disabled_sequences.discard(sequence_name)
            logger.info("Re-enabled sequence '%s' for %s", sequence_name, self.instrument_id)
            return True
        return False

    # -- Lookup ---

    def get_command(self, command_name: str) -> Optional[CommandConfig]:
        """Return CommandConfig (by path or alias) if it exists and is enabled, else None."""
        if self.profile is None:
            return None
        try:
            path = self._path_of(command_name)
        except KeyError:
            return None
        if path not in self.enabled_commands:
            return None
        return self.profile.commands[path]

    def get_sequence(self, sequence_name: str) -> Optional[SequenceConfig]:
        """Return SequenceConfig if the sequence exists and is enabled."""
        if self.profile is None:
            return None
        if sequence_name not in self.enabled_sequences:
            return None
        return self.profile.get_sequence(sequence_name)

    # -- Resolution and validation (validation-trace; edge-api.md §4) ---

    def command_path(self, name_or_alias: str) -> str:
        """Canonical command path for a name or alias (semantics.md §7.3); the input if unknown."""
        try:
            return self._path_of(name_or_alias)
        except (KeyError, AttributeError):
            return name_or_alias

    def writes_for(self, path: str) -> Tuple[WriteTarget, ...]:
        """``writes`` targets of a leaf via the profile hook (CI-6); () when unavailable."""
        hook = getattr(self.profile, "writes_for", None) if self.profile is not None else None
        if hook is None:
            return ()
        try:
            return tuple(hook(path))
        except Exception:
            logger.debug("writes_for(%r) failed for %s", path, self.instrument_id, exc_info=True)
            return ()

    def resolve_command(
        self,
        name_or_alias: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        is_query: bool = True,
    ) -> Tuple[CommandConfig, Dict[str, Any]]:
        """Look up an enabled command and validate its params.

        Raises KeyError (unknown or disabled) or ParamValidationError.
        ``is_query`` is the additive CI-1 keyword: for a property it selects
        the getter (True) or the setter (False) whose params are validated.
        """
        cmd = self.get_command(name_or_alias)
        if cmd is None:
            raise KeyError(name_or_alias)
        return cmd, validate_params(cmd, params, is_query=is_query)

    # -- Serialization ---

    def to_capability_dict(self) -> Dict[str, Any]:
        """Export capabilities as a plain dictionary for gRPC/API responses."""
        if self.profile is not None:
            base = self.profile.to_capability_dict()
            enabled_cmds = self.enabled_commands
            base["commands"] = [
                cmd for cmd in base["commands"] if cmd["name"] in enabled_cmds
            ]
            enabled_seqs = self.enabled_sequences
            base["sequences"] = [
                seq for seq in base["sequences"] if seq["name"] in enabled_seqs
            ]
        else:
            base: Dict[str, Any] = {
                "has_profile": False,
                "profile_key": "",
                "manufacturer": self.manufacturer,
                "model": self.model,
                "instrument_class": "",
                "commands": [],
                "sequences": [],
                "settings": {},
            }

        base["instrument_id"] = self.instrument_id
        base["visa_address"] = self.visa_address
        return base


# ---------------------------------------------------------------------------
# SDK command request (returned when a profile command is SDK-based)
# ---------------------------------------------------------------------------

@dataclass
class SDKCommandRequest:
    """Returned instead of a SCPI string when the profile command
    dispatches via a vendor SDK. The gRPC server should forward this
    to the SDKExecutor."""

    command_name: str
    sdk_call: SDKCallConfig
    params: Optional[Dict[str, Any]]
    is_query: bool


# ---------------------------------------------------------------------------
# Capability Manager
# ---------------------------------------------------------------------------

class CapabilityManager:
    """Manages per-instrument capabilities for the entire edge node.

    Single source of truth for what commands and sequences are
    available on each connected instrument. Consulted by gRPC
    GetCapabilities, the command dispatch pipeline, and the
    registration manager.

    Thread safety: discovery registers instruments on the instrument I/O
    thread while MCP and gRPC read on the event loop. ``_lock`` guards the
    registries; readers iterate a snapshot taken under it, never the live
    dict, and no caps/profile code or listener runs while it is held.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._instruments: Dict[str, InstrumentCapabilities] = {}
        # Phase 3: observers notified on register/unregister so the MCP
        # DynamicToolRegistry (and any future listener) can update its
        # tool surface in lock-step with hot-plug events.
        self._listeners: List[Callable[[str, str], None]] = []

    def _records(self) -> List[InstrumentCapabilities]:
        """Snapshot of the registered records, taken under the lock."""
        with self._lock:
            return list(self._instruments.values())

    def _items(self) -> List[Tuple[str, InstrumentCapabilities]]:
        """Snapshot of (instrument_id, record) pairs, taken under the lock."""
        with self._lock:
            return list(self._instruments.items())

    # -- Listener pattern (Phase 3) ---

    def add_listener(self, fn: Callable[[str, str], None]) -> None:
        """Register a (event, instrument_id) callback.

        Events fired: "registered" on register_instrument /
        register_protocol_driver, "unregistered" on unregister_instrument.
        Listener exceptions are logged and swallowed so a misbehaving
        observer cannot break instrument registration. Listeners run on the
        registering thread, outside the registry lock.
        """
        with self._lock:
            self._listeners.append(fn)

    def remove_listener(self, fn: Callable[[str, str], None]) -> None:
        """Detach a previously-added listener; no-op if absent."""
        with self._lock:
            try:
                self._listeners.remove(fn)
            except ValueError:
                pass

    def _emit(self, event: str, instrument_id: str) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for fn in listeners:
            try:
                fn(event, instrument_id)
            except Exception:
                logger.exception(
                    "capability listener raised on %s/%s",
                    event, instrument_id,
                )

    # -- Registration ---

    # -- Protocol driver registration ---

    def register_protocol_driver(
        self,
        instrument_id: str,
        driver: Any,
    ) -> InstrumentCapabilities:
        """Register a protocol driver (Modbus, HART, etc.).

        Creates an InstrumentCapabilities record that advertises the
        driver's capabilities alongside SCPI instruments.
        """
        caps = InstrumentCapabilities(
            instrument_id=instrument_id,
            visa_address=driver.transport_uri,
            idn_response=driver.identify(),
        )
        # Attach the driver to the caps object for dispatch
        caps._protocol_driver = driver  # type: ignore[attr-defined]
        with self._lock:
            self._instruments[instrument_id] = caps
        driver_caps = driver.get_capabilities()
        logger.info(
            "Registered protocol driver %s (%s, %d commands)",
            instrument_id,
            driver_caps.get("protocol", "?"),
            len(driver_caps.get("commands", [])),
        )
        self._emit("registered", instrument_id)
        return caps

    def get_protocol_driver(self, instrument_id: str) -> Optional[Any]:
        """Return the protocol driver for an instrument, or None."""
        caps = self.get_instrument_caps(instrument_id)
        if caps is not None:
            return getattr(caps, "_protocol_driver", None)
        return None

    # -- SCPI instrument registration ---

    def register_instrument(
        self,
        instrument_id: str,
        visa_address: str,
        idn_response: str = "",
        profile: Optional[InstrumentProfile] = None,
    ) -> InstrumentCapabilities:
        """Register an instrument and its matched profile.

        Args:
            instrument_id: Unique identifier (VISA address or synthetic SDK id).
            visa_address: The VISA resource string.
            idn_response: Raw *IDN? response.
            profile: Matched InstrumentProfile, or None.
        """
        caps = InstrumentCapabilities(
            instrument_id=instrument_id,
            visa_address=visa_address,
            idn_response=idn_response,
            profile=profile,
        )
        with self._lock:
            self._instruments[instrument_id] = caps

        if profile is not None:
            logger.info(
                "Registered instrument %s (%s) with profile %s (%d cmds, %d seqs)",
                instrument_id, visa_address, profile.profile_key,
                len(caps.enabled_commands), len(caps.enabled_sequences),
            )
        else:
            logger.info(
                "Registered instrument %s (%s) with no matching profile",
                instrument_id, visa_address,
            )
        self._emit("registered", instrument_id)
        return caps

    def unregister_instrument(self, instrument_id: str) -> bool:
        """Unregister an instrument. Returns True if it was found."""
        with self._lock:
            removed = self._instruments.pop(instrument_id, None)
        if removed is not None:
            logger.info("Unregistered instrument: %s", instrument_id)
            self._emit("unregistered", instrument_id)
            return True
        logger.warning("Cannot unregister unknown instrument: %s", instrument_id)
        return False

    # -- Single-instrument queries ---

    def get_capabilities(self, instrument_id: str) -> Optional[Dict[str, Any]]:
        """Get capability dict for one instrument, or None if not found."""
        caps = self.get_instrument_caps(instrument_id)
        if caps is None:
            return None
        return caps.to_capability_dict()

    def get_instrument_caps(self, instrument_id: str) -> Optional[InstrumentCapabilities]:
        """Get the raw InstrumentCapabilities record."""
        with self._lock:
            return self._instruments.get(instrument_id)

    # -- All-instruments queries ---

    def get_all_capabilities(self) -> Dict[str, Dict[str, Any]]:
        """Get capabilities for every registered instrument (dict keyed by id)."""
        return {
            inst_id: caps.to_capability_dict()
            for inst_id, caps in self._items()
        }

    def get_all_capabilities_list(self) -> List[Dict[str, Any]]:
        """Get capabilities for every registered instrument (flat list)."""
        return [caps.to_capability_dict() for caps in self._records()]

    @property
    def all_instruments(self) -> Mapping[str, InstrumentCapabilities]:
        """Read-only snapshot of instrument_id -> InstrumentCapabilities.

        Taken under the lock, so a caller may iterate it while discovery
        registers on the I/O thread; later registrations are not reflected.
        """
        with self._lock:
            return MappingProxyType(dict(self._instruments))

    @property
    def instrument_count(self) -> int:
        with self._lock:
            return len(self._instruments)

    @property
    def profiled_count(self) -> int:
        return sum(1 for c in self._records() if c.has_profile)

    # -- Enable / disable (delegated) ---

    def disable_command(self, instrument_id: str, command_name: str) -> bool:
        caps = self.get_instrument_caps(instrument_id)
        if caps is None:
            logger.warning("Cannot disable command: instrument not found: %s", instrument_id)
            return False
        return caps.disable_command(command_name)

    def enable_command(self, instrument_id: str, command_name: str) -> bool:
        caps = self.get_instrument_caps(instrument_id)
        return caps.enable_command(command_name) if caps else False

    def disable_sequence(self, instrument_id: str, sequence_name: str) -> bool:
        caps = self.get_instrument_caps(instrument_id)
        return caps.disable_sequence(sequence_name) if caps else False

    def enable_sequence(self, instrument_id: str, sequence_name: str) -> bool:
        caps = self.get_instrument_caps(instrument_id)
        return caps.enable_sequence(sequence_name) if caps else False

    # -- Command resolution ---

    def resolve_command(
        self,
        instrument_id: str,
        command_name: str,
        params: Optional[Dict[str, Any]] = None,
        is_query: bool = True,
    ) -> Optional[Any]:
        """Resolve a profile command to a SCPI string or SDKCommandRequest.

        Returns None when the instrument/command is not found or disabled.
        Raises ParamValidationError for bad params (edge-api.md §4): gRPC maps
        it to INVALID_ARGUMENT, MCP to {"error", "field"}. The SCPI string is a
        ResolvedSCPI carrying the CommandContext used by tracing (CI-5).
        """
        caps = self.get_instrument_caps(instrument_id)
        if caps is None:
            logger.error("Instrument not found: %s", instrument_id)
            return None
        try:
            cmd, validated = caps.resolve_command(command_name, params, is_query=is_query)
        except KeyError:
            logger.error("Command '%s' not found or disabled for %s", command_name, instrument_id)
            return None

        if cmd.is_sdk_command:
            return SDKCommandRequest(
                command_name=command_name,
                sdk_call=cmd.sdk_call,
                params=params,
                is_query=is_query,
            )

        wire = wire_params(cmd, params, validated)
        try:
            text = cmd.format_scpi(wire or None, is_query)
        except Exception as exc:
            logger.error("Failed to format command '%s': %s", command_name, exc)
            return None

        _template, form = select_template(cmd, dict(params or {}), is_query, use_defaults=True)
        path = caps.command_path(command_name)
        context = CommandContext(
            instrument_id=instrument_id,
            path=path,
            params=json_safe(validated),
            command=cmd,
            is_query=is_query,
            form=form,
            writes=caps.writes_for(path),
        )
        return ResolvedSCPI(text, context)

    # -- Lookup helpers ---

    def find_by_class(self, instrument_class: str) -> List[InstrumentCapabilities]:
        """Find all instruments of a given class (e.g. 'smu', 'dmm')."""
        target = instrument_class.lower()
        return [c for c in self._records() if c.instrument_class == target]

    def find_with_command(self, command_name: str) -> List[InstrumentCapabilities]:
        """Find all instruments that have a specific command (path or alias) enabled."""
        return [c for c in self._records() if c.get_command(command_name) is not None]

    def find_with_sequence(self, sequence_name: str) -> List[InstrumentCapabilities]:
        """Find all instruments that have a specific sequence enabled."""
        return [c for c in self._records() if sequence_name in c.enabled_sequences]

    def get_available_classes(self) -> List[str]:
        """Sorted list of instrument classes present."""
        classes: Set[str] = set()
        for caps in self._records():
            if caps.instrument_class:
                classes.add(caps.instrument_class)
        return sorted(classes)

    def get_available_commands(self) -> Dict[str, List[str]]:
        """Map of instrument_id -> list of enabled command names."""
        return {
            iid: sorted(c.enabled_commands) for iid, c in self._items()
        }

    # -- Summary ---

    def get_summary(self) -> Dict[str, Any]:
        """High-level summary suitable for registration payloads."""
        records = self._records()
        total_commands = sum(len(c.enabled_commands) for c in records)
        total_sequences = sum(len(c.enabled_sequences) for c in records)
        classes = sorted({c.instrument_class for c in records if c.instrument_class})
        return {
            "total_instruments": len(records),
            "profiled_instruments": sum(1 for c in records if c.has_profile),
            "instrument_classes": classes,
            "total_commands": total_commands,
            "total_sequences": total_sequences,
        }
