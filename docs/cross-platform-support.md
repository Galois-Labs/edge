# Cross-Platform Instrument Support Analysis

This document covers every non-SCPI instrument in the daemon's profile set — what they need to run, what works on Linux vs Windows, and what to do about the gaps.

---

## Tier-by-tier breakdown

### Tier 1: Plug and play (stdlib only)

| Instrument | Interface | Dependency | Linux | Windows | Notes |
|---|---|---|---|---|---|
| BlueFors Logging | Ethernet | Internal wrapper (reads log files over network) | Yes | Yes | Pure Python, no vendor SDK |

**What to build:** The `bluefors_wrapper` in `sdk_wrappers/` just needs to open a TCP socket or read files from a network share. No platform concerns.

---

### Tier 2: pip install

| Instrument | Interface | PyPI Package | Linux | Windows | Constraint |
|---|---|---|---|---|---|
| Ocean Optics spectrometers | USB | `seabreeze` | Yes | Yes | Linux needs udev rules for non-root USB access |
| NI DAQ (generic PCI/PCIe) | PCI/PCIe | `nidaqmx` | Yes | Yes | Requires NI-DAQmx runtime driver installed on host |
| NI USB-6218 | USB | `nidaqmx` | **No** | Yes | NI does not support USB DAQs on Linux |
| QD PPMS DynaCool | Ethernet (port 5000) | `MultiPyVu` | Client only | Full | Server must run on Windows alongside MultiVu GUI |
| SignalHound SA124B | USB | None official | Yes (.so) | Yes (.dll) | Manual binary download; write ctypes wrapper |
| Zurich HDAWG | Ethernet/USB | `zhinst` | Yes | Yes | Requires LabOne data server running somewhere on network |
| Zurich MFLI | Ethernet/USB | `zhinst` | Yes | Yes | Same |
| Zurich UHF | Ethernet/USB/GPIB | `zhinst` | Yes | Yes | Same |

**Key issue: NI USB-6218.** NI explicitly does not support USB-based DAQmx devices on Linux. Options:
1. Run a Windows machine with NI-DAQmx and proxy via NI's gRPC device server (`ni/grpc-device`)
2. Use USB passthrough into a Windows VM
3. Accept Windows-only for this device

**Key issue: QD PPMS.** The `MultiPyVu.Server` requires `pywin32` and the MultiVu Windows application. The daemon can run `MultiPyVu.Client` from Linux to connect to a Windows server on the network — this is the intended usage pattern.

**Key issue: SignalHound.** No maintained PyPI package. The vendor provides platform-specific C libraries (`bb_api.dll` / `bb_api.so`). Write a ctypes wrapper in `sdk_wrappers/signalhound_wrapper.py` that loads the right binary for the platform.

**Zurich Instruments note:** The profiles currently list these as SCPI-only, but the `zhinst` Python API (cross-platform, on PyPI) would give better control. Worth considering as an alternative to raw SCPI for these instruments.

---

### Tier 3: Vendor SDK required

| Instrument | Interface | PyPI Package | Linux | Windows | Constraint |
|---|---|---|---|---|---|
| QDevil QDAC | Serial (460800 baud) | `qcodes_contrib_drivers` or SCPI via pyvisa | Yes | Yes | QDAC-II uses SCPI over Ethernet/USB; QDAC-I uses serial |

**No real issues here.** The QDAC-I talks serial (pyserial is fully cross-platform). The QDAC-II talks SCPI over VISA. Either path works on both OSes without vendor DLLs.

---

### Tier 4: Vendor DLL (Windows-centric)

| Instrument | Interface | Vendor Binary | Linux | Windows | Constraint |
|---|---|---|---|---|---|
| LabBrick LMS synthesizer | USB | `vnx_fsynth.dll` / `.so` / `.dylib` | Yes | Yes | Vaunix provides platform-specific binaries |
| LabBrick LSG signal generator | USB | `vnx_fsynth.dll` / `.so` / `.dylib` | Yes | Yes | Same binary as LMS |
| Vaunix attenuator | USB | `vnx_atten.dll` / `.so` | Yes | Yes | Vaunix provides Linux `.so`; no PyPI package for attenuators |

**Better than the table suggests.** Vaunix actually provides Linux `.so` and macOS `.dylib` binaries alongside the Windows DLLs. The instruments use USB HID, which is inherently cross-platform. The wrapper needs to:

1. Detect platform at runtime
2. Load the correct binary (`ctypes.CDLL("vnx_fsynth.dll")` on Windows, `ctypes.CDLL("libvnx_fsynth.so")` on Linux)
3. Map the C function signatures (same API across platforms)

The `vaunix-api` package on PyPI covers signal generators only. For synthesizers and attenuators, write a ctypes wrapper in `sdk_wrappers/labbrick_wrapper.py` with platform-aware library loading:

```python
import ctypes, sys

if sys.platform == "win32":
    _lib = ctypes.CDLL("vnx_fsynth")
elif sys.platform == "linux":
    _lib = ctypes.CDLL("libvnx_fsynth.so")
elif sys.platform == "darwin":
    _lib = ctypes.CDLL("libvnx_fsynth.dylib")
```

---

### Tier 5: Vendor SDK + PXI hardware

| Instrument | Interface | Vendor SDK | Linux | Windows | Constraint |
|---|---|---|---|---|---|
| Keysight PXI AWG (M3201A/M3202A) | PXI | `keysightSD1` | **No** | Yes | Windows-only; not on PyPI; 1GB+ vendor installer |
| Keysight PXI Digitizer (M3100A/M3102A) | PXI | `keysightSD1` | **No** | Yes | Same |
| Keysight PXI HVI Trigger | PXI | `keysightSD1` | **No** | Yes | Same |
| Signadyne AWG | PXI | `keysightSD1` | **No** | Yes | Signadyne was acquired by Keysight; same SDK |
| Signadyne Digitizer | PXI | `keysightSD1` | **No** | Yes | Same |

**Hard Windows lock.** The `keysightSD1` module is:
- Not on PyPI
- Distributed only through a Windows installer
- A ctypes wrapper around `SD1core.dll` (no Linux `.so` exists)
- Requires a PXI chassis with the physical cards installed

**Options for Linux support:**
1. **Run the daemon on Windows** for labs with PXI chassis — this is the realistic path
2. **Split architecture:** Run a lightweight Windows-only daemon instance on the PXI controller that exposes these instruments over gRPC, and have the main Linux daemon proxy to it
3. **Accept Windows-only** for these 5 instruments

---

### Tier 6: Vendor SDK + PXI hardware (NI RF)

| Instrument | Interface | Vendor SDK | Linux | Windows | Constraint |
|---|---|---|---|---|---|
| Aeroflex 302x (NI-RFSG) | PXI | `nirfsg` | Partial | Yes | PyPI package exists; Linux desktop support added in 2023 |
| Aeroflex 303x (NI-RFSA) | PXI | `nirfsa` (no PyPI) | Partial | Yes | No Python package on PyPI; C API only |

**Better than Keysight PXI, worse than standard NI.** NI-RFSG has a PyPI package (`nirfsg`) and Linux support (both Linux RT on PXI controllers and desktop x86_64). NI-RFSA has no Python package on PyPI yet (open issue [nimi-python#984](https://github.com/ni/nimi-python/issues/984)).

**Options:**
1. Use `nirfsg` from PyPI for the signal generator side (works on both OSes)
2. For the analyzer (RFSA): use the C API via ctypes, or use NI's gRPC device server (`ni/grpc-device`) which exposes RFSA over gRPC from the PXI controller
3. Wait for NI to publish `nirfsa` on PyPI

---

### Tier 7: C SDK + ctypes

| Instrument | Interface | Vendor SDK | Linux | Windows | Constraint |
|---|---|---|---|---|---|
| AlazarTech digitizer (ATS9870 etc.) | PCIe | `atsapi` (bundled in ATS-SDK) | Yes | Yes | Must install ATS-SDK from vendor; vendor `atsapi.py` into project |
| Acqiris U1084A | PCIe | IVI-C driver (`AgMD1Fundamental`) | Yes | Yes | No Python wrapper exists; must write ctypes wrapper from C headers |

**Both work on Linux and Windows** — the vendor provides platform-specific C libraries for both. The work is writing (or vendoring) the Python ctypes layer:

- **AlazarTech:** The ATS-SDK includes `atsapi.py` (a single file). Copy it into `sdk_wrappers/` and have it load `ATSApi.dll` on Windows or `libATSApi.so` on Linux. The API is identical across platforms.
- **Acqiris:** No existing Python wrapper. Write `sdk_wrappers/acqiris_wrapper.py` using ctypes against the IVI-C shared library. Reference the Labber driver source for function signatures. Note: the U1084A is marked obsolete by Keysight, so driver updates may stop.

---

### Tier 8: Network/serial (inherently cross-platform)

| Instrument | Interface | Dependency | Linux | Windows | Notes |
|---|---|---|---|---|---|
| MiniCircuits MW switch | Ethernet (HTTP) | `urllib` (stdlib) | Yes | Yes | Simple HTTP GET API |
| MiniCircuits switch matrix | Ethernet (HTTP) | `urllib` (stdlib) | Yes | Yes | Same |
| MuSwitch | Serial (115200) | `pyserial` | Yes | Yes | Fully cross-platform |
| MuSwitchEX | Serial (115200) | `pyserial` | Yes | Yes | Fully cross-platform |
| Oxford ILM | GPIB or Serial (9600) | `pyserial` / PyVISA | Yes | Yes | Oxford serial protocol |
| Oxford Mercury IPS | GPIB/Ethernet/Serial | `pyserial` / PyVISA | Yes | Yes | Oxford ISOBUS protocol |
| Oxford PS120 | GPIB or Serial (9600) | `pyserial` / PyVISA | Yes | Yes | Oxford serial protocol |
| Leiden Pressure | Ethernet (port 9001) | `socket` (stdlib) | Yes | Yes | TCP socket protocol |

**No issues.** These all use cross-platform transports. The wrappers need:
- MiniCircuits: HTTP client (use `urllib.request` from stdlib)
- MuSwitch: Serial port at 115200 baud via pyserial
- Oxford instruments: Either GPIB via PyVISA or serial via pyserial with Oxford's proprietary protocol
- Leiden: Raw TCP socket

---

### Not in the tier table but worth noting

| Instrument | Interface | Status | Linux | Windows |
|---|---|---|---|---|
| WITec | GPIB (profile says SCPI) | Profile is SCPI-only | Yes | Yes |
| WITec (via WITecSDK) | COM automation | Requires WITec Control GUI | **No** | Yes |

The existing WITec profile uses SCPI over GPIB, which is cross-platform. The vendor's `WITecSDK` PyPI package uses Windows COM automation and requires a separate license — only needed for advanced microscope control beyond what SCPI provides.

---

## Summary: what works where

| Status | Count | Instruments |
|---|---|---|
| **Works on both** | 22 | BlueFors, Ocean Optics, NI DAQ (PCI/PCIe), QDevil QDAC, SignalHound, Zurich (3), LabBrick (3), AlazarTech, Acqiris, MiniCircuits (2), MuSwitch (2), Oxford (3), Leiden |
| **Works on both with caveats** | 3 | QD PPMS (client-only on Linux), Aeroflex 302x (Linux support recent), Aeroflex 303x (no Python pkg for RFSA) |
| **Windows only** | 6 | Keysight PXI AWG, Keysight PXI Digitizer, Keysight PXI HVI, Signadyne AWG, Signadyne Digitizer, NI USB-6218 |

---

## Recommended implementation approach

### For each wrapper, the pattern is the same:

```
sdk_wrappers/
├── __init__.py
├── bluefors_wrapper.py       # Tier 1: pure Python, reads log files
├── ocean_optics_wrapper.py   # Tier 2: import seabreeze
├── ni_daq_wrapper.py         # Tier 2: import nidaqmx
├── ppms_wrapper.py           # Tier 2: import MultiPyVu (Client)
├── signalhound_wrapper.py    # Tier 2: ctypes, load vendor .dll/.so
├── qdac_wrapper.py           # Tier 3: pyserial or pyvisa SCPI
├── labbrick_wrapper.py       # Tier 4: ctypes, load vendor .dll/.so/.dylib
├── keysight_pxi_wrapper.py   # Tier 5: import keysightSD1 (Windows only)
├── aeroflex_wrapper.py       # Tier 6: import nirfsg / ctypes for nirfsa
├── alazartech_wrapper.py     # Tier 7: vendor atsapi.py (ctypes)
├── acqiris_wrapper.py        # Tier 7: ctypes against IVI-C lib
├── minicircuits_wrapper.py   # Tier 8: urllib HTTP GET
├── muswitch_wrapper.py       # Tier 8: pyserial
├── oxford_ilm_wrapper.py     # Tier 8: pyserial / Oxford protocol
├── oxford_mercury_wrapper.py # Tier 8: pyserial / Oxford ISOBUS
├── oxford_serial_wrapper.py  # Tier 8: pyserial / Oxford protocol
└── leiden_wrapper.py         # Tier 8: socket TCP
```

### Each wrapper should follow this contract:

```python
class SomeWrapper:
    """Wrapper loaded by sdk_executor.py via the profile's sdk: block."""

    def connect(self, address: str, **kwargs) -> None:
        """Open connection to instrument."""

    def disconnect(self) -> None:
        """Close connection."""

    def identify(self) -> str:
        """Return identity string for profile matching."""

    # Then instrument-specific methods mapped by sdk_call in the YAML profile
```

### Platform-gated imports

For wrappers that depend on vendor SDKs that may not be installed:

```python
# In sdk_executor.py, imports are already dynamic (importlib).
# The wrapper itself should fail fast with a clear message:

try:
    import nidaqmx
except ImportError:
    raise ImportError(
        "nidaqmx package not installed. Install with: pip install nidaqmx\n"
        "Also requires NI-DAQmx runtime driver: https://ni.com/downloads"
    )
```

For ctypes wrappers that load platform-specific binaries:

```python
import ctypes
import sys

def _load_library():
    names = {
        "win32": "vnx_fsynth",
        "linux": "libvnx_fsynth.so",
        "darwin": "libvnx_fsynth.dylib",
    }
    name = names.get(sys.platform)
    if name is None:
        raise OSError(f"Unsupported platform: {sys.platform}")
    try:
        return ctypes.CDLL(name)
    except OSError:
        raise OSError(
            f"Could not load {name}. Download from https://vaunix.com/software/ "
            f"and ensure it is on your library path."
        )
```

### Handling Windows-only instruments on a Linux daemon

For the 6 instruments that are Windows-only (Keysight PXI, Signadyne, NI USB-6218), the cleanest approach is a **split daemon**:

```
Linux lab server                    Windows PXI controller
┌─────────────────────┐            ┌──────────────────────────┐
│ galois-edge daemon   │            │ galois-edge daemon       │
│ (main instance)     │◄──gRPC────►│ (PXI-only instance)      │
│                     │            │ keysightSD1, nidaqmx     │
│ all other           │            │ only serves PXI/USB-DAQ  │
│ instruments         │            │ instruments              │
└─────────────────────┘            └──────────────────────────┘
```

The main daemon would treat the PXI daemon as another instrument source, forwarding `ExecuteCommand` / `StreamMeasurement` calls to it. This requires no code changes to the gRPC protocol — the cloud doesn't need to know which daemon instance is serving which instrument.

Alternatively, if the lab only has a Windows machine, just run the single daemon on Windows. The daemon already works on Windows (PyVISA, pyserial, aiohttp all support it; only linux-gpib is Linux-specific, and it's optional).

---

## Priority order for building wrappers

Based on how many labs are likely to have each instrument and how much work each wrapper requires:

| Priority | Wrapper | Effort | Why |
|---|---|---|---|
| 1 | `minicircuits_wrapper.py` | Small (HTTP GET, ~50 lines) | Many labs have these; stdlib only |
| 2 | `oxford_serial_wrapper.py` | Small (pyserial, ~100 lines) | Covers ILM, PS120; common in cryo labs |
| 3 | `oxford_mercury_wrapper.py` | Small (pyserial/ISOBUS, ~120 lines) | Common magnet PSU |
| 4 | `muswitch_wrapper.py` | Small (pyserial, ~60 lines) | Simple serial protocol |
| 5 | `leiden_wrapper.py` | Small (TCP socket, ~80 lines) | Simple protocol |
| 6 | `bluefors_wrapper.py` | Small (file/network read, ~100 lines) | Common cryostat |
| 7 | `ocean_optics_wrapper.py` | Small (seabreeze API, ~80 lines) | pip install, well-documented |
| 8 | `labbrick_wrapper.py` | Medium (ctypes, ~200 lines) | Covers 3 instruments; cross-platform |
| 9 | `ni_daq_wrapper.py` | Medium (nidaqmx API, ~150 lines) | Common DAQ; Linux caveat for USB |
| 10 | `alazartech_wrapper.py` | Medium (vendor atsapi.py, ~150 lines) | Vendor provides the ctypes module |
| 11 | `qdac_wrapper.py` | Medium (serial protocol, ~150 lines) | Precision DAC, common in qubit labs |
| 12 | `ppms_wrapper.py` | Medium (MultiPyVu client, ~100 lines) | Common cryostat; client-only on Linux |
| 13 | `signalhound_wrapper.py` | Medium (ctypes, ~200 lines) | Less common; manual binary download |
| 14 | `keysight_pxi_wrapper.py` | Large (keysightSD1 API, ~300 lines) | Windows-only; covers 5 instruments |
| 15 | `aeroflex_wrapper.py` | Large (nirfsg + ctypes for nirfsa, ~250 lines) | Niche; partial Linux |
| 16 | `acqiris_wrapper.py` | Large (ctypes from C headers, ~300 lines) | Obsolete hardware; write from scratch |
