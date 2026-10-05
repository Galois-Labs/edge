"""M1 criterion 1, the topology half: a bench in the cloud's topology format loads unchanged into edgesim.

Each contract bench must:
- pass the cloud BenchTopology shape plus `ext` (contracts/schemas/bench-topology.schema.json, from the
  submodule);
- survive a JSON round trip, which is how the cloud stores and returns it;
- be accepted by `edgesim validate`;
- be served by `edgesim world serve`, whose World reports the very same document from another process.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from tests.e2e.conftest import BENCH_SCHEMA, PSU_BENCH, SCOPE_BENCH, console_script

pytestmark = [pytest.mark.e2e, pytest.mark.slow]


def load_bench_document(path) -> Any:
    if path.suffix == ".json":
        return json.loads(path.read_text())
    from galois_profiles.loader import load_yaml           # YAML 1.2 floats (`1.0e-7`), as edgesim reads it
    return load_yaml(path.read_text())


def without_ext(value: Any) -> Any:
    """The document as a consumer that ignores `ext` sees it."""
    if isinstance(value, dict):
        return {k: without_ext(v) for k, v in value.items() if k != "ext"}
    if isinstance(value, list):
        return [without_ext(v) for v in value]
    return value


@pytest.mark.parametrize("bench", [PSU_BENCH, SCOPE_BENCH], ids=lambda p: p.name.split(".")[0])
def test_cloud_topology_loads_unchanged_into_edgesim(e2e, tmp_path, bench):
    import jsonschema

    import edgesim.remote

    validator = jsonschema.Draft202012Validator(json.loads(BENCH_SCHEMA.read_text()))
    document = load_bench_document(bench)
    validator.validate(document)
    validator.validate(without_ext(document))               # `ext` is purely additive

    wire = json.loads(json.dumps(document))                  # what the cloud's PUT/GET .../topology carries
    assert wire == document
    cloud_copy = tmp_path / "from-cloud.bench.json"
    cloud_copy.write_text(json.dumps(wire, indent=2))

    checked = e2e.run([console_script("edgesim"), "validate", str(cloud_copy), "--format", "json"])
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert json.loads(checked.stdout) == []

    world = e2e.world_serve(cloud_copy)
    assert world.bench_id == document["ext"]["sim"]["name"]
    remote = edgesim.remote.connect(world.socket, timeout_s=30.0)
    try:
        assert remote.topology() == wire
        nodes = [n for n in document["nodes"] if n["kind"] == "instrument"]
        instruments = remote.instruments()
        assert set(instruments) == {n["instrumentId"] for n in nodes}
        assert {i.profile_key for i in instruments.values()} == {n["ext"]["sim"]["profile"] for n in nodes}
    finally:
        remote.close()
    assert world.proc.terminate() == 0, world.proc.output_tail()
