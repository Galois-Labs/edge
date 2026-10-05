"""M1 criterion 3, as processes: edge's v1 profiles convert losslessly to nested v2, and old names still resolve.

The `galois-profiles convert` CLI converts edge's shipped v1 profiles. The contracts vendor the same files
as the converter's golden inputs. `convert --check` proves the conversion is byte-stable. Lossless means
semantics §7.5.7: for every v1 command `n`, edge's shim gives an identical `CommandConfig` for `v1[n]` and
for `converted.resolve(n)`. That is the check CI-31 deferred to Phase 4.

edgesim's bundled DSOX3000 profile, the one the simulator serves, is that conversion plus generated
enrichment (state, ports, sim). So `convert --check` cannot compare against it. Its generator's own
`--check` proves it is current for this v1 file, and the same lossless rule holds for it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.e2e.conftest import CONTRACTS, EDGESIM, ROOT, console_script

pytestmark = [pytest.mark.e2e, pytest.mark.slow]

EDGE_V1 = ROOT / "src/galois_edge/profiles/scpi"
PROFILE_SCHEMA = CONTRACTS / "schemas/profile-v2.schema.json"
DSOX_GENERATOR = EDGESIM / "examples/tools/make_dsox_subset.py"


def edge_profile(path: Path):
    """`path` as edge loads it: galois-profiles parses, edge's shim builds the dataclasses (edge-api §6)."""
    from galois_profiles import load_profile

    from galois_edge.profile_schema import profile_from_galois
    return profile_from_galois(load_profile(path))


def assert_lossless(v1_path: Path, v2_path: Path) -> int:
    """Every v1 command resolves by its v1 name in the v2 profile to an identical CommandConfig."""
    from galois_profiles.loader import load_yaml

    names = list(load_yaml(v1_path.read_text())["commands"])
    v1, v2 = edge_profile(v1_path), edge_profile(v2_path)
    assert names
    lost = [n for n in names if v2.resolve(n) != v1.get_command(n) or v2.get_command(n) != v1.get_command(n)]
    assert lost == [], f"{v2_path.name} does not keep these v1 commands: {lost}"
    return len(names)


@pytest.mark.parametrize("name", ["keysight_dsox3000", "rigol_dp800"])
def test_edge_v1_profile_converts_losslessly(e2e, tmp_path, name):
    import jsonschema

    from galois_profiles.loader import load_yaml

    v1 = EDGE_V1 / f"{name}.yaml"
    assert v1.read_bytes() == (CONTRACTS / "examples/v1" / f"{name}.yaml").read_bytes(), \
        "the contracts' golden v1 input must be edge's shipped profile (contracts/README.md)"
    out = tmp_path / f"{name}.v2.yaml"
    convert = console_script("galois-profiles")

    converted = e2e.run([convert, "convert", str(v1), "-o", str(out)])
    assert converted.returncode == 0, converted.stderr
    assert all(line.startswith("W-") for line in converted.stderr.splitlines()), converted.stderr
    checked = e2e.run([convert, "convert", "--check", "-o", str(out), str(v1)])
    assert checked.returncode == 0, checked.stderr                    # deterministic, byte for byte

    stale = tmp_path / "stale.v2.yaml"
    stale.write_bytes(out.read_bytes() + b"\n")
    refused = e2e.run([convert, "convert", "--check", "-o", str(stale), str(v1)])
    assert refused.returncode == 1 and "differs" in refused.stderr   # --check really compares

    document = load_yaml(out.read_text())
    assert document["schema_version"] == 2
    jsonschema.Draft202012Validator(json.loads(PROFILE_SCHEMA.read_text())).validate(document)
    assert assert_lossless(v1, out) > 50


def test_bundled_dsox3000_is_the_lossless_conversion_plus_enrichment(e2e):
    import edgesim

    bundled = Path(edgesim.__file__).parent / "profiles/keysight_dsox3000.yaml"
    generated = e2e.run([sys.executable, str(DSOX_GENERATOR), "--check", "--output", str(bundled)])
    assert generated.returncode == 0, generated.stdout + generated.stderr
    assert assert_lossless(EDGE_V1 / "keysight_dsox3000.yaml", bundled) > 50
