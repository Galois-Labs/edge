# Implementation Plan: Labber Profile Migration & Platform Hardening

**Status:** Ready for implementation
**Date:** 2026-03-10
**Prerequisites:** Read `docs/agentfindings.md` and `docs/architecture-extensions.md` for full context.

---

## How to Use This Document

Each task has:
- **Context files to read** (read these first to understand the code you're touching)
- **Files to edit/create** (the actual implementation work)
- **Acceptance criteria** (how to know you're done)
- **Dependencies** (what must be complete before starting)

Tasks within a phase can be parallelized unless noted. Tasks across phases are sequential.

---

## Phase 0: Stabilization (Pre-Existing Bugs)

> **Goal:** Fix 6 bugs that make the current system crash or behave incorrectly. Nothing else ships until these are fixed.
> **Parallelism:** Tasks 0.1-0.4 (daemon) can run in parallel with 0.5-0.6 (cloud).

---

### Task 0.1: Fix `format_command()` → `format_scpi()` crash

**Problem:** Every `ExecuteCommand` RPC for non-SDK instruments crashes with `AttributeError` because `capability_manager.py` calls `cmd.format_command()` but daemon's `CommandConfig` only defines `format_scpi()`.

**Context files to read:**
- `src/galois_edge/capability_manager.py` — line 383 (the bug), and the full `resolve_command()` method (lines 353-386)
- `src/galois_edge/profile_schema.py` — lines 110-128, `CommandConfig.format_scpi()` method

**Files to edit:**
- `src/galois_edge/capability_manager.py` — line 383: change `cmd.format_command(params, is_query)` → `cmd.format_scpi(params, is_query)`

**Acceptance criteria:**
- `resolve_command()` returns a SCPI string without raising `AttributeError`
- `tests/test_grpc_server.py` passes (run `pytest tests/`)
- Manual: `ExecuteCommand` RPC with a SCPI command works end-to-end

**Dependencies:** None

---

### Task 0.2: Fix `.instrument_class.value` crash

**Problem:** `capability_manager.py` line 83 calls `.value` on `instrument_class`, but in the daemon it's a plain `str`, not an Enum. Crashes on any instrument class lookup.

**Context files to read:**
- `src/galois_edge/capability_manager.py` — lines 80-84 (the `instrument_class` property)
- `src/galois_edge/profile_schema.py` — lines 286-293, `InstrumentMetadata` (confirm `instrument_class` is `str`)

**Files to edit:**
- `src/galois_edge/capability_manager.py` — line 83: change `return self.profile.instrument.instrument_class.value` → `return self.profile.instrument.instrument_class`

**Acceptance criteria:**
- `InstrumentCapabilities.instrument_class` property returns the string without `AttributeError`
- `tests/test_grpc_server.py` passes

**Dependencies:** None

---

### Task 0.3: Extend `SDKConfig` dataclass

**Problem:** The daemon's `SDKConfig` only has 4 fields. `sdk_executor.py` accesses `sdk_config.connect.constructor_args`, `sdk_config.disconnect.method`, `sdk_config.identity` — all of which raise `AttributeError`. No SDK instrument can connect.

**Context files to read:**
- `src/galois_edge/profile_schema.py` — lines 271-278 (current minimal `SDKConfig`)
- `src/galois_edge/sdk_executor.py` — lines 48-99 (`connect()` method — see what fields it accesses on `sdk_config`)
- Cloud reference: `~/work/galois/cloud/edge/galois_edge/profile_schema.py` — lines 140-168 (full Pydantic `SDKConfig` with `SDKConnectConfig`, `SDKDisconnectConfig`, `SDKIdentityConfig`)
- `src/galois_edge/profiles/quantum_design_ppms.yaml` — reference SDK profile showing the YAML structure

**Files to edit:**
- `src/galois_edge/profile_schema.py`:
  1. Add three new dataclasses before `SDKConfig` (around line 270):
     ```python
     @dataclass
     class SDKConnectConfig:
         method: Optional[str] = None
         args: Optional[Dict[str, str]] = None
         defaults: Optional[Dict[str, Any]] = None
         constructor_args: Optional[Dict[str, Any]] = None

     @dataclass
     class SDKDisconnectConfig:
         method: Optional[str] = None

     @dataclass
     class SDKIdentityConfig:
         method: Optional[str] = None
         property: Optional[str] = None
         pattern: Optional[str] = None
     ```
  2. Extend `SDKConfig` to include them:
     ```python
     @dataclass
     class SDKConfig:
         package: str = ""
         import_path: str = ""
         class_name: str = ""
         is_async: bool = False
         connect: SDKConnectConfig = field(default_factory=SDKConnectConfig)
         disconnect: SDKDisconnectConfig = field(default_factory=SDKDisconnectConfig)
         identity: Optional[SDKIdentityConfig] = None
     ```
  3. Update the SDK builder in `profile_from_dict()` (lines 583-591) to parse nested `connect`, `disconnect`, `identity` dicts from YAML.

**Acceptance criteria:**
- `quantum_design_ppms.yaml` loads without error and `profile.sdk.connect.method` is populated
- `sdk_executor.connect()` no longer crashes on attribute access
- `tests/test_profile_loader.py` passes
- Add a new test that loads a YAML with full `sdk:` block and verifies all sub-configs

**Dependencies:** None

---

### Task 0.4: Wire `sdk_executor.connect()` into scan flow

**Problem:** Nothing in `main.py` calls `sdk_executor.connect()` when a profile with `sdk:` config is matched. SDK instruments are matched but never connected.

**Context files to read:**
- `src/galois_edge/main.py` — lines 250-288 (`_try_match_profile()`)
- `src/galois_edge/main.py` — line 128 (`self._command_handler` instantiation), line 131 (`self._sdk_executor` instantiation)
- `src/galois_edge/sdk_executor.py` — lines 48-99 (`connect()` method signature and behavior)

**Files to edit:**
- `src/galois_edge/main.py` — In `_try_match_profile()`, after the profile match (around line 277, after `capability_manager.register_instrument()`):
  ```python
  # After register_instrument, connect SDK if profile has SDK config
  if profile and profile.sdk and profile.sdk.import_path:
      try:
          runtime_args = {"address": visa_addr}
          self._sdk_executor.connect(visa_addr, profile.sdk, runtime_args)
      except Exception as exc:
          logger.warning("SDK connect failed for %s: %s", visa_addr, exc)
  ```

**Acceptance criteria:**
- When a profile with `sdk:` block is matched, `sdk_executor.connect()` is called
- If the vendor SDK is not installed, the error is logged but the daemon continues running
- SCPI instruments are unaffected (no SDK connect attempted)

**Dependencies:** Task 0.3 (SDKConfig must be complete first)

---

### Task 0.5: Fix `stream.go` Timestamp panic (CLOUD)

**Problem:** `service/stream.go` line 111 calls `point.Timestamp.AsTime()` on a nil pointer. The `Timestamp` field is a phantom field in the hand-written Go stub — never populated by the Python daemon.

**Context files to read:**
- `~/work/galois/cloud/backend/internal/service/stream.go` — lines 90-127 (`streamLoop`), especially line 111 (the panic)
- `~/work/galois/cloud/backend/internal/gen/proto/galois/edge/v1/edge_grpc.go` — lines 234-247 (`MeasurementDataPoint` struct with phantom fields)
- `~/work/galois/daemon-clean/proto/edge/v1/edge.proto` — lines 296-304 (`MeasurementDataPoint` — note: uses `int64 timestamp_ms`, not `google.protobuf.Timestamp`)

**Files to edit:**
- `~/work/galois/cloud/backend/internal/service/stream.go` — line 111: Replace the JSON marshaling block:
  ```go
  // Before (panics):
  "timestamp": point.Timestamp.AsTime().Format(time.RFC3339Nano),
  // After (correct):
  "timestamp_ms": point.TimestampMs,
  ```
  Also remove references to `point.RawData` and `point.Metadata` (phantom fields) from the same marshal block.

**Acceptance criteria:**
- `streamLoop` no longer panics when receiving `MeasurementDataPoint` from daemon
- SSE events are emitted with `timestamp_ms` (int64) instead of formatted timestamp string
- Frontend `useSSE` hook still parses the data (check `~/work/galois/cloud/web/src/hooks/use-sse.ts` for the expected shape)

**Dependencies:** None

---

### Task 0.6: Fix gRPC max receive message size (CLOUD)

**Problem:** Cloud Go gRPC client has no `MaxRecvMsgSize` option. Default is 4MB. Daemon can send up to 50MB. Any large response silently fails with `ResourceExhausted`.

**Context files to read:**
- `~/work/galois/cloud/backend/internal/grpcclient/manager.go` — lines 44-46 (dial options — only `insecure.NewCredentials()`)
- `~/work/galois/daemon-clean/src/galois_edge/grpc_server.py` — line 1358 area (server options set `max_send_message_length`)

**Files to edit:**
- `~/work/galois/cloud/backend/internal/grpcclient/manager.go` — lines 44-46: Add max receive option:
  ```go
  conn, err := grpc.NewClient(target,
      grpc.WithTransportCredentials(insecure.NewCredentials()),
      grpc.WithDefaultCallOptions(grpc.MaxCallRecvMsgSize(64 * 1024 * 1024)), // 64MB
  )
  ```

**Acceptance criteria:**
- Cloud can receive gRPC responses up to 64MB from the daemon
- Existing small responses are unaffected

**Dependencies:** None

---

## Phase 1: Core Schema & Engine (Tier 0 + Tier 1)

> **Goal:** Add `map`, `response_parser`, `force_query`, `init_commands` to the profile schema and wire them into the execution engine. Fix Labber-converted profile YAML files. This makes Oxford IPS120, Triton, and similar partially-functional profiles actually work.
> **Parallelism:** Tasks 1.1-1.4 (schema layer) can be done as one batch. Task 1.5 (engine wiring) depends on 1.1-1.4. Task 1.6 (profile fixes) depends on 1.5. Task 1.7 (tests) can partially parallel with 1.5-1.6.

---

### Task 1.1: Add `map` field to `ParameterConfig`

**Problem:** 153 `map:` entries across 43 YAML profiles are silently dropped at load time. The `map` field doesn't exist on `ParameterConfig`.

**Context files to read:**
- `src/galois_edge/profile_schema.py` — lines 24-46 (`ParameterConfig`), lines 434-443 (`_build_parameter_config()`)
- `src/galois_edge/profiles/yokogawa_gs200.yaml` — search for `map:` to see existing usage pattern
- `src/galois_edge/profiles/srs_cs580.yaml` — another example of `map:` usage

**Files to edit:**
- `src/galois_edge/profile_schema.py`:
  1. Add to `ParameterConfig` (after line 34): `map: Optional[Dict[str, Any]] = None`
  2. In `_build_parameter_config()` (line 443 area): add `map=data.get("map")`

**Acceptance criteria:**
- Loading `yokogawa_gs200.yaml` populates `ParameterConfig.map` for `output_state.params.state`
- `ParameterConfig.validate()` still passes

**Dependencies:** Phase 0 complete

---

### Task 1.2: Add `response_parser` to `ReturnConfig`

**Problem:** Non-SCPI instruments (Oxford, Triton) return unparseable response strings. Need regex/strip parsing on `ReturnConfig`.

**Context files to read:**
- `src/galois_edge/profile_schema.py` — lines 48-66 (`ReturnConfig`), lines 446-454 (`_build_return_config()`)
- `docs/architecture-extensions.md` — section C ("Non-SCPI Response Parsing") for the parser design

**Files to edit:**
- `src/galois_edge/profile_schema.py`:
  1. Add to `ReturnConfig` (after line 57): `parser: Optional[Dict[str, Any]] = None`
  2. Add a `parse_response()` method to `ReturnConfig`:
     ```python
     def parse_response(self, raw: str) -> str:
         """Apply parser rules to raw instrument response. Falls back to raw on no match."""
         if not self.parser:
             return raw
         ptype = self.parser.get("type", "regex")
         if ptype == "regex":
             pattern = self.parser.get("pattern", "")
             group = self.parser.get("group", 0)
             m = re.search(pattern, raw)
             if m:
                 return m.group(group)
         elif ptype == "strip":
             result = raw
             prefix = self.parser.get("prefix", "")
             suffix = self.parser.get("suffix", "")
             if prefix and result.startswith(prefix):
                 result = result[len(prefix):]
             if suffix and result.endswith(suffix):
                 result = result[:-len(suffix)]
             return result
         elif ptype == "split":
             delimiter = self.parser.get("delimiter", ",")
             index = self.parser.get("index", 0)
             parts = raw.split(delimiter)
             if index < len(parts):
                 return parts[index].strip()
         return raw
     ```
  3. In `_build_return_config()`: add `parser=data.get("parser")`
  4. Add `import re` at top if not already present (it is — line 13)

**Acceptance criteria:**
- `ReturnConfig(parser={"type": "regex", "pattern": r".*:([\d.]+)[A-Za-z]*$", "group": 1}).parse_response("STAT:DEV:T1:TEMP:SIG:TEMP:1.234K")` returns `"1.234"`
- `ReturnConfig(parser={"type": "strip", "prefix": "R"}).parse_response("R+00.0000")` returns `"+00.0000"`
- `ReturnConfig(parser=None).parse_response("hello")` returns `"hello"` (passthrough)

**Dependencies:** Phase 0 complete

---

### Task 1.3: Add `force_query` to `CommandConfig`

**Problem:** Non-SCPI getters (Oxford IPS120 `R7`, `R9`) don't end with `?`. Need a profile-level flag to force query behavior.

**Context files to read:**
- `src/galois_edge/profile_schema.py` — lines 88-148 (`CommandConfig`), lines 468-496 (`_build_command()`)
- `src/galois_edge/command_handler.py` — line 139 (`is_query = force_query or scpi_cmd.strip().endswith("?")`)

**Files to edit:**
- `src/galois_edge/profile_schema.py`:
  1. Add to `CommandConfig` (after line 99): `force_query: bool = False`
  2. In `_build_command()` (line 496 area): add `force_query=data.get("force_query", False)`

**Acceptance criteria:**
- Loading a YAML with `force_query: true` on a command populates `cmd.force_query == True`
- Default is `False` for all existing profiles

**Dependencies:** Phase 0 complete

---

### Task 1.4: Add `init_commands` / `cleanup_commands` to `SettingsConfig`

**Problem:** Some instruments need initialization commands on connect (Oxford IPS120 needs `Q4` to set response format). No mechanism exists.

**Context files to read:**
- `src/galois_edge/profile_schema.py` — lines 192-199 (`SettingsConfig`), lines 560-566 (settings builder in `profile_from_dict()`)

**Files to edit:**
- `src/galois_edge/profile_schema.py`:
  1. Add to `SettingsConfig` (after line 198):
     ```python
     init_commands: Optional[List[str]] = None
     cleanup_commands: Optional[List[str]] = None
     ```
  2. In `profile_from_dict()` settings builder (around line 563): add:
     ```python
     init_commands=s_data.get("init_commands"),
     cleanup_commands=s_data.get("cleanup_commands"),
     ```

**Acceptance criteria:**
- Loading a YAML with `settings.init_commands: ["Q4"]` populates `profile.settings.init_commands == ["Q4"]`
- Existing profiles without these fields load normally (default `None`)

**Dependencies:** Phase 0 complete

---

### Task 1.5: Wire schema changes into the execution engine

**Problem:** The new schema fields need to actually be used in the gRPC handlers.

**Context files to read:**
- `src/galois_edge/grpc_server.py`:
  - Lines 568-687: `ExecuteCommand` handler (especially lines 598-603 for `resolve_command`, lines 642-654 for SCPI dispatch, lines 658-675 for response handling)
  - Lines 862-1062: `StreamMeasurement` handler (especially lines 1008-1013 for `float()` conversion)
- `src/galois_edge/capability_manager.py` — lines 353-386 (`resolve_command()`)
- `src/galois_edge/main.py` — lines 250-288 (`_try_match_profile()`), lines 179-212 (`stop()`)
- `src/galois_edge/command_handler.py` — lines 56-98 (`execute_command()` — note the `force_query` parameter)
- GPT 5.2 Pro recommendation: create a shared helper in `grpc_server.py` instead of duplicating logic

**Files to edit:**

**1. `src/galois_edge/profile_schema.py` — Extend `format_scpi()` to apply map (lines 116-128):**

Change the `format_scpi()` method to apply `ParameterConfig.map` during substitution:
```python
def format_scpi(
    self,
    params: Optional[Dict[str, Any]] = None,
    is_query: bool = True,
) -> str:
    scpi = self.get_scpi_string(is_query)
    if scpi is None:
        raise ValueError("No SCPI string available for this command")
    if params and self.params:
        for key, value in params.items():
            # Apply map transformation if available
            pc = self.params.get(key)
            if pc and pc.map and str(value) in pc.map:
                value = pc.map[str(value)]
            scpi = scpi.replace(f"{{{key}}}", str(value))
    elif params:
        for key, value in params.items():
            scpi = scpi.replace(f"{{{key}}}", str(value))
    return scpi
```

**2. `src/galois_edge/grpc_server.py` — Add shared helper and wire force_query + parser:**

Add a helper method to `EdgeDaemonServicer` (before the `ExecuteCommand` handler):
```python
def _apply_response_processing(
    self,
    raw_response: str,
    instrument_id: str,
    command_name: str,
) -> str:
    """Apply response_parser from profile ReturnConfig to raw instrument response."""
    caps = self._capability_manager.get_capabilities(instrument_id)
    if not caps:
        return raw_response
    cmd = caps.get_command(command_name)
    if cmd and cmd.returns:
        return cmd.returns.parse_response(raw_response)
    return raw_response
```

In `ExecuteCommand` handler (around line 652):
- After `resolve_command()` returns the SCPI string, look up `force_query`:
  ```python
  # Look up force_query from profile
  caps = self._capability_manager.get_capabilities(instrument_id)
  cmd_config = caps.get_command(command_name) if caps else None
  profile_force_query = cmd_config.force_query if cmd_config else False
  ```
- Pass to handler: `force_query=request.is_query or profile_force_query`
- After getting `result["response"]`, apply parser:
  ```python
  response_data = self._apply_response_processing(
      result["response"], instrument_id, command_name
  )
  ```

In `StreamMeasurement` handler (around line 1008, before `float()` conversion):
- Apply parser to `raw` before attempting float conversion:
  ```python
  raw = self._apply_response_processing(raw, instrument_id, command_name)
  ```

**3. `src/galois_edge/main.py` — Wire init_commands and cleanup_commands:**

In `_try_match_profile()` (after profile match and registration, around line 277):
```python
# Send init commands if profile defines them
if profile and profile.settings.init_commands:
    for cmd in profile.settings.init_commands:
        try:
            self._command_handler.execute_command(
                cmd, visa_addr, timeout_ms=profile.settings.timeout_ms
            )
        except Exception as exc:
            logger.warning("Init command '%s' failed for %s: %s", cmd, visa_addr, exc)
```

In `stop()` (after line 202, before `disconnect_all()`):
```python
# Send cleanup commands for all registered instruments
if self._capability_manager and self._command_handler:
    for inst_id, caps in self._capability_manager.all_instruments.items():
        if caps.has_profile and caps.profile.settings.cleanup_commands:
            for cmd in caps.profile.settings.cleanup_commands:
                try:
                    self._command_handler.execute_command(
                        cmd, inst_id, timeout_ms=caps.profile.settings.timeout_ms
                    )
                except Exception:
                    pass  # Best-effort on shutdown
```

Note: Verify that `capability_manager` exposes an `all_instruments` property or equivalent. If not, add one that returns the internal `_instruments` dict.

**Important — do NOT reverse-map responses.** Per GPT 5.2 Pro's recommendation: apply map forward-only (label → wire value on writes). Do not reverse-map numeric returns, as this would break `StreamMeasurement` float conversion.

**Acceptance criteria:**
- `ExecuteCommand` with Oxford IPS120 profile: `command_name="b", is_query=true` sends `R7` as a query (force_query kicks in) and the response `R+00.0000` is parsed to `+00.0000`
- `ExecuteCommand` with Yokogawa GS200 profile: `command_name="output_state", parameters={"state": "ON"}` sends `:OUTPut[:STATe] 1` (map applied)
- `StreamMeasurement` with Triton `t1` command: raw response `STAT:DEV:T1:TEMP:SIG:TEMP:1.234K` is parsed to `1.234` before float conversion
- Init commands are sent when an instrument connects
- Cleanup commands are sent on daemon shutdown

**Dependencies:** Tasks 1.1-1.4 complete

---

### Task 1.6: Fix Labber-converted profile YAML files

**Problem:** Several converted profiles have wrong values, unresolved placeholders, or missing fields.

**Context files to read:**
- `src/galois_edge/profiles/oxford_ips120.yaml` (82 lines)
- `src/galois_edge/profiles/triton.yaml` (290 lines)
- `docs/labbertoyaml.md` — sections 1.6 (Non-SCPI), 1.8 (Combo Labels), 1.11 (Init/Final)

**Files to edit:**

**1. `src/galois_edge/profiles/oxford_ips120.yaml`:**
- Add `init_commands` to settings:
  ```yaml
  settings:
    timeout_ms: 5000
    terminator: "\r"
    opc_query: false
    init_commands: ["Q4"]
    cleanup_commands: ["C0"]
  ```
- Add `force_query: true` to `b` and `sweeprate` commands
- Add `parser` to returns on `b` and `sweeprate`:
  ```yaml
  returns:
    type: float
    unit: T
    parser:
      type: strip
      prefix: "R"
  ```

**2. `src/galois_edge/profiles/triton.yaml`:**
- Fix `<c>` placeholders: convert `controlloop`, `tset`, `heaterrange` to use `{channel}` param:
  ```yaml
  controlloop:
    getter: "READ:DEV:T{channel}:TEMP:LOOP:MODE"
    setter: "SET:DEV:T{channel}:TEMP:LOOP:MODE:{value}"
    type: property
    params:
      channel:
        type: string
        description: "Temperature channel (e.g., 5, 8)"
      value:
        type: enum
        options: ["ON", "OFF"]
  ```
- Add `parser` to all temperature query returns:
  ```yaml
  returns:
    type: float
    unit: K
    parser:
      type: regex
      pattern: ".*:([\\d.]+)[A-Za-z]*$"
      group: 1
  ```
- Add `map` to `heaterrange` enum:
  ```yaml
  params:
    value:
      type: enum
      options: ["Off", "31.6 uA", "100 uA", "316 uA", "1 mA", "3.16 mA", "10 mA", "31.6 mA", "100 mA"]
      map:
        "Off": "0.0"
        "31.6 uA": "0.0316"
        "100 uA": "0.1"
        "316 uA": "0.316"
        "1 mA": "1.0"
        "3.16 mA": "3.16"
        "10 mA": "10.0"
        "31.6 mA": "31.6"
        "100 mA": "100.0"
  ```
- Add `map` to `coordsys` enum:
  ```yaml
  options: ["Cartesian", "Cylindrical", "Spherical"]
  map:
    "Cartesian": "CART"
    "Cylindrical": "CYL"
    "Spherical": "SPH"
  ```
- Change interface type from `gpib` to `ethernet`
- Add `force_query: true` to all temperature queries and magnet queries (non-SCPI, no `?`)

**Acceptance criteria:**
- Both profiles load without validation errors
- All `<c>` placeholders replaced with `{channel}` params
- All temperature queries have response parsers
- All enum params with Labber `cmd_def` values have `map:` fields

**Dependencies:** Tasks 1.1-1.4 complete (schema must support new fields)

---

### Task 1.7: Tests for Phase 1

**Context files to read:**
- `tests/test_profile_loader.py` — existing profile loading tests
- `tests/test_grpc_server.py` — existing gRPC server tests
- `tests/test_command_handler.py` — existing command handler tests
- `tests/conftest.py` — shared fixtures

**Files to edit/create:**

**1. `tests/test_profile_loader.py` — Add tests:**
- Test loading a profile with `map:` on a parameter → verify `pc.map` is populated
- Test loading a profile with `init_commands` / `cleanup_commands` → verify `settings.init_commands` is populated
- Test loading a profile with `force_query: true` → verify `cmd.force_query is True`
- Test loading a profile with `returns.parser` → verify `rc.parser` is populated

**2. `tests/test_profile_schema.py` — New file with unit tests:**
- Test `ReturnConfig.parse_response()`:
  - Regex parser with capture group
  - Strip parser with prefix
  - Split parser with delimiter and index
  - No parser (passthrough)
  - Regex with no match (passthrough fallback)
- Test `CommandConfig.format_scpi()` with map:
  - Enum value that exists in map → substituted with wire value
  - Enum value not in map → substituted as-is (no crash)
  - No map on param → normal substitution

**3. `tests/test_grpc_server.py` — Add tests:**
- Test `ExecuteCommand` with `force_query=true` profile flag → verify `command_handler.execute_command()` called with `force_query=True`
- Test `_apply_response_processing()` with parser → verify parsed response

**Acceptance criteria:**
- All new tests pass
- All existing tests still pass
- `pytest tests/ -v` shows green

**Dependencies:** Tasks 1.1-1.5 complete

---

## Phase 2: Serial Interface + Proto Management

> **Goal:** Enable serial instruments and fix the proto management workflow before any proto-changing features.
> **Parallelism:** Tasks 2.1 (serial) and 2.2 (proto management) can run in parallel.

---

### Task 2.1: Serial interface settings

**Problem:** Serial instruments (QDevil QDAC at 460800 baud, SRS instruments, etc.) cannot configure baud rate, parity, etc. through profiles.

**Context files to read:**
- `src/galois_edge/profile_schema.py` — lines 258-265 (`InterfaceConfig`)
- `src/galois_edge/instrument_manager.py` — the `connect()` method (find where `open_resource()` is called, look for any serial-specific code)
- `src/galois_edge/config.py` — check for `include_serial_ports` env var
- `src/galois_edge/profiles/srs_cs580.yaml` — existing serial profile using `port: 9600` convention

**Files to edit:**
- `src/galois_edge/profile_schema.py` — extend `InterfaceConfig`:
  ```python
  @dataclass
  class InterfaceConfig:
      type: str = "gpib"
      port: Optional[int] = None
      default_address: Optional[int] = None
      baud_rate: Optional[int] = None
      parity: Optional[str] = None       # "none", "even", "odd"
      data_bits: Optional[int] = None
      stop_bits: Optional[float] = None   # 1, 1.5, 2
  ```
  Update the interfaces builder in `profile_from_dict()` (around line 553).

- `src/galois_edge/instrument_manager.py` — In `connect()`, after `open_resource()`, apply serial settings for ASRL resources:
  ```python
  if visa_address.startswith("ASRL") and serial_config:
      if serial_config.baud_rate:
          resource.baud_rate = serial_config.baud_rate
      # ... parity, data_bits, stop_bits
  ```
  This requires passing serial config into `connect()` or looking it up from the profile.

- `src/galois_edge/config.py` — Add `INCLUDE_SERIAL_PORTS` env var
- `src/galois_edge/main.py` — Wire `include_serial_ports` from config to `InstrumentManager`

**Acceptance criteria:**
- A profile with `type: serial, baud_rate: 9600` loads correctly
- When connecting to an ASRL resource, baud rate is set on the VISA resource object
- Existing GPIB/USB/Ethernet connections unaffected

**Dependencies:** Phase 1 complete

---

### Task 2.2: Proto source-of-truth + generation pipeline (CLOUD)

**Problem:** The cloud's `edge_grpc.go` is hand-written but tagged "DO NOT EDIT". It has phantom fields that already caused Bug 5. Every proto change requires manual double-edit. This must be fixed before any proto-changing features (sweep, visibility, vector).

**Context files to read:**
- `~/work/galois/daemon-clean/proto/buf.yaml` and `proto/buf.gen.yaml`
- `~/work/galois/cloud/proto/buf.yaml` and `proto/buf.gen.yaml`
- `~/work/galois/cloud/backend/internal/gen/proto/galois/edge/v1/edge_grpc.go` — the hand-written stub
- `~/work/galois/daemon-clean/proto/edge/v1/edge.proto` — canonical proto

**Files to edit:**
- Designate `daemon-clean/proto/edge/v1/edge.proto` as the single source of truth
- Update cloud's `buf.gen.yaml` to generate Go stubs from the same proto (or add a sync script)
- Replace the hand-written `edge_grpc.go` with generated code
- Remove phantom fields (`Timestamp`, `RawData`, `Metadata`) from `MeasurementDataPoint`
- Add a CI check or Makefile target that verifies both repos' generated code matches the proto
- Document the workflow in a `proto/README.md`

**Acceptance criteria:**
- Cloud Go stubs are generated from proto, not hand-written
- `stream.go` uses `TimestampMs` (int64) after stub regeneration
- A single `make proto` (or equivalent) regenerates stubs in both repos
- CI fails if generated stubs are stale

**Dependencies:** Task 0.5 complete (stream.go panic must be fixed first or simultaneously)

---

## Phase 3: Sweep Support (Tier 2a)

> **Goal:** Implement safe sweep RPCs for superconducting magnets. Safety-critical — this is the most important feature for cryogenics/quantum customers.
> **Parallelism:** Must be sequential (proto → schema → engine → profiles).

---

### Task 3.1: Proto changes for sweep RPCs

**Context files to read:**
- `proto/edge/v1/edge.proto` — existing service definition (lines 14-80)
- `docs/architecture-extensions.md` — section A ("Sweep/Ramp & Safety Architecture")

**Files to edit:**
- `proto/edge/v1/edge.proto` — Add three new RPCs and messages as specified in `architecture-extensions.md` lines 40-81
- Run `make proto` (or equivalent) to regenerate Python and Go stubs
- Update cloud Go stubs via the pipeline from Task 2.2

**Dependencies:** Task 2.2 complete (proto pipeline must be working)

---

### Task 3.2: Sweep schema and engine

**Context files to read:**
- `src/galois_edge/profile_schema.py` — `CommandConfig` (lines 88-148)
- `src/galois_edge/grpc_server.py` — `EdgeDaemonServicer.__init__()` (lines 254-279, note `self._active_streams` dict pattern), `ExecuteCommand` handler (lines 568-687)
- `src/galois_edge/command_handler.py` — locking model (lines 94-98, 171-176)
- `docs/architecture-extensions.md` — full sweep design
- `docs/agentfindings.md` — Tier 2a findings (lock incompatibility, instrument reservation gate)

**Files to edit:**
- `src/galois_edge/profile_schema.py` — Add `SweepConfig` dataclass, `requires_sweep` on `CommandConfig`
- `src/galois_edge/grpc_server.py`:
  1. Add `self._active_sweeps: Dict[str, asyncio.Task] = {}` to servicer
  2. Add instrument reservation gate: `self._sweeping_instruments: Set[str] = set()`
  3. Implement `StartSweep` handler (asyncio task that polls `check_command`)
  4. Implement `GetSweepStatus` handler
  5. Implement `StopSweep` handler (cancels task, fires `stop_command`)
  6. Add safety interlock in `ExecuteCommand`: if `cmd.requires_sweep`, reject with descriptive error
  7. Add reservation gate in `ExecuteCommand`: if instrument is sweeping, reject writes (allow reads)
- Magnet profile YAML files: add `requires_sweep: true` and `sweep:` blocks

**Key design constraint (from GPT 5.2 Pro):** The sweep asyncio task must:
- Acquire the per-instrument lock only for each individual VISA write/query
- Release the lock between polls
- The reservation gate (not the lock) prevents interleaving of other write commands
- `StopSweep` sets a cancellation flag, waits for current poll to finish, then fires `stop_command`

**Dependencies:** Task 3.1 complete

---

## Phase 4: Tier 4 — SDK Instruments (Parallelizable)

> **Goal:** Enable the 26 instruments in `_needs_python/` via SDK integration.
> **Parallelism:** Each instrument is independent. Prioritize by customer demand.
> **Prerequisite:** Phase 0 (Tasks 0.3 + 0.4) must be complete.

---

### Task 4.x Template: Enable an SDK Instrument

For each instrument, follow this pattern:

**Context files to read:**
- `src/galois_edge/profiles/_needs_python/{instrument}.yaml` — the empty stub
- `src/galois_edge/profiles/quantum_design_ppms.yaml` — reference SDK profile
- `src/galois_edge/sdk_executor.py` — `connect()` (lines 48-99) and `execute()` (lines 128-160)
- Original Labber driver (if available) for the instrument's protocol/API

**Files to create/edit:**
1. Write a thin Python wrapper class (if vendor SDK API is not directly `sdk_call`-mappable):
   - Place in `src/galois_edge/sdk_wrappers/{instrument}_wrapper.py`
   - Expose simple methods: `get_temperature(channel)`, `set_voltage(channel, value)`, etc.
2. Move profile from `_needs_python/` to `profiles/` and populate:
   - `sdk:` block with `package`, `import_path`, `class_name`, `connect`, `disconnect`, `identity`
   - `commands:` with `sdk_call:` entries mapping to wrapper methods
3. Add optional dependency to `pyproject.toml` if the vendor SDK is pip-installable

**Priority order:**
1. BlueFors Logging (1 day, stdlib only, every dilution fridge lab)
2. Ocean Optics Spectrometer (1 day, `seabreeze` on PyPI)
3. QDevil QDAC (1 day, vendor `qdac.py`)
4. NI USB-6218 + NI DAQ (2 days, `nidaqmx` on PyPI)
5. MiniCircuits switches (2 days, DLL or HTTP)
6. Oxford ILM / Mercury IPS / PS120 (6 days, serial wrappers)
7. LabBrick / Vaunix (4 days, ctypes DLL wrappers)
8. Keysight PXI family (10+ days, vendor hardware required)
9. AlazarTech / Acqiris (5+ days, C SDK, ProxySDKCall only)

**Dependencies:** Phase 0 Tasks 0.3 + 0.4 complete

---

## Phase 5: Vector/Trace Data (Tier 3a)

> **Goal:** Enable oscilloscopes, digitizers, and spectrum analyzers to return waveform data.
> **Prerequisite:** Phase 2 (proto pipeline), Phase 0 (Bug 6 — gRPC size limit).

---

### Task 5.1: Proto + schema for vector data

**Context files to read:**
- `proto/edge/v1/edge.proto` — `ExecuteCommandResponse` (lines 253-260), `MeasurementDataPoint` (lines 296-304)
- `docs/architecture-extensions.md` — section B ("Vector/Trace Data")
- `src/galois_edge/profile_schema.py` — `ReturnConfig` (lines 48-66)

**Files to edit:**
- `proto/edge/v1/edge.proto` — Add `VectorData` message and `vector_data` field on `ExecuteCommandResponse` (as specified in architecture-extensions.md)
- `src/galois_edge/profile_schema.py` — Add `t0`, `dt`, `x_name`, `x_unit`, `format` fields to `ReturnConfig`; add `"vector"` to allowed return types

### Task 5.2: Binary query plumbing

**Files to edit:**
- `src/galois_edge/command_handler.py` — Add `execute_binary_query()` method that calls `instrument_manager.query_binary_values()` instead of `query()`
- `src/galois_edge/instrument_manager.py` — Add `query_binary()` method for VISA/GPIB/LAN instruments (currently only USB has `read_binary()`)
- `src/galois_edge/grpc_server.py` — In `ExecuteCommand` and `StreamMeasurement`, detect `returns.type == "vector"` or `returns.format == "ieee_binary"` and call the binary query path

### Task 5.3: Cloud relay + frontend

**Files to edit (cloud):**
- `backend/internal/gen/proto/.../edge_grpc.go` — Regenerate with `VectorData` struct
- `backend/internal/handler/instrument.go` — Handle `VectorData` in `ExecuteCommand` response (base64-encode `y_data` bytes)
- `backend/internal/service/stream.go` — Handle trace payloads in `streamLoop`
- `web/src/hooks/use-sse.ts` — Extend `SSEDataPoint` interface
- `web/src/components/monitor/ChartWidget.tsx` — Add waveform display mode (new component)

**Dependencies:** Phase 2 complete (proto pipeline + gRPC size fix)

---

## Appendix A: File Reference Index

| File (daemon-clean) | Key Contents | Phases Touched |
|---------------------|-------------|----------------|
| `src/galois_edge/profile_schema.py` | All dataclass models | 0, 1, 2, 3, 5 |
| `src/galois_edge/capability_manager.py` | `resolve_command()`, instrument caps | 0, 1, 3 |
| `src/galois_edge/grpc_server.py` | All RPC handlers, `grpc.aio` server | 1, 3, 5 |
| `src/galois_edge/command_handler.py` | SCPI execution + locking | 3, 5 |
| `src/galois_edge/main.py` | Daemon lifecycle, scan, init/cleanup | 0, 1 |
| `src/galois_edge/sdk_executor.py` | SDK instrument connection + execution | 0, 4 |
| `src/galois_edge/instrument_manager.py` | VISA resource management | 2, 5 |
| `src/galois_edge/profile_loader.py` | YAML loading + IDN matching | 1 |
| `src/galois_edge/config.py` | Environment variable config | 2 |
| `proto/edge/v1/edge.proto` | gRPC service + message definitions | 3, 5 |
| `profiles/oxford_ips120.yaml` | Oxford magnet controller profile | 1 |
| `profiles/triton.yaml` | Triton dilution fridge profile | 1 |

| File (cloud) | Key Contents | Phases Touched |
|-------------|-------------|----------------|
| `backend/internal/gen/proto/.../edge_grpc.go` | Hand-written Go proto stubs | 0, 2, 3, 5 |
| `backend/internal/service/stream.go` | SSE streaming + CircularBuffer | 0, 5 |
| `backend/internal/grpcclient/manager.go` | gRPC client dial options | 0 |
| `backend/internal/handler/instrument.go` | HTTP → gRPC proxy for commands | 3, 5 |
| `proto/buf.yaml` + `proto/buf.gen.yaml` | Proto generation config | 2 |

## Appendix B: Test File Reference

| Test File | What It Tests | Phases |
|-----------|--------------|--------|
| `tests/test_profile_loader.py` | Profile YAML loading + validation | 1 |
| `tests/test_grpc_server.py` | gRPC handler behavior | 1, 3 |
| `tests/test_command_handler.py` | SCPI command execution + locking | 3 |
| `tests/test_config.py` | Config env vars | 2 |
| `tests/test_profile_schema.py` | **NEW** — format_scpi, parse_response | 1 |
