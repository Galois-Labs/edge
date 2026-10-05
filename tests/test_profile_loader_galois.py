from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from galois_edge.profile_loader import ProfileLoader

pytestmark = pytest.mark.critical
EX = Path(__file__).resolve().parents[1] / "third_party/edgesim/contracts/examples/profiles"
V1 = "instrument: {{manufacturer: ACME, model: {m}}}\nidentity: {{patterns: ['ACME,{m}']}}\ncommands:\n  c: {{scpi: '*IDN?', type: query, params: {{v: {{type: float, max: 1e7}}}}}}\n"


def test_protocol_driver_subtrees_are_not_scanned(tmp_path, caplog):
    (tmp_path / "scpi").mkdir()
    (tmp_path / "scpi" / "a.yaml").write_text(V1.format(m="A"))
    (tmp_path / "can").mkdir()
    (tmp_path / "can" / "bms.yaml").write_text("protocol: can\ncommands: {x: {can: {message_id: 1}}}\n")
    with caplog.at_level(logging.WARNING):
        loader = ProfileLoader(str(tmp_path))
        assert loader.load_all() == 1
    assert "bms.yaml" not in caplog.text


def test_yaml12_floats_in_profiles(tmp_path):
    (tmp_path / "a.yaml").write_text(V1.format(m="A"))
    loader = ProfileLoader(str(tmp_path))
    loader.load_all()
    assert loader.get_profile("acme_a").commands["c"].params["v"].max == 1e7


def test_v2_and_v1_load_side_by_side(tmp_path):
    (tmp_path / "psu.yaml").write_text((EX / "galois_sim-psu-2.yaml").read_text())
    (tmp_path / "a.yaml").write_text(V1.format(m="A"))
    loader = ProfileLoader(str(tmp_path))
    assert loader.load_all() == 2
    assert loader.get_profile("galois_sim-psu-2").get_command("set_voltage") is not None


def test_json_cache_lives_in_the_config_dir_not_the_profile_dir(tmp_path):
    (tmp_path / "scpi").mkdir()
    (tmp_path / "scpi" / "a.yaml").write_text(V1.format(m="A"))
    ProfileLoader(str(tmp_path)).load_all()       # no protocol subtrees ⇒ passed whole ⇒ cached
    cache = Path(os.environ["HOME"]) / ".config" / "galois-edge" / "profile-cache"   # HOME isolated by tests/conftest.py
    assert cache.is_dir() and any(cache.iterdir())
    assert not list(tmp_path.rglob("_cache.pkl"))


def test_legacy_reader_safety_net(tmp_path, caplog, monkeypatch):
    (tmp_path / "odd.yaml").write_text(V1.format(m="ODD"))
    (tmp_path / "can").mkdir()                    # forces the split path: root files go through load_profile
    import galois_edge.profile_schema as ps
    real = ps._gp_api

    def strict(name):
        fn = real(name)
        if name == "load_profile":
            def reject(path, _fn=fn):
                raise real("ProfileError")([])  # simulate galois rejecting a v1 quirk
            return reject
        return fn

    monkeypatch.setattr("galois_edge.profile_loader._gp_api", strict)
    loader = ProfileLoader(str(tmp_path))
    with caplog.at_level(logging.WARNING):
        assert loader.load_all() == 1
    assert "legacy v1 reader" in caplog.text


def test_within_dir_duplicates_keep_the_first_by_sorted_path_even_when_deployed(tmp_path, caplog):
    dynamic = tmp_path / "dynamic"
    dynamic.mkdir()
    (dynamic / "a.yaml").write_text(V1.format(m="DUP").replace("*IDN?", ":FIRST?"))
    (dynamic / "b.yaml").write_text(V1.format(m="DUP").replace("*IDN?", ":SECOND?"))
    loader = ProfileLoader(str(tmp_path / "bundled"), dynamic_dir=str(dynamic))
    with caplog.at_level(logging.WARNING):
        assert loader.load_all() == 1
    assert loader.get_profile("acme_dup").commands["c"].scpi == ":FIRST?"   # never resurrected by the safety net
    assert "E-PROFILE-DUPKEY" in caplog.text and "legacy v1 reader" not in caplog.text
