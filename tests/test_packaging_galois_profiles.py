from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.critical
ROOT = Path(__file__).resolve().parents[1]


def test_pyproject_declares_galois_profiles_and_the_sim_extra():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert "galois-profiles" in data["project"]["dependencies"]
    extras = data["project"]["optional-dependencies"]
    assert extras["sim"] == ["edgesim"]
    assert not any("sim" in e for e in extras["all"])
    assert data["tool"]["uv"]["sources"] == {
        "galois-profiles": {"path": "third_party/edgesim/packages/galois-profiles", "editable": True},
        "edgesim": {"path": "third_party/edgesim/packages/edgesim", "editable": True},
    }


def test_requirements_install_galois_profiles_first():
    lines = [l.strip() for l in (ROOT / "requirements.txt").read_text().splitlines()
             if l.strip() and not l.lstrip().startswith("#")]
    assert lines[0] == "./third_party/edgesim/packages/galois-profiles"


def test_pyinstaller_collects_galois_profiles_and_new_modules():
    text = (ROOT / "galois-edge-daemon.spec").read_text()
    assert 'gp_datas, gp_binaries, gp_hiddenimports = collect_all("galois_profiles")' in text
    for var in ("gp_datas", "gp_binaries", "gp_hiddenimports"):
        assert text.count(var) >= 2, var
    for mod in ("galois_edge.validation", "galois_edge.tracing", "galois_edge.backends.demo"):
        assert f'"{mod}"' in text


def test_galois_profiles_resolves_into_this_checkouts_submodule():
    import galois_profiles
    root = (ROOT / "third_party/edgesim/packages/galois-profiles").resolve()
    assert Path(galois_profiles.__file__).resolve().is_relative_to(root)
