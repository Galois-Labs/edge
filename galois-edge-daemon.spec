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

from PyInstaller.utils.hooks import collect_all, copy_metadata

block_cipher = None

# Paths
ROOT = Path(SPECPATH)
SRC = ROOT / "src"
CONTRIB = ROOT / "contrib"
PROFILES = SRC / "galois_edge" / "profiles"

# Collect pyvisa and aiohttp fully (they use lazy/conditional imports)
pyvisa_datas, pyvisa_binaries, pyvisa_hiddenimports = collect_all("pyvisa")
pyvisapy_datas, pyvisapy_binaries, pyvisapy_hiddenimports = collect_all("pyvisa_py")
aiohttp_datas, aiohttp_binaries, aiohttp_hiddenimports = collect_all("aiohttp")

# MCP stack — no upstream PyInstaller hooks for these as of 2026-04.
# Skip mcp.cli during submodule collection: it imports `typer`, which we
# don't ship and which would fail collect_submodules' import probe.
def _skip_mcp_cli(name: str) -> bool:
    return not name.startswith("mcp.cli")


mcp_datas, mcp_binaries, mcp_hiddenimports = collect_all(
    "mcp", filter_submodules=_skip_mcp_cli,
)
starlette_datas, starlette_binaries, starlette_hiddenimports = collect_all("starlette")
sse_datas, sse_binaries, sse_hiddenimports = collect_all("sse_starlette")
pyd_datas, pyd_binaries, pyd_hiddenimports = collect_all("pydantic")

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
    # NOTE: Do NOT add `SRC / "galois_edge"` to pathex. Doing so makes
    # the local subpackage `galois_edge.mcp` visible as bare `mcp`, which
    # shadows the upstream `mcp` SDK and confuses PyInstaller's module
    # graph (it then resolves `from mcp.server.fastmcp import FastMCP`
    # against the empty local subpackage).
    pathex=[str(SRC), str(ROOT)],
    binaries=pyvisa_binaries + pyvisapy_binaries + aiohttp_binaries
        + mcp_binaries + starlette_binaries + sse_binaries + pyd_binaries,
    datas=profile_datas + pyvisa_datas + pyvisapy_datas + aiohttp_datas
        + mcp_datas + starlette_datas + sse_datas + pyd_datas
        + copy_metadata("mcp") + copy_metadata("pydantic"),
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
        # Simulation engine (demo mode)
        "contrib",
        "contrib.simulation",
        "contrib.simulation.engine",
        "contrib.simulation.bench",
        # Optional (included if available, ignored if not)
        "gpib_ctypes",
        "usb",
        "zeroconf",
        "zmq",
        "msgpack",
        "pyudev",
        # MCP server (no upstream hook in pyinstaller-hooks-contrib as of 2026-04)
        "galois_edge.mcp",
        "galois_edge.mcp.server",
        "galois_edge.mcp.context",
        "galois_edge.mcp.schema",
        "galois_edge.mcp.auth",
        "galois_edge.mcp.tools",
        "galois_edge.mcp.tools.discovery",
        "galois_edge.mcp.tools.execute",
        "galois_edge.mcp.tools.sweep",
        "galois_edge.mcp.tools.stream",
        # Phase 2: caller-JWT validation
        "jwt",
        "jwt.algorithms",
        "jwt.api_jwk",
        "mcp.server.fastmcp",
        "mcp.server.streamable_http",
        "mcp.server.streamable_http_manager",
        "mcp.server.sse",
        "mcp.server.stdio",
        "mcp.server.auth.handlers.token",
        "mcp.server.auth.handlers.authorize",
        "mcp.server.auth.middleware.bearer_auth",
        "mcp.server.auth.middleware.client_auth",
        "mcp.server.lowlevel.server",
        "mcp.shared.session",
        "mcp.types",
        # Starlette / sse-starlette (no upstream hooks)
        "sse_starlette.sse",
        # uvicorn[standard] extras (separate packages, NOT uvicorn submodules)
        "uvloop",
        "httptools",
        "httptools.parser",
        "websockets",
        "websockets.legacy",
        "websockets.legacy.server",
        "wsproto",
    ] + pyvisa_hiddenimports + pyvisapy_hiddenimports + aiohttp_hiddenimports
      + mcp_hiddenimports + starlette_hiddenimports + sse_hiddenimports + pyd_hiddenimports,
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
