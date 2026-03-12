# Agent Findings: Labber Profile Migration — Full Investigation

**Date:** 2026-03-10
**Scope:** Cross-codebase investigation of `~/work/galois/daemon-clean/` and `~/work/galois/cloud/`
**Method:** Five parallel investigation agents (one per tier), each tracing code paths across both repos

---

## Table of Contents

1. [Pre-Existing Blockers](#pre-existing-blockers)
2. [Tier 0: Profile-Only Fixes](#tier-0-profile-only-fixes)
3. [Tier 1: Small Schema + Engine Changes](#tier-1-small-schema--engine-changes)
4. [Tier 2: Medium Schema + Engine Features](#tier-2-medium-schema--engine-features)
5. [Tier 3: Proto + Platform Changes](#tier-3-proto--platform-changes)
6. [Tier 4: SDK Integration](#tier-4-sdk-integration)
7. [Cross-Tier Dependency Map](#cross-tier-dependency-map)
8. [Unified Implementation Order](#unified-implementation-order)

---

## Pre-Existing Blockers

Six bugs/gaps discovered during investigation that must be fixed before any tier work ships.

### Bug 1: `format_command()` Does Not Exist on Daemon `CommandConfig`

- **File:** `daemon-clean/src/galois_edge/capability_manager.py` line 383
- **Issue:** Calls `cmd.format_command(params, is_query)` but the daemon's `CommandConfig` (plain dataclass in `profile_schema.py`) only defines `format_scpi()`. The cloud's Pydantic `CommandConfig` defines `format_command()`.
- **Impact:** `AttributeError` on every `ExecuteCommand` RPC for non-SDK instruments. The daemon and cloud codebases have diverged.
- **Fix:** Rename call to `cmd.format_scpi(params, is_query)`.

### Bug 2: `.instrument_class.value` on Plain String

- **File:** `daemon-clean/src/galois_edge/capability_manager.py` line 83
- **Issue:** `self.profile.instrument.instrument_class.value` — `.value` is only valid on an Enum, but the daemon's `InstrumentMetadata.instrument_class` is a plain `str`.
- **Impact:** `AttributeError` on instrument class lookup.
- **Fix:** Remove `.value` — use `self.profile.instrument.instrument_class` directly.

### Bug 3: `SDKConfig` Dataclass is Incomplete

- **File:** `daemon-clean/src/galois_edge/profile_schema.py`
- **Issue:** `SDKConfig` only has 4 fields (`package`, `import_path`, `class_name`, `is_async`). The `sdk_executor.py` accesses `sdk_config.connect.constructor_args`, `sdk_config.connect.method`, `sdk_config.disconnect.method`, `sdk_config.identity` — all of which raise `AttributeError`.
- **Reference:** The cloud's Pydantic `profile_schema.py` has the full schema: `SDKConnectConfig`, `SDKDisconnectConfig`, `SDKIdentityConfig`.
- **Impact:** No SDK instrument can connect. The `quantum_design_ppms.yaml` reference profile would crash at connection time.
- **Fix:** Port `SDKConnectConfig`, `SDKDisconnectConfig`, `SDKIdentityConfig` dataclasses from cloud to daemon, extend `SDKConfig`, update `profile_from_dict()`.

### Bug 4: SDK Connection Not Wired

- **File:** `daemon-clean/src/galois_edge/main.py`
- **Issue:** Nothing in the scan/registration flow calls `sdk_executor.connect()`. VISA instruments connect lazily via `instrument_manager.connect()`, but SDK instruments have no parallel path.
- **Impact:** Even with a correct SDK profile, the instrument would never connect.
- **Fix:** In `_try_match_profile()`, after profile match, if `profile.sdk` is not None, call `sdk_executor.connect(instrument_id, profile.sdk, runtime_args)`.

### Bug 5: `stream.go` Timestamp Panic (Cloud)

- **File:** `cloud/backend/internal/service/stream.go`
- **Issue:** References `point.Timestamp.AsTime()` on a `MeasurementDataPoint` field that does not exist in the proto. The hand-written Go stub (`edge_grpc.go`) has phantom fields (`Timestamp`, `RawData`, `Metadata`) not present in the actual `edge.proto`. The daemon sends `TimestampMs` (int64), never `Timestamp`.
- **Impact:** Will panic at runtime when streaming is used.
- **Fix:** Update `stream.go` to use `point.TimestampMs` (int64) instead of `point.Timestamp.AsTime()`.

### Bug 6: gRPC Max Receive Size Asymmetry (Cloud)

- **File:** `cloud/backend/internal/grpcclient/manager.go`
- **Issue:** Cloud Go gRPC client has no explicit `grpc.WithDefaultCallOptions(grpc.MaxCallRecvMsgSize(...))`. Default is 4 MB. The daemon sets `grpc.max_send_message_length = 50 MB`.
- **Impact:** Any response over 4 MB (e.g., a 512k-point trace) silently fails with `ResourceExhausted`.
- **Fix:** Add `grpc.WithDefaultCallOptions(grpc.MaxCallRecvMsgSize(64 * 1024 * 1024))` to Go client dial options.

---

## Tier 0: Profile-Only Fixes

### Finding 0.1: Interface Type Is Metadata-Only

`InterfaceConfig.type` is stored but **never dispatched on** in either codebase. The daemon routes connections based on the VISA address string format (`GPIB0::...`, `TCPIP::...`, `ASRL::...`), not the profile's interface type. The field is purely informational.

- **Daemon:** No validation — accepts any string.
- **Cloud:** Strict Pydantic `InterfaceType` enum: only `gpib | usb | ethernet | serial`. Using `pxi` or `none` silently drops the profile at load time.
- **Conclusion:** Changing interface types is safe (no behavioral change), but `pxi`/`none` would break cloud validation.

**Profiles with wrong types:**

| Profile | Current | Correct | Notes |
|---------|---------|---------|-------|
| `keysight_pxi_lo.yaml` | `gpib` | `ethernet` or remove | PXI chassis, not GPIB |
| `triton.yaml` | `gpib` | `ethernet` | Oxford Triton uses TCP/IP |
| `cryomagnetics_lm510.yaml` | `gpib` | `gpib` | Actually correct |
| `oxford_ips120.yaml` | `gpib` | `gpib` | Actually correct |

### Finding 0.2: Cloud Has Separate Profile Copies

The cloud edge at `~/work/galois/cloud/edge/galois_edge/profiles/` has its **own copy** of profiles (~80 standard ones). The Labber-converted profiles (`triton.yaml`, `oxford_ips120.yaml`, etc.) do not exist there. The Go backend ignores profiles entirely. Daemon profile changes have zero cloud impact today.

### Finding 0.3: Profiles Failing Cloud-Side Validation

Even without our changes, several profiles already fail the cloud's Pydantic validation:
- `keysight_pxi_lo.yaml`: `class: instrument` — not a valid `InstrumentClass` enum value
- `oxford_ips120.yaml`: `class: magnet_controller` — not in cloud enum
- Any profile with non-standard interface types

### Finding 0.4: Unresolved `<c>` Placeholders in Triton

Three commands in `triton.yaml` have literal `<c>` in SCPI strings (`controlloop`, `tset`, `heaterrange`). The `format_scpi()` method only substitutes `{key}` syntax — angle-bracket `<c>` is never replaced. The literal string `READ:DEV:T<c>:TEMP:LOOP:MODE` would be sent to the instrument, which rejects it.

**Fix options:**
- **Option A (YAML-only):** Split into per-channel commands (`controlloop_t5`, `tset_t5`, etc.)
- **Option B (param change):** Rename `<c>` to `{channel}`, add `channel` param — this changes the API contract

### Finding 0.5: `force_query` Requires Code Changes

The daemon passes `force_query=request.is_query` to the command handler, so it works **if the caller sends `is_query=True`**. But there's no per-command flag in the profile to force it automatically. Oxford IPS120's `R7` getter doesn't end with `?` — if a caller sends `is_query=False` (default), the handler does `write("R7")` → `"OK"` instead of `query("R7")` → `"R+00.0000"`.

### Finding 0.6: `map` Field Used in 43 Profiles, Silently Dropped

**153 `map:` entries across 43 YAML profiles** are silently ignored at load time. The `ParameterConfig` dataclass has no `map` field. The `_build_parameter_config()` function doesn't read it. The cloud Pydantic model already has `map: Optional[Dict[str, Any]]` — cloud is ahead, but cloud's `format_command()` also doesn't apply the map. **Same bug in both codebases.**

Profiles affected include: `yokogawa_gs200.yaml`, `srs_cs580.yaml`, `keithley_2450.yaml`, `keysight_e3631a.yaml`, `rigol_dp800.yaml`, and ~38 others.

---

## Tier 1: Small Schema + Engine Changes

### Finding 1a: `map` Field Execution Path

**Current flow for `ExecuteCommand` with `{"state": "ON"}`:**

1. `grpc_server.py:ExecuteCommand` extracts `params = dict(request.parameters)` → `{"state": "ON"}`
2. Calls `capability_manager.resolve_command(instrument_id, "output_state", params, is_query=False)`
3. `capability_manager.py` calls `cmd.format_command(params, is_query)` → **CRASHES** (Bug 1 above)
4. Assuming fix to `format_scpi()`: returns `":OUTPut[:STATe] ON"` — raw string substituted
5. `command_handler.py` sends `write(":OUTPut[:STATe] ON")` to instrument

**Problem:** The instrument expects `":OUTPut[:STATe] 1"` (the mapped wire value), not `"ON"` (the display label). The map transformation never happens.

**Design challenge:** `format_scpi()` receives `Dict[str, Any]` (raw values) but has no access to `ParameterConfig` objects to look up the map. Fix: pass `self.params` into the substitution loop so it can check `param_config.map[value]` before substituting.

**Reverse direction:** When reading back, instrument returns `"1"` but the user might expect `"ON"`. This needs a reverse-map derived from inverting `ParameterConfig.map`. Applied in `grpc_server.py` after `execute_command()` returns but before building the proto response.

**StreamMeasurement impact:** The streaming loop does `float(raw)` — if the raw response is a mapped enum label, float conversion fails silently (yields `0.0`). Must apply reverse-map or handle gracefully.

**Proto changes for Tier 1:** Not required. The map is purely an engine concern. The UI only needs option labels (already in `enum_values`). If the cloud should display mapped values, `CommandParameter` would need a `map<string, string> value_map = 8` field — but that's a follow-on.

**Files requiring changes:**
1. `profile_schema.py` — Add `map: Optional[Dict[str, str]] = None` to `ParameterConfig`; update `_build_parameter_config()`; extend `format_scpi()` to apply map; add reverse-map helper
2. `grpc_server.py` — `ExecuteCommand`: apply reverse-map on response; `StreamMeasurement`: apply reverse-map before `float()`
3. Tests: `test_profile_loader.py`, `test_grpc_server.py`, new `test_profile_schema.py`

### Finding 1b: `init_commands` / `cleanup_commands`

**Current state:** `SettingsConfig` has only `timeout_ms`, `terminator`, `opc_query`. No lifecycle command fields in either codebase. No YAML profile uses `init_commands` or `cleanup_commands` — grep returned zero matches.

**Insertion points identified:**

| Lifecycle Event | Insertion Point | Method |
|----------------|----------------|--------|
| **Init** | `main.py:_try_match_profile()` after profile match, before capability registration | `command_handler.execute_command()` per init command |
| **Cleanup** | `main.py:stop()` after gRPC stops, before `disconnect_all()` | Iterate registered instruments, send cleanup commands |
| **Reconnect** | `_periodic_rescan()` detects reconnected instrument as `new_resource` → `_try_match_profile()` | Init commands re-sent automatically (correct behavior) |
| **Lost instrument** | Instrument already gone | Cannot send cleanup (acceptable) |
| **Signal handler** | `SIGINT`/`SIGTERM` → `daemon.stop()` | Routes through cleanup path |

**Challenge:** `CommandHandler` reference isn't currently accessible in `_try_match_profile()` — needs plumbing through `self._command_handler`.

**Files requiring changes:**
1. `profile_schema.py` — Add `init_commands: Optional[List[str]] = None`, `cleanup_commands: Optional[List[str]] = None` to `SettingsConfig`; update builder
2. `main.py` — `_try_match_profile()`: dispatch init commands; `stop()`: dispatch cleanup commands
3. Tests: profile loading, lifecycle mock tests

### Finding 1c: `response_parser` on `ReturnConfig`

**Current parsing behavior:**

| Code Path | Parsing | Location |
|-----------|---------|----------|
| `ExecuteCommand` | **None** — raw string goes directly to `data=result["response"]` | `grpc_server.py:658` |
| `StreamMeasurement` | `float(raw.split(separator)[0].strip())` with ValueError fallback to `0.0` | `grpc_server.py:984` |
| Transport layer | PyVISA `.strip()` on query response | `instrument_manager.py:501` |

**Where parser plugs in:** After `execute_command()` returns but before building the proto response. The `CommandConfig` / `ReturnConfig` is already in scope in both code paths.

**Design:** Add `response_parser: Optional[str] = None` (regex pattern with capture group) to `ReturnConfig`. Add `parse_response(raw: str) -> str` method that applies regex, falls back to raw string on no match.

**Examples that would be fixed:**
- Triton: `STAT:DEV:T1:TEMP:SIG:TEMP:1.234K` → regex `([\\d.]+)[A-Za-z]*$` → `1.234`
- Oxford IPS120: `R+00.0000` → regex `[A-Za-z]([+\\-\\d.]+)` → `+00.0000`

**Files requiring changes:**
1. `profile_schema.py` — Add `response_parser` to `ReturnConfig`; add `parse_response()` helper; update `_build_return_config()`
2. `grpc_server.py` — `ExecuteCommand`: apply parser; `StreamMeasurement`: apply parser before `float()`
3. Tests: parser unit tests, integration tests

### Finding 1d: `force_query` on `CommandConfig`

**Current `is_query` flow:** `request.is_query` from gRPC → `resolve_command(is_query=...)` → selects getter/setter → `force_query=request.is_query` → `command_handler.py:139`: `is_query = force_query or scpi_cmd.strip().endswith("?")`.

**The gap:** No profile-level way to declare that a command always needs `force_query=True` regardless of caller. Adding `force_query: bool = False` to `CommandConfig` allows the profile to declare this.

**Implementation:** In `grpc_server.py:ExecuteCommand`, after `resolve_command()` returns a SCPI string, look up `caps.get_command(command_name).force_query` and OR it with `request.is_query` when passing to `execute_command()`.

**Files requiring changes:**
1. `profile_schema.py` — Add `force_query: bool = False` to `CommandConfig`; update `_build_command()`
2. `grpc_server.py` — Look up `force_query` from profile; OR with `request.is_query`
3. Tests: profile loading, execution path

### Tier 1: Implementation Order

```
Phase 0: Fix pre-existing blockers (Bug 1, Bug 2)
Phase 1: Schema layer (all profile_schema.py changes at once — no runtime deps)
Phase 2: Engine layer (grpc_server.py, main.py — depends on Phase 1)
Phase 3: Tests
```

**Cloud symmetry:** Cloud edge needs identical changes but uses Pydantic (stricter) and different proto service name (`SCPIService` vs `EdgeDaemonService`). Not copy-pasteable — structurally identical, syntactically different.

---

## Tier 2: Medium Schema + Engine Features

### Finding 2a: Sweep Support — Per-Instrument Lock Is Incompatible

**Critical architectural issue:** `CommandHandler` holds a `threading.Lock` for the entire duration of `execute_command()`. A magnet sweep runs 10-60 minutes. Consequences:
- Lock blocks ALL other commands to that instrument (status queries, abort)
- Sending `stop_cmd` would **deadlock** — needs the same lock the sweep holds
- `ThreadPoolExecutor(max_workers=10)` would have workers blocked indefinitely
- gRPC client timeout expires but VISA command keeps running

**Sequences can't model sweeps:** `SequenceConfig` is a linear list of steps with no loop/poll primitives.

**Required: New RPCs.** `StartSweep`, `GetSweepStatus`, `StopSweep` with an async task model:
- Sweep task acquires lock only per-VISA-transaction, releases between polls
- `StartSweep` returns immediately; sweep runs as autonomous `asyncio.create_task()`
- `StopSweep` cancels the task, acquires lock to fire `stop_command`
- `StopSweep("*")` all-stop for quench abort

**Files affected (14 files across both repos):**

| File | Change |
|------|--------|
| `proto/edge/v1/edge.proto` | Add `StartSweep`, `GetSweepStatus`, `StopSweep` RPCs + messages |
| `proto/gen/` (Go + Python) | Regenerated via `make proto` |
| `cloud/backend/internal/gen/proto/.../edge_grpc.go` | **Manual** Go struct additions |
| `profile_schema.py` | Add `SweepConfig` dataclass; `requires_sweep: bool` on `CommandConfig` |
| `grpc_server.py` | Sweep task registry; 3 new handlers; `ExecuteCommand` safety interlock |
| `capability_manager.py` | Surface `requires_sweep` in `to_capability_dict()` |
| `cloud/.../grpcclient/client.go` | Add sweep client methods |
| `cloud/.../handler/instrument.go` | Add HTTP endpoints to proxy sweep RPCs |
| `cloud/.../server/routes.go` | Register new routes |
| Oxford/magnet YAML profiles | Add `requires_sweep: true` + `sweep:` blocks |

**Risk:** Cloud sequences engine (`sequences/engine.go`) calls `ExecuteCommand` directly. If a sequence step involves a sweep-required command, it hits the safety interlock and fails. Sequences need a `StartSweep` step type.

### Finding 2b: Serial Interface Settings — Isolated to Daemon

**Current state:** `InterfaceConfig` has `type`, `port`, `default_address`. The `srs_cs580.yaml` profile uses `type: serial` with `port: 9600` (baud rate stored in port field by convention). No `baud_rate`, `parity`, `data_bits`, `stop_bits` fields.

`instrument_manager.py:connect()` calls `open_resource()` but never sets serial attributes. For pyvisa, ASRL resources require post-open configuration:
```python
resource.baud_rate = 9600
resource.parity = pyvisa.constants.Parity.none
resource.data_bits = 8
resource.stop_bits = pyvisa.constants.StopBits.one
```

**Gap:** There's no path to get `InterfaceConfig` into `InstrumentManager.connect()` — it only takes VISA address + timeout. The profile lookup → serial settings → connect path doesn't exist.

**Also:** `include_serial_ports` flag exists in `instrument_manager.py` (line 93, 198) but is never wired to config. `config.py` has no `include_serial_ports` env var.

**No proto changes needed. No cloud backend changes needed.**

**Files affected:**
1. `profile_schema.py` — Add `baud_rate`, `parity`, `data_bits`, `stop_bits` to `InterfaceConfig`
2. `instrument_manager.py` — Apply serial settings in `connect()` for ASRL addresses
3. `config.py` — Add `include_serial_ports: bool` env var
4. `main.py` — Wire serial settings through from profile to connect

### Finding 2c: Conditional Visibility — Full-Stack Metadata Propagation

Zero execution-path risk (engine doesn't enforce it), but requires changes across the entire stack:

```
YAML → CommandConfig → to_capability_dict() → proto CommandCapability
     → Go handler → JSON API → TypeScript CommandPanel.tsx
```

Also: `SequenceBuilder.tsx` renders instrument commands — needs visibility filtering too.

**Files affected (8 files):**
1. `profile_schema.py` — Add `VisibilityCondition` dataclass; `visibility` on `CommandConfig`
2. `edge.proto` — Add `VisibilityCondition` message on `CommandCapability`
3. Both buf pipelines — Regenerate
4. `edge_grpc.go` (cloud) — Manual stub update
5. `grpc_server.py` — Populate visibility in capabilities
6. `instrument.go` — Add visibility to JSON
7. `CommandPanel.tsx` — Filtering logic
8. `SequenceBuilder.tsx` — Also needs filtering

### Finding 2d: Proto Management Architecture

**Critical discovery:** The proto files are **NOT shared** between repos.
- Daemon owns `proto/edge/v1/edge.proto`, uses `buf generate`
- Cloud has a **hand-written Go stub** at `cloud/backend/internal/gen/proto/.../edge_grpc.go` tagged "DO NOT EDIT" but actually manually maintained
- Every proto change requires **two coordinated edits**
- No automated sync — repos can drift silently (proto3 ignores unknown fields)
- The Go stub already has phantom fields (`Timestamp`, `RawData`, `Metadata`) diverging from the proto

### Tier 2: Execution Order

```
Step 1: Serial settings      — daemon only, no proto, lowest risk
Step 2: Conditional visibility — proto + cloud + frontend, metadata only
Step 3: Sweep support         — most invasive, do last
```

---

## Tier 3: Proto + Platform Changes

### Finding 3a: Vector/Trace Data — Three Options Evaluated

| Option | Approach | Proto Impact | Recommendation |
|--------|----------|-------------|----------------|
| **1** | Extend `MeasurementDataPoint` with `trace_data`, `t0`, `dt`, `x_unit`, `x_name` (fields 8-12) | Backward compat (proto3 ignores new fields) | **Do this first** |
| **2** | New `TraceDataPoint` + `StreamTrace` RPC | Clean separation, largest surface area | Follow-on |
| **3** | JSON-encode array in `ExecuteCommandResponse.data` | Zero proto changes | **Not recommended** — lossy, breaks `ParseNumericResponse` |

**Binary block transfer — missing plumbing:**
- `command_handler.py` only does string `query()`/`write()`
- `instrument_manager.py` has `read_binary()` but only for USB
- No `query_binary_values()` for VISA/GPIB/LAN
- IEEE 488.2 `#<digits><count><data>` parsing doesn't exist in SCPI path
- Needs new `execute_binary_query()` in command handler

**Data volume / bandwidth:**
- 10k-point trace × 8 bytes = 80 KB per message (fine)
- 1M-point trace × 8 bytes = 8 MB (exceeds cloud's 4 MB default receive limit)
- Daemon allows 50 MB send — asymmetry is dangerous

**Database impact:**
- `dataset_records.data JSONB`: 10k-point trace ≈ 120 KB per record. For 1M-point traces, JSONB is inappropriate — need `BYTEA` or object storage
- `test_results.numeric_value DOUBLE PRECISION`: scalar only — vector results need new column/type
- Stream data (`measurement_streams`): RAM-only via `CircularBuffer(50)` — 50 traces × 80 KB = 4 MB in memory per stream

**Frontend impact:**
- `ChartWidget.tsx` uses Recharts `LineChart` expecting `point.value` (scalar)
- Waveform display requires entirely new component (x-y plot with t0/dt axes, zoom)
- `useSSE` hook keeps last 200 points — for traces, that's 200 full waveforms in React state (too much memory)

**ReturnConfig changes needed:**
- Add `t0`, `dt`, `x_name`, `x_unit` fields
- Already supports `type: "array"` and `type: "binary"` as valid values but they're unused

### Finding 3b: Hardware Trigger — ProxySDKCall as Viable Interim

**Polling model (`StreamMeasurement`) is fundamentally incompatible with trigger-based acquisition.** You cannot poll a trigger — you must block on it.

**ProxySDKCall interim path:**
- `arm_card()` → ProxySDKCall → returns immediately ✓
- `wait_for_trigger(timeout=60s)` → blocks in ThreadPoolExecutor → ties up 1 of 10 workers ✓
- `read_dma_buffers()` → returns `google.protobuf.Value` → 1M-point buffer ≈ 80 MB → **fails** ✗

**`sdk_executor.py` lock contention:** Per-client `threading.Lock` held during entire SDK call. A 60s trigger wait blocks all concurrent access to that instrument. Long-lived operations need a separate non-locking execution path.

**Proper support would need:**
```proto
rpc TriggerStream(TriggerStreamRequest) returns (stream TraceDataPoint);
```
Client sends one request; server blocks until trigger, then streams acquired data, re-arms, repeats. `ThreadPoolExecutor` sizing (currently `max_workers=10`) needs review — each armed digitizer consumes one thread indefinitely.

### Finding 3c: `stream.go` / `edge_grpc.go` Divergence Detail

The hand-written Go stub has non-standard fields on `MeasurementDataPoint`:
- `Timestamp *timestamppb.Timestamp` (not in proto — daemon sends `TimestampMs int64`)
- `RawData string` (not in proto)
- `Metadata map[string]string` (not in proto)

`service/stream.go` references `point.Timestamp.AsTime()` — will panic because `Timestamp` is never populated. Also references `point.RawData` and `point.Metadata` in the SSE JSON marshaling.

### Finding 3d: Files Affected (25+ files)

**For vector data (3a):**

| File | Change |
|------|--------|
| `daemon-clean/proto/edge/v1/edge.proto` | Add trace fields to `MeasurementDataPoint` |
| `cloud/proto/galois/edge/v1/edge.proto` | Same (manual sync) |
| `daemon-clean/proto/gen/` | Regenerated |
| `cloud/.../edge_grpc.go` | Manual stub update |
| `grpc_server.py` | Detect array return, call binary path, populate new fields |
| `command_handler.py` | Add `execute_binary_query()` |
| `instrument_manager.py` | Add `query_binary()` for VISA/GPIB/LAN |
| `profile_schema.py` | Add `t0`, `dt`, `x_name`, `x_unit` to `ReturnConfig` |
| `cloud/.../service/stream.go` | Fix marshaling, handle trace payloads |
| `cloud/.../grpcclient/manager.go` | Fix max receive message size |
| `cloud/.../handler/instrument.go` | Handle trace data variant |
| `cloud/.../db/migrations/` | Trace storage columns |
| `cloud/web/.../hooks/use-sse.ts` | `SSEDataPoint` interface update |
| `cloud/web/.../components/monitor/ChartWidget.tsx` | Waveform display mode |

**For hardware trigger (3b):**

| File | Change |
|------|--------|
| `edge.proto` (both repos) | New RPCs or ProxySDKCall documentation |
| `grpc_server.py` | `TriggerStream` handler or ProxySDKCall pattern docs |
| `sdk_executor.py` | Separate non-locking "wait" path |
| `cloud/.../service/stream.go` | CircularBuffer sizing review |
| `cloud/.../handler/stream.go` | New `type: "trigger"` stream option |
| `cloud/web/.../pages/Monitor.tsx` | New UI for arm/trigger workflow |

### Tier 3: Recommended Sequencing

```
Pre-work (do NOW, independent of Tier 3):
  1. Fix stream.go Timestamp panic
  2. Fix grpcclient/manager.go max message size

3a-phase-1: Extend MeasurementDataPoint (backward compat)
  → Daemon: proto + schema + binary query + streaming handler
  → Cloud: stub update + stream.go + SSE marshaling
  → Frontend: waveform chart component

3a-phase-2: StreamTrace RPC (clean separation, follow-on)

3b-interim: Document ProxySDKCall arm/wait pattern
  → Fix sdk_executor.py lock for long-running ops

3b-proper: TriggerStream RPC (after trace data stable)
```

---

## Tier 4: SDK Integration

### Finding 4a: SDK Executor Architecture

`SDKExecutor` is vendor-agnostic:
1. Uses `importlib.import_module(sdk_config.import_path)` at runtime — no hard imports
2. Holds `Dict[str, _SDKClient]` mapping `instrument_id → (client, config, Lock)`
3. `connect()`: imports module, instantiates class, optionally calls connect method
4. `execute()`: routes to method call or property get/set based on `SDKCallConfig`
5. Missing SDK: `ImportError` caught, `connect()` returns `False`, logged with install suggestion

**SDK commands are invisible to cloud (by design):** `GetCapabilities` shows SDK commands identically to SCPI commands — same `name`, `type`, `parameters`, `is_streamable`. Cloud has no knowledge of which path is taken. **No cloud changes needed for Tier 4.**

**Profile loading:** `_needs_python/` profiles are excluded because `glob("*.yaml")` doesn't recurse into subdirectories. Profiles must be graduated to the main `profiles/` directory once they have real SDK blocks.

### Finding 4b: ProxySDKCall vs Profile-Based SDK

| Aspect | Profile-based (`ExecuteCommand`) | `ProxySDKCall` |
|--------|----------------------------------|----------------|
| Discovery | `GetCapabilities` enumerable | Not discoverable |
| Type safety | YAML-defined params, typed | Freeform `args`/`kwargs` |
| Streaming | `StreamMeasurement` works | Not streamable |
| Cloud UI | Appears in command panel | Not exposed in web UI |
| Fallback | N/A | Module-level function call if no executor client |

### Finding 4c: Per-Instrument Breakdown

#### Quick Wins (13 instruments, ~18 days)

| Instrument | SDK | Pip? | Effort | Path |
|-----------|-----|------|--------|------|
| BlueFors Logging | stdlib (file I/O) | N/A | 1 day | Full `sdk_call` + 50-line wrapper |
| Ocean Optics Spectrometer | `seabreeze` | Yes (PyPI) | 1 day | Full `sdk_call` |
| QDevil QDAC | `qdac.py` (vendor) | No | 1 day | `sdk_call` + thin wrapper |
| MiniCircuits MW Switch | DLL or HTTP | Partial | 1 day | Full `sdk_call` |
| MiniCircuits Switch | DLL or HTTP | Partial | 1 day | Full `sdk_call` |
| Vaunix Attenuator | Vaunix DLL (ctypes) | No | 1 day | Full `sdk_call` + wrapper |
| NI USB-6218 | `nidaqmx` | Yes (PyPI) + OS driver | 1 day | `sdk_call` + thin wrapper |
| NI DAQ | `nidaqmx` | Yes (PyPI) + OS driver | 2 days | `sdk_call` + thin wrapper |
| LabBrick LMS Synth | Vaunix DLL (ctypes) | No | 2 days | `sdk_call` + wrapper |
| LabBrick LSG SigGen | Vaunix DLL (ctypes) | No | 2 days | `sdk_call` + wrapper |
| Oxford ILM | serial ASCII | N/A | 2 days | `sdk_call` + serial wrapper |
| Oxford Mercury IPS | `oxfordmercury` / custom | Partial | 2 days | `sdk_call` + wrapper |
| Oxford PS120 | serial ASCII | N/A | 2 days | `sdk_call` + serial wrapper |

#### Heavy Lifts (10 instruments, ~24 days, vendor hardware required)

| Instrument | SDK | Effort | Path |
|-----------|-----|--------|------|
| AlazarTech Digitizer | C SDK (ctypes) | 3 days | ProxySDKCall only |
| Keysight PXI AWG | `keysightSD1` | 3 days | Partial `sdk_call` + ProxySDKCall |
| Keysight PXI Digitizer | `keysightSD1` | 2 days | ProxySDKCall |
| Keysight PXI HVI | `keysightSD1` + `keysightHVI` | 2 days | ProxySDKCall |
| Signadyne AWG | `keysightSD1` | 2 days | Same as Keysight PXI AWG |
| Signadyne Digitizer | `keysightSD1` | 2 days | Same as Keysight PXI Digitizer |
| Acqiris U1084A | C library | 2 days | ProxySDKCall only |
| PXI Aeroflex 302x | IVI-C | 3 days | Research needed |
| PXI Aeroflex 303x | IVI-C | 3 days | ProxySDKCall |
| SignalHound SA124B | `bbapi` (ctypes) | 2 days | Partial `sdk_call` |

#### Blocked (3 instruments)

- **MuSwitch / MuSwitchEX:** Unknown protocol — need original Labber `.py` driver
- **LeidenPressure:** Unknown protocol — need original Labber `.py` driver

### Finding 4d: Dependency Management Gaps

- `pyproject.toml` has optional groups (`[gpib]`, `[usb]`, etc.) but **zero vendor SDK groups**
- No Dockerfile in daemon-clean
- No plugin system, no `entry_points`
- Fix: add optional-dependency groups like `[sdk-nidaqmx]`, `[sdk-seabreeze]`, `[sdk-vaunix]`

---

## Cross-Tier Dependency Map

### Proto Changes Required Per Tier

| Tier | Proto Change? | Impact |
|------|--------------|--------|
| 0 | No | YAML-only |
| 1 | No (optional map field) | Daemon-only code changes |
| 2a (sweep) | Yes — 3 new RPCs | Both repos |
| 2b (serial) | No | Daemon-only |
| 2c (visibility) | Yes — new message on `CommandCapability` | Both repos |
| 3a (vector) | Yes — new fields on `MeasurementDataPoint` | Both repos |
| 3b (trigger) | Yes — new RPC (or interim via ProxySDKCall) | Both repos |
| 4 | No | Daemon-only + per-instrument wrappers |

### Shared Files Modified Across Multiple Tiers

| File | Tiers | Changes |
|------|-------|---------|
| `profile_schema.py` | 0, 1, 2, 3, 4 | `map`, `response_parser`, `force_query`, `init/cleanup`, `SweepConfig`, serial settings, `SDKConfig` extension, visibility, `ReturnConfig` trace fields |
| `grpc_server.py` | 1, 2, 3 | Map/reverse-map, response parser, force_query, sweep handlers, trigger handler, binary query, trace streaming |
| `capability_manager.py` | 0 (bugfix), 2 | `format_scpi` fix, `requires_sweep` in capabilities |
| `main.py` | 1, 4 | Init/cleanup commands, SDK connect wiring |
| `edge.proto` | 2, 3 | Sweep RPCs, visibility, trace fields, trigger RPC |
| `command_handler.py` | 2, 3 | Lock redesign for sweeps, binary query method |
| `instrument_manager.py` | 2, 3 | Serial settings, binary read for VISA/GPIB/LAN |

---

## Unified Implementation Order

### Phase 0: Pre-Existing Blockers (MUST do first)

```
daemon-clean:
  capability_manager.py  — format_command → format_scpi (Bug 1)
  capability_manager.py  — .instrument_class.value → .instrument_class (Bug 2)
  profile_schema.py      — Extend SDKConfig with connect/disconnect/identity (Bug 3)
  main.py                — Wire sdk_executor.connect() in scan flow (Bug 4)

cloud:
  service/stream.go      — Fix Timestamp panic → use TimestampMs (Bug 5)
  grpcclient/manager.go  — Add max receive message size 64MB (Bug 6)
```

### Phase 1: Tier 0 + Tier 1 (Weeks 1-2)

No proto changes. Daemon-only (cloud edge symmetry later).

```
profile_schema.py (all at once):
  ├── ParameterConfig: add map field
  ├── ReturnConfig: add response_parser
  ├── CommandConfig: add force_query
  ├── SettingsConfig: add init_commands, cleanup_commands
  ├── format_scpi(): apply map during substitution
  └── ReturnConfig.parse_response(): regex helper

grpc_server.py:
  ├── ExecuteCommand: reverse-map + response_parser + force_query
  └── StreamMeasurement: response_parser before float()

main.py:
  ├── _try_match_profile(): send init_commands
  └── stop(): send cleanup_commands

Profile YAML fixes:
  ├── triton.yaml: fix <c>, add map to HeaterRange/CoordSys
  ├── oxford_ips120.yaml: add force_query, add init_commands: ["Q4"]
  └── Interface type corrections
```

### Phase 2: Tier 2 (Weeks 3-6)

```
Step 1 — Serial (no proto, daemon-only):
  ├── InterfaceConfig: baud_rate, parity, data_bits, stop_bits
  ├── instrument_manager.py: apply on ASRL connect
  └── config.py + main.py: wire include_serial_ports

Step 2 — Conditional visibility (proto + full stack):
  ├── edge.proto: VisibilityCondition
  ├── Both buf pipelines
  ├── edge_grpc.go (manual)
  ├── grpc_server.py → instrument.go → CommandPanel.tsx

Step 3 — Sweep support (most invasive):
  ├── edge.proto: 3 new RPCs
  ├── profile_schema.py: SweepConfig
  ├── grpc_server.py: async sweep tasks + safety interlock
  ├── command_handler.py: per-transaction lock redesign
  └── Cloud: Go client + handler + routes
```

### Phase 3: Tier 3 (Weeks 7-10+)

```
Pre-work (can start now):
  ├── Fix stream.go Timestamp panic (Bug 5)
  └── Fix grpcclient max receive size (Bug 6)

3a — Vector data:
  ├── Extend MeasurementDataPoint (backward compat)
  ├── Binary query plumbing
  ├── ReturnConfig trace fields
  ├── DB migration for trace storage
  └── Waveform chart component

3b — Hardware trigger:
  ├── Interim: ProxySDKCall arm/wait pattern
  ├── Fix sdk_executor.py lock contention
  └── Later: TriggerStream RPC
```

### Phase 4: Tier 4 (Weeks 5+, parallelizable)

Depends on Phase 0 blocker fixes (SDKConfig + connect wiring).

```
Week 5-6: Quick wins — BlueFors, Ocean Optics, QDAC, MiniCircuits, NI-DAQ
Week 7-8: LabBrick/Vaunix, Oxford serial instruments
Week 9+:  PXI cluster (requires vendor hardware on-site)
```

### Key Risk: Proto Coordination

Every proto change requires manual double-edit (daemon `edge.proto` + cloud `edge_grpc.go`). No shared source, no submodule, no automated sync. **Consider unifying proto management before Tier 2 Step 2.**
