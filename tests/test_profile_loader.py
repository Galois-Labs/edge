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


SDK_PROFILE_YAML = """\
instrument:
  manufacturer: Quantum Design
  model: "PPMS DynaCool"
  class: cryostat
  description: "Physical Property Measurement System via MultiPyVu SDK"

sdk:
  package: "MultiPyVu"
  import_path: "MultiPyVu"
  class_name: "Client"
  is_async: false
  connect:
    method: "open"
    args:
      host: "{host}"
      port: "{port}"
    defaults:
      host: "192.168.0.1"
      port: 5000
    constructor_args:
      timeout: 30
  disconnect:
    method: "close"
  identity:
    method: "get_system_info"
    pattern: "PPMS.*DynaCool"

identity:
  query: "SDK_IDENTITY"
  pattern: "PPMS.*DynaCool"

interfaces:
  - type: ethernet
    port: 5000

settings:
  timeout_ms: 30000

commands:
  get_temperature:
    sdk_call:
      method: "get_temperature"
    type: query
    returns:
      type: float
      unit: K
    description: "Read current sample temperature"
"""


class TestSDKProfileLoading:
    """Test that SDK profiles load with all sub-configs populated."""

    def test_sdk_profile_loads(self, tmp_path):
        from galois_edge.profile_schema import profile_from_dict
        profile_file = tmp_path / "ppms.yaml"
        profile_file.write_text(SDK_PROFILE_YAML)
        data = yaml.safe_load(SDK_PROFILE_YAML)
        profile = profile_from_dict(data)
        assert profile.sdk is not None
        assert profile.is_sdk_instrument is True

    def test_sdk_connect_config(self, tmp_path):
        from galois_edge.profile_schema import profile_from_dict
        data = yaml.safe_load(SDK_PROFILE_YAML)
        profile = profile_from_dict(data)
        connect = profile.sdk.connect
        assert connect.method == "open"
        assert connect.args == {"host": "{host}", "port": "{port}"}
        assert connect.defaults == {"host": "192.168.0.1", "port": 5000}
        assert connect.constructor_args == {"timeout": 30}

    def test_sdk_disconnect_config(self, tmp_path):
        from galois_edge.profile_schema import profile_from_dict
        data = yaml.safe_load(SDK_PROFILE_YAML)
        profile = profile_from_dict(data)
        assert profile.sdk.disconnect.method == "close"

    def test_sdk_identity_config(self, tmp_path):
        from galois_edge.profile_schema import profile_from_dict
        data = yaml.safe_load(SDK_PROFILE_YAML)
        profile = profile_from_dict(data)
        identity = profile.sdk.identity
        assert identity is not None
        assert identity.method == "get_system_info"
        assert identity.pattern == "PPMS.*DynaCool"
        assert identity.property is None

    def test_sdk_top_level_fields(self, tmp_path):
        from galois_edge.profile_schema import profile_from_dict
        data = yaml.safe_load(SDK_PROFILE_YAML)
        profile = profile_from_dict(data)
        assert profile.sdk.package == "MultiPyVu"
        assert profile.sdk.import_path == "MultiPyVu"
        assert profile.sdk.class_name == "Client"
        assert profile.sdk.is_async is False

    def test_sdk_config_defaults_when_no_sub_blocks(self):
        """An SDK block without connect/disconnect/identity sub-blocks
        should get safe defaults (no AttributeError)."""
        from galois_edge.profile_schema import profile_from_dict
        minimal_yaml = """\
instrument:
  manufacturer: TestCo
  model: "X1"
  class: generic
identity:
  pattern: "TESTCO.*X1"
sdk:
  package: "testpkg"
  import_path: "testpkg.driver"
  class_name: "Driver"
commands:
  ping:
    sdk_call:
      method: "ping"
    type: query
"""
        data = yaml.safe_load(minimal_yaml)
        profile = profile_from_dict(data)
        assert profile.sdk is not None
        # connect/disconnect should have safe defaults
        assert profile.sdk.connect.method is None
        assert profile.sdk.connect.constructor_args is None
        assert profile.sdk.disconnect.method is None
        # identity should be None when not specified
        assert profile.sdk.identity is None

    def test_sdk_profile_via_loader(self, tmp_path):
        """Verify the ProfileLoader can load a full SDK YAML file."""
        from galois_edge.profile_loader import ProfileLoader
        profile_file = tmp_path / "ppms.yaml"
        profile_file.write_text(SDK_PROFILE_YAML)
        loader = ProfileLoader(str(tmp_path))
        count = loader.load_all()
        assert count == 1
        profile = loader.match_instrument("PPMS DynaCool SN123")
        assert profile is not None
        assert profile.sdk.connect.method == "open"


# ---------------------------------------------------------------------------
# Phase 1: map, init_commands, cleanup_commands, force_query, parser
# ---------------------------------------------------------------------------


PHASE1_PROFILE_YAML = """\
instrument:
  manufacturer: TestCo
  model: "X100"
  class: sourcemeter
identity:
  query: "*IDN?"
  patterns:
    - "TESTCO.*X100"
settings:
  timeout_ms: 10000
  init_commands:
    - "*RST"
    - ":SYST:BEEP:STAT OFF"
  cleanup_commands:
    - ":OUTP OFF"
    - "*RST"
commands:
  set_output:
    type: write
    scpi: ":OUTP:STAT {state}"
    params:
      state:
        type: enum
        options: ["ON", "OFF"]
        map:
          "ON": 1
          "OFF": 0
    description: "Enable or disable output"
  read_status:
    type: query
    scpi: "STATUS"
    force_query: true
    returns:
      type: string
    description: "Read status register (non-standard, no trailing ?)"
  measure_voltage:
    type: query
    scpi: ":MEAS:VOLT?"
    returns:
      type: float
      unit: V
      parser:
        type: regex
        pattern: "([\\\\d.]+)"
        group: 1
    description: "Measure DC voltage with parser"
"""


class TestPhase1ProfileLoading:
    """Test Phase 1 schema extensions: map, init_commands, cleanup_commands,
    force_query, and parser load correctly from YAML."""

    @pytest.fixture
    def phase1_profile(self):
        from galois_edge.profile_schema import profile_from_dict
        data = yaml.safe_load(PHASE1_PROFILE_YAML)
        return profile_from_dict(data)

    def test_map_on_parameter(self, phase1_profile):
        """map: field on a ParameterConfig is populated correctly."""
        cmd = phase1_profile.commands["set_output"]
        pc = cmd.params["state"]
        assert pc.map is not None
        assert pc.map["ON"] == 1
        assert pc.map["OFF"] == 0

    def test_init_commands(self, phase1_profile):
        """init_commands on SettingsConfig is populated."""
        assert phase1_profile.settings.init_commands is not None
        assert phase1_profile.settings.init_commands == ["*RST", ":SYST:BEEP:STAT OFF"]

    def test_cleanup_commands(self, phase1_profile):
        """cleanup_commands on SettingsConfig is populated."""
        assert phase1_profile.settings.cleanup_commands is not None
        assert phase1_profile.settings.cleanup_commands == [":OUTP OFF", "*RST"]

    def test_force_query_flag(self, phase1_profile):
        """force_query on CommandConfig is True when set in YAML."""
        cmd = phase1_profile.commands["read_status"]
        assert cmd.force_query is True

    def test_force_query_default_false(self, phase1_profile):
        """force_query defaults to False when not specified."""
        cmd = phase1_profile.commands["set_output"]
        assert cmd.force_query is False

    def test_returns_parser_populated(self, phase1_profile):
        """returns.parser is populated from YAML."""
        cmd = phase1_profile.commands["measure_voltage"]
        assert cmd.returns is not None
        assert cmd.returns.parser is not None
        assert cmd.returns.parser["type"] == "regex"
        assert "group" in cmd.returns.parser

    def test_returns_no_parser(self, phase1_profile):
        """returns without parser has parser=None."""
        cmd = phase1_profile.commands["read_status"]
        assert cmd.returns is not None
        assert cmd.returns.parser is None

    def test_profile_loader_loads_phase1_yaml(self, tmp_path):
        """ProfileLoader can load a YAML file with Phase 1 extensions."""
        from galois_edge.profile_loader import ProfileLoader
        profile_file = tmp_path / "testco_x100.yaml"
        profile_file.write_text(PHASE1_PROFILE_YAML)
        loader = ProfileLoader(str(tmp_path))
        count = loader.load_all()
        assert count == 1
        profile = loader.match_instrument("TESTCO X100 SN001 v1")
        assert profile is not None
        assert profile.settings.init_commands == ["*RST", ":SYST:BEEP:STAT OFF"]
        assert profile.commands["set_output"].params["state"].map is not None


# ---------------------------------------------------------------------------
# Task 2.1: Serial interface settings
# ---------------------------------------------------------------------------


SERIAL_PROFILE_YAML = """\
instrument:
  manufacturer: "Stanford Research Systems"
  model: "CS580"
  class: power_supply
identity:
  query: "*IDN?"
  pattern: "Stanford Research Systems,CS580"
interfaces:
  - type: serial
    baud_rate: 9600
    parity: none
    data_bits: 8
    stop_bits: 1
  - type: ethernet
    port: 5025
settings:
  timeout_ms: 5000
commands:
  identify:
    scpi: "*IDN?"
    type: query
    returns:
      type: string
"""


class TestSerialInterfaceConfig:
    """Test serial interface fields load correctly from YAML profiles."""

    @pytest.fixture
    def serial_profile(self):
        from galois_edge.profile_schema import profile_from_dict
        data = yaml.safe_load(SERIAL_PROFILE_YAML)
        return profile_from_dict(data)

    def test_serial_interface_type(self, serial_profile):
        """Serial interface has type 'serial'."""
        serial_iface = serial_profile.interfaces[0]
        assert serial_iface.type == "serial"

    def test_serial_baud_rate(self, serial_profile):
        """baud_rate is parsed from YAML."""
        serial_iface = serial_profile.interfaces[0]
        assert serial_iface.baud_rate == 9600

    def test_serial_parity(self, serial_profile):
        """parity is parsed from YAML."""
        serial_iface = serial_profile.interfaces[0]
        assert serial_iface.parity == "none"

    def test_serial_data_bits(self, serial_profile):
        """data_bits is parsed from YAML."""
        serial_iface = serial_profile.interfaces[0]
        assert serial_iface.data_bits == 8

    def test_serial_stop_bits(self, serial_profile):
        """stop_bits is parsed from YAML."""
        serial_iface = serial_profile.interfaces[0]
        assert serial_iface.stop_bits == 1

    def test_non_serial_interface_unaffected(self, serial_profile):
        """Non-serial interfaces have None for serial fields."""
        ethernet_iface = serial_profile.interfaces[1]
        assert ethernet_iface.type == "ethernet"
        assert ethernet_iface.baud_rate is None
        assert ethernet_iface.parity is None
        assert ethernet_iface.data_bits is None
        assert ethernet_iface.stop_bits is None

    def test_serial_defaults_none(self):
        """InterfaceConfig without serial fields has None defaults."""
        from galois_edge.profile_schema import InterfaceConfig
        iface = InterfaceConfig(type="gpib")
        assert iface.baud_rate is None
        assert iface.parity is None
        assert iface.data_bits is None
        assert iface.stop_bits is None

    def test_serial_profile_via_loader(self, tmp_path):
        """ProfileLoader can load a YAML with serial interface settings."""
        from galois_edge.profile_loader import ProfileLoader
        profile_file = tmp_path / "srs_cs580.yaml"
        profile_file.write_text(SERIAL_PROFILE_YAML)
        loader = ProfileLoader(str(tmp_path))
        count = loader.load_all()
        assert count == 1
        profile = loader.match_instrument(
            "Stanford Research Systems,CS580,SN001,v1"
        )
        assert profile is not None
        serial_iface = profile.interfaces[0]
        assert serial_iface.type == "serial"
        assert serial_iface.baud_rate == 9600
        assert serial_iface.parity == "none"
        assert serial_iface.data_bits == 8
        assert serial_iface.stop_bits == 1

    def test_high_baud_rate(self):
        """High baud rates like 460800 (QDAC) parse correctly."""
        from galois_edge.profile_schema import profile_from_dict
        data = yaml.safe_load("""\
instrument:
  manufacturer: QDevil
  model: QDAC
  class: dac
identity:
  pattern: "QDevil.*QDAC"
interfaces:
  - type: serial
    baud_rate: 460800
    parity: even
    data_bits: 8
    stop_bits: 1.5
commands:
  identify:
    scpi: "*IDN?"
    type: query
    returns:
      type: string
""")
        profile = profile_from_dict(data)
        serial_iface = profile.interfaces[0]
        assert serial_iface.baud_rate == 460800
        assert serial_iface.parity == "even"
        assert serial_iface.stop_bits == 1.5
