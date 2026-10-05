"""
Profile loader for YAML instrument profiles.

Loads, validates, and caches instrument profiles from a directory of
YAML files.  Provides matching functionality to find the right profile
for a given ``*IDN?`` response string.
"""

from __future__ import annotations

import hashlib
import logging
import os
import pickle
from pathlib import Path
from typing import Dict, List, Optional, Sequence

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

    # -- loading -------------------------------------------------------------

    def load_all(self) -> int:
        """Load every YAML profile from the bundled, backend, and dynamic dirs.

        Scan order and precedence (edge-api.md §2):

        - ``profiles_dir`` (bundled), then ``extra_dirs`` in order, then
          ``dynamic_dir``.
        - A key already loaded from an earlier non-dynamic dir is kept;
          the later copy is logged at WARNING, so a backend's profile dirs
          can never shadow a bundled profile.
        - A dynamic-dir (deployed) profile replaces a same-key profile.
        - Within one dir, the last file by sorted path wins.
        - The ``_``-prefix filter applies to the bundled dir and
          ``extra_dirs``, not to the dynamic dir.

        Uses a pickle cache to avoid re-parsing 130+ YAML files on
        every startup (saves ~60s on Raspberry Pi SD cards).  The cache
        is invalidated when any scanned YAML file is added, removed, or
        modified.

        Returns:
            Number of profiles loaded.
        """
        if yaml is None:
            logger.error("PyYAML is not installed; cannot load profiles")
            self._loaded = True
            return 0

        self._profiles.clear()

        def _scan(directory: Path, underscore_filter: bool) -> List[Path]:
            files = sorted(list(directory.rglob("*.yaml")) + list(directory.rglob("*.yml")))
            if underscore_filter:
                # The bundled tree uses a leading underscore to mark internal
                # files. A deployed filename is whatever the deploying tool
                # chose and is not ours to reinterpret, so the dynamic dir
                # is scanned unfiltered.
                files = [f for f in files
                         if not any(part.startswith("_") for part in f.relative_to(directory).parts)]
            return files

        # (dir_index, path, is_dynamic) in precedence order (edge-api.md §2)
        sources: List[tuple] = []
        if self._profiles_dir.is_dir():
            sources += [(0, f, False) for f in _scan(self._profiles_dir, True)]
        else:
            logger.warning("Profiles directory not found: %s", self._profiles_dir)
        for i, extra in enumerate(self._extra_dirs, start=1):
            if extra.is_dir():
                sources += [(i, f, False) for f in _scan(extra, True)]
            else:
                logger.debug("Backend profile dir not found: %s", extra)
        if self._dynamic_dir and self._dynamic_dir.is_dir():
            sources += [(len(self._extra_dirs) + 1, f, True) for f in _scan(self._dynamic_dir, False)]

        # Try loading from pickle cache (keyed by every scanned file).
        # The key covers the backend and dynamic files too, so deploying a
        # profile invalidates the cache and the next load picks it up.
        cache_path = self._profiles_dir / "_cache.pkl" if self._profiles_dir.is_dir() else None
        cache_key = self._compute_cache_key([f for _i, f, _d in sources])

        if cache_path is not None and cache_path.exists():
            try:
                with open(cache_path, "rb") as fh:
                    cached = pickle.load(fh)
                if cached.get("key") == cache_key:
                    self._profiles = cached["profiles"]
                    self._loaded = True
                    logger.info(
                        "Loaded %d profile(s) from cache", len(self._profiles)
                    )
                    return len(self._profiles)
            except Exception:
                logger.debug("Profile cache invalid, rebuilding")

        # Cache miss — parse all YAML files
        origin: Dict[str, tuple] = {}   # key -> (dir_index, path)
        for dir_index, path, is_dynamic in sources:
            try:
                profile = self._load_file(path)
            except Exception:
                logger.exception("Failed to load profile %s", path)
                continue
            if profile is None:
                continue
            key = profile.profile_key
            prev = origin.get(key)
            if prev is not None and not is_dynamic and prev[0] < dir_index:
                logger.warning("profile %s from %s shadowed by %s", key, path, prev[1])
                continue
            self._profiles[key] = profile
            origin[key] = (dir_index, path)
            logger.info(
                "Loaded profile: %s (%d commands)",
                key,
                len(profile.commands),
            )
        loaded = len(self._profiles)

        # Write cache for next startup. Atomic (CI-29): parallel readers
        # never see a torn pickle.
        if cache_path is not None:
            tmp = cache_path.with_name(f"._cache.{os.getpid()}.tmp")
            try:
                with open(tmp, "wb") as fh:
                    pickle.dump({"key": cache_key, "profiles": self._profiles}, fh)
                os.replace(tmp, cache_path)
                logger.info("Profile cache written (%d profiles)", loaded)
            except Exception as exc:
                logger.debug("Could not write profile cache: %s", exc)
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass

        self._loaded = True
        logger.info(
            "Loaded %d profile(s) from %s", loaded, self._profiles_dir
        )
        return loaded

    @staticmethod
    def _compute_cache_key(yaml_files: list[Path]) -> str:
        """Hash every scanned file's full path, mtime, and size, in scan order."""
        h = hashlib.md5()
        for f in yaml_files:
            st = f.stat()
            h.update(f"{f}\0{st.st_mtime_ns}\0{st.st_size}\0".encode())
        return h.hexdigest()

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
