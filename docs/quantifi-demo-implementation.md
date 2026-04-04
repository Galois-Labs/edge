# Quantifi Photonics Demo — Codebase Implementation Plan

## Status

| Phase | Description | Repo | Status |
|-------|------------|------|--------|
| 1 | YAML Profiles (26 files) | edge | DONE — merged to main |
| 2 | Simulation Engine (`contrib/simulation/`) | edge | To do |
| 3 | SCPI Command Log Panel | cloud | To do |
| 5 | Topology Enhancements | cloud | To do |
| 6 | IL Validation Sequence + Topology Fixture | cloud | To do |
| 7 | AI Photonics Context | cloud/agent | To do |
| CF | Charting Fixes + CSV Export | cloud | Cofounder (independent) |

Phases 2, 3, 5, 7 can run in parallel. Phase 6 depends on 2, 5, and cofounder's charting.

## Branches

- `edge`: `quantifi-demo` (branched from `bhavik/edits`, main merged)
- `cloud`: `quantifi-demo` (branched from `bhavik/edits`, main merged)

---

## Phase 2: Simulation Engine

**Location:** `edge/contrib/simulation/`

**Files to create:**
- `contrib/simulation/__init__.py` — package init
- `contrib/simulation/engine.py` — SimulatedInstrumentManager
- `contrib/simulation/bench.py` — physics model + instrument state
- `contrib/simulation/run_sim.py` — standalone entry point
- `tests/test_simulation.py` — unit tests

**How to run:** `python -m contrib.simulation.run_sim` (no env vars, no production code changes)

**Interface:** Must match `InstrumentManager` from `src/galois_edge/instrument_manager.py` and `MockInstrumentManager` from `tests/conftest.py`:
- `list_resources()`, `discover_resources()`, `rescan_all()`, `rescan_gpib()`
- `connect(visa_address, timeout, max_attempts, retry_delay)` → instrument_id
- `disconnect(instrument_id)`, `disconnect_all()`, `is_connected(instrument_id)`
- `query(instrument_id, command)`, `write(instrument_id, command)`, `read(instrument_id)`
- `identify(instrument_id)`, `query_binary_values(instrument_id, command, ...)`
- `mark_absent(visa_address)`, `canonical_id(instrument_id)`
- Properties: `gpib_available`, `usb_available`, `lan_available`, `visa_available`

**5 virtual instruments (TCPIP VISA addresses):**
- `TCPIP::192.168.1.10::5025::SOCKET` — LASER 1000 (tunable laser)
- `TCPIP::192.168.1.11::5025::SOCKET` — SWITCH (optical switch)
- `TCPIP::192.168.1.12::5025::SOCKET` — VOA (variable optical attenuator)
- `TCPIP::192.168.1.13::5025::SOCKET` — POWER 1400 (power meter)
- `TCPIP::192.168.1.14::5025::SOCKET` — OSA 1000 (spectrum analyzer)

**Real SCPI commands from profiles (simulation must handle these):**

Laser (`quantifi_photonics_laser_1000.yaml`):
- `:OUTPut{source}:CHANnel{channel}:STATE?` / `:OUTPut{source}:CHANnel{channel}:STATE {state}`
- `:SOURce{source}:CHANnel{channel}:POWer? {param}` / `:SOURce{source}:CHANnel{channel}:POWer {value}`
- `:SOURce{source}:CHANnel{channel}:WAVelength? {param}` / `:SOURce{source}:CHANnel{channel}:WAVelength {value}`
- `*IDN?` → `Quantifi Photonics,LASER 1000,SN00001,1.0.0`

Switch (`quantifi_photonics_switch.yaml`):
- `:ROUTe{route}:CHANnel{channel}:STATE? {mode}` / `:ROUTe{route}:CHANnel{channel}:STATE {value}`
- `*IDN?` → `Quantifi Photonics,SWITCH,SN00002,1.0.0`

VOA (`quantifi_photonics_voa.yaml`):
- `:INPut{slot}:CHANnel{channel}:ATTenuation? {param}` (query)
- `:INPut{slot}:CHANnel{channel}:ATTenuation {value} {unit}` (write, unit default DB)
- `:OUTPut{slot}:CHANnel{channel}:POWer? {param}` (query output power)
- `*IDN?` → `Quantifi Photonics,VOA,SN00003,1.0.0`

Power Meter (`quantifi_photonics_power_1400.yaml`):
- `:SENSe{slot}:CHANnel{channel}:POWer? {param}` (param default ACT)
- `*IDN?` → `Quantifi Photonics,POWER-1400,SN00004,1.0.0`

OSA (`quantifi_photonics_osa_1000.yaml`):
- `:INITiate{slot}:CHANnel{channel}:SWEep` (write — start sweep)
- `:SENSe{slot}:CHANnel{channel}:SWEep:WAVelength? {data_type}` (data_type: X, Y, FULL)
- `:SENSe{slot}:CHANnel{channel}:WAVelength:STARt?` / setter
- `:SENSe{slot}:CHANnel{channel}:WAVelength:STOP?` / setter
- `:CALCulate{slot}:MARKer{marker}:MSEarch? {pth}` (peak search)
- `*IDN?` → `Quantifi Photonics,OSA 1000,SN00005,1.0.0`

**Physics model (bench.py):**
```
P_received = P_laser - IL_switch - A_VOA - IL_path - IL_DUT

P_laser = 6.0 dBm (default)
IL_switch = 0.8 dB
A_VOA = user-controlled (0-60 dB)
IL_path_A (channel 1) = 1.3 dB
IL_path_B (channel 2) = 4.5 dB  ← the anomaly (3.2 dB excess)
IL_DUT = 0.5 dB

Path A at 0 dB VOA: 6.0 - 0.8 - 0.0 - 1.3 - 0.5 = 3.4 dBm
Path B at 0 dB VOA: 6.0 - 0.8 - 0.0 - 4.5 - 0.5 = 0.2 dBm
Path B at 26 dB VOA: 0.2 - 26 = -25.8 dBm (FAIL, threshold = -25.0)

Spectrum: Gaussian centered at laser wavelength, FWHM = 0.08 nm, 401 points
```

---

## Phase 3: SCPI Command Log Panel (Permanent Feature)

**Location:** `cloud/`

**New files:**
- `backend/internal/db/migrations/017_command_log.up.sql` — command_log table
- `backend/internal/db/migrations/017_command_log.down.sql` — drop table
- `backend/internal/handler/command_log.go` — List + SSE endpoints
- `web/src/components/monitor/CommandLog.tsx` — real-time scrolling log

**Modified files:**
- `backend/internal/handler/instrument.go` — log after gRPC command execution
- `backend/internal/server/routes.go` — register command-log routes
- `web/src/pages/Monitor.tsx` — add CommandLog component

**DB schema:**
```sql
CREATE TABLE command_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL,
    team_id UUID,
    edge_id UUID NOT NULL,
    instrument_id UUID NOT NULL,
    command_name TEXT,
    scpi_sent TEXT NOT NULL,
    scpi_received TEXT,
    status TEXT NOT NULL DEFAULT 'ok',
    duration_ms INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

**API:**
- `GET /api/v1/command-log?instrument_id=X&limit=100` — paginated history
- `GET /api/v1/command-log/sse` — real-time SSE stream

---

## Phase 5: Topology Enhancements

**New files:**
- `web/src/components/topology/PhotonicsEdge.tsx` — custom edge with arrows + active/inactive

**Modified files:**
- `web/src/components/topology/TopologyEditor.tsx` — register PhotonicsEdge as default edge type
- `web/src/components/topology/InstrumentNode.tsx` — link to real instruments, live status polling
- `web/src/types/topology.ts` — extend with isActive, instrumentId, measurementStatus
- `web/src/hooks/use-topology.ts` — add instrument status refresh

---

## Phase 6: IL Validation Sequence + Topology Fixture

Demo content only (API payloads, not code). Create after Phases 2 and 5 complete.

---

## Phase 7: AI Photonics Context

**Modified file:** `agent/galois_agent/agent/claude.py` — append photonics domain knowledge block.
