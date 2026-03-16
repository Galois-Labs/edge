"""Tests for SDK wrapper import-error messages and module imports.

Verifies that Keysight PXI and NI DAQ wrappers raise descriptive
ImportError / RuntimeError messages when vendor SDKs are missing,
and that every SDK wrapper module imports cleanly and can be
instantiated without calling connect().
"""

from __future__ import annotations

import builtins
import importlib
import sys
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_real_import = builtins.__import__


def _block_import(name_to_block: str):
    """Return a side_effect function for monkeypatching __import__."""

    def _guarded_import(name, *args, **kwargs):
        if name == name_to_block:
            raise ImportError(f"No module named '{name_to_block}'")
        return _real_import(name, *args, **kwargs)

    return _guarded_import


# ---------------------------------------------------------------------------
# Keysight PXI tests
# ---------------------------------------------------------------------------


class TestKeysightPxiImportErrors:
    """All three Keysight PXI classes should raise clear ImportErrors."""

    def test_keysight_awg_import_error_message(self, monkeypatch):
        from galois_edge.sdk_wrappers.keysight_pxi_wrapper import KeysightPxiAwg

        monkeypatch.setattr(builtins, "__import__", _block_import("keysightSD1"))
        awg = KeysightPxiAwg(slot=0, chassis=0)

        with pytest.raises(ImportError) as exc_info:
            awg.connect()

        msg = str(exc_info.value)
        assert "Windows-only" in msg
        assert "keysight.com" in msg

    def test_keysight_digitizer_import_error_message(self, monkeypatch):
        from galois_edge.sdk_wrappers.keysight_pxi_wrapper import KeysightPxiDigitizer

        monkeypatch.setattr(builtins, "__import__", _block_import("keysightSD1"))
        dig = KeysightPxiDigitizer(slot=0, chassis=0)

        with pytest.raises(ImportError) as exc_info:
            dig.connect()

        msg = str(exc_info.value)
        assert "Windows-only" in msg
        assert "keysight.com" in msg

    def test_keysight_hvi_import_error_message(self, monkeypatch):
        from galois_edge.sdk_wrappers.keysight_pxi_wrapper import KeysightPxiHvi

        monkeypatch.setattr(builtins, "__import__", _block_import("keysightSD1"))
        hvi = KeysightPxiHvi(hvi_file="test.hvi")

        with pytest.raises(ImportError) as exc_info:
            hvi.connect()

        msg = str(exc_info.value)
        assert "Windows-only" in msg
        assert "keysight.com" in msg


# ---------------------------------------------------------------------------
# NI DAQ tests
# ---------------------------------------------------------------------------


class TestNiDaqImportErrors:
    """NI DAQ wrapper should raise descriptive errors with platform hints."""

    def test_ni_daq_import_error_message(self, monkeypatch):
        from galois_edge.sdk_wrappers.ni_daq_wrapper import NiDaqClient

        monkeypatch.setattr(builtins, "__import__", _block_import("nidaqmx.system"))
        client = NiDaqClient(device_name="Dev1")

        with pytest.raises(ImportError) as exc_info:
            client.connect()

        msg = str(exc_info.value)
        assert "pip install nidaqmx" in msg

    def test_ni_daq_linux_usb_hint(self, monkeypatch):
        from galois_edge.sdk_wrappers.ni_daq_wrapper import NiDaqClient

        # Make the nidaqmx.system import succeed but System.local() fail
        fake_nidaqmx_system = type(sys)("nidaqmx.system")
        fake_nidaqmx = type(sys)("nidaqmx")

        class FakeSystem:
            @staticmethod
            def local():
                raise RuntimeError("device not found")

        fake_nidaqmx_system.System = FakeSystem
        fake_nidaqmx.system = fake_nidaqmx_system

        monkeypatch.setitem(sys.modules, "nidaqmx", fake_nidaqmx)
        monkeypatch.setitem(sys.modules, "nidaqmx.system", fake_nidaqmx_system)

        # Patch platform.system to return "Linux"
        monkeypatch.setattr("platform.system", lambda: "Linux")

        client = NiDaqClient(device_name="Dev1")

        with pytest.raises(RuntimeError) as exc_info:
            client.connect()

        msg = str(exc_info.value)
        assert "USB DAQ devices on Linux" in msg

    def test_ni_daq_windows_no_usb_hint(self, monkeypatch):
        from galois_edge.sdk_wrappers.ni_daq_wrapper import NiDaqClient

        # Make the nidaqmx.system import succeed but System.local() fail
        fake_nidaqmx_system = type(sys)("nidaqmx.system")
        fake_nidaqmx = type(sys)("nidaqmx")

        class FakeSystem:
            @staticmethod
            def local():
                raise RuntimeError("device not found")

        fake_nidaqmx_system.System = FakeSystem
        fake_nidaqmx.system = fake_nidaqmx_system

        monkeypatch.setitem(sys.modules, "nidaqmx", fake_nidaqmx)
        monkeypatch.setitem(sys.modules, "nidaqmx.system", fake_nidaqmx_system)

        # Patch platform.system to return "Windows"
        monkeypatch.setattr("platform.system", lambda: "Windows")

        client = NiDaqClient(device_name="Dev1")

        with pytest.raises(RuntimeError) as exc_info:
            client.connect()

        msg = str(exc_info.value)
        assert "USB DAQ devices on Linux" not in msg


# ---------------------------------------------------------------------------
# Wrapper module import & instantiation tests
# ---------------------------------------------------------------------------

WRAPPER_MODULES = [
    ("galois_edge.sdk_wrappers.bluefors_wrapper", "BlueForsClient"),
    ("galois_edge.sdk_wrappers.ocean_optics_wrapper", "OceanOpticsSpectrometer"),
    ("galois_edge.sdk_wrappers.ni_daq_wrapper", "NiDaqClient"),
    ("galois_edge.sdk_wrappers.keysight_pxi_wrapper", "KeysightPxiAwg"),
    ("galois_edge.sdk_wrappers.labbrick_wrapper", "LabBrickSynthesizer"),
    ("galois_edge.sdk_wrappers.alazartech_wrapper", "AlazarTechClient"),
    ("galois_edge.sdk_wrappers.acqiris_wrapper", "AcqirisClient"),
    ("galois_edge.sdk_wrappers.aeroflex_wrapper", "Aeroflex302xClient"),
    ("galois_edge.sdk_wrappers.signalhound_wrapper", "SignalHoundClient"),
    ("galois_edge.sdk_wrappers.minicircuits_wrapper", "MiniCircuitsClient"),
    ("galois_edge.sdk_wrappers.muswitch_wrapper", "MuSwitchClient"),
    ("galois_edge.sdk_wrappers.oxford_ilm_wrapper", "OxfordILMClient"),
    ("galois_edge.sdk_wrappers.oxford_mercury_wrapper", "OxfordMercuryIPSClient"),
    ("galois_edge.sdk_wrappers.oxford_serial_wrapper", "OxfordSerialClient"),
    ("galois_edge.sdk_wrappers.leiden_wrapper", "LeidenClient"),
    ("galois_edge.sdk_wrappers.qdac_wrapper", "QDACClient"),
    ("galois_edge.sdk_wrappers.ni_scope_wrapper", "NiScopeClient"),
    ("galois_edge.sdk_wrappers.ni_fgen_wrapper", "NiFgenClient"),
    ("galois_edge.sdk_wrappers.ni_dcpower_wrapper", "NiDCPowerClient"),
    ("galois_edge.sdk_wrappers.ni_dmm_wrapper", "NiDmmClient"),
]


def _discover_ppms():
    """Include ppms_wrapper if it exists (another agent may have created it)."""
    try:
        importlib.import_module("galois_edge.sdk_wrappers.ppms_wrapper")
        return [("galois_edge.sdk_wrappers.ppms_wrapper", "PPMSClient")]
    except (ImportError, ModuleNotFoundError):
        return []


WRAPPER_MODULES += _discover_ppms()


# ---------------------------------------------------------------------------
# NI PXI tests (niscope, nifgen, nidcpower, nidmm)
# ---------------------------------------------------------------------------


class TestNiPxiImportErrors:
    """NI PXI wrappers should raise descriptive ImportErrors when SDK missing."""

    def test_niscope_import_error_message(self, monkeypatch):
        from galois_edge.sdk_wrappers.ni_scope_wrapper import NiScopeClient

        monkeypatch.setattr(builtins, "__import__", _block_import("niscope"))
        client = NiScopeClient(resource="PXI1Slot2")

        with pytest.raises(ImportError) as exc_info:
            client.connect()

        msg = str(exc_info.value)
        assert "pip install niscope" in msg

    def test_nifgen_import_error_message(self, monkeypatch):
        from galois_edge.sdk_wrappers.ni_fgen_wrapper import NiFgenClient

        monkeypatch.setattr(builtins, "__import__", _block_import("nifgen"))
        client = NiFgenClient(resource="PXI1Slot3")

        with pytest.raises(ImportError) as exc_info:
            client.connect()

        msg = str(exc_info.value)
        assert "pip install nifgen" in msg

    def test_nidcpower_import_error_message(self, monkeypatch):
        from galois_edge.sdk_wrappers.ni_dcpower_wrapper import NiDCPowerClient

        monkeypatch.setattr(builtins, "__import__", _block_import("nidcpower"))
        client = NiDCPowerClient(resource="PXI1Slot4")

        with pytest.raises(ImportError) as exc_info:
            client.connect()

        msg = str(exc_info.value)
        assert "pip install nidcpower" in msg

    def test_nidmm_import_error_message(self, monkeypatch):
        from galois_edge.sdk_wrappers.ni_dmm_wrapper import NiDmmClient

        monkeypatch.setattr(builtins, "__import__", _block_import("nidmm"))
        client = NiDmmClient(resource="PXI1Slot5")

        with pytest.raises(ImportError) as exc_info:
            client.connect()

        msg = str(exc_info.value)
        assert "pip install nidmm" in msg


# ---------------------------------------------------------------------------
# Wrapper module import & instantiation tests
# ---------------------------------------------------------------------------


class TestWrapperModuleImports:
    """Verify every SDK wrapper module imports cleanly and its primary
    class can be instantiated without calling connect()."""

    @pytest.mark.parametrize(
        "module_path, class_name",
        WRAPPER_MODULES,
        ids=[m.rsplit(".", 1)[-1] for m, _ in WRAPPER_MODULES],
    )
    def test_import_and_instantiate(self, module_path: str, class_name: str):
        # 1. Module imports without errors
        module = importlib.import_module(module_path)

        # 2. Expected class exists in the module
        assert hasattr(module, class_name), (
            f"{module_path} has no attribute '{class_name}'"
        )
        cls = getattr(module, class_name)

        # 3. Class can be instantiated with default args (no connect())
        instance = cls()
        assert instance is not None
