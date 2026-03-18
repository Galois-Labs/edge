"""
Capability Manager for per-instrument feature tracking.

Tracks which commands, sequences, and settings are available for each
connected instrument based on its matched profile. Supports runtime
enable/disable of individual commands and sequences.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, TYPE_CHECKING

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

    @property
    def enabled_commands(self) -> Set[str]:
        """Names of commands that are profile-enabled and not runtime-disabled."""
        if self.profile is None:
            return set()
        return {
            name
            for name, cmd in self.profile.commands.items()
            if cmd.enabled and name not in self._disabled_commands
        }

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
        """Disable a command at runtime. Returns True if it existed."""
        if self.profile is None or command_name not in self.profile.commands:
            return False
        self._disabled_commands.add(command_name)
        logger.info("Disabled command '%s' for %s", command_name, self.instrument_id)
        return True

    def enable_command(self, command_name: str) -> bool:
        """Re-enable a runtime-disabled command. Returns True if it was disabled."""
        if command_name in self._disabled_commands:
            self._disabled_commands.discard(command_name)
            logger.info("Re-enabled command '%s' for %s", command_name, self.instrument_id)
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
        """Return CommandConfig if the command exists and is enabled, else None."""
        if self.profile is None:
            return None
        if command_name not in self.enabled_commands:
            return None
        return self.profile.get_command(command_name)

    def get_sequence(self, sequence_name: str) -> Optional[SequenceConfig]:
        """Return SequenceConfig if the sequence exists and is enabled."""
        if self.profile is None:
            return None
        if sequence_name not in self.enabled_sequences:
            return None
        return self.profile.get_sequence(sequence_name)

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
    """

    def __init__(self) -> None:
        self._instruments: Dict[str, InstrumentCapabilities] = {}

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
        self._instruments[instrument_id] = caps
        driver_caps = driver.get_capabilities()
        logger.info(
            "Registered protocol driver %s (%s, %d commands)",
            instrument_id,
            driver_caps.get("protocol", "?"),
            len(driver_caps.get("commands", [])),
        )
        return caps

    def get_protocol_driver(self, instrument_id: str) -> Optional[Any]:
        """Return the protocol driver for an instrument, or None."""
        caps = self._instruments.get(instrument_id)
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
        return caps

    def unregister_instrument(self, instrument_id: str) -> bool:
        """Unregister an instrument. Returns True if it was found."""
        if instrument_id in self._instruments:
            del self._instruments[instrument_id]
            logger.info("Unregistered instrument: %s", instrument_id)
            return True
        logger.warning("Cannot unregister unknown instrument: %s", instrument_id)
        return False

    # -- Single-instrument queries ---

    def get_capabilities(self, instrument_id: str) -> Optional[Dict[str, Any]]:
        """Get capability dict for one instrument, or None if not found."""
        caps = self._instruments.get(instrument_id)
        if caps is None:
            return None
        return caps.to_capability_dict()

    def get_instrument_caps(self, instrument_id: str) -> Optional[InstrumentCapabilities]:
        """Get the raw InstrumentCapabilities record."""
        return self._instruments.get(instrument_id)

    # -- All-instruments queries ---

    def get_all_capabilities(self) -> Dict[str, Dict[str, Any]]:
        """Get capabilities for every registered instrument (dict keyed by id)."""
        return {
            inst_id: caps.to_capability_dict()
            for inst_id, caps in self._instruments.items()
        }

    def get_all_capabilities_list(self) -> List[Dict[str, Any]]:
        """Get capabilities for every registered instrument (flat list)."""
        return [caps.to_capability_dict() for caps in self._instruments.values()]

    @property
    def all_instruments(self) -> Dict[str, InstrumentCapabilities]:
        """Return the full map of instrument_id -> InstrumentCapabilities."""
        return self._instruments

    @property
    def instrument_count(self) -> int:
        return len(self._instruments)

    @property
    def profiled_count(self) -> int:
        return sum(1 for c in self._instruments.values() if c.has_profile)

    # -- Enable / disable (delegated) ---

    def disable_command(self, instrument_id: str, command_name: str) -> bool:
        caps = self._instruments.get(instrument_id)
        if caps is None:
            logger.warning("Cannot disable command: instrument not found: %s", instrument_id)
            return False
        return caps.disable_command(command_name)

    def enable_command(self, instrument_id: str, command_name: str) -> bool:
        caps = self._instruments.get(instrument_id)
        return caps.enable_command(command_name) if caps else False

    def disable_sequence(self, instrument_id: str, sequence_name: str) -> bool:
        caps = self._instruments.get(instrument_id)
        return caps.disable_sequence(sequence_name) if caps else False

    def enable_sequence(self, instrument_id: str, sequence_name: str) -> bool:
        caps = self._instruments.get(instrument_id)
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
        """
        caps = self._instruments.get(instrument_id)
        if caps is None:
            logger.error("Instrument not found: %s", instrument_id)
            return None

        cmd = caps.get_command(command_name)
        if cmd is None:
            logger.error("Command '%s' not found or disabled for %s", command_name, instrument_id)
            return None

        if cmd.is_sdk_command:
            return SDKCommandRequest(
                command_name=command_name,
                sdk_call=cmd.sdk_call,
                params=params,
                is_query=is_query,
            )

        try:
            return cmd.format_scpi(params, is_query)
        except Exception as exc:
            logger.error("Failed to format command '%s': %s", command_name, exc)
            return None

    # -- Lookup helpers ---

    def find_by_class(self, instrument_class: str) -> List[InstrumentCapabilities]:
        """Find all instruments of a given class (e.g. 'smu', 'dmm')."""
        target = instrument_class.lower()
        return [c for c in self._instruments.values() if c.instrument_class == target]

    def find_with_command(self, command_name: str) -> List[InstrumentCapabilities]:
        """Find all instruments that have a specific command enabled."""
        return [c for c in self._instruments.values() if command_name in c.enabled_commands]

    def find_with_sequence(self, sequence_name: str) -> List[InstrumentCapabilities]:
        """Find all instruments that have a specific sequence enabled."""
        return [c for c in self._instruments.values() if sequence_name in c.enabled_sequences]

    def get_available_classes(self) -> List[str]:
        """Sorted list of instrument classes present."""
        classes: Set[str] = set()
        for caps in self._instruments.values():
            if caps.instrument_class:
                classes.add(caps.instrument_class)
        return sorted(classes)

    def get_available_commands(self) -> Dict[str, List[str]]:
        """Map of instrument_id -> list of enabled command names."""
        return {
            iid: sorted(c.enabled_commands) for iid, c in self._instruments.items()
        }

    # -- Summary ---

    def get_summary(self) -> Dict[str, Any]:
        """High-level summary suitable for registration payloads."""
        total_commands = sum(len(c.enabled_commands) for c in self._instruments.values())
        total_sequences = sum(len(c.enabled_sequences) for c in self._instruments.values())
        return {
            "total_instruments": self.instrument_count,
            "profiled_instruments": self.profiled_count,
            "instrument_classes": self.get_available_classes(),
            "total_commands": total_commands,
            "total_sequences": total_sequences,
        }
