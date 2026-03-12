# Cross-Platform Support — Subagent Spec

This document defines 6 independent tasks to make the daemon's SDK wrappers and packaging work correctly across Linux and Windows. Each task is self-contained with exact file paths, code changes, acceptance criteria, and required tests.

Tasks 1–4 have no dependencies on each other and can run in parallel.
Task 5 depends on Tasks 1–4 completing (it verifies the full test suite).
Task 6 is independent (documentation only).

---

## Task 1: Fix LabBrick wrapper cross-platform library loading

**Files to edit:**
- `src/galois_edge/sdk_wrappers/labbrick_wrapper.py`

**Files to create:**
- `tests/test_labbrick_wrapper.py`

### What to change

Replace `_load_dll()` (lines 51–75) with a cross-platform version. The current code rejects all non-Windows platforms at line 62, but Vaunix provides `.so` (Linux) and `.dylib` (macOS) binaries.

New `_load_dll` must:
1. Map `platform.system()` to the correct extension: `{"Windows": ".dll", "Linux": ".so", "Darwin": ".dylib"}`
2. Strip any existing `.dll` extension from `dll_name` before appending the platform extension (callers pass `"vnx_fsynth.dll"` — we need `"vnx_fsynth.so"` on Linux)
3. Check for an env var `LABBRICK_LIB_PATH` — if set, use it as the directory containing the libraries
4. Try `ctypes.util.find_library(base_name)` as a fallback
5. Final fallback: try loading just `{base_name}{ext}` from system library path
6. On failure, raise `OSError` with a message that includes the platform, the paths tried, and a link to `https://vaunix.com/software/`
7. Add `import ctypes.util` and `import os` to the imports at the top of the file (line 28 area)

Also update the classes `LabBrickSynthesizer` and `LabBrickAttenuator`:
- `LabBrickSynthesizer.DLL_NAME` (line 97): change from `"vnx_fsynth.dll"` to `"vnx_fsynth"` (no extension)
- `LabBrickAttenuator.DLL_NAME` (line 262): change from `"vnx_atten.dll"` to `"vnx_atten"` (no extension)

### Tests to write (`tests/test_labbrick_wrapper.py`)

```
Test class: TestLoadDll

1. test_load_dll_resolves_extension_linux
   - Monkeypatch platform.system() -> "Linux"
   - Monkeypatch ctypes.cdll.LoadLibrary to capture the path arg
   - Call _load_dll("vnx_fsynth")
   - Assert the path ends with ".so"

2. test_load_dll_resolves_extension_windows
   - Same but platform.system() -> "Windows"
   - Assert path ends with ".dll"

3. test_load_dll_resolves_extension_darwin
   - Same but platform.system() -> "Darwin"
   - Assert path ends with ".dylib"

4. test_load_dll_unsupported_platform_raises
   - Monkeypatch platform.system() -> "FreeBSD"
   - Assert _load_dll raises OSError with "Unsupported platform"

5. test_load_dll_env_var_override
   - Monkeypatch platform.system() -> "Linux"
   - Set env var LABBRICK_LIB_PATH="/opt/vaunix"
   - Monkeypatch ctypes.cdll.LoadLibrary to capture path
   - Call _load_dll("vnx_fsynth")
   - Assert path starts with "/opt/vaunix"

6. test_load_dll_explicit_path_takes_priority
   - Call _load_dll("vnx_fsynth", dll_path="/custom/path/lib.so")
   - Assert LoadLibrary called with "/custom/path/lib.so"

7. test_load_dll_failure_message_includes_platform
   - Monkeypatch LoadLibrary to raise OSError
   - Assert the re-raised OSError message contains "vaunix.com"
```

### Acceptance criteria
- [ ] `_load_dll` works on Linux, Windows, Darwin
- [ ] `_load_dll` raises clear `OSError` on unsupported platforms
- [ ] `_load_dll` respects `LABBRICK_LIB_PATH` env var
- [ ] `_load_dll` respects explicit `dll_path` argument (takes priority over env)
- [ ] `DLL_NAME` constants no longer have `.dll` extension
- [ ] All 7 tests pass
- [ ] Existing code in `LabBrickSynthesizer` and `LabBrickAttenuator` still works (they call `_load_dll(self.DLL_NAME)`)

---

## Task 2: Clean up pyproject.toml — remove phantom dependencies

**Files to edit:**
- `pyproject.toml`

### What to change

Remove optional dependency groups that reference packages NOT on PyPI. These break `pip install galois-edge[all]`.

**Remove these groups entirely:**
- `keysight-pxi` (line 46–48): `keysightSD1` is not on PyPI
- `signalhound` (line 49–51): `signal-hound` is not on PyPI
- `qdac` (line 40–42): `qdac` is not on PyPI as this package name

**Check and fix `qd-ppms`** (lines 52–54):
- `MultiPyVu` IS on PyPI but may pull in `pywin32` unconditionally on Linux. Check its metadata. If it does, remove the group. If it has proper platform markers (`pywin32; sys_platform == 'win32'`), keep it.
- To check: run `pip download MultiPyVu --no-deps -d /tmp/mpv && unzip -p /tmp/mpv/*.whl '*/METADATA' | grep -i pywin32` or check the PyPI page.
- If uncertain, remove it to be safe — the wrapper does a deferred import anyway.

**Update the `all` group** to remove references to deleted groups:
```toml
all = [
    "galois-edge[gpib,usb,discovery,streaming,ocean-optics,ni-daq]",
]
```

### Verification

```bash
# 1. Check that pyproject.toml is valid
python -c "import tomllib; tomllib.load(open('pyproject.toml', 'rb'))"

# 2. Check that the package builds without error
python -m build --wheel --no-isolation 2>&1 | tail -5

# 3. Check that pip install with all extras resolves (dry run)
pip install --dry-run -e ".[all]" 2>&1 | tail -10
```

### Acceptance criteria
- [ ] No optional dependency group references a package that doesn't exist on PyPI
- [ ] `pip install -e ".[all]"` resolves without errors (dry run)
- [ ] `python -m build --wheel` succeeds
- [ ] Groups that DO exist on PyPI are preserved: `gpib`, `usb`, `discovery`, `streaming`, `ocean-optics`, `ni-daq`

---

## Task 3: Improve error messages in keysight_pxi and ni_daq wrappers

**Files to edit:**
- `src/galois_edge/sdk_wrappers/keysight_pxi_wrapper.py`
- `src/galois_edge/sdk_wrappers/ni_daq_wrapper.py`

**Files to create:**
- `tests/test_sdk_wrappers.py`

### What to change

**keysight_pxi_wrapper.py** — all three classes (`KeysightPxiAwg`, `KeysightPxiDigitizer`, `KeysightPxiHvi`) do bare `import keysightSD1` inside `connect()`. Wrap each in try/except `ImportError` with a descriptive message:

```python
def connect(self) -> None:
    try:
        import keysightSD1  # type: ignore[import-untyped]
    except ImportError:
        raise ImportError(
            "Keysight SD1 SDK not found. This SDK is Windows-only and must "
            "be installed via the Keysight SD1 Software installer "
            "(not available on PyPI). See: https://www.keysight.com/sd1"
        )
    # ... rest of connect
```

Apply this pattern to:
- `KeysightPxiAwg.connect()` (line 51–67)
- `KeysightPxiDigitizer.connect()` (line 264–280)
- `KeysightPxiHvi.connect()` (line 441–453)

**ni_daq_wrapper.py** — add a platform-specific hint when connect fails. Wrap the body of `connect()` (lines 39–52) in try/except:

```python
def connect(self) -> None:
    try:
        import nidaqmx.system  # type: ignore[import-untyped]
        system = nidaqmx.system.System.local()
        device_names = [d.name for d in system.devices]
        if self.device_name not in device_names:
            raise RuntimeError(
                f"NI-DAQmx device '{self.device_name}' not found. "
                f"Available devices: {device_names}"
            )
        self._device = system.devices[self.device_name]
        self._connected = True
        logger.info("NI DAQ connected: %s", self.device_name)
    except ImportError:
        raise ImportError(
            "nidaqmx package not found. Install with: pip install nidaqmx. "
            "Also requires NI-DAQmx runtime driver installed on the host OS."
        )
    except Exception as exc:
        import platform
        msg = str(exc)
        if platform.system() == "Linux":
            msg += (
                " (Note: NI-DAQmx does not support USB DAQ devices on Linux. "
                "PCI/PCIe/PXI devices are supported.)"
            )
        raise RuntimeError(msg) from exc
```

### Tests to write (`tests/test_sdk_wrappers.py`)

```
1. test_keysight_awg_import_error_message
   - Instantiate KeysightPxiAwg()
   - Monkeypatch builtins.__import__ to raise ImportError for "keysightSD1"
   - Call connect(), expect ImportError
   - Assert "Windows-only" in str(exc)
   - Assert "keysight.com" in str(exc)

2. test_keysight_digitizer_import_error_message
   - Same for KeysightPxiDigitizer

3. test_keysight_hvi_import_error_message
   - Same for KeysightPxiHvi

4. test_ni_daq_import_error_message
   - Monkeypatch to block nidaqmx import
   - Assert ImportError message mentions "pip install nidaqmx"

5. test_ni_daq_linux_usb_hint
   - Monkeypatch platform.system() -> "Linux"
   - Monkeypatch nidaqmx.system.System.local() to raise RuntimeError("device not found")
   - Call connect(), expect RuntimeError
   - Assert "USB DAQ devices on Linux" in str(exc)

6. test_ni_daq_windows_no_usb_hint
   - Same setup but platform.system() -> "Windows"
   - Assert "USB DAQ devices on Linux" NOT in str(exc)
```

### Acceptance criteria
- [ ] All 3 Keysight PXI classes catch `ImportError` with a message mentioning "Windows-only"
- [ ] NI DAQ wrapper catches `ImportError` with install instructions
- [ ] NI DAQ wrapper appends Linux USB hint when running on Linux
- [ ] NI DAQ wrapper does NOT append the hint on Windows
- [ ] All 6 tests pass
- [ ] Existing behavior unchanged when SDKs ARE installed (deferred import still works)

---

## Task 4: Vendor AlazarTech atsapi.py stub

**Files to create:**
- `src/galois_edge/vendor/__init__.py`
- `src/galois_edge/vendor/atsapi.py`

**Files to edit:**
- `src/galois_edge/sdk_wrappers/alazartech_wrapper.py`

### What to change

The AlazarTech wrapper (line 43) does `import atsapi as ats` — this module is not on PyPI. It's a single-file ctypes wrapper shipped inside the AlazarTech ATS-SDK. We need to vendor a minimal stub so the import works, while the actual C library (`ATSApi.dll` / `libATSApi.so`) remains a system prerequisite.

**Step 1:** Create `src/galois_edge/vendor/__init__.py` (empty file).

**Step 2:** Create `src/galois_edge/vendor/atsapi.py` — a minimal stub that:
- Uses `ctypes` to load `ATSApi` (platform-aware: `.dll` on Windows, `libATSApi.so` on Linux)
- Defines the `Board` class with methods used by `alazartech_wrapper.py`:
  - `Board(systemId, boardId)` — constructor, calls `AlazarGetBoardBySystemID`
  - `Board.setCaptureClock(source, rate, edge, decimation)`
  - `Board.inputControlEx(channel, coupling, inputRange, impedance)`
  - `Board.setTriggerOperation(op, engJ, srcJ, slopeJ, levelJ, engK, srcK, slopeK, levelK)`
  - `Board.setRecordSize(preTrigger, postTrigger)`
  - `Board.setRecordCount(count)`
  - `Board.startCapture()`
  - `Board.abortCapture()`
  - `Board.busy()` — returns 0 if idle
  - `Board.read(channel, buffer, bytesToCopy, record, transferOffset)`
  - `Board.getBoardKind()`
- Defines constants used by the wrapper: `CHANNEL_A`, `CHANNEL_B`, `INTERNAL_CLOCK`, `CLOCK_EDGE_RISING`, `SAMPLE_RATE_1GSPS`, `SAMPLE_RATE_500MSPS`, `SAMPLE_RATE_250MSPS`, `SAMPLE_RATE_100MSPS`, `INPUT_RANGE_PM_400_MV`, `DC_COUPLING`, `IMPEDANCE_50_OHM`, `TRIG_EXTERNAL`, `TRIG_CHAN_A`, `TRIG_ENGINE_OP_J`, `TRIG_ENGINE_J`, `TRIG_ENGINE_K`, `TRIG_DISABLE`, `TRIGGER_SLOPE_POSITIVE`, `ATS9870`, `ATS9373`, `ATS9360`
- Loads the C library lazily (on first `Board()` call), so import alone doesn't fail

The stub should have a module-level docstring explaining: "Minimal ctypes binding for AlazarTech ATS-SDK. Requires ATSApi.dll (Windows) or libATSApi.so (Linux) installed via the vendor SDK."

**Step 3:** Update `alazartech_wrapper.py` line 43:
```python
# Before:
import atsapi as ats  # type: ignore[import-untyped]

# After:
from galois_edge.vendor import atsapi as ats
```

### Verification

```bash
# 1. The vendor module imports without the C library installed
python -c "from galois_edge.vendor import atsapi; print('import OK')"

# 2. Board() should fail with a clear error about the missing C library
python -c "from galois_edge.vendor import atsapi; atsapi.Board(1,1)" 2>&1 | grep -i "ATSApi"

# 3. The alazartech_wrapper imports cleanly
python -c "from galois_edge.sdk_wrappers.alazartech_wrapper import AlazarTechClient; print('OK')"

# 4. Existing tests still pass
pytest tests/ -v
```

### Acceptance criteria
- [ ] `from galois_edge.vendor import atsapi` succeeds without the C library
- [ ] `atsapi.Board(1, 1)` raises `OSError` mentioning the missing C library name
- [ ] `alazartech_wrapper.py` imports from `galois_edge.vendor` instead of bare `atsapi`
- [ ] All constants referenced in `alazartech_wrapper.py` exist in the stub
- [ ] All `Board` methods referenced in `alazartech_wrapper.py` exist in the stub
- [ ] `pytest tests/ -v` passes (no regressions)

---

## Task 5: Full test suite verification

**Depends on:** Tasks 1–4

### Steps

```bash
# 1. Run the full test suite
pytest tests/ -v 2>&1

# 2. Verify no import errors at module level
python -c "
import galois_edge.sdk_wrappers.labbrick_wrapper
import galois_edge.sdk_wrappers.keysight_pxi_wrapper
import galois_edge.sdk_wrappers.ni_daq_wrapper
import galois_edge.sdk_wrappers.alazartech_wrapper
import galois_edge.sdk_wrappers.aeroflex_wrapper
import galois_edge.sdk_wrappers.signalhound_wrapper
import galois_edge.sdk_wrappers.acqiris_wrapper
print('All SDK wrappers import cleanly')
"

# 3. Verify the package builds
python -m build --wheel --no-isolation

# 4. Verify pip install with all extras
pip install --dry-run -e ".[all]"
```

### Acceptance criteria
- [ ] `pytest tests/ -v` — all tests pass, zero failures
- [ ] All SDK wrappers import without error (they use deferred imports for vendor libs)
- [ ] `python -m build --wheel` succeeds
- [ ] `pip install -e ".[all]"` dry run resolves

---

## Task 6: Write provisioning scripts and vendor setup docs

**Files to create:**
- `scripts/provision_linux.sh`
- `scripts/provision_windows.ps1`

### provision_linux.sh

```bash
#!/usr/bin/env bash
# Provision a Linux lab machine for galois-edge daemon.
# Run as root or with sudo.

set -euo pipefail

echo "=== Galois Edge — Linux Provisioning ==="

# 1. System dependencies
apt-get update && apt-get install -y \
    python3.10 python3.10-venv python3-pip \
    libusb-1.0-0-dev  # for pyusb

# 2. udev rules for USB instruments
# Ocean Optics
if pip3 show seabreeze >/dev/null 2>&1; then
    seabreeze_os_setup
fi

# 3. Install daemon
pip3 install galois-edge[gpib,usb,discovery,ocean-optics,ni-daq]

# 4. Vendor SDK instructions (printed, not automated — requires user action)
cat <<'MSG'

=== Manual vendor SDK installation (if needed) ===

LabBrick (Vaunix) synthesizers/attenuators:
  Download Linux .so from https://vaunix.com/software/
  Copy to /usr/local/lib/ and run ldconfig

AlazarTech digitizers:
  Download ATS-SDK from https://www.alazartech.com/
  Install to default location, run ldconfig

NI-DAQmx (PCI/PCIe DAQs only — USB DAQs NOT supported on Linux):
  Add NI repo: https://www.ni.com/en/support/downloads/drivers/download.ni-linux-device-drivers.html
  apt-get install ni-daqmx

MSG

echo "=== Done ==="
```

### provision_windows.ps1

```powershell
# Provision a Windows lab machine for galois-edge daemon.
# Run as Administrator.

Write-Host "=== Galois Edge — Windows Provisioning ===" -ForegroundColor Green

# 1. Install daemon
pip install galois-edge[usb,discovery,ocean-optics,ni-daq]

# 2. Vendor SDK instructions
Write-Host @"

=== Manual vendor SDK installation (as needed) ===

Keysight PXI (AWG/Digitizer/HVI + Signadyne):
  Install Keysight SD1 Software from:
  https://www.keysight.com/sd1
  The installer places keysightSD1 in the Python path automatically.

NI-DAQmx (all device types including USB):
  Download from https://www.ni.com/en/support/downloads/drivers/download.ni-daqmx.html

LabBrick (Vaunix) synthesizers/attenuators:
  Download DLLs from https://vaunix.com/software/
  Place vnx_fsynth.dll and vnx_atten.dll on the system PATH
  (e.g., C:\Windows\System32\ or the daemon's working directory)

AlazarTech digitizers:
  Download ATS-SDK from https://www.alazartech.com/
  Install to default location (adds ATSApi.dll to PATH)

NI-RFSG (Aeroflex 302x):
  pip install nirfsg
  Also install NI-RFSG runtime driver from NI.

NI-RFSA (Aeroflex 303x):
  Install NI gRPC Device Server from:
  https://github.com/ni/grpc-device/releases
  No Python package needed — the daemon wrapper connects via local gRPC.

SignalHound SA124B:
  Download bb_api.dll from https://signalhound.com/support/
  Place on PATH.

QD PPMS:
  pip install MultiPyVu
  Ensure MultiVu GUI application is running on this machine.

"@ -ForegroundColor Yellow

Write-Host "=== Done ===" -ForegroundColor Green
```

### Acceptance criteria
- [ ] `provision_linux.sh` is executable (`chmod +x`)
- [ ] `provision_windows.ps1` runs without syntax errors (`powershell -Command "Get-Content scripts/provision_windows.ps1"` parses)
- [ ] Both scripts list every vendor SDK from the tier table with download links
- [ ] Linux script correctly notes that NI USB DAQs are not supported
- [ ] Windows script includes Keysight SD1 (the Windows-only SDK) with install instructions
