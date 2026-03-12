# Lost in Translation: Labber INI to Galois YAML Profile Conversion

## Executive Summary

Forty-two Labber instrument drivers (MIT licensed) were auto-converted from INI format to Galois YAML profiles using `labber2galois.py`. The conversion successfully extracted basic metadata (manufacturer, model, interface type, timeouts) and simple SCPI get/set command pairs. However, the conversion produced **functionally hollow profiles** for approximately 40% of the instruments -- instruments whose Labber drivers rely entirely on custom Python logic, vendor SDK wrappers, or non-VISA interfaces. For the remaining 60%, the profiles are usable but incomplete, missing conditional parameter visibility, hardware sweep support, human-readable combo labels, and vector data handling.

This document catalogs every category of loss, its impact on researchers and the platform, severity, and remediation path.

---

## 1. What's Lost (Technical)

### 1.1 Custom Python Logic

**What Labber does:** Every Labber driver with a `driver_path` field in its INI has a companion Python file that subclasses either `VISA_Driver` or `InstrumentDriver.InstrumentWorker`. These Python drivers override `performOpen`, `performSetValue`, `performGetValue`, `performArm`, and `checkIfSweeping` to implement logic that cannot be expressed in a declarative config file.

**What the converter produced:** A comment line `# NOTE: Original Labber driver includes custom Python logic not captured here` and nothing else for the instrument-specific logic. 40 of the 42 converted profiles carry this note.

**Specific examples from the files read:**

1. **AlazarTech Digitizer** (`AlazarTech_Digitizer.py`): The entire driver is Python-only. It wraps a C-level SDK (`AlazarTech_Digitizer_Wrapper.py`) with methods like `AlazarSetCaptureClock`, `AlazarSetTriggerOperation`, `readTracesDMA`. The INI defines 60+ parameters (sample rates, trigger configs, FFT settings, channel ranges) but **zero set_cmd/get_cmd fields** -- all I/O goes through the Python wrapper. The converted YAML profile is an empty shell: just `instrument:`, `identity:`, `interfaces:`, `settings:` -- no commands at all. The Python logic includes:
   - DMA buffer management and memory allocation (`readTracesDMA`, `removeBuffersDMA`)
   - Hardware trigger arming and round-robin averaging (`performArm`, `getSignalHardwareLoop`)
   - Clock/sample-rate configuration with model-specific branching (9870 vs 9373 vs 9360)
   - FFT computation with windowing and spectral interpolation (`get_fft_config`, `extract_trace_value` using `scipy.interpolate.interp1d`)
   - Progress callbacks (`_callbackProgress` via `self.reportStatus`)

2. **Oxford IPS120** (`Oxford IPS120.py`): Uses non-SCPI commands (`V` for identification, `Q4` to set query format, `C3`/`C2` for control mode, `A1`/`A0` for action). The Python `performSetValue` implements a full state machine:
   - Enter remote control mode (`C3`)
   - Send the set-point command
   - Issue "go to set-point" (`A1`)
   - Poll with `waitForTarget` (reads `R7` in a loop until `abs(target-current) <= precision`)
   - Return to local mode (`C2`)

   The converted YAML has `getter: "R7"` and `setter: "J{value}"` for the B field, which would send the raw commands but **skip the control-mode handshake and the wait-for-target loop entirely**. The magnet would receive a set-point but never be told to sweep to it.

3. **Triton dilution fridge** (`Triton.py`): Complex multi-axis vector magnet control:
   - `performOpen` queries `READ:SYS:VRM:RFMX` to detect which magnet axes are physically installed, then calls `setInstalledOptions` -- this auto-detection is lost
   - Response parsing strips Oxford-style responses (format: `STAT:SYS:VRM:VECT:[x y z]`) by splitting on `:` and stripping letter suffixes
   - `setTargetField` reads the current 3-vector, modifies one component, and writes back the full vector -- this read-modify-write pattern cannot be expressed in YAML
   - `performSetValue` for B-fields coordinates the full sequence: HOLD, set coordinate system, read current target, modify, write, RTOS (ramp-to-set), then `waitForIdle` polling
   - Sweep commands use `<sr>` substitution for sweep rate and call `sweep_check_cmd`/`stop_cmd` -- none of which exist in our schema

4. **BlueFors Logging** (`BlueForsLogging.py`): This instrument uses **no VISA at all** (`interface: None` in INI). The Python driver reads temperature and pressure data from log files on disk:
   - Constructs file paths from date: `{logFolder}/{YY-MM-DD}/maxigauge {YY-MM-DD}.log`
   - Parses CSV lines from the last row of the file
   - Converts pressure units (bar to mbar, dividing by 1000)
   - The converted YAML has no commands section at all -- it is completely inert

5. **QDevil QDAC** (`QDevil_QDAC.py`): Wraps a vendor SDK (`qdac.py`) with a custom VISA sub-class (`labberqdac`). The INI defines 7,720 lines covering 24-48 voltage channels, 10 generators, sync ports, AWG signals, and pulse trains. The Python implements:
   - Custom serial protocol via `labberqdac._sendReceive`
   - Per-channel voltage setting via `qdac.setDCVoltage`
   - Voltage/current range validation with mutual exclusion rules (cannot set 1V range with 100uA current range)
   - Function generator programming (`defineFunctionGenerator`, `definePulsetrain`, `defineAWG`)
   - AWG signal loading from file with 8000-point truncation
   - Trigger management across generators

   The converted YAML is an empty shell -- none of these 48 channels or 10 generators are represented.

6. **Keysight PXI Digitizer** (`Keysight_PXI_Digitizer.py`): Uses `use_visa = False` with `interface: PXI`. The entire driver works through the Keysight SD1 SDK. The converted YAML has zero commands.

7. **Keysight M8195 AWG** (`Keysight_M8195_AWG.py`): While it uses VISA, the Python handles waveform upload via binary block transfers (`self.writeAndLog(':TRAC:DEL:ALL')`), memory mode configuration, and multi-channel sequencing. The INI defines VECTOR-type quantities for waveform data that the converter cannot represent.

### 1.2 state_quant / state_value (Conditional Visibility)

**What Labber does:** The INI fields `state_quant`, `state_value_1`, `state_value_2`, etc. create a dependency tree where parameters are only visible/active when a parent parameter has a specific value. This is used extensively for:
- Showing trigger configuration only when trigger source is not "Immediate"
- Showing coordinate-specific magnetic field parameters based on the selected coordinate system
- Showing FFT parameters only when FFT is enabled
- Showing channel-specific settings only when a channel is enabled

**What's lost:** The Galois YAML schema has no concept of conditional parameter visibility or parameter dependencies.

**Specific examples:**

From `AlazarTech_Digitizer.ini`:
```ini
[Trig slope]
state_quant: Trig source
state_value_1: Channel 1
state_value_2: Channel 2
state_value_3: External

[Ch1 - Data]
state_quant: Ch1 - Enabled
state_value_1: True
```
The trigger slope, level, delay, and coupling parameters should only appear when the trigger source is set to an appropriate value. Without `state_quant`, a user sees all parameters at all times, leading to confusion about which are active.

From `Triton.ini`:
```ini
[Bx]
state_quant: CoordSys
state_value_1: Cartesian

[Br]
state_quant: CoordSys
state_value_1: Spherical

[Btheta]
state_quant: CoordSys
state_value_1: Cylindrical
state_value_2: Spherical
```
The magnetic field components (Bx/By/Bz vs Br/Btheta/Bphi vs Brho/Btheta/Bz) should switch based on the coordinate system. Without this, all 7 field components are presented simultaneously, which is physically wrong and confusing.

### 1.3 sweep_cmd / stop_cmd / sweep_check_cmd

**What Labber does:** The INI supports hardware-level sweep commands:
- `sweep_cmd`: Command to start a sweep with rate substitution (`<sr>` for sweep rate, `<st>` for sweep time, `<*>` for target)
- `stop_cmd`: Command to abort a sweep
- `sweep_check_cmd`: Command to poll whether a sweep is still in progress

**What's lost:** The Galois YAML schema has no sweep support. `CommandConfig` has `scpi`, `getter`, `setter` -- no equivalent for ramped/swept values.

**Specific example from `Triton.ini`:**
```ini
[Bx]
set_cmd: SET:SYS:VRM:RVST:MODE:ASAP:VSET:[<*>]
sweep_cmd: SET:SYS:VRM:RVST:MODE:RATE:RATE:<sr>:VSET:[<*>]
sweep_check_cmd: READ:SYS:VRM:ACTN
stop_cmd: SET:SYS:VRM:ACTN:HOLD
```

The converted YAML only keeps `set_cmd` as `setter`. A researcher setting a magnetic field through Galois would get an instantaneous jump (if the hardware allows it) rather than a controlled ramp. For superconducting magnets, this is potentially **equipment-damaging** -- rapid field changes can quench the magnet.

### 1.4 VECTOR / VECTOR_COMPLEX Data Types

**What Labber does:** The INI supports `datatype: VECTOR` and `datatype: VECTOR_COMPLEX` for trace/waveform data. These quantities carry associated x-axis metadata:
```ini
[Ch1 - Data]
x_name: Time
x_unit: s
datatype: VECTOR
```

**What's lost:** The Galois `ParameterConfig.type` only supports: `float`, `int`, `string`, `enum`, `bool`. The `ReturnConfig.type` adds `array` and `binary`, but there is no concept of axis metadata (x_name, x_unit, dt, t0). This affects:
- All digitizers/oscilloscopes returning time-domain traces
- All spectrum analyzers returning frequency-domain data
- AWGs that accept waveform uploads

The `MeasurementDataPoint` proto message returns `double value` (scalar) or `map<string, double> values` (named scalars). There is no mechanism for streaming arrays of points with x-axis metadata.

**Instruments affected:** AlazarTech Digitizer (Ch1/Ch2 Data, FFT Data), Keysight PXI Digitizer, Keysight S-Series Oscilloscope, LeCroy Oscilloscope, Keysight M8195 AWG, Acqiris U1084A, Agilent Spectrum Analyzer, Agilent Network Analyzer, Keysight 6000X Scope.

### 1.5 Model Variants and Option Detection

**What Labber does:** The INI `[Model and options]` section supports:
- `model_str_1`, `model_str_2`, ...: Multiple hardware models served by one driver
- `model_value_1` on parameters: Show this parameter only for specific models
- `option_str_1`, `option_value_1`: Hardware options that can be auto-detected
- `check_model`: Whether to verify model at startup

The Python `performOpen` often queries the instrument to detect installed options and calls `setInstalledOptions()`.

**What's lost:** Galois uses one profile per model (or a single `identity.pattern` regex that matches multiple models). There is no concept of:
- Conditionally showing parameters per model variant
- Runtime option detection
- Option-dependent parameter filtering

**Specific examples:**

From `AlazarTech_Digitizer.ini`:
```ini
model_str_1: 9870
model_str_2: 9373
model_str_3: 9360
option_str_1: FFT

[Pre-trig samples]
model_value_1: 9870    # Only shown for model 9870

[Ch1 - Range]
model_value_1: 9870    # Range selection only for 9870
```

The AlazarTech digitizer YAML has `pattern: "AlazarTech.*(9870|9373|9360)"` matching all three models but no way to differentiate their capabilities. The 9870 supports pre-trigger samples, bandwidth limiting, and per-channel range/coupling/impedance selection; the 9373 and 9360 do not. A user with a 9360 would see a profile that claims capabilities it does not have.

From `Triton.ini`:
```ini
option_str_1: x magnet
option_str_2: y magnet
option_str_3: z magnet
option_str_4: switch heater

[CoordSys]
option_value_1: x magnet
option_value_2: y magnet
option_value_3: z magnet
```
The coordinate system selector only appears if x, y, and z magnets are all installed. The Python driver auto-detects this by querying `READ:SYS:VRM:RFMX`. In Galois, these fields always appear regardless of hardware configuration.

### 1.6 Non-SCPI Protocols

**What Labber does:** Many instruments, particularly from Oxford Instruments, use proprietary command syntaxes that do not follow SCPI conventions:

**What's lost:** The Galois command handler (`command_handler.py`) detects queries by checking if the command string ends with `?`. Non-SCPI instruments do not follow this convention.

**Specific examples:**

- **Oxford IPS120**: Commands are single letters + numbers: `R7` (read field), `J0.5` (set target), `C3` (remote mode), `A1` (go to setpoint), `Q4` (set query format). None end with `?`. The `force_query` flag on `CommandHandler.execute_command` can work around this, but the profile would need to mark every getter command as requiring `force_query`.

- **Triton**: Uses Oxford-style `READ:DEV:T1:TEMP:SIG:TEMP` (no `?`) and `SET:DEV:T1:TEMP:LOOP:MODE:ON`. Responses come as `STAT:DEV:T1:TEMP:SIG:TEMP:1.234K` and require parsing to extract the value. The YAML profile naively stores these as SCPI commands, but the response parser would not know to strip the `STAT:...` prefix and `K` suffix.

- **Cryomagnetics LM510**: Uses `MEAS?` (does end with `?`) and `INTVL` / `INTVL?`, but response values need parsing from the proprietary format.

- **HP 3478 Multimeter**: Uses HP-IB commands that predate SCPI.

### 1.7 Multi-Channel Repetition

**What Labber does:** The INI uses naming conventions like `CH01`, `CH02`, ... `CH48` to define repeated per-channel quantities. The QDevil QDAC INI has 7,720 lines defining 48 identical voltage channels with identical parameters (Voltage, Voltage-Range, Current-Range, Mode, Apply, etc.) per channel.

**What's lost:** The Galois YAML has no template/repetition mechanism. The converter either:
- Omits the repeated channels entirely (QDevil QDAC: empty profile)
- Flattens them into individual top-level commands (impractical for 48 channels x 5 parameters = 240 commands)

The Triton YAML does flatten T1-T13 into 13 separate `t1` through `t13` commands, which works but is verbose and loses the grouping context.

### 1.8 Combo Display Labels (cmd_def vs combo_def)

**What Labber does:** COMBO parameters have two parallel lists:
- `combo_def_N`: Human-readable display labels (e.g., "100 MS/s", "31.6 uA", "Cartesian")
- `cmd_def_N`: Actual values sent to the instrument (e.g., "0x00000024", "0.0316", "CART")

This allows researchers to select from readable options while the driver translates to wire format.

**What's lost:** The Galois YAML `enum` type only has `options` -- a single list. The converter drops the display labels and uses either the `cmd_def` values or the `combo_def` values (inconsistently).

**Specific examples:**

From `Triton.ini`, HeaterRange:
```ini
combo_def_1: Off          ->  cmd_def_1: 0.0
combo_def_2: 31.6 uA     ->  cmd_def_2: 0.0316
combo_def_3: 100 uA      ->  cmd_def_3: 0.1
...
combo_def_9: 100 mA      ->  cmd_def_9: 100.0
```

The converted YAML HeaterRange has `options: ["0.0", "0.0316", "0.1", "0.316", "1.0", "3.16", "10.0", "31.6", "100.0"]`. A researcher using the Galois API must know that `3.16` means "3.16 mA" heater range. The human-readable labels "Off", "31.6 uA", etc. are lost.

From `AlazarTech_Digitizer.ini`, Sample rate:
```ini
combo_def_19: 1 GS/s     ->  cmd_def_19: 0x00000035
```
The hex command codes are internal SDK constants, not SCPI values. These are meaningless outside the Python wrapper.

From `Triton.ini`, CoordSys:
```ini
combo_def_1: Cartesian    ->  cmd_def_1: CART
combo_def_2: Cylindrical  ->  cmd_def_2: CYL
combo_def_3: Spherical    ->  cmd_def_3: SPH
```
The converted YAML uses `options: ["CART", "CYL", "SPH"]` -- functional but less readable. The `state_quant` for Bx references "Cartesian" (the display label), not "CART" (the cmd value), so the dependency chain breaks.

### 1.9 Parameter Groups, Sections, and UI Layout

**What Labber does:** The INI supports `group:` and `section:` fields that organize parameters into logical clusters:
```ini
[T1]
group: Temperatures
section: Temperature

[ControlLoop]
group: Control loop
section: Temperature

[Bx]
group: Magnetic field
section: Magnet
```

**What's lost:** The Galois YAML has no `group` or `section` concept. All commands are flat under the `commands:` key. For instruments with dozens of parameters, this makes the profile harder to navigate and the eventual UI harder to lay out.

### 1.10 Default Values, Permissions, and Tooltips

**What Labber does:**
- `def_value`: Default value for a parameter
- `permission: READ` / `WRITE` / `BOTH` / `NONE`: Access control
- `tooltip`: Hover text for the UI
- `label`: Human-readable display name (different from the section name)
- `show_in_measurement_dlg: True`: Whether to show in the measurement dialog
- `enabled: False`: Whether the control is enabled by default

**What's lost:** The Galois schema supports `enabled: bool` on commands, but has no:
- Default values for parameters
- Read-only vs write-only distinctions (a `property` always implies both getter and setter)
- UI labels or tooltips
- Measurement dialog filtering

The `permission: READ` distinction is partially handled: the converter creates `type: query` for read-only quantities. But `permission: WRITE` (write-only) has no equivalent.

### 1.11 Initialization and Finalization Commands

**What Labber does:** The INI `[VISA settings]` section supports:
```ini
init: Q4
final: C0
```
These are sent when the driver opens/closes the connection.

**What's lost:** The Galois YAML schema has no `init_commands` or `cleanup_commands` field. For the Oxford IPS120, the `Q4` init command sets the query response format -- without it, the `R7` getter returns data in an unparseable format.

### 1.12 Hardware Triggering and Arming

**What Labber does:**
```ini
support_arm: True
support_hardware_loop: True
```
These enable the `performArm` callback and hardware-synchronized measurement loops. The AlazarTech digitizer's entire measurement workflow depends on this: arm the card, wait for external trigger, read DMA buffers.

**What's lost:** The Galois gRPC proto has `StreamMeasurement` for periodic polling but no concept of hardware arming, external triggering, or hardware-synchronized loops. The `StreamMeasurementRequest` only has `interval_ms` for polling -- it cannot wait for an external trigger event.

### 1.13 Non-VISA Interfaces

**What Labber does:** Supports `interface: None`, `interface: PXI`, `interface: Other`, `interface: serial` with specific settings (`baud_rate`, `parity`, `data_bits`, `stop_bits`, `send_end_on_write`).

**What's lost:** The Galois `InterfaceConfig` has `type: gpib | usb | ethernet | serial` but the converter defaults everything to `gpib`. The PXI instruments (Keysight PXI AWG, PXI Digitizer, PXI HVI Trigger, PXI LO) get `type: gpib` which is wrong. BlueForsLogging gets `type: gpib` when it uses no VISA at all. QDevil QDAC gets `type: gpib` when it uses serial at 460800 baud.

Serial communication settings (baud rate, parity, data bits, stop bits) have no fields in the Galois schema.

---

## 2. Impact on Platform & Customers

### 2.1 Custom Python Logic
- **Researcher impact:** For the ~16 instruments whose drivers are 100% Python (AlazarTech, BlueFors, QDevil QDAC, Keysight PXI family, NI DAQ, NI USB-6218, LabBrick, MiniCircuits, MuSwitch, Ocean Optics, Newport MM4006, LeidenPressure, MultiQubit PulseGenerator, etc.), the converted profiles are **non-functional decoration**. They provide identity matching but zero operational commands. A researcher connecting one of these instruments would see it detected but could do absolutely nothing with it through the profile-based API.
- **Basic SCPI workflow:** Does NOT work for these instruments -- they don't use SCPI.
- **Segments affected:** Quantum computing labs (AlazarTech, PXI instruments, MultiQubit PulseGenerator), cryogenics labs (BlueFors, Oxford, Triton), nanofabrication (QDevil QDAC). These are premium/enterprise customers.

### 2.2 Conditional Visibility
- **Researcher impact:** All parameters shown simultaneously regardless of context. For the AlazarTech digitizer (if it worked), a user would see trigger coupling/slope/level settings even when trigger source is "Immediate". For the Triton, all 7 magnetic field components appear even though only 3 are physically meaningful at once. Not dangerous but confusing and unprofessional.
- **Basic SCPI workflow:** Still works -- these are UI concerns, not protocol concerns.
- **Segments affected:** All, but academic users (less technical infrastructure, more reliance on intuitive UIs) are most affected.

### 2.3 Hardware Sweep Support
- **Researcher impact:** Magnet sweeps, temperature ramps, and voltage sweeps must be done manually (step-by-step from the notebook) rather than delegated to the instrument's internal sweep engine. This is: (a) slower, (b) less smooth, (c) potentially unsafe for magnets.
- **Basic SCPI workflow:** Partially works -- you can set a target, but you lose the ramp.
- **Workflows broken:** Magnetic field sweeps with rate limiting, temperature ramp programs, any automated sweep.
- **Segments affected:** Quantum (superconducting magnets), cryogenics (temperature control). **Safety concern for magnet controllers.**

### 2.4 Vector/Trace Data
- **Researcher impact:** Cannot acquire oscilloscope traces, digitizer waveforms, spectrum analyzer sweeps, or network analyzer S-parameters through the profile-based API. These are the core data products of measurement instruments.
- **Basic SCPI workflow:** Raw SCPI (`SendCommand`) can still be used to query trace data, but the response will be a raw string/binary blob with no metadata. The researcher must parse it manually.
- **Segments affected:** Everyone -- oscilloscopes and digitizers are in every lab.

### 2.5 Model Variants
- **Researcher impact:** A profile that matches multiple models may advertise commands that the specific model doesn't support, leading to errors at runtime. Or worse, the profile may omit model-specific features.
- **Basic SCPI workflow:** Works for shared commands; fails for model-specific ones.
- **Segments affected:** Moderate impact across all segments.

### 2.6 Non-SCPI Protocols
- **Researcher impact:** For Oxford instruments, the YAML commands will be sent as raw strings, but responses will not be parsed correctly. Querying `R7` on the IPS120 returns something like `R+00.0000` -- the leading `R` needs to be stripped. The Triton returns `STAT:DEV:T1:TEMP:SIG:TEMP:1.234K` -- the value `1.234` needs to be extracted from the colon-delimited response. Without parsing, the returned data is a raw string that the user must parse manually.
- **Basic SCPI workflow:** Partially works -- commands can be sent via `SendCommand`, but responses need manual parsing.
- **Segments affected:** Cryogenics and condensed matter labs (Oxford/Triton heavy users).

### 2.7 Multi-Channel Repetition
- **Researcher impact:** For the QDevil QDAC (24/48 channels), the profile is empty. Even if populated, addressing 48 channels through 48 individual commands is unwieldy. Researchers expect `set_voltage(channel=5, value=1.0)`, not `ch05_voltage(value=1.0)`.
- **Segments affected:** Quantum computing (QDACs are standard in qubit tuning setups).

### 2.8 Combo Display Labels
- **Researcher impact:** Parameters show raw protocol values instead of human-readable labels. A researcher must know that heater range `0.0316` means "31.6 uA" or that sample rate `0x00000035` means "1 GS/s". Minor for SCPI-native values like "CART"/"CYL"/"SPH", but severe for hex codes and floating-point encodings.
- **Basic SCPI workflow:** Works -- the wire values are correct.
- **Segments affected:** All users; particularly harmful for onboarding new researchers.

---

## 3. Severity Ranking

| # | Loss Category | Severity | Justification |
|---|---|---|---|
| 1 | Custom Python logic (SDK-only instruments) | **CRITICAL** | 16+ instruments are completely non-functional. Zero commands, zero capabilities. Customers paying for Galois who have these instruments get nothing. |
| 2 | VECTOR/VECTOR_COMPLEX data types | **CRITICAL** | Oscilloscopes, digitizers, and spectrum analyzers cannot return their primary data product (traces). This is the core measurement workflow. |
| 3 | Hardware sweep support | **HIGH** | Safety issue for magnets. Workflow issue for any ramped measurement. Cannot be worked around without writing custom notebook code. |
| 4 | Non-SCPI response parsing | **HIGH** | Oxford/Triton instruments return data in proprietary formats. The raw string is returned but not parsed, requiring manual extraction. These are among the most common cryogenics instruments. |
| 5 | Non-VISA / wrong interface type | **HIGH** | PXI, file-based, and serial instruments are assigned GPIB interface, which will fail on connection. 8-10 instruments affected. |
| 6 | Model variant / option detection | **MEDIUM** | Presents wrong capabilities for the specific hardware, causing runtime errors. Workaround: create separate profiles per model. |
| 7 | Combo display labels | **MEDIUM** | Usability issue. Raw protocol values are confusing but functional. Can be fixed per-profile. |
| 8 | state_quant conditional visibility | **MEDIUM** | UI clutter and confusion. All parameters always visible. No runtime breakage. Schema change needed. |
| 9 | Init/final commands | **MEDIUM** | Some instruments require initialization to function (Oxford IPS120 `Q4`). Without init, even correct commands may return garbage. |
| 10 | Multi-channel repetition | **MEDIUM** | Profile verbosity and missing channel abstraction. Workaround: raw SCPI or hand-edit profiles. |
| 11 | Groups/sections/labels/tooltips | **LOW** | Pure UI/organization concern. No functional impact. |
| 12 | Default values, permissions | **LOW** | Minor UX improvement. Defaults can be documented. Read-only distinction is nice-to-have. |
| 13 | Hardware triggering/arming | **MEDIUM** | Only affects instruments with `support_arm: True` (digitizers, AWGs), but those are critical quantum computing instruments. No workaround in the current Galois architecture. |

---

## 4. Remediation Path

### 4.1 Custom Python Logic (SDK-only instruments)
**Effort: Large (per-instrument)**

Three options:
1. **ProxySDKCall path (already in proto):** The `ProxySDKCallRequest` RPC is designed for exactly this case. For each SDK-dependent instrument, register the vendor SDK on the edge and let researchers call methods directly. No profile changes needed, but researchers lose tab-completion and must know the SDK API.

2. **SDK-mapped commands in profiles:** The Galois schema already supports `sdk_call` on `CommandConfig`. For each instrument, hand-write a profile that maps named commands to SDK methods:
   ```yaml
   commands:
     set_voltage:
       type: write
       sdk_call:
         method: "setDCVoltage"
         args_map:
           channel: "channel"
           value: "volts"
   ```
   Effort: 2-4 hours per instrument, requires understanding each vendor SDK.

3. **Don't convert these instruments:** Acknowledge that Labber's value for these instruments was 100% in the Python driver, not the INI. The INI conversion adds no value. Remove the hollow profiles to avoid false advertising and direct users to the ProxySDKCall path.

**Recommendation:** Option 3 (remove hollow profiles) immediately. Option 2 for the highest-priority instruments (AlazarTech, QDevil QDAC, Keysight PXI) on the roadmap. Option 1 as the general escape hatch.

### 4.2 VECTOR/VECTOR_COMPLEX Data Types
**Effort: Schema change + proto change**

1. Add `vector` and `vector_complex` to `ReturnConfig.type`
2. Add `x_name`, `x_unit`, `t0`, `dt` fields to `ReturnConfig`
3. Add an `ArrayDataPoint` message to the proto with `repeated double values`, `double t0`, `double dt`, `string x_unit`
4. Modify `MeasurementDataPoint` or add a separate `TraceDataPoint` message
5. Implement binary block transfer parsing in the command handler

This is a platform-level change affecting the proto, profile schema, command handler, and cloud backend.

### 4.3 Hardware Sweep Support
**Effort: Schema change + engine feature**

1. Add `sweep_cmd`, `stop_cmd`, `sweep_check_cmd` fields to `CommandConfig`
2. Add a `sweep_rate` parameter to `ExecuteCommandRequest`
3. Implement a sweep executor in the Python engine that:
   - Sends the sweep command with rate substitution
   - Polls the check command until idle
   - Supports stop/abort

### 4.4 Non-SCPI Response Parsing
**Effort: Schema change (small) + per-profile work**

1. Add a `response_parser` field to `CommandConfig` or `ReturnConfig`:
   ```yaml
   returns:
     type: float
     parser:
       type: regex
       pattern: ".*:([\\d.]+)[A-Za-z]*$"
       group: 1
   ```
   Or:
   ```yaml
   returns:
     type: float
     strip_prefix: true
     strip_suffix: "K"
   ```
2. Implement a pluggable response parser in the command handler
3. Update converted profiles with appropriate parsers

### 4.5 Interface Type Fix
**Effort: Manual profile editing (small)**

For each affected profile, correct the interface type:
- PXI instruments: `type: pxi` (add PXI to `InterfaceConfig.type` enum)
- BlueForsLogging: `type: none` or remove the instrument entirely
- QDevil QDAC: `type: serial` with baud rate
- Add serial settings to `InterfaceConfig` or `SettingsConfig`: `baud_rate`, `parity`, `data_bits`, `stop_bits`

### 4.6 Model Variant Support
**Effort: Schema change (medium)**

1. Add `model_variants` section to profile schema:
   ```yaml
   variants:
     "9870":
       commands:
         pre_trig_samples: { ... }
         ch1_range: { ... }
     "9373":
       excluded_commands: ["pre_trig_samples", "ch1_range"]
   ```
2. Or simpler: split multi-model drivers into separate profiles (one per model)

**Recommendation:** Split into separate profiles. Simpler, clearer, no schema change.

### 4.7 Combo Display Labels
**Effort: Schema change (small)**

Add a `map` field to enum parameters:
```yaml
params:
  value:
    type: enum
    options: ["Off", "31.6 uA", "100 uA"]
    map:
      "Off": "0.0"
      "31.6 uA": "0.0316"
      "100 uA": "0.1"
```

The Yokogawa GS200 hand-written profile already uses a `map` field on `output_state`. This pattern exists; it just needs to be applied to converted profiles.

### 4.8 Conditional Visibility (state_quant)
**Effort: Schema change (medium)**

Add `depends_on` to `CommandConfig`:
```yaml
commands:
  trig_slope:
    depends_on:
      parameter: trig_source
      values: ["Channel 1", "Channel 2", "External"]
```

This requires the UI (cloud frontend, Jupyter tab-completion) to evaluate dependencies at display time.

### 4.9 Init/Final Commands
**Effort: Schema change (small)**

Add to `SettingsConfig`:
```yaml
settings:
  init_commands: ["Q4"]
  cleanup_commands: ["C0"]
```

And execute them in the instrument connection lifecycle.

### 4.10 Groups/Sections
**Effort: Schema change (small, low priority)**

Add `group` and `section` to `CommandConfig`:
```yaml
commands:
  t1:
    group: Temperatures
    section: Temperature
```

### 4.11 Hardware Triggering
**Effort: Engine feature (large)**

1. Add `support_arm` and `support_hardware_loop` to profile schema
2. Add `ArmInstrument` and `WaitForTrigger` RPCs to the proto
3. Implement a trigger/arm lifecycle in the Python engine

This is a significant architectural addition and should be tied to the digitizer/AWG roadmap.

---

## 5. Specific Instrument Concerns

### 5.1 AlazarTech Digitizer (CRITICAL)
**Converted profile:** Empty shell -- zero commands.
**What a customer would experience:** Instrument detected by IDN pattern matching but completely inoperable. Cannot configure acquisition, cannot arm, cannot read traces. The entire driver is a C SDK wrapper; there is no SCPI fallback.
**Customer segment:** Quantum computing labs (signal readout for transmon qubits).
**Required fix:** ProxySDKCall integration with the AlazarTech C SDK, or hand-written SDK-mapped profile.

### 5.2 QDevil QDAC (CRITICAL)
**Converted profile:** Empty shell -- zero commands (despite the original INI being 7,720 lines).
**What a customer would experience:** Instrument unrecognized (serial interface, not GPIB). Even if connected, no commands available. Cannot set voltages on any of the 24/48 channels.
**Customer segment:** Quantum computing labs (DC bias for qubit tuning, gate voltage control).
**Required fix:** Vendor SDK (`qdac.py`) integration via ProxySDKCall or hand-written profile with SDK call mappings.

### 5.3 BlueFors Logging (CRITICAL)
**Converted profile:** Empty shell -- zero commands, wrong interface type.
**What a customer would experience:** Connection failure (tries GPIB, should be file-based). No temperature or pressure readings. This is a purely file-reading "instrument" with no VISA communication at all.
**Customer segment:** Every dilution refrigerator lab using BlueFors (very common in quantum).
**Required fix:** Custom driver implementation (file reader) or remove from profile set.

### 5.4 Triton Dilution Fridge (HIGH)
**Converted profile:** Has commands for temperature reading and magnet control, but:
- Temperature response parsing is wrong (returns `STAT:DEV:T1:TEMP:SIG:TEMP:1.234K` raw)
- Magnet vector commands are broken (read-modify-write pattern not expressible)
- Sweep commands missing (no controlled ramp, risk of magnet quench)
- Option detection missing (presents magnets that may not be installed)
- `<c>` placeholder in control loop commands not resolved (the Python iterates T1-T13 to find the active loop)
**What a customer would experience:** Temperature readback returns unparsed strings. Magnet field setting sends wrong commands (individual axis without composing the full vector). Sweeps jump instantly instead of ramping.
**Customer segment:** Cryogenics / condensed matter / quantum labs.

### 5.5 Oxford IPS120 (HIGH)
**Converted profile:** Has B and SweepRate commands but:
- Missing init command `Q4` (sets response format)
- Missing control mode handshake (`C3` before set, `C2` after)
- Missing go-to-setpoint command (`A1`)
- Missing wait-for-target polling loop
- Response parsing wrong (returns `R+00.0000`, needs `[1:]` stripping)
**What a customer would experience:** Setting B field sends `J{value}` but the magnet ignores it because it is not in remote control mode and never receives the "go" command. Reading B returns `R+00.0000` as a string instead of `0.0` as a float.
**Customer segment:** Condensed matter / materials science labs.

### 5.6 Keysight PXI AWG/Digitizer/HVI/LO (HIGH - 4 instruments)
**Converted profiles:** Empty shells. All use `use_visa = False` with `interface: PXI` and custom Python wrapping the Keysight SD1 SDK.
**What a customer would experience:** Connection failure (GPIB interface on a PXI instrument). No commands available.
**Customer segment:** Quantum computing labs (signal generation and readout).

### 5.7 NI DAQ / NI USB-6218 (HIGH)
**Converted profiles:** Empty shells. Entire driver is a wrapper around PyDAQmx (NI-DAQmx C library).
**What a customer would experience:** Connection failure. No analog input/output capability.
**Customer segment:** General-purpose lab automation, teaching labs.

### 5.8 Keysight M8195 AWG (MEDIUM-HIGH)
**Converted profile:** Has basic SCPI commands from the INI but missing:
- Waveform upload (VECTOR data type via Python binary transfer)
- Memory management and sequencing logic
- Multi-channel configuration state machine
**What a customer would experience:** Can set basic parameters (sample rate, etc.) but cannot upload waveforms -- the core function of an AWG.

### 5.9 LabBrick LMS/LSG Synthesizers (MEDIUM)
**Converted profiles:** Empty shells. These use a USB HID interface via vendor DLL, not VISA.
**What a customer would experience:** Connection failure. Cannot set frequency or power.
**Customer segment:** Microwave labs, quantum computing (qubit drive/readout).

### 5.10 MultiQubit Pulse Generator (CRITICAL for quantum)
**Converted profile:** Not in the 42-profile set (it may have been excluded), but worth calling out. This is a 13-file Python package implementing Clifford gates, randomized benchmarking sequences, cross-talk compensation, and pulse predistortion. It is a pure software instrument with zero hardware communication. If converted, it would be an empty shell. If a quantum computing lab expects this capability from Galois, they need to be told it is out of scope for YAML profiles.

---

## 6. Summary Metrics

| Metric | Count |
|---|---|
| Total instruments converted | 42 |
| Profiles with zero instrument-specific commands | ~16 (empty shells) |
| Profiles with commands but broken parsing | ~8 (Oxford, Triton, Cryomagnetics, etc.) |
| Profiles with commands that work correctly as-is | ~18 (SCPI-compliant instruments) |
| Profiles with wrong interface type | ~10 |
| Instruments requiring Python/SDK logic | ~35 (83%) |
| Instruments where YAML profile is fully sufficient | ~7 (17%) |

The fundamental insight: **Labber drivers are programs, not configurations.** The INI file is the input form; the Python file is the brain. Converting only the INI and discarding the Python is like converting a web application's HTML forms while discarding the backend server. For SCPI-compliant instruments with simple get/set commands (Agilent multimeters, Keithley SMUs, basic power supplies), this works. For anything requiring protocol translation, state management, binary data handling, or vendor SDK access, it produces an attractive but inert profile.

---

## 7. Recommendations

1. **Immediately remove or quarantine** the ~16 empty-shell profiles. They create a false impression of instrument support and will frustrate customers who discover they are non-functional.

2. **Audit the ~8 profiles with broken parsing** (Oxford, Triton, Cryomagnetics). Either fix them with response parsers and init commands, or downgrade them to "partial support" with clear documentation.

3. **Prioritize schema additions** in this order: (a) response parser/non-SCPI support, (b) init/final commands, (c) combo label maps, (d) sweep support.

4. **Use the ProxySDKCall path** for SDK-dependent instruments rather than trying to encode SDK logic in YAML.

5. **Hand-write high-quality profiles** for the most common instruments rather than relying on automated conversion. The Yokogawa GS200 and Keithley 2400 hand-written profiles demonstrate the quality bar -- 600-900 lines with complete SCPI coverage, proper parameter types, sequences, and status register definitions. The auto-converted profiles are 25-80 lines of boilerplate.

