"""edge-api.md §2 (F1): backend profile dirs are scanned between the bundled and dynamic dirs."""
from __future__ import annotations

import logging
import os

import pytest

from galois_edge.profile_loader import ProfileLoader

pytestmark = pytest.mark.critical


def _profile(directory, filename, model, scpi="*IDN?"):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / filename).write_text(
        f"instrument: {{manufacturer: ACME, model: {model}, class: dmm}}\n"
        f"identity: {{patterns: ['ACME,{model}']}}\n"
        f"commands:\n  probe: {{scpi: '{scpi}', type: query}}\n"
    )


@pytest.fixture
def dirs(tmp_path):
    return tmp_path / "bundled", tmp_path / "backend", tmp_path / "dynamic"


def test_extra_dir_profiles_load(dirs):
    bundled, backend, dynamic = dirs
    _profile(bundled, "a.yaml", "A")
    _profile(backend, "sim.yaml", "SIM")
    loader = ProfileLoader(str(bundled), dynamic_dir=str(dynamic), extra_dirs=[str(backend)])
    assert loader.load_all() == 2
    assert loader.get_profile("acme_sim") is not None
    assert [str(p) for p in loader.extra_dirs] == [str(backend)]


def test_bundled_wins_over_a_backend_dir_with_a_warning(dirs, caplog):  # Review Focus 4
    bundled, backend, _ = dirs
    _profile(bundled, "a.yaml", "A", scpi=":BUNDLED?")
    _profile(backend, "a.yaml", "A", scpi=":BACKEND?")
    with caplog.at_level(logging.WARNING, logger="galois_edge.profile_loader"):
        loader = ProfileLoader(str(bundled), extra_dirs=[str(backend)])
        loader.load_all()
    assert loader.get_profile("acme_a").commands["probe"].scpi == ":BUNDLED?"
    assert any("shadowed" in r.getMessage().lower() for r in caplog.records)


def test_earlier_extra_dir_wins(dirs, tmp_path):
    bundled, backend, _ = dirs
    other = tmp_path / "other"
    _profile(backend, "x.yaml", "X", scpi=":FIRST?")
    _profile(other, "x.yaml", "X", scpi=":SECOND?")
    loader = ProfileLoader(str(bundled), extra_dirs=[str(backend), str(other)])
    loader.load_all()
    assert loader.get_profile("acme_x").commands["probe"].scpi == ":FIRST?"


def test_dynamic_dir_still_replaces(dirs):  # Review Focus 4: deploy semantics preserved
    bundled, backend, dynamic = dirs
    _profile(bundled, "a.yaml", "A", scpi=":BUNDLED?")
    _profile(backend, "b.yaml", "B", scpi=":BACKEND?")
    _profile(dynamic, "_deployed-a.yaml", "A", scpi=":DEPLOYED?")   # underscore NOT filtered in dynamic
    _profile(dynamic, "b.yaml", "B", scpi=":DEPLOYED-B?")
    loader = ProfileLoader(str(bundled), dynamic_dir=str(dynamic), extra_dirs=[str(backend)])
    loader.load_all()
    assert loader.get_profile("acme_a").commands["probe"].scpi == ":DEPLOYED?"
    assert loader.get_profile("acme_b").commands["probe"].scpi == ":DEPLOYED-B?"


def test_underscore_filter_applies_to_extra_dirs(dirs):
    bundled, backend, _ = dirs
    _profile(backend, "_internal.yaml", "HIDDEN")
    _profile(backend / "_private", "p.yaml", "PRIVATE")
    loader = ProfileLoader(str(bundled), extra_dirs=[str(backend)])
    loader.load_all()
    assert loader.get_profile("acme_hidden") is None and loader.get_profile("acme_private") is None


def test_missing_bundled_dir_still_loads_extra_dirs(tmp_path):
    backend = tmp_path / "backend"
    _profile(backend, "s.yaml", "S")
    loader = ProfileLoader(str(tmp_path / "nope"), extra_dirs=[str(backend), str(tmp_path / "missing")])
    assert loader.load_all() == 1


def test_cache_key_covers_extra_dir_files(dirs):
    bundled, backend, _ = dirs
    _profile(bundled, "a.yaml", "A")
    _profile(backend, "s.yaml", "S", scpi=":ONE?")
    ProfileLoader(str(bundled), extra_dirs=[str(backend)]).load_all()   # warms the JSON cache
    _profile(backend, "s.yaml", "S", scpi=":TWO?")
    st = os.stat(backend / "s.yaml")
    os.utime(backend / "s.yaml", ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    loader = ProfileLoader(str(bundled), extra_dirs=[str(backend)])
    loader.load_all()
    assert loader.get_profile("acme_s").commands["probe"].scpi == ":TWO?"
