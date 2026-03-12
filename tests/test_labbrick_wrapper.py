"""
Tests for labbrick_wrapper.py — cross-platform library loading.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "src")
)

from galois_edge.sdk_wrappers.labbrick_wrapper import _load_dll


class TestLoadDll:
    """Test cross-platform _load_dll behaviour."""

    def test_load_dll_resolves_extension_linux(self, monkeypatch):
        """On Linux, _load_dll appends .so to the base name."""
        monkeypatch.setattr("platform.system", lambda: "Linux")
        monkeypatch.delenv("LABBRICK_LIB_PATH", raising=False)

        captured = {}

        def fake_load(path):
            captured["path"] = path
            return MagicMock()

        monkeypatch.setattr("ctypes.cdll.LoadLibrary", fake_load)
        monkeypatch.setattr("ctypes.util.find_library", lambda _: None)

        _load_dll("vnx_fsynth")
        assert captured["path"].endswith(".so")

    def test_load_dll_resolves_extension_windows(self, monkeypatch):
        """On Windows, _load_dll appends .dll to the base name."""
        monkeypatch.setattr("platform.system", lambda: "Windows")
        monkeypatch.delenv("LABBRICK_LIB_PATH", raising=False)

        captured = {}

        def fake_load(path):
            captured["path"] = path
            return MagicMock()

        monkeypatch.setattr("ctypes.cdll.LoadLibrary", fake_load)
        monkeypatch.setattr("ctypes.util.find_library", lambda _: None)

        _load_dll("vnx_fsynth")
        assert captured["path"].endswith(".dll")

    def test_load_dll_resolves_extension_darwin(self, monkeypatch):
        """On macOS (Darwin), _load_dll appends .dylib to the base name."""
        monkeypatch.setattr("platform.system", lambda: "Darwin")
        monkeypatch.delenv("LABBRICK_LIB_PATH", raising=False)

        captured = {}

        def fake_load(path):
            captured["path"] = path
            return MagicMock()

        monkeypatch.setattr("ctypes.cdll.LoadLibrary", fake_load)
        monkeypatch.setattr("ctypes.util.find_library", lambda _: None)

        _load_dll("vnx_fsynth")
        assert captured["path"].endswith(".dylib")

    def test_load_dll_unsupported_platform_raises(self, monkeypatch):
        """On an unsupported platform, _load_dll raises OSError."""
        monkeypatch.setattr("platform.system", lambda: "FreeBSD")

        with pytest.raises(OSError, match="Unsupported platform"):
            _load_dll("vnx_fsynth")

    def test_load_dll_env_var_override(self, monkeypatch):
        """LABBRICK_LIB_PATH env var is used as the library directory."""
        monkeypatch.setattr("platform.system", lambda: "Linux")
        monkeypatch.setenv("LABBRICK_LIB_PATH", "/opt/vaunix")

        captured = {}

        def fake_load(path):
            captured["path"] = path
            return MagicMock()

        monkeypatch.setattr("ctypes.cdll.LoadLibrary", fake_load)
        monkeypatch.setattr("ctypes.util.find_library", lambda _: None)

        _load_dll("vnx_fsynth")
        assert captured["path"].startswith("/opt/vaunix")
        assert captured["path"].endswith(".so")

    def test_load_dll_explicit_path_takes_priority(self, monkeypatch):
        """An explicit dll_path argument bypasses all other resolution."""
        captured = {}

        def fake_load(path):
            captured["path"] = path
            return MagicMock()

        monkeypatch.setattr("ctypes.cdll.LoadLibrary", fake_load)

        _load_dll("vnx_fsynth", dll_path="/custom/path/lib.so")
        assert captured["path"] == "/custom/path/lib.so"

    def test_load_dll_failure_message_includes_platform(self, monkeypatch):
        """When loading fails, the error message includes vaunix.com."""
        monkeypatch.setattr("platform.system", lambda: "Linux")
        monkeypatch.delenv("LABBRICK_LIB_PATH", raising=False)
        monkeypatch.setattr("ctypes.util.find_library", lambda _: None)

        def fail_load(path):
            raise OSError(f"cannot open shared object: {path}")

        monkeypatch.setattr("ctypes.cdll.LoadLibrary", fail_load)

        with pytest.raises(OSError, match="vaunix.com"):
            _load_dll("vnx_fsynth")
