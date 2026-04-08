# Demo Fix Progress Tracker

## Overview
Tracking fixes needed to make the Quantifi Photonics demo fully functional end-to-end.
Each task is implemented by an independent subagent. Update status as you complete work.

---

## Task 1: Profile Matching Fix
**Status:** COMPLETED
**Assignee:** opus-profiles
**Priority:** CRITICAL (blocks all data flow)
**Completed:** 2026-04-07

### Problem
- `get_profile("quantifi_photonics_laser_1000")` returns None on the Pi (stale pickle cache)
- Fallback `match_instrument(idn)` hits `quantifi_photonics_cohesion.yaml`'s greedy pattern `QUANTIFI.*PHOTONICS.*` before specific profiles
- All 5 virtual instruments get cohesion (10 generic commands) instead of their specific profiles (16-35 commands each)
- Streaming commands like `measure_power`, `query_attenuation` won't resolve

### Root Causes
1. **Pickle cache**: `ProfileLoader` caches to `_cache.pkl`. If stale, new profiles aren't found by key
2. **Greedy patterns**: cohesion pattern `QUANTIFI.*PHOTONICS.*` matches ALL Quantifi IDN strings. It sorts alphabetically before `laser_1000`, `osa_1000`, etc.
3. **`_register_demo_instruments`** falls through from `get_profile(key)` → `match_instrument(idn)` when key lookup fails

### Changes Made

**Greedy patterns tightened (6 files):**
| Profile | Old Pattern | New Pattern |
|---------|------------|-------------|
| `quantifi_photonics_cohesion.yaml` | `QUANTIFI.*PHOTONICS.*` | `Quantifi Photonics,.*Cohesion.*` |
| `quantifi_photonics_switch.yaml` | `Quantifi Photonics,.*` | `Quantifi Photonics,.*SWITCH.*` |
| `quantifi_photonics_power_1500.yaml` | `Quantifi Photonics.*` | `Quantifi Photonics.*POWER.?1500.*` |
| `quantifi_photonics_laser_1000.yaml` | `Quantifi Photonics.*LASER.*` | `Quantifi Photonics.*LASER.?1000.*` |
| `quantifi_photonics_laser_1100.yaml` | `Quantifi Photonics.*LASER.*` | `Quantifi Photonics.*LASER.?1100.*` |
| `quantifi_photonics_laser_1200.yaml` | `Quantifi Photonics.*LASER.*` | `Quantifi Photonics.*LASER.?1200.*` |

**`_register_demo_instruments()` in `main.py`:**
- When `PROFILE_OVERRIDES` key is set and `get_profile()` returns None, logs an error but does NOT fall through to `match_instrument()`. The override is authoritative.

### Validation
- [x] `get_profile("quantifi_photonics_laser_1000")` returns the laser profile (16 commands)
- [x] `match_instrument("Quantifi Photonics,LASER 1000,SN00001,1.0.0")` returns laser_1000 (not cohesion)
- [x] Same for all 5 demo instruments (switch, voa, power_1400, osa_1000) via get_profile()
- [x] Cohesion profile still matches actual Cohesion instruments (IDN containing "Cohesion")
- [x] No other Quantifi profiles regress (73 existing tests pass)
- [x] `_register_demo_instruments` logs correct profile keys in its "Demo: ... (profile: ...)" messages

---

## Task 2: Photonics Measurement Templates
**Status:** COMPLETED
**Assignee:** opus-templates
**Priority:** CRITICAL (blocks Monitor page charts)

### Problem
`web/src/measurement-templates.ts` has templates for dmm, power_supply, smu, oscilloscope, spectrum_analyzer but ZERO for photonics instrument classes: laser, voa, power_meter, osa, switch. "Monitor All" falls back to `_generic` with `commandName: "measure"` which doesn't exist in Quantifi profiles.

### Fix
Add measurement template entries for each photonics instrument class. The command names MUST match the commands defined in the Quantifi YAML profiles.

### Command Reference (from YAML profiles)
**Laser** (class: `laser`, profile: `quantifi_photonics_laser_1000.yaml`):
- `get_wavelength` — query wavelength (streamable: false)
- `get_power` — query output power (streamable: false)  
- `temperature` — query temperature (streamable: true)
- `get_output_state` — query on/off state

**VOA** (class: `voa`, profile: `quantifi_photonics_voa.yaml`):
- `query_attenuation` — query current attenuation (streamable: true)
- `query_input_power` — query input power (streamable: true)
- `query_output_power` — query output power (streamable: true)

**Power Meter** (class: `power_meter`, profile: `quantifi_photonics_power_1400.yaml`):
- `measure_power` — measure optical power (streamable: true)
- `query_trace` — query trace data (streamable: true)

**OSA** (class: `osa`, profile: `quantifi_photonics_osa_1000.yaml`):
- `sweep_wavelength_data` — get spectrum sweep data (streamable: true)
- `sweep_frequency_data` — get frequency sweep data (streamable: true)
- `calculate_osnr` — calculate OSNR (streamable: true)

**Switch** (class: `switch`, profile: `quantifi_photonics_switch.yaml`):
- `get_channel_state` — query current switch channel
- No streamable commands

### Files to Modify
- `web/src/measurement-templates.ts` (in ~/work/galois/cloud/) — add template entries

### Files to Read for Context
- `web/src/measurement-templates.ts` — existing template format/structure
- `web/src/pages/Monitor.tsx` — how templates are consumed (getDefaultTemplates, MeasurementPicker)
- `web/src/components/monitor/ChartWidget.tsx` — how chart type is determined from data
- The 5 Quantifi YAML profiles listed above (in daemon-clean repo) — exact command names, params, return types

### Validation
- [x] `getDefaultTemplates("power_meter")` returns templates with `measure_power` 
- [x] `getDefaultTemplates("laser")` returns templates with `temperature`
- [x] `getDefaultTemplates("voa")` returns templates with `query_attenuation`, `query_output_power`
- [x] `getDefaultTemplates("osa")` returns templates with `sweep_wavelength_data`
- [x] Template format matches existing entries (correct fields, chartType, etc.)
- [x] No TypeScript compilation errors

---

## Task 3: Networking — LAN IP Passthrough
**Status:** COMPLETED
**Assignee:** opus-networking
**Priority:** HIGH (blocks gRPC from Docker → daemon)

### Problem
Cloud backend in Docker can't reach daemon because it only has `tailnet_ip` (unreachable from Docker bridge) and `hostname` (unresolvable). Daemon doesn't report its LAN IP.

### Spec
Full spec at `docs/edge-networking-spec.md` — read it entirely before starting.

### Summary of Changes
**Daemon** (`/Users/alexhernandez/work/galois/daemon-clean/`):
- `internal/registration/registration.go` — add `lan_ip` field to payloads, add `localOutboundIP()` helper, populate in register + heartbeat

**Backend** (`~/work/galois/cloud/backend/`):
- New migration `021_lan_ip` — add `lan_ip TEXT` to `edges` table
- `internal/handler/edge.go` — accept/store `lan_ip` in register + heartbeat
- `internal/grpcclient/manager.go` — three-tier fallback: tailnet → lan_ip → hostname
- All caller sites (13+) — pass lan_ip to connection functions

### Validation
See acceptance criteria in `docs/edge-networking-spec.md` (16 items).

---

## Task 4: Custom SCPI Stream Button
**Status:** COMPLETED  
**Assignee:** opus-scpi-button
**Priority:** MEDIUM (nice-to-have for demo)

### Problem
The "create a custom SCPI stream" link in MeasurementPicker's footer is a noop: `onCustomStream: () => {/* noop for now */}`. Users can't create streams for arbitrary SCPI commands.

### Fix
Wire up the button to open a dialog/modal that lets the user type a raw SCPI command string and interval, then creates a stream via `POST /api/v1/streams`.

### Files to Read
- `~/work/galois/cloud/web/src/pages/Monitor.tsx` — where MeasurementPicker is rendered, how streams are created (handleStartStream)
- `~/work/galois/cloud/web/src/components/monitor/MeasurementPicker.tsx` — the noop callback
- `~/work/galois/cloud/web/src/components/monitor/ChartWidget.tsx` — how data renders

### Files to Modify
- `~/work/galois/cloud/web/src/pages/Monitor.tsx` — add custom SCPI stream creation handler
- `~/work/galois/cloud/web/src/components/monitor/MeasurementPicker.tsx` — wire up the callback, possibly add inline form

### Validation
- [ ] "Create custom SCPI stream" button opens a form/dialog
- [ ] User can type a SCPI command string and interval
- [ ] Submitting creates a stream and chart appears
- [ ] No TypeScript compilation errors
