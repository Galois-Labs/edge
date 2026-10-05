"""edge-api.md §1, §2: main.py builds the InstrumentManager with its extra backends."""
from __future__ import annotations

import pathlib

import pytest

from galois_edge.backends.demo import DemoBackend
from galois_edge.capability_manager import CapabilityManager
from galois_edge.config import Config
from galois_edge.main import EdgeDaemon
from galois_edge.profile_loader import ProfileLoader
from galois_edge.profile_schema import IdentityConfig, InstrumentMetadata, InstrumentProfile
from tests.test_instrument_manager_backends import FakeBackend

pytestmark = pytest.mark.critical
LASER = "TCPIP::192.168.1.10::5025::SOCKET"


@pytest.fixture
def make_daemon(tmp_path, monkeypatch):
    # Hermetic and fast (spec §10): no PyVISA or raw-USB enumeration, so list_resources() never
    # probes host devices or broadcasts VXI-11 discovery on the network.
    monkeypatch.setattr("galois_edge.instrument_manager.PYVISA_AVAILABLE", False)
    monkeypatch.setattr("galois_edge.instrument_manager.USB_AVAILABLE", False)
    made = []

    def make(**overrides):
        base = dict(gpib_enabled=False, usb_monitor_enabled=False, lan_instruments="", include_serial_ports=False,
                    profile_dir=str(tmp_path / "bundled"), dynamic_profile_dir=str(tmp_path / "dynamic"),
                    driver_profile_dir=str(tmp_path / "drivers"), mcp_enabled=False, demo=False,
                    sim_mode=False, visa_backend="", trace_dir="")
        base.update(overrides)
        daemon = EdgeDaemon(Config(**base))
        made.append(daemon)
        return daemon

    yield make
    for daemon in made:
        if daemon._instrument_manager is not None:
            daemon._instrument_manager.close_backends()
        daemon._io_executor.shutdown(wait=False)


def test_demo_proxy_is_gone():
    import galois_edge.main as m
    assert not hasattr(m, "_DemoInstrumentManagerProxy")


@pytest.mark.parametrize("cfg_value,expected", [("", "@py"), ("@sim", "@sim")])
def test_visa_backend_is_plumbed(make_daemon, monkeypatch, cfg_value, expected):
    seen = {}
    monkeypatch.setattr("galois_edge.main.InstrumentManager", lambda **kw: seen.update(kw) or object())
    make_daemon(visa_backend=cfg_value)._build_instrument_manager()
    assert seen["visa_backend"] == expected and seen["extra_backends"] == []


def test_demo_mode_registers_demo_backend(make_daemon):
    daemon = make_daemon(demo=True)
    mgr = daemon._build_instrument_manager()
    assert len(mgr.extra_backends) == 1 and isinstance(mgr.extra_backends[0], DemoBackend)
    assert daemon._demo_backend is mgr.extra_backends[0]
    assert LASER in mgr.list_resources() and mgr.backend_for(LASER) is daemon._demo_backend


def test_demo_mode_without_contrib_warns_and_continues(make_daemon, monkeypatch, caplog):
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name.startswith("contrib.simulation"):
            raise ImportError("no contrib")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    mgr = make_daemon(demo=True)._build_instrument_manager()
    assert mgr.extra_backends == ()
    assert "contrib.simulation not available" in caplog.text


def test_sim_mode_without_a_usable_bench_exits_2(make_daemon, monkeypatch):
    monkeypatch.delenv("SIM_BENCH", raising=False)
    monkeypatch.delenv("SIM_REMOTE_SOCKET", raising=False)
    with pytest.raises(SystemExit) as ei:
        make_daemon(sim_mode=True)._build_instrument_manager()
    assert ei.value.code == 2


async def test_register_demo_instruments_uses_hints_authoritatively(make_daemon):
    daemon = make_daemon(demo=True)
    daemon._instrument_manager = daemon._build_instrument_manager()
    daemon._capability_manager = CapabilityManager()
    loader = ProfileLoader(daemon._cfg.profile_dir)
    loader._loaded = True
    loader.add_profile(InstrumentProfile(
        instrument=InstrumentMetadata(manufacturer="Quantifi Photonics", model="LASER 1000"),
        identity=IdentityConfig(patterns=[".*"])))
    daemon._profile_loader = loader
    await daemon._register_demo_instruments()
    caps = daemon._capability_manager.all_instruments
    assert caps[LASER].profile_key == "quantifi_photonics_laser_1000"
    assert caps["TCPIP::192.168.1.11::5025::SOCKET"].profile is None   # override authoritative, profile absent


class HintingBackend(FakeBackend):
    def __init__(self, addrs, hint, dirs=()):
        super().__init__(addrs)
        self.hint, self.dirs = hint, tuple(dirs)

    def identify(self, address): return "GENERIC,WHATEVER"
    def profile_hint(self, address): return self.hint
    def profile_dirs(self): return self.dirs


def _yaml(directory, model, pattern):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{model}.yaml").write_text(
        f"instrument: {{manufacturer: ACME, model: {model}}}\nidentity: {{patterns: ['{pattern}']}}\n"
        "commands:\n  idn: {scpi: '*IDN?', type: query}\n")


def _wire(daemon, backend):
    from galois_edge.instrument_manager import InstrumentManager
    daemon._instrument_manager = InstrumentManager(gpib_enabled=False, usb_raw_enabled=False, extra_backends=[backend])
    daemon._capability_manager = CapabilityManager()


def test_load_profiles_scans_backend_dirs(make_daemon, tmp_path):
    daemon = make_daemon()
    _yaml(tmp_path / "simprofiles", "SIMX", "NEVER")
    _wire(daemon, HintingBackend(["SIM::1"], None, dirs=[str(tmp_path / "simprofiles")]))
    daemon._load_profiles()
    assert [str(p) for p in daemon._profile_loader.extra_dirs] == [str(tmp_path / "simprofiles")]
    assert daemon._profile_loader.get_profile("acme_simx") is not None


@pytest.mark.parametrize("hint,expected", [("acme_hinted", "acme_hinted"),   # hint wins over a generic regex
                                           ("acme_missing", "acme_generic"),  # unknown hint -> regex fallback
                                           (None, "acme_generic")])
def test_try_match_profile_uses_the_backend_hint_first(make_daemon, tmp_path, hint, expected):
    daemon = make_daemon()
    _yaml(pathlib.Path(daemon._cfg.profile_dir), "GENERIC", ".*")
    _yaml(pathlib.Path(daemon._cfg.profile_dir), "HINTED", "NEVER-MATCHES")
    _wire(daemon, HintingBackend(["SIM::1"], hint))
    daemon._load_profiles()
    daemon._try_match_profile("SIM::1")
    assert daemon._capability_manager.get_instrument_caps("SIM::1").profile_key == expected


def test_main_no_longer_touches_private_gpib():
    text = pathlib.Path(__import__("galois_edge.main").main.__file__).read_text()
    assert "._gpib" not in text
