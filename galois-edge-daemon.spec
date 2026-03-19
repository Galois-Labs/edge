# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for the galois-edge Python instrument engine.

Build with:
    pyinstaller galois-edge-daemon.spec

Produces a single-file binary: dist/galois-edge-daemon (Linux/macOS)
or dist/galois-edge-daemon.exe (Windows).
"""

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

block_cipher = None

# Paths
ROOT = Path(SPECPATH)
SRC = ROOT / "src"
PROFILES = SRC / "galois_edge" / "profiles"

# Collect pyvisa and aiohttp fully (they use lazy/conditional imports)
pyvisa_datas, pyvisa_binaries, pyvisa_hiddenimports = collect_all("pyvisa")
pyvisapy_datas, pyvisapy_binaries, pyvisapy_hiddenimports = collect_all("pyvisa_py")
aiohttp_datas, aiohttp_binaries, aiohttp_hiddenimports = collect_all("aiohttp")

# Collect YAML instrument profiles as data files (recurse into subdirs)
profile_datas = []
if PROFILES.is_dir():
    for yaml_file in PROFILES.rglob("*.yaml"):
        rel_dir = yaml_file.parent.relative_to(SRC)
        profile_datas.append((str(yaml_file), str(rel_dir)))
    for yml_file in PROFILES.rglob("*.yml"):
        rel_dir = yml_file.parent.relative_to(SRC)
        profile_datas.append((str(yml_file), str(rel_dir)))

a = Analysis(
    [str(SRC / "galois_edge" / "__main__.py")],
    pathex=[str(SRC), str(SRC / "galois_edge")],
    binaries=pyvisa_binaries + pyvisapy_binaries + aiohttp_binaries,
    datas=profile_datas + pyvisa_datas + pyvisapy_datas + aiohttp_datas,
    hiddenimports=[
        # gRPC / protobuf stubs
        "galois_edge.edge_pb2",
        "galois_edge.edge_pb2_grpc",
        "edge",
        "edge.v1",
        "edge.v1.edge_pb2",
        "edge.v1.edge_pb2_grpc",
        "galois_edge.edge",
        "galois_edge.edge.v1",
        "galois_edge.edge.v1.edge_pb2",
        "galois_edge.edge.v1.edge_pb2_grpc",
        "grpc",
        "grpc._cython",
        "grpc._cython._cygrpc",
        # Core deps
        "pyvisa",
        "pyvisa.resources",
        "pyvisa.constants",
        "pyvisa_py",
        "pyvisa_py.tcpip",
        "pyvisa_py.highlevel",
        "aiohttp",
        "aiohttp.web",
        "aiohttp.web_runner",
        "aiohttp.web_app",
        "aiohttp.web_request",
        "aiohttp.web_response",
        "aiohttp.web_ws",
        "multidict",
        "yarl",
        "aiosignal",
        "frozenlist",
        "yaml",
        # Vendored SDK drivers
        "galois_edge.vendor.dps150",
        "galois_edge.vendor.dps150.device",
        "galois_edge.vendor.dps150.protocol",
        "galois_edge.vendor.dps150.transport",
        "galois_edge.vendor.dps150.discovery",
        "galois_edge.vendor.dps150.types",
        "galois_edge.vendor.dps150.exceptions",
        # SDK wrappers (dynamically imported by SDKExecutor)
        "galois_edge.sdk_wrappers.dps150_wrapper",
        "galois_edge.sdk_wrappers.digilent_dwf_wrapper",
        "serial",
        "serial.tools",
        "serial.tools.list_ports",
        # Digilent WaveForms (optional — requires libdwf.so)
        "dwfpy",
        "dwfpy.bindings",
        "dwfpy.device",
        "dwfpy.analog_input",
        "dwfpy.analog_output",
        "dwfpy.analog_io",
        "dwfpy.digital_io",
        "dwfpy.digital_input",
        "dwfpy.digital_output",
        "dwfpy.constants",
        "dwfpy.protocols",
        # Optional (included if available, ignored if not)
        "gpib_ctypes",
        "usb",
        "zeroconf",
        "zmq",
        "msgpack",
        "pyudev",
    ] + pyvisa_hiddenimports + pyvisapy_hiddenimports + aiohttp_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Not needed at runtime — save space
        "tkinter",
        "unittest",
        "xmlrpc",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="galois-edge-daemon",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
