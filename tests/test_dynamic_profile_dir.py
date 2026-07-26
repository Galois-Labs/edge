"""Deployed instrument profiles must be scanned, matched and bindable.

A daemon that accepts a profile over gRPC, writes it, reports success and
never matches an instrument against it is the worst shape this can take:
the deploy looks like it worked, and the failure surfaces much later and
somewhere else, as an instrument "registered with no matching profile" and
an empty command catalog. Nothing connects the two.

Two registries are involved and they are easy to confuse:

* ``driver_profile_dir`` holds PROTOCOL DRIVER profiles for the driver
  registry (modbus, can, ...).
* ``dynamic_profile_dir`` holds INSTRUMENT profiles, matched against
  ``*IDN?`` by ProfileLoader.

Writing to the wrong one produces a file that is correct, present, and
never read.
"""

from __future__ import annotations

import textwrap

import pytest

from galois_edge.profile_loader import ProfileLoader

GATEWAY_YAML = textwrap.dedent(
    """
    instrument:
      manufacturer: GALOIS-SIM
      model: CAN-GATEWAY
      class: can_gateway
    identity:
      query: "*IDN?"
      pattern: "GALOIS-SIM,CAN-GATEWAY"
    interfaces:
      - type: ethernet
        port: 5027
    settings:
      timeout_ms: 2000
      terminator: "\\n"
    commands:
      sdo_read:
        scpi: "CAN:SDO:READ? {index},{subindex}"
        type: query
      nmt:
        scpi: "CAN:NMT {command},{node_id}"
        type: write
    """
).strip()

IDN = "GALOIS-SIM,CAN-GATEWAY,0,1.0"


@pytest.fixture
def dynamic_dir(tmp_path):
    d = tmp_path / "instrument-profiles"
    d.mkdir()
    (d / "galois_can_gateway.yaml").write_text(GATEWAY_YAML)
    return d


def test_a_profile_in_the_dynamic_dir_is_matched(dynamic_dir):
    """The whole point. Without this the deploy is a write to nowhere."""
    loader = ProfileLoader(dynamic_dir=str(dynamic_dir))
    loader.load_all()

    profile = loader.match_instrument(IDN)
    assert profile is not None, "deployed profile was written but never read"
    assert profile.instrument.model == "CAN-GATEWAY"


def test_its_commands_are_the_ones_a_sequence_can_call(dynamic_dir):
    """A sequence issues a NAMED command resolved through the bound
    profile, so an empty catalog makes the sequence impossible to build —
    which is exactly how this failure was first noticed."""
    loader = ProfileLoader(dynamic_dir=str(dynamic_dir))
    loader.load_all()

    profile = loader.match_instrument(IDN)
    assert sorted(profile.commands or {}) == ["nmt", "sdo_read"]


def test_without_a_dynamic_dir_the_same_instrument_does_not_match():
    """The discriminating case. A loader that matched anyway would make
    the test above pass for the wrong reason."""
    loader = ProfileLoader()
    loader.load_all()
    assert loader.match_instrument(IDN) is None


def test_the_dynamic_dir_is_exposed(dynamic_dir):
    """DeployProfile reads it off the loader to decide where to write."""
    loader = ProfileLoader(dynamic_dir=str(dynamic_dir))
    assert loader.dynamic_dir == dynamic_dir
    assert ProfileLoader().dynamic_dir is None


def test_bundled_profiles_still_load_alongside(dynamic_dir):
    """Additive, not a replacement. The bundled tree must survive."""
    loader = ProfileLoader(dynamic_dir=str(dynamic_dir))
    loader.load_all()

    assert loader.match_instrument(IDN) is not None
    assert loader.match_instrument("RIGOL TECHNOLOGIES,DP832,X,1") is not None


def test_a_missing_dynamic_dir_is_not_an_error(tmp_path):
    """The directory is created on first deploy. A daemon that has never
    had one must still start."""
    loader = ProfileLoader(dynamic_dir=str(tmp_path / "not-created-yet"))
    loader.load_all()
    assert loader.match_instrument(IDN) is None


def test_a_deployed_profile_can_be_added_without_a_full_reload(dynamic_dir):
    """DeployProfile registers in memory rather than reloading, because a
    full reload rescans the bundled tree — minutes on slow storage, during
    which every profile is gone."""
    import yaml

    from galois_edge.profile_schema import profile_from_dict

    loader = ProfileLoader()
    loader.load_all()
    assert loader.match_instrument(IDN) is None

    loader.add_profile(profile_from_dict(yaml.safe_load(GATEWAY_YAML)))
    assert loader.match_instrument(IDN) is not None


# ---------------------------------------------------------------------------
# Bind is given the FILENAME, not the profile key
#
# ADDED after bind silently found nothing on prod. The cloud sends the name
# the profile was deployed under — "galois_sim_can_gateway" — while the
# loader keys profiles by manufacturer_model, "galois-sim_can-gateway".
# Hyphens against underscores. Matching only on profile_key reported a
# profile that was sitting right there as missing.
# ---------------------------------------------------------------------------

def test_the_deployed_filename_and_the_profile_key_differ(dynamic_dir):
    """The premise. If these were ever equal the bug would be invisible
    and this whole test would be vacuous."""
    import yaml

    from galois_edge.profile_schema import profile_from_dict

    profile = profile_from_dict(yaml.safe_load(GATEWAY_YAML))
    assert profile.profile_key == "galois-sim_can-gateway"
    assert (dynamic_dir / "galois_can_gateway.yaml").is_file()
    assert profile.profile_key != "galois_can_gateway"


def test_normalising_punctuation_reconciles_the_two_forms():
    """A deployer choosing hyphens over underscores has not authored a
    different profile."""
    def norm(value):
        return value.lower().replace("-", "_").replace(" ", "_")

    assert norm("galois-sim_can-gateway") == norm("galois_sim_can_gateway")
    assert norm("GALOIS-SIM_CAN-GATEWAY") == norm("galois_sim_can_gateway")
    assert norm("rigol_dp800") != norm("keysight_dsox3000")


# ---------------------------------------------------------------------------
# Deploy must reject exactly what load rejects
#
# ADDED after an agent-authored profile deployed successfully and then
# failed to load on every startup. deploy parsed it; load parses AND
# validates. So the daemon accepted a profile, wrote it, reported success,
# and the instrument never gained a single command — with the reason
# visible only in a log line nobody was reading.
#
# The real error was: "Command 'bitrate': Property commands require at
# least 'getter' or 'setter'". That sentence is actionable. "Deploy
# succeeded" is not, and it is worse than a failure because it stops the
# author looking.
# ---------------------------------------------------------------------------

INVALID_YAML = textwrap.dedent(
    """
    instrument:
      manufacturer: GALOIS-SIM
      model: CAN-GATEWAY
    identity:
      query: "*IDN?"
      pattern: "GALOIS-SIM,CAN-GATEWAY"
    interfaces:
      - type: ethernet
        port: 5027
    settings:
      timeout_ms: 2000
      terminator: "\\n"
    commands:
      bitrate:
        type: property
    """
).strip()


def test_the_invalid_profile_parses_but_does_not_validate():
    """The premise, and the reason the bug existed: parsing is not
    validation, and deploy was only parsing."""
    import yaml

    from galois_edge.profile_schema import profile_from_dict

    profile = profile_from_dict(yaml.safe_load(INVALID_YAML))  # parses fine
    with pytest.raises(ValueError, match="getter|setter"):
        profile.validate()


def test_the_loader_refuses_it(tmp_path):
    """Which is what made the mismatch observable: written, reported
    deployed, and absent from every load afterwards."""
    d = tmp_path / "instrument-profiles"
    d.mkdir()
    (d / "broken.yaml").write_text(INVALID_YAML)

    loader = ProfileLoader(dynamic_dir=str(d))
    loader.load_all()
    assert loader.match_instrument(IDN) is None


def test_a_valid_profile_still_deploys(dynamic_dir):
    """The other half. Validating on deploy must not reject good
    profiles — otherwise the fix trades a silent failure for a loud one
    that is equally wrong."""
    import yaml

    from galois_edge.profile_schema import profile_from_dict

    profile = profile_from_dict(yaml.safe_load(GATEWAY_YAML))
    profile.validate()   # must not raise
    assert profile.profile_key == "galois-sim_can-gateway"
