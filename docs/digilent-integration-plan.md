# Digilent WaveForms Integration Plan

## Overview

Integrate Digilent Analog Discovery (and compatible) devices into the galois-edge daemon
as SDK instruments. These are multi-function USB test instruments (oscilloscope, waveform
generator, power supply, logic analyzer, pattern generator, digital IO, protocol analyzers)
controlled via Digilent's proprietary `libdwf.so` C library through the `dwfpy` Python package.

### Key differences from DPS-150 integration

| Aspect | DPS-150 | Digilent |
|---|---|---|
| Protocol | Custom binary serial | C library (libdwf.so) via ctypes |
| Python dep | pyserial (already bundled) | dwfpy (external pip install) |
| OS dep | None | Adept Runtime + WaveForms .deb packages |
| Vendoring | Yes (pure Python, zero deps) | No — dwfpy loads native libdwf.so |
| Discovery | pyserial VID/PID match | DWF device enumeration API |
| USB driver | CDC ACM (standard serial) | FTDI D2XX (ftdi_sio must be unbound) |
| Complexity | Simple register read/write | Multi-subsystem instrument |

### Supported devices (all use the same DWF API)

- Analog Discovery (original) — **confirmed working on Pi5**
- Analog Discovery 2, 3
- Analog Discovery Studio, Studio Max
- Analog Discovery Pro (ADP2230, ADP2440, ADP3450)
- Digital Discovery
- Electronics Explorer Board

### Verified device capabilities (Analog Discovery original on Pi5)

- Oscilloscope: 2 channels, 100 MHz, 8192 sample buffer
- Waveform Generator: 2 channels
- Power Supply: V+ (0–5V), V- (0 to -5V), system monitor (USB V, I, temp)
- Digital I/O: 16 channels (logic analyzer + pattern generator)

---

## OS Prerequisites (DONE)

Installed on Pi5:
- `digilent.adept.runtime_2.27.9-arm64.deb` — D2XX driver, udev rules, dftdrvdtch
- `digilent.waveforms_3.25.1_arm64.deb` — libdwf.so 3.25.1
- `dwfpy==1.2.0` via pip

The udev rule at `/etc/udev/rules.d/52-digilent-usb.rules` auto-unbinds `ftdi_sio`
and sets `MODE:="666"` on Digilent USB devices.

---

## Phase 1: SDK Wrapper + Basic Profile (Power Supply + System Monitor)

Start with the simplest subsystems — power supply control and system readback.
These are simple get/set operations, identical to the DPS-150 pattern.

### Task 1.1: Create SDK wrapper

**File:** `src/galois_edge/sdk_wrappers/digilent_dwf_wrapper.py`

**Context files to read:**
- `src/galois_edge/sdk_wrappers/dps150_wrapper.py` (pattern to follow)
- `src/galois_edge/sdk_executor.py` (lifecycle: connect/disconnect/identify/execute)

**Design:**
- Class `DigilentDwfClient` following `DPS150Client` pattern
- Deferred `import dwfpy` inside `connect()` — graceful `ImportError` if not installed
- `connect(serial_number=None)`: enumerate DWF devices, open first (or by serial)
- `disconnect()`: close device handle
- `get_identity()`: return `"Digilent,{device_name},{serial},{dwf_version}"`
- `discover()` class method: enumerate all DWF devices, return list of serial numbers

**Power supply methods:**
- `get_positive_supply_voltage()` → read V+ status
- `set_positive_supply_voltage(value)` → set V+ voltage (0–5V)
- `get_positive_supply_current()` → read V+ current status
- `enable_positive_supply()` / `disable_positive_supply()`
- Same for negative supply (V-)
- `get_usb_voltage()` → system monitor USB voltage
- `get_usb_current()` → system monitor USB current
- `get_temperature()` → device temperature

**Oscilloscope methods (simple single-shot):**
- `read_analog(channel, range, samples, frequency)` → JSON array of voltage samples
- `get_analog_voltage(channel, range)` → single-shot average voltage (DC measurement)

**Waveform generator methods:**
- `set_waveform(channel, function, frequency, amplitude, offset)` → configure + enable
- `stop_waveform(channel)` → disable output
- `get_waveform_status(channel)` → current config as JSON

**Digital IO methods:**
- `set_digital_output(pin, value)` → set a digital pin high/low
- `get_digital_input(pin)` → read a digital pin
- `set_digital_bus(mask, value)` → set multiple pins
- `get_digital_bus()` → read all digital pins

**Acceptance criteria:**
- `DigilentDwfClient` can be instantiated without dwfpy installed (no crash)
- `connect()` opens the device, `disconnect()` closes it
- All methods return strings (SDK executor serializes to gRPC)
- Thread-safe (dwfpy operations protected by SDK executor's per-client lock)

### Task 1.2: Create profile YAML

**File:** `src/galois_edge/profiles/digilent_analog_discovery.yaml`

**Context files to read:**
- `src/galois_edge/profiles/serial/fnirsi_dps150.yaml` (pattern to follow)
- `src/galois_edge/profiles/blueforslogging.yaml` (another SDK profile)

**Design:**
- `instrument.class: oscilloscope` (primary function)
- `identity.query: SDK_IDENTITY`, `identity.pattern: "Digilent.*Analog Discovery"`
- `sdk.import_path: galois_edge.sdk_wrappers.digilent_dwf_wrapper`
- `sdk.class_name: DigilentDwfClient`
- No `interfaces` with VID/PID (FTDI VID is shared; discovery via DWF enum)
- Commands for all wrapper methods above (~20-25 commands)
- Power supply + scope readback commands marked `streamable: true`
- `output_enable` marked `is_dangerous: true`

**Acceptance criteria:**
- Profile loads without errors
- All commands map to wrapper methods via `sdk_call`
- Profile matches IDN string from `get_identity()`

### Task 1.3: DWF device discovery in daemon

**File:** `src/galois_edge/main.py`

**Context files to read:**
- `src/galois_edge/main.py` (existing `_discover_serial_sdk_instruments`)
- `src/galois_edge/sdk_executor.py` (`connect`, `identify`)

**Design:**
Add `_discover_dwf_instruments()` method to `EdgeDaemon`:
1. Import `dwfpy` (deferred, skip if not installed)
2. Call `dwfpy.Device.enumerate()` to list connected Digilent devices
3. For each device, check if already registered in capability_manager
4. Connect via SDK executor, get identity, register with profile
5. Call from `_background_profile_match()` after serial SDK discovery
6. Call from `_periodic_reconcile()` for hot-plug support

The instrument_id for DWF devices will be `DWF:{serial_number}`
(e.g., `DWF:210244694170`).

**Acceptance criteria:**
- Digilent device auto-discovered on daemon startup
- Shows up in `ListInstruments` gRPC response
- Hot-plug: unplugging and re-plugging re-discovers the device
- No crash if dwfpy/libdwf not installed (graceful skip)

### Task 1.4: PyInstaller + dependency config

**Files:** `galois-edge-daemon.spec`, `pyproject.toml`, `requirements.txt`

**Context files to read:**
- `galois-edge-daemon.spec` (current hiddenimports)
- `pyproject.toml` (optional deps section)

**Design:**
- Add `digilent = ["dwfpy>=1.1.0"]` to `pyproject.toml` optional deps
- Add `"dwfpy"` to `hiddenimports` in spec (if installed, it gets bundled)
- dwfpy's only runtime dep is `numpy` (already commonly available)
- `libdwf.so` is NOT bundled — it's an OS-level install requirement

**Acceptance criteria:**
- PyInstaller build succeeds with or without dwfpy installed
- When dwfpy is pip-installed and libdwf.so is present, device works
- When dwfpy is absent, daemon starts normally without Digilent support

---

## Phase 2: Oscilloscope Acquisition Workflow

More complex — scope acquisition is a multi-step workflow, not a simple get/set.

### Task 2.1: Scope acquisition commands

Add to wrapper and profile:
- `configure_scope(channel, range, offset, coupling, frequency, buffer_size)`
- `configure_trigger(source, type, level, hysteresis, position)`
- `acquire_single()` → start acquisition, wait, return data as JSON array
- `acquire_scan(duration)` → continuous scan mode, return latest buffer
- `measure_frequency(channel)` → auto-measure frequency
- `measure_amplitude(channel)` → auto-measure Vpp

### Task 2.2: Vector data support

Scope data returns arrays (thousands of samples). The gRPC proto has
`vector_data` support — wire the scope acquisition to return data through
that path instead of cramming arrays into the `data` string field.

---

## Phase 3: Protocol Analyzers

### Task 3.1: UART/SPI/I2C protocol commands

dwfpy exposes bit-banged protocol engines. Add commands:
- `uart_write(tx_pin, baud, data)` / `uart_read(rx_pin, baud, count)`
- `spi_transfer(clk, mosi, miso, cs, data)` → full-duplex SPI
- `i2c_write(scl, sda, address, data)` / `i2c_read(scl, sda, address, count)`

These are powerful for testing embedded systems connected to the Pi5.

---

## Phase 4: Provisioning Script

### Task 4.1: Digilent setup script

**File:** `scripts/provision-digilent.sh`

Automate the OS-level installation:
```bash
#!/bin/bash
# Downloads and installs Digilent Adept Runtime + WaveForms for ARM64
# Requires: sudo, internet access
```

- Download arm64 .deb files from files.digilent.com
- Install via `dpkg -i` + `apt --fix-broken install`
- Reload udev rules
- pip install dwfpy
- Verify with a quick enumeration test

---

## Execution Notes

- **Phase 1 tasks are independent** — wrapper (1.1), profile (1.2), discovery (1.3),
  and config (1.4) can be developed in parallel by subagents.
- **Phase 2 depends on Phase 1** — scope commands build on the working wrapper.
- **Phase 3 and 4 are optional** — can be deferred.
- **Testing:** After each phase, deploy to Pi5 via `./scripts/deploy-pi.sh --skip-go`
  and verify with gRPC commands.
