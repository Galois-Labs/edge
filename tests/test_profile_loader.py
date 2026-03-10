"""
Tests for profile_loader.py -- YAML loading and *IDN? matching.
"""

import os
import sys
import tempfile

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "src")
)

# Skip entire module if pyyaml is not installed
yaml = pytest.importorskip("yaml")


SAMPLE_PROFILE_YAML = """\
instrument:
  manufacturer: Keithley
  model: "2400"
  class: sourcemeter
identity:
  query: "*IDN?"
  patterns:
    - "KEITHLEY.*MODEL 2400"
    - "KEITHLEY.*2400"
commands:
  measure_voltage:
    type: query
    scpi: ":MEAS:VOLT?"
    returns:
      type: float
      unit: V
    description: "Measure DC voltage"
  set_voltage:
    type: write
    scpi: ":SOUR:VOLT {voltage}"
    params:
      voltage:
        type: float
        unit: V
        description: "Source voltage level"
  reset:
    type: write
    scpi: "*RST"
    is_dangerous: true
    description: "Reset to factory defaults"
"""


@pytest.fixture
def profile_dir(tmp_path):
    """Create a temporary directory with a sample YAML profile."""
    profile_file = tmp_path / "keithley_2400.yaml"
    profile_file.write_text(SAMPLE_PROFILE_YAML)
    return str(tmp_path)


class TestProfileLoader:
    """Test YAML loading and profile management."""

    def test_load_all_finds_profiles(self, profile_dir):
        from galois_edge.profile_loader import ProfileLoader
        loader = ProfileLoader(profile_dir)
        count = loader.load_all()
        assert count >= 1

    def test_load_all_populates_profiles(self, profile_dir):
        from galois_edge.profile_loader import ProfileLoader
        loader = ProfileLoader(profile_dir)
        loader.load_all()
        assert len(loader.profiles) >= 1

    def test_empty_directory(self, tmp_path):
        from galois_edge.profile_loader import ProfileLoader
        loader = ProfileLoader(str(tmp_path))
        count = loader.load_all()
        assert count == 0

    def test_nonexistent_directory(self, tmp_path):
        from galois_edge.profile_loader import ProfileLoader
        loader = ProfileLoader(str(tmp_path / "nonexistent"))
        count = loader.load_all()
        assert count == 0


class TestProfileMatching:
    """Test *IDN? matching against loaded profiles."""

    def test_match_exact(self, profile_dir):
        from galois_edge.profile_loader import ProfileLoader
        loader = ProfileLoader(profile_dir)
        loader.load_all()

        profile = loader.match_instrument(
            "KEITHLEY INSTRUMENTS INC.,MODEL 2400,1234567,A01"
        )
        assert profile is not None
        assert "2400" in profile.profile_key or "keithley" in profile.profile_key.lower()

    def test_match_partial(self, profile_dir):
        from galois_edge.profile_loader import ProfileLoader
        loader = ProfileLoader(profile_dir)
        loader.load_all()

        profile = loader.match_instrument(
            "KEITHLEY,2400,SN001,v1"
        )
        assert profile is not None

    def test_no_match(self, profile_dir):
        from galois_edge.profile_loader import ProfileLoader
        loader = ProfileLoader(profile_dir)
        loader.load_all()

        profile = loader.match_instrument(
            "AGILENT,34401A,SN002,v2"
        )
        assert profile is None

    def test_empty_idn(self, profile_dir):
        from galois_edge.profile_loader import ProfileLoader
        loader = ProfileLoader(profile_dir)
        loader.load_all()

        profile = loader.match_instrument("")
        assert profile is None

    def test_none_idn(self, profile_dir):
        from galois_edge.profile_loader import ProfileLoader
        loader = ProfileLoader(profile_dir)
        loader.load_all()

        profile = loader.match_instrument(None)
        assert profile is None
