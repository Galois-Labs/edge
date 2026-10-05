"""
Profile loader for YAML instrument profiles.

Loads, validates, and caches instrument profiles from a directory of
YAML files.  Provides matching functionality to find the right profile
for a given ``*IDN?`` response string.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .config import _default_config_dir
from .profile_schema import InstrumentProfile, _gp_api, _legacy_profile_from_dict, profile_from_galois

logger = logging.getLogger(__name__)

#: Protocol-driver profile subtrees of the bundled dir (driver registry, not
#: instrument profiles); never scanned as instrument profiles (edge-api.md §6).
_PROTOCOL_SUBTREES = frozenset({"can", "spi", "i2c", "opcua", "modbus"})

#: Error diagnostics that do not mean galois-profiles rejected the file: a
#: within-dir duplicate key loaded fine and lost to the first by sorted path
#: (semantics §7.1), so the legacy safety net must not resurrect it.
_NOT_A_REJECTION = frozenset({"E-PROFILE-DUPKEY"})


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

    def __init__(
        self,
        profiles_dir: Optional[str] = None,
        dynamic_dir: Optional[str] = None,
        extra_dirs: Sequence[str] = (),
    ) -> None:
        if profiles_dir:
            self._profiles_dir = Path(profiles_dir)
        else:
            self._profiles_dir = Path(__file__).parent / "profiles"

        # A second directory, scanned alongside the bundled one, on a
        # writable path. DeployProfile writes here.
        #
        # Without it a deployed profile lands somewhere nothing reads.
        # The bundled directory ships inside the image, so a daemon that
        # could only scan that one would accept a profile over gRPC,
        # write it, report success, and never match an instrument against
        # it — the instrument stays "registered with no matching profile"
        # and every named command a sequence needs is absent. The failure
        # surfaces much later, as an empty command catalog, with nothing
        # connecting it back to the deploy that appeared to work.
        self._dynamic_dir = Path(dynamic_dir) if dynamic_dir else None

        # Profile dirs of the registered instrument backends (edge-api.md §2,
        # F1), scanned after the bundled dir and before the dynamic dir.
        self._extra_dirs: tuple[Path, ...] = tuple(Path(d) for d in extra_dirs)

        self._profiles: Dict[str, InstrumentProfile] = {}
        self._loaded: bool = False

    # -- properties ----------------------------------------------------------

    @property
    def profiles_dir(self) -> Path:
        return self._profiles_dir

    @property
    def dynamic_dir(self) -> Optional[Path]:
        """Writable directory for deployed profiles, or None if unset."""
        return self._dynamic_dir

    @property
    def extra_dirs(self) -> tuple[Path, ...]:
        """Backend profile dirs, scanned after profiles_dir and before dynamic_dir (edge-api.md §2)."""
        return self._extra_dirs

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

    # -- loading (galois-profiles; contracts/edge-api.md §6) -----------------

    def _bundled_sources(self) -> Tuple[List[Path], List[Path]]:
        """(dirs, root files) to scan in the bundled dir, minus protocol-driver subtrees (CI-15)."""
        root = self._profiles_dir
        if not root.is_dir():
            return [], []
        children = sorted(root.iterdir())
        if not any(c.is_dir() and c.name in _PROTOCOL_SUBTREES for c in children):
            return [root], []
        dirs = [c for c in children if c.is_dir() and not c.name.startswith("_") and c.name not in _PROTOCOL_SUBTREES]
        files = [c for c in children if c.is_file() and c.suffix in (".yaml", ".yml") and not c.name.startswith("_")]
        return dirs, files

    @staticmethod
    def _cache_dir() -> Optional[Path]:
        path = Path(_default_config_dir()) / "profile-cache"
        try:
            path.mkdir(parents=True, exist_ok=True)
            return path
        except OSError as exc:
            logger.debug("profile cache disabled (%s): %s", path, exc)
            return None

    @staticmethod
    def _log_diagnostics(diagnostics, default_file: Optional[Path] = None) -> List[Path]:
        """Log per file (as before M1); return the files galois-profiles rejected."""
        failed: List[Path] = []
        for d in diagnostics:
            where = d.file or (str(default_file) if default_file else "?")
            level = logging.ERROR if d.severity == "error" else logging.WARNING
            logger.log(level, "%s: %s %s", where, d.code, d.message)
            if d.severity == "error" and d.file and d.code not in _NOT_A_REJECTION:
                failed.append(Path(d.file))
        return list(dict.fromkeys(failed))

    def _legacy_fallback(self, path: Path) -> Optional[InstrumentProfile]:
        """CI-16 safety net: a v1 file galois-profiles rejects still loads as it did before M1."""
        try:
            data = _gp_api("load_yaml")(path.read_text(encoding="utf-8"))
            if not data or not isinstance(data, dict):
                logger.warning("Empty or non-dict profile file: %s", path)
                return None
            if data.get("schema_version") is not None:
                return None
            profile = _legacy_profile_from_dict(data)
            profile.validate()
        except Exception:
            logger.exception("Failed to load profile %s", path)
            return None
        logger.warning("galois-profiles rejected %s; loaded with the legacy v1 reader", path)
        return profile

    def _convert(self, gp_profile, source) -> Optional[InstrumentProfile]:
        try:
            profile = profile_from_galois(gp_profile)
            profile.validate()
            return profile
        except Exception:
            logger.exception("Failed to load profile %s", source)
            return None

    def load_all(self) -> int:
        """Load bundled → extra_dirs → dynamic profiles through galois-profiles.

        Scan order and precedence (edge-api.md §2, §6):

        - ``profiles_dir`` (bundled), then ``extra_dirs`` in order, then
          ``dynamic_dir``. The bundled scan skips the protocol-driver
          subtrees ``can/``, ``spi/``, ``i2c/``, ``opcua/``, ``modbus/``.
        - A key already loaded from an earlier non-dynamic dir is kept;
          the later copy is logged at WARNING, so a backend's profile dirs
          can never shadow a bundled profile.
        - A dynamic-dir (deployed) profile replaces a same-key profile.
        - Within one dir, the first file by sorted path wins
          (``E-PROFILE-DUPKEY``).
        - The ``_``-prefix filter applies to the bundled dir and
          ``extra_dirs``, not to the dynamic dir.

        Parsed profiles are cached as JSON under
        ``<config dir>/profile-cache`` (galois-profiles; no pickle).

        Returns:
            Number of profiles loaded.
        """
        try:
            load_profiles, load_profile = _gp_api("load_profiles"), _gp_api("load_profile")
            profile_error = _gp_api("ProfileError")
        except ImportError:
            logger.error("galois-profiles is not installed; cannot load profiles")
            self._loaded = True
            return 0

        self._profiles.clear()
        if not self._profiles_dir.is_dir():
            logger.warning("Profiles directory not found: %s", self._profiles_dir)
        for extra in self._extra_dirs:
            if not extra.is_dir():
                logger.debug("Backend profile dir not found: %s", extra)
        cache_dir = self._cache_dir()
        cache_arg = str(cache_dir) if cache_dir is not None else None
        loaded: Dict[str, InstrumentProfile] = {}
        origin: Dict[str, str] = {}

        def accept(profile: Optional[InstrumentProfile], source, replace: bool) -> None:
            if profile is None:
                return
            key = profile.profile_key
            if key in loaded and not replace:
                logger.warning("profile %s from %s shadowed by %s", key, source, origin[key])
                return
            loaded[key] = profile
            origin[key] = str(source)

        bundled_dirs, root_files = self._bundled_sources()
        for path in root_files:                                   # uncached (CI-15)
            try:
                accept(self._convert(load_profile(path), path), path, replace=False)
            except profile_error as exc:
                self._log_diagnostics(exc.diagnostics, path)
                accept(self._legacy_fallback(path), path, replace=False)

        static_dirs = [*bundled_dirs, *[d for d in self._extra_dirs if d.is_dir()]]
        if static_dirs:
            pset = load_profiles([str(d) for d in static_dirs], cache_dir=cache_arg)
            failed = self._log_diagnostics(pset.diagnostics)
            for key in pset:
                accept(self._convert(pset[key], pset[key].source_path), pset[key].source_path, replace=False)
            for path in failed:
                accept(self._legacy_fallback(path), path, replace=False)

        if self._dynamic_dir is not None and self._dynamic_dir.is_dir():
            dset = load_profiles([str(self._dynamic_dir)], cache_dir=cache_arg, exclude_underscore=False)
            failed = self._log_diagnostics(dset.diagnostics)
            for key in dset:
                accept(self._convert(dset[key], dset[key].source_path), dset[key].source_path, replace=True)
            for path in failed:
                accept(self._legacy_fallback(path), path, replace=True)

        self._profiles = loaded
        self._loaded = True
        for key, profile in loaded.items():
            logger.info("Loaded profile: %s (%d commands)", key, len(profile.commands))
        logger.info("Loaded %d profile(s) from %s", len(loaded), self._profiles_dir)
        return len(loaded)

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
