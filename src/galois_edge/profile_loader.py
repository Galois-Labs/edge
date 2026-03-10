"""
Profile loader for YAML instrument profiles.

Loads, validates, and caches instrument profiles from a directory of
YAML files.  Provides matching functionality to find the right profile
for a given ``*IDN?`` response string.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional

try:
    import yaml
except ImportError:  # pragma: no cover — optional at import time
    yaml = None  # type: ignore[assignment]

from .profile_schema import InstrumentProfile, profile_from_dict

logger = logging.getLogger(__name__)


class ProfileLoader:
    """Load, cache, and match YAML instrument profiles.

    Profiles are loaded from a directory (glob ``*.yaml`` / ``*.yml``),
    validated, and cached in memory keyed by ``profile_key``
    (``manufacturer_model``).

    Usage::

        loader = ProfileLoader("/path/to/profiles")
        loader.load_all()
        profile = loader.match_instrument("KEITHLEY INSTRUMENTS INC.,MODEL 2400,...")
    """

    def __init__(self, profiles_dir: Optional[str] = None) -> None:
        if profiles_dir:
            self._profiles_dir = Path(profiles_dir)
        else:
            self._profiles_dir = Path(__file__).parent / "profiles"

        self._profiles: Dict[str, InstrumentProfile] = {}
        self._loaded: bool = False

    # -- properties ----------------------------------------------------------

    @property
    def profiles_dir(self) -> Path:
        return self._profiles_dir

    @property
    def profiles(self) -> Dict[str, InstrumentProfile]:
        """Return a *copy* of the profile cache."""
        return dict(self._profiles)

    @property
    def profile_count(self) -> int:
        return len(self._profiles)

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    # -- loading -------------------------------------------------------------

    def load_all(self) -> int:
        """Load every YAML profile from ``profiles_dir``.

        Clears the cache first, then scans for ``*.yaml`` and ``*.yml``
        files.  Invalid files are logged and skipped.

        Returns:
            Number of profiles successfully loaded.
        """
        if yaml is None:
            logger.error("PyYAML is not installed; cannot load profiles")
            self._loaded = True
            return 0

        self._profiles.clear()

        if not self._profiles_dir.exists():
            logger.warning("Profiles directory does not exist: %s", self._profiles_dir)
            self._loaded = True
            return 0

        if not self._profiles_dir.is_dir():
            logger.warning("Profiles path is not a directory: %s", self._profiles_dir)
            self._loaded = True
            return 0

        yaml_files = sorted(
            list(self._profiles_dir.glob("*.yaml"))
            + list(self._profiles_dir.glob("*.yml"))
        )

        loaded = 0
        for path in yaml_files:
            if path.name.startswith("_"):
                continue
            try:
                profile = self._load_file(path)
                if profile is not None:
                    self._profiles[profile.profile_key] = profile
                    loaded += 1
                    logger.info(
                        "Loaded profile: %s (%d commands)",
                        profile.profile_key,
                        len(profile.commands),
                    )
            except Exception:
                logger.exception("Failed to load profile %s", path)

        self._loaded = True
        logger.info(
            "Loaded %d profile(s) from %s", loaded, self._profiles_dir
        )
        return loaded

    def _load_file(self, path: Path) -> Optional[InstrumentProfile]:
        """Parse and validate a single YAML profile file."""
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)

        if not data or not isinstance(data, dict):
            logger.warning("Empty or non-dict profile file: %s", path)
            return None

        profile = profile_from_dict(data)
        profile.validate()
        return profile

    # -- matching ------------------------------------------------------------

    def match_instrument(self, idn_response: Optional[str]) -> Optional[InstrumentProfile]:
        """Find the first profile whose identity patterns match *idn_response*.

        If profiles have not been loaded yet, ``load_all()`` is called
        automatically.

        Args:
            idn_response: The raw string returned by ``*IDN?``.

        Returns:
            The matching ``InstrumentProfile``, or ``None``.
        """
        if not idn_response:
            return None

        if not self._loaded:
            self.load_all()

        for profile in self._profiles.values():
            if profile.matches_idn(idn_response):
                logger.debug(
                    "Matched IDN '%s' to profile %s",
                    idn_response,
                    profile.profile_key,
                )
                return profile

        logger.debug("No profile match for IDN: %s", idn_response)
        return None

    # -- lookup helpers ------------------------------------------------------

    def get_profile(self, key: str) -> Optional[InstrumentProfile]:
        """Look up a profile by its key (``manufacturer_model``).

        Auto-loads if needed.
        """
        if not self._loaded:
            self.load_all()
        return self._profiles.get(key.lower())

    def get_profiles_by_class(
        self, instrument_class: str
    ) -> List[InstrumentProfile]:
        """Return all profiles for a given instrument class."""
        if not self._loaded:
            self.load_all()
        return [
            p
            for p in self._profiles.values()
            if p.instrument.instrument_class == instrument_class.lower()
        ]

    def get_profiles_with_command(
        self, command_name: str
    ) -> List[InstrumentProfile]:
        """Return all profiles that define a given command name."""
        if not self._loaded:
            self.load_all()
        return [
            p for p in self._profiles.values() if command_name in p.commands
        ]

    def get_all_instrument_classes(self) -> List[str]:
        """Return sorted list of unique instrument classes across profiles."""
        if not self._loaded:
            self.load_all()
        return sorted(
            {p.instrument.instrument_class for p in self._profiles.values()}
        )

    # -- mutation ------------------------------------------------------------

    def add_profile(self, profile: InstrumentProfile) -> None:
        """Programmatically add a profile (useful for testing)."""
        self._profiles[profile.profile_key] = profile
        logger.info("Added profile: %s", profile.profile_key)

    def remove_profile(self, key: str) -> bool:
        """Remove a profile by key.  Returns True if found."""
        lower = key.lower()
        if lower in self._profiles:
            del self._profiles[lower]
            logger.info("Removed profile: %s", lower)
            return True
        return False

    def reload(self) -> int:
        """Reload all profiles from disk (clears cache first)."""
        self._loaded = False
        return self.load_all()

    # -- identity probes (for non-standard queries) --------------------------

    def get_identity_probes(self) -> List[tuple]:
        """Return ``(bytes, str)`` probes for non-standard identity queries.

        Instruments whose profile uses a query other than ``*IDN?`` need
        a custom probe.  The returned tuples are
        ``(command_bytes, profile_key)``.
        """
        if not self._loaded:
            self.load_all()

        seen: set[bytes] = set()
        probes: List[tuple] = []

        for profile in self._profiles.values():
            query = profile.identity.query
            if query == "*IDN?":
                continue

            terminator = profile.settings.terminator if profile.settings else "\n"
            cmd = (query + terminator).encode()
            if cmd in seen:
                continue
            seen.add(cmd)
            probes.append((cmd, profile.profile_key))
            logger.debug(
                "Identity probe from profile %s: %r",
                profile.profile_key,
                cmd,
            )

        return probes


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_loader: Optional[ProfileLoader] = None


def get_profile_loader(profiles_dir: Optional[str] = None) -> ProfileLoader:
    """Return (or create) the module-level ``ProfileLoader`` singleton.

    On first call the *profiles_dir* argument is used; subsequent calls
    ignore it and return the existing instance.
    """
    global _loader
    if _loader is None:
        _loader = ProfileLoader(profiles_dir)
    return _loader
