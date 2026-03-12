"""Tests for the SignalHound SA124B vendored ctypes binding and wrapper.

Verifies:
- The vendor module ``galois_edge.vendor.sa_api`` imports without error
- Constants are defined with expected values
- ``SignalHoundClient`` can be instantiated without connecting
- A descriptive error message is raised when the C library is missing
"""

from __future__ import annotations

import ctypes
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Vendor module import tests
# ---------------------------------------------------------------------------


class TestSaApiVendorModule:
    """Verify vendor module imports cleanly and exposes expected symbols."""

    def test_import_succeeds(self):
        """The vendor module should import without error (no library loaded)."""
        from galois_edge.vendor import sa_api

        assert sa_api is not None

    def test_constants_defined(self):
        """Core constants used by the wrapper must be present."""
        from galois_edge.vendor import sa_api

        assert hasattr(sa_api, "SA_SWEEPING")
        assert hasattr(sa_api, "SA_FALSE")
        assert hasattr(sa_api, "SA_TRUE")
        assert hasattr(sa_api, "SA_OK")

    def test_constant_values(self):
        """Constants must have the expected integer values."""
        from galois_edge.vendor import sa_api

        assert sa_api.SA_SWEEPING == 0x0
        assert sa_api.SA_FALSE == 0
        assert sa_api.SA_TRUE == 1
        assert sa_api.SA_OK == 0

    def test_public_functions_exist(self):
        """All functions used by the wrapper must be importable."""
        from galois_edge.vendor import sa_api

        for fn_name in (
            "sa_open_device",
            "sa_close_device",
            "sa_config_center_span",
            "sa_config_level",
            "sa_config_sweep_coupling",
            "sa_initiate",
            "sa_get_sweep_64f",
            "sa_get_serial_number",
            "sa_get_firmware_version",
        ):
            assert callable(getattr(sa_api, fn_name)), (
                f"sa_api.{fn_name} should be callable"
            )


# ---------------------------------------------------------------------------
# Library loading error tests
# ---------------------------------------------------------------------------


class TestSaApiLoadErrors:
    """Verify descriptive errors when the C shared library is missing."""

    def test_load_lib_error_includes_download_link(self, monkeypatch):
        """OSError message must include the signalhound.com download URL."""
        from galois_edge.vendor import sa_api

        # Reset the cached library so _load_lib() actually tries to load
        monkeypatch.setattr(sa_api, "_lib", None)
        monkeypatch.delenv("SIGNALHOUND_LIB_PATH", raising=False)
        monkeypatch.setattr("ctypes.util.find_library", lambda _: None)

        # Make CDLL raise for any path
        def fail_load(name):
            raise OSError(f"cannot open shared object: {name}")

        monkeypatch.setattr(ctypes, "CDLL", fail_load)

        with pytest.raises(OSError, match="signalhound.com/software"):
            sa_api._load_lib()

    def test_load_lib_error_lists_library_names(self, monkeypatch):
        """OSError message should list the library names that were tried."""
        from galois_edge.vendor import sa_api

        monkeypatch.setattr(sa_api, "_lib", None)
        monkeypatch.delenv("SIGNALHOUND_LIB_PATH", raising=False)
        monkeypatch.setattr("ctypes.util.find_library", lambda _: None)

        def fail_load(name):
            raise OSError(f"cannot open: {name}")

        monkeypatch.setattr(ctypes, "CDLL", fail_load)

        with pytest.raises(OSError, match="sa_api"):
            sa_api._load_lib()

    def test_load_lib_unsupported_platform(self, monkeypatch):
        """Unsupported platforms should raise OSError immediately."""
        from galois_edge.vendor import sa_api

        monkeypatch.setattr(sa_api, "_lib", None)
        monkeypatch.delenv("SIGNALHOUND_LIB_PATH", raising=False)
        monkeypatch.setattr("platform.system", lambda: "FreeBSD")

        with pytest.raises(OSError, match="Unsupported platform"):
            sa_api._load_lib()

    def test_env_var_override_file(self, monkeypatch, tmp_path):
        """SIGNALHOUND_LIB_PATH pointing to a file should be tried first."""
        from galois_edge.vendor import sa_api

        monkeypatch.setattr(sa_api, "_lib", None)

        fake_lib = tmp_path / "libsa_api.so"
        fake_lib.touch()

        monkeypatch.setenv("SIGNALHOUND_LIB_PATH", str(fake_lib))

        captured = {}

        original_cdll = ctypes.CDLL

        def tracking_cdll(name, *args, **kwargs):
            captured["path"] = name
            return MagicMock()

        monkeypatch.setattr(ctypes, "CDLL", tracking_cdll)

        sa_api._load_lib()
        assert captured["path"] == str(fake_lib)

    def test_env_var_override_directory(self, monkeypatch, tmp_path):
        """SIGNALHOUND_LIB_PATH pointing to a directory should prepend it."""
        from galois_edge.vendor import sa_api

        monkeypatch.setattr(sa_api, "_lib", None)
        monkeypatch.setenv("SIGNALHOUND_LIB_PATH", str(tmp_path))
        monkeypatch.setattr("ctypes.util.find_library", lambda _: None)
        monkeypatch.setattr("platform.system", lambda: "Linux")

        captured_paths = []

        def tracking_cdll(name, *args, **kwargs):
            captured_paths.append(name)
            if str(tmp_path) in name:
                return MagicMock()
            raise OSError(f"cannot open: {name}")

        monkeypatch.setattr(ctypes, "CDLL", tracking_cdll)

        sa_api._load_lib()
        # The first attempted path should be inside the env directory
        assert any(str(tmp_path) in p for p in captured_paths)


# ---------------------------------------------------------------------------
# SignalHoundClient instantiation tests
# ---------------------------------------------------------------------------


class TestSignalHoundClient:
    """Verify that SignalHoundClient can be created without connecting."""

    def test_instantiation_no_connect(self):
        """Creating a SignalHoundClient must not require the C library."""
        from galois_edge.sdk_wrappers.signalhound_wrapper import SignalHoundClient

        client = SignalHoundClient()
        assert client is not None
        assert client._device is None

    def test_identity_without_connect(self):
        """get_identity() should return a fallback string when not connected."""
        from galois_edge.sdk_wrappers.signalhound_wrapper import SignalHoundClient

        client = SignalHoundClient()
        identity = client.get_identity()
        assert "SignalHound" in identity
        assert "SA124B" in identity

    def test_status_disconnected(self):
        """get_status() should report 'disconnected' before connect()."""
        from galois_edge.sdk_wrappers.signalhound_wrapper import SignalHoundClient

        client = SignalHoundClient()
        assert client.get_status() == "disconnected"

    def test_connect_uses_vendor_module(self, monkeypatch):
        """connect() should import from galois_edge.vendor.sa_api."""
        from galois_edge.sdk_wrappers.signalhound_wrapper import SignalHoundClient
        import galois_edge.vendor as _vendor_pkg

        mock_sa = MagicMock()
        mock_sa.sa_open_device.return_value = 42

        monkeypatch.setattr(_vendor_pkg, "sa_api", mock_sa)
        monkeypatch.setitem(
            __import__("sys").modules, "galois_edge.vendor.sa_api", mock_sa
        )

        client = SignalHoundClient()
        client.connect()

        mock_sa.sa_open_device.assert_called_once()
        assert client._device == 42

    def test_disconnect_calls_close(self, monkeypatch):
        """disconnect() should call sa_close_device on the handle."""
        from galois_edge.sdk_wrappers.signalhound_wrapper import SignalHoundClient
        import galois_edge.vendor as _vendor_pkg

        mock_sa = MagicMock()
        mock_sa.sa_open_device.return_value = 42

        monkeypatch.setattr(_vendor_pkg, "sa_api", mock_sa)
        monkeypatch.setitem(
            __import__("sys").modules, "galois_edge.vendor.sa_api", mock_sa
        )

        client = SignalHoundClient()
        client.connect()
        client.disconnect()

        mock_sa.sa_close_device.assert_called_once_with(42)
        assert client._device is None

    def test_sweep_raises_when_not_connected(self):
        """sweep() should raise RuntimeError if not connected."""
        from galois_edge.sdk_wrappers.signalhound_wrapper import SignalHoundClient

        client = SignalHoundClient()
        with pytest.raises(RuntimeError, match="not connected"):
            client.sweep()
