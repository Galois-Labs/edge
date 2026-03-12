# Architecture Extensions: Labber Compatibility & Safety

## Context

Forty-two Labber instrument drivers (MIT licensed) were auto-converted from INI+Python format to Galois YAML profiles. The conversion revealed critical gaps in our architecture — most notably the lack of hardware sweep support (safety-critical for superconducting magnets), vector/trace data types, non-SCPI response parsing, and several schema gaps. See `labbertoyaml.md` for the full audit.

This document proposes concrete architectural extensions to close these gaps. The designs were developed collaboratively with Gemini 3 Pro (Google) and refined against the actual codebase.

---

## Gemini 3 Pro Recommendations

### A & E. Sweep/Ramp & Safety Architecture (Critical)

**Core Principle:** Sweeps must execute entirely on the Python edge daemon. If the Tailscale link drops halfway through a magnet ramp, the daemon must continue the controlled ramp to the setpoint or safely hold. We cannot rely on the cloud or Jupyter sending incremental SCPI steps.

#### 1. Schema Extension (YAML)

Add a `sweep` block to the command definition and a safety flag:

```yaml
commands:
  b_field_x:
    type: write
    setter: "SET:SYS:VRM:RVST:MODE:ASAP:VSET:[{value}]"  # Instant jump (unsafe)
    requires_sweep: true  # SAFETY INTERLOCK
    sweep:
      rate_param: "sweep_rate"
      command: "SET:SYS:VRM:RVST:MODE:RATE:RATE:{sweep_rate}:VSET:[{value}]"
      check_command: "READ:SYS:VRM:ACTN"
      check_idle_match: "IDLE"  # Regex or exact match to indicate completion
      stop_command: "SET:SYS:VRM:ACTN:HOLD"
      poll_interval_ms: 1000
```

#### 2. Proto Extensions (`edge.proto`)

Do not overload `ExecuteCommand`. Sweeps are long-running stateful operations. Add three new RPCs:

```protobuf
// Add to EdgeDaemonService
rpc StartSweep(StartSweepRequest) returns (StartSweepResponse);
rpc GetSweepStatus(GetSweepStatusRequest) returns (SweepStatusResponse);
rpc StopSweep(StopSweepRequest) returns (StopSweepResponse);

message StartSweepRequest {
  string instrument_id = 1;
  string command_name = 2;
  double target_value = 3;
  double sweep_rate = 4;
  map<string, string> extra_parameters = 5;
}

message StartSweepResponse {
  string sweep_id = 1;
  bool accepted = 2;
  string error = 3;
}

message GetSweepStatusRequest {
  string sweep_id = 1;
}

message SweepStatusResponse {
  string sweep_id = 1;
  string status = 2;         // "sweeping", "completed", "error", "aborted"
  double current_value = 3;  // If the instrument supports reading during sweep
  double target_value = 4;
  double sweep_rate = 5;
  string error = 6;
}

message StopSweepRequest {
  string sweep_id = 1;
}

message StopSweepResponse {
  bool success = 1;
  string status = 2;  // "holding", "stopped"
}
```

#### 3. Edge Daemon Logic & Safety Implementation

- **Safety Interlock:** In `grpc_server.py`'s `ExecuteCommand` handler, if the resolved command has `requires_sweep == true`, immediately reject the request with a descriptive error. The user *must* use `StartSweep`.

- **Execution:** Create an `asyncio` task in the daemon that:
  1. Sends the `sweep.command` with the substituted rate and target.
  2. Enters a `while not completed:` loop, polling `sweep.check_command` every `poll_interval_ms`.
  3. Checks the response against `check_idle_match` to determine completion.
  4. Handles cancellation (via `StopSweep` RPC) by catching `asyncio.CancelledError` and immediately firing `sweep.stop_command`.

- **Network Resilience:** Once `StartSweep` is accepted, the sweep task runs entirely on the edge daemon as an autonomous `asyncio` task. The cloud can poll `GetSweepStatus` or request `StopSweep`, but the edge continues the ramp to target if the network disconnects. This is mandatory for safety.

---

### B. Vector/Trace Data (Critical)

**The Strategy:** Oscilloscope traces are typically IEEE 488.2 binary blocks. Sending them as comma-separated strings over gRPC is highly inefficient and loses axis metadata. We need a structured type.

#### 1. Proto Extensions (`edge.proto`)

Add a dedicated message for array data:

```protobuf
message VectorData {
  repeated double y_values = 1;  // Or bytes for raw binary
  double x_start = 2;
  double x_increment = 3;
  string x_unit = 4;
  string y_unit = 5;
  string x_name = 6;
}

// Update ExecuteCommandResponse
message ExecuteCommandResponse {
  // ... existing fields ...
  VectorData vector_data = 7;  // Populated if the command returns a vector
}
```

#### 2. Schema Extension (YAML)

```yaml
returns:
  type: vector
  format: ieee_binary  # Tells the edge daemon to use query_binary_values()
  x_name: "Time"
  x_unit: "s"
  y_unit: "V"
  # Optional: commands to fetch the axis metadata if not static
  x_start_query: "WAV:XOR?"
  x_increment_query: "WAV:XINC?"
```

#### 3. Edge Daemon Logic

In `command_handler.py`, the `_execute_locked` method currently hardcodes `self._instruments.query(...)` which returns a string. PyVISA has a dedicated `query_binary_values()` method for traces. The `CommandHandler` needs to know the expected return type. If `type == vector`, it should:
1. Fetch x-axis metadata queries (if defined in the profile)
2. Call `query_binary_values()` instead of `query()`
3. Package the result into the `VectorData` protobuf message

The Jupyter-side proxy generator would return a numpy array with metadata:
```python
trace = scope.get_waveform(channel=1)
# trace.data → numpy array
# trace.x_start, trace.x_increment → axis info
```

---

### C. Non-SCPI Response Parsing (High)

**The Strategy:** Keep it lightweight. A pluggable regex/strip pipeline executed right after the VISA read handles 99% of proprietary formats.

#### 1. Schema Extension

```yaml
# Oxford Triton — extract float from "STAT:DEV:T1:TEMP:SIG:TEMP:1.234K"
returns:
  type: float
  parser:
    type: regex
    pattern: ".*:([\\d\\.\\+\\-]+)[A-Za-z]*$"
    group: 1

# Oxford IPS120 — strip "R" prefix from "R+00.0000"
returns:
  type: float
  parser:
    type: strip
    prefix: "R"
```

#### 2. Edge Daemon Logic

In `command_handler.py`, right after `response = self._instruments.query(instrument_id, scpi_cmd)`, pass the raw string through a `ResponseParser` utility class that applies the schema's regex/strip rules before returning it to `grpc_server.py`.

---

### D. Schema Extensions (Init/Cleanup, Conditional Visibility, Combo Labels)

#### 1. Init/Cleanup Commands

Add `init_commands: [str]` and `cleanup_commands: [str]` to the YAML `settings` block:

```yaml
settings:
  timeout_ms: 5000
  init_commands: ["Q4"]
  cleanup_commands: ["C0"]
```

**Implementation:** In the Python edge daemon, when `InstrumentManager.connect()` is called, iterate and execute `init_commands`. Execute `cleanup_commands` on daemon shutdown or explicit disconnect.

#### 2. Combo Label Maps

Modify the `CommandParameter` proto to use a map instead of a list:

```protobuf
message CommandParameter {
  // ...
  // Change from: repeated string enum_values = 6;
  map<string, string> enum_options = 6;  // e.g., {"Cartesian": "CART", "Cylindrical": "CYL"}
}
```

Update the YAML schema to match:

```yaml
params:
  heater_range:
    type: enum
    options:
      "Off": "0.0"
      "31.6 uA": "0.0316"
      "100 uA": "0.1"
```

The Jupyter notebook UI will display the keys ("31.6 uA") and send the values ("0.0316") via the `ExecuteCommand` RPC. The edge daemon requires zero logic changes for this — it just passes the string it receives.

#### 3. Conditional Visibility (`state_quant`)

Add a `visibility` block to the YAML `CommandConfig`:

```yaml
commands:
  b_field_x:
    visibility:
      depends_on: "coord_sys"
      equals: "Cartesian"
```

**Implementation:** This is purely a UI/Frontend concern. Pass this metadata up through the `GetCapabilitiesResponse` proto. The Jupyter PyVISA backend/UI dynamically hides parameters based on this metadata. Do not enforce this in the edge daemon to keep the daemon stateless and fast.

---

### Gemini's Explicit Recommendations on What NOT to Do

1. **Don't try to build a Turing-complete YAML DSL.** SDK-only instruments (AlazarTech, QDevil QDAC, BlueFors, NI DAQ) should use `ProxySDKCall`, not increasingly complex YAML schemas.

2. **Don't enforce conditional visibility on the edge.** It's a UI concern — keep the daemon stateless.

3. **Don't overload `ExecuteCommand` for sweeps.** They're fundamentally different (long-running, stateful, abortable) and deserve their own RPCs.

---

## Claude's Assessment & Additional Considerations

### On the Sweep Architecture

Gemini's design is correct in its fundamentals. Three additional considerations:

**1. The `requires_sweep` interlock needs a bypass for instruments that support both modes.**

Some power supplies (Yokogawa GS200, Keithley 2400) have both instant-set and ramped modes. The safety interlock should be per-command, not per-instrument. The current design handles this correctly since `requires_sweep` is on the command, not the instrument. But we should also consider a `--force` or `allow_instant: true` parameter on `ExecuteCommand` for cases where a researcher explicitly wants an instant jump (e.g., resetting to zero field with the magnet already at zero).

**2. Sweep progress reporting should stream, not poll.**

The `GetSweepStatus` RPC requires the cloud backend to poll repeatedly. A better fit for the existing architecture is a server-streaming RPC:

```protobuf
rpc StartSweep(StartSweepRequest) returns (stream SweepProgressUpdate);
```

This gives the notebook a live progress bar without polling overhead. The edge daemon emits updates each time it polls the instrument's check command. If the stream disconnects (network loss), the sweep continues autonomously — the stream is observational, not controlling.

However, `StopSweep` should remain a separate unary RPC since abort is a control action, not an observation. And `GetSweepStatus` is still useful for reconnecting to an in-progress sweep after network recovery. So all three RPCs have value — but `StartSweep` should return a stream.

**3. Multiple concurrent sweeps need consideration.**

A Triton has X, Y, Z magnets that might sweep simultaneously to reach a target vector field. The edge daemon needs a sweep task registry (map of sweep_id → asyncio.Task). The `StopSweep` RPC should support `sweep_id = "*"` to emergency-stop all active sweeps (magnet quench abort). This is a simple dictionary in the gRPC server — nothing architectural.

### On Vector/Trace Data

Gemini's `VectorData` message is the right approach. Two refinements:

**1. Use `bytes` for the raw data, not `repeated double`.**

Oscilloscope traces can be 10M+ points. Encoding as `repeated double` in protobuf is extremely wasteful (8 bytes per value + varint overhead). Instead:

```protobuf
message VectorData {
  bytes y_data = 1;            // Raw IEEE 754 doubles, little-endian
  string y_dtype = 2;          // "float64", "float32", "int16" etc.
  int32 y_length = 3;          // Number of points
  double x_start = 4;
  double x_increment = 5;
  string x_unit = 6;
  string y_unit = 7;
  string x_name = 8;
  double y_scale = 9;          // For int16 data: actual = raw * y_scale + y_offset
  double y_offset = 10;
}
```

On the pyvisa-galois side: `np.frombuffer(vector_data.y_data, dtype=vector_data.y_dtype)`. This is zero-copy and handles arbitrary trace sizes efficiently.

**2. The cloud backend relay needs to pass bytes through without parsing.**

The `kernel_proxy.go` handler for `ExecuteCommand` currently JSON-encodes the response for HTTP. For `VectorData`, it should base64-encode the `y_data` bytes and pass the metadata as JSON fields. The pyvisa-galois client decodes. This avoids the backend needing to understand binary instrument data.

### On Response Parsing

Gemini's regex/strip approach is correct and sufficient. One addition:

**Add a `split` parser type** for delimiter-based extraction (common in older instruments):

```yaml
returns:
  type: float
  parser:
    type: split
    delimiter: ","
    index: 0  # Take the first field from "1.234,5.678,9.012"
```

The full parser chain should be: `strip_prefix` → `strip_suffix` → `regex` → `split` → type cast. Multiple stages can be defined if needed, but for most instruments one stage suffices.

### On Init/Cleanup Commands

Gemini's approach is straightforward. One concern: **init commands should be idempotent and safe to re-send**. The `InstrumentManager` may reconnect after a transient VISA error, and init commands will re-execute. For the Oxford IPS120, `Q4` (set query format) is idempotent — safe. But some instruments have destructive init commands (like `*RST`). The schema should distinguish:

```yaml
settings:
  init_commands: ["Q4"]              # Sent on every connect
  first_connect_commands: ["*RST"]   # Sent only on first connect (not reconnect)
  cleanup_commands: ["C0"]
```

In practice, most instruments only need `init_commands`. The `first_connect_commands` is a nice-to-have that prevents surprises on reconnect during long experiments.

### On Conditional Visibility

Agree with Gemini: purely a UI concern, don't enforce on the edge. One nuance: the `visibility` metadata should support multiple conditions and both `equals` and `not_equals`:

```yaml
commands:
  b_field_x:
    visibility:
      - depends_on: "coord_sys"
        equals: ["Cartesian"]
      - depends_on: "x_magnet_installed"
        equals: ["true"]
```

This covers the Triton case where Bx only appears when coordinate system is Cartesian AND the X magnet is physically installed.

### On Combo Label Maps

Gemini's `map<string, string>` proto change is the right call. One note: the existing `yokogawa_gs200.yaml` profile already uses a `map` field on `output_state`. So the YAML-side convention exists — we just need to formalize it in the schema definition and update the proto to match. The converter script should be re-run against the Labber INI files with `combo_def_N` → key and `cmd_def_N` → value mapping.

### On What's Explicitly Out of Scope for YAML Profiles

Strongly agree with Gemini's "don't build a Turing-complete YAML DSL" position. The following instruments should NOT have YAML profiles and should be directed to `ProxySDKCall`:

- **AlazarTech Digitizer** — C SDK with DMA buffer management
- **QDevil QDAC** — Vendor serial protocol + SDK
- **BlueFors Logging** — File-based, no VISA at all
- **Keysight PXI family** — SD1 SDK, not VISA
- **NI DAQ / USB-6218** — PyDAQmx wrapper
- **LabBrick synthesizers** — USB HID via vendor DLL
- **MultiQubit PulseGenerator** — Pure software instrument

The current empty-shell profiles for these instruments should be removed. They create a false impression of support and will frustrate customers. Better to have no profile (which transparently falls through to raw SCPI or ProxySDKCall) than a profile that matches the instrument but provides no functionality.

### Implementation Priority

Based on safety criticality and customer impact:

| Priority | Extension | Effort | Why |
|----------|-----------|--------|-----|
| **P0** | Sweep RPCs + safety interlock | 1-2 weeks | Magnet quench prevention. Cannot ship Oxford/Triton profiles without this. |
| **P0** | Init/cleanup commands | 1 day | Oxford IPS120 is non-functional without `Q4` init. |
| **P0** | Response parser | 2-3 days | Oxford/Triton profiles return unparseable strings without it. |
| **P1** | Vector/trace data | 1 week | Oscilloscopes and spectrum analyzers can't return their primary data product. |
| **P1** | Combo label maps | 2 days | Usability issue for all converted Labber profiles with enum parameters. |
| **P2** | Conditional visibility | 3 days | UI clutter but no functional breakage. Schema change + frontend work. |
| **P2** | Remove empty-shell profiles | 1 day | Stops false advertising of unsupported instruments. |

### Safety Architecture Summary

| Layer | Mechanism |
|-------|-----------|
| **Profile schema** | `requires_sweep: true` declares dangerous parameters |
| **Edge daemon** | `ExecuteCommand` rejects commands marked `requires_sweep` |
| **Edge daemon** | `StartSweep` requires explicit `sweep_rate` parameter — no default |
| **Edge daemon** | Sweep task runs autonomously as async task — survives network loss |
| **Edge daemon** | `StopSweep` immediately fires `stop_command` (emergency abort) |
| **Edge daemon** | Sweep task registry supports `StopSweep("*")` for all-stop |
| **Cloud backend** | Proxy endpoints for sweep start/status/abort |
| **pyvisa-galois** | Proxy generator emits `set_b_field(value, sweep_rate=)` with rate as required arg |
| **pyvisa-galois** | Refuses to call sweep commands without explicit rate parameter |

---

*Document generated 2026-03-10. Based on analysis of daemon-clean, cloud, and Labber driver codebases.*
*Gemini 3 Pro (Google) consulted for architectural strategy. Claude Opus 4.6 (Anthropic) provided refinements and implementation assessment.*
