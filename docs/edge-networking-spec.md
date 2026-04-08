# Edge Daemon -> Cloud Backend Networking Fix

## Problem Statement

The cloud backend cannot reach edge daemons over gRPC because it lacks a routable IP address for them.

**Root cause:** The daemon reports `tailnet_ip` (100.x.x.x from tsnet) and `hostname` (OS hostname) during registration. The backend runs in a Docker bridge network where:
- Tailnet IPs are not routable (the container does not join the tailnet)
- OS hostnames are not DNS-resolvable from inside Docker

The daemon always binds a fallback listener on `0.0.0.0:50051`, so it IS reachable via its LAN IP (e.g., `192.168.1.81`). The problem is that the LAN IP is never reported to the backend.

**Current fallback chain in backend:** `tailnet_ip` -> `hostname` -> failure

**Required fallback chain:** `tailnet_ip` -> `lan_ip` -> `hostname`

---

## Scope

Two repositories are affected:

| Repo | Path | Language |
|------|------|----------|
| Edge daemon | `/Users/alexhernandez/work/galois/daemon-clean/` | Go |
| Cloud backend | `/Users/alexhernandez/work/galois/cloud/backend/` | Go |

---

## Part 1: Edge Daemon Changes

### 1.1 Report `lan_ip` in registration and heartbeat payloads

**File:** `internal/registration/registration.go`

The `localOutboundIP()` helper already exists (line 672) and is already wired as the default `IPFunc` fallback (line 188). The problem is that `lan_ip` is never sent as a separate field -- it only populates `tailnet_ip` when tsnet is absent.

**Changes:**

#### 1.1a Add `lan_ip` field to `registerPayload` (line 380)

Current:
```go
type registerPayload struct {
	Name        string           `json:"name"`
	Hostname    string           `json:"hostname"`
	TailnetIP   string           `json:"tailnet_ip,omitempty"`
	GRPCPort    int              `json:"grpc_port"`
	WSPort      int              `json:"ws_port"`
	Version     string           `json:"version,omitempty"`
	OSInfo      string           `json:"os_info,omitempty"`
	Instruments []InstrumentInfo `json:"instruments"`
}
```

New:
```go
type registerPayload struct {
	Name        string           `json:"name"`
	Hostname    string           `json:"hostname"`
	TailnetIP   string           `json:"tailnet_ip,omitempty"`
	LanIP       string           `json:"lan_ip,omitempty"`
	GRPCPort    int              `json:"grpc_port"`
	WSPort      int              `json:"ws_port"`
	Version     string           `json:"version,omitempty"`
	OSInfo      string           `json:"os_info,omitempty"`
	Instruments []InstrumentInfo `json:"instruments"`
}
```

#### 1.1b Add `lan_ip` field to `heartbeatPayload` (line 406)

Current:
```go
type heartbeatPayload struct {
	TailnetIP   string           `json:"tailnet_ip,omitempty"`
	Status      string           `json:"status"`
	Instruments []InstrumentInfo `json:"instruments,omitempty"`
}
```

New:
```go
type heartbeatPayload struct {
	TailnetIP   string           `json:"tailnet_ip,omitempty"`
	LanIP       string           `json:"lan_ip,omitempty"`
	Status      string           `json:"status"`
	Instruments []InstrumentInfo `json:"instruments,omitempty"`
}
```

#### 1.1c Populate `LanIP` in `doRegister` (line 425)

In the `doRegister` method, set `LanIP` to the result of `localOutboundIP()`:

```go
payload := registerPayload{
	Name:        m.cfg.EdgeName,
	Hostname:    m.cfg.Hostname,
	TailnetIP:   m.cfg.IPFunc(),
	LanIP:       localOutboundIP(),
	GRPCPort:    m.cfg.GRPCPort,
	WSPort:      m.cfg.WSPort,
	Version:     m.cfg.Version,
	OSInfo:      m.cfg.OSInfo,
	Instruments: instruments,
}
```

Note: `LanIP` always calls `localOutboundIP()` directly, regardless of whether tsnet is running. `IPFunc()` continues to return the tsnet IP when available (or LAN IP as fallback via the default `IPFunc`). This means:
- With tsnet: `tailnet_ip` = 100.x.x.x, `lan_ip` = 192.168.x.x
- Without tsnet: `tailnet_ip` = 192.168.x.x (from default IPFunc), `lan_ip` = 192.168.x.x (same value, harmless duplication)

#### 1.1d Populate `LanIP` in `heartbeat` (line 529)

```go
payload := heartbeatPayload{
	TailnetIP: m.cfg.IPFunc(),
	LanIP:     localOutboundIP(),
	Status:    "online",
}
```

### 1.2 Fix `IPFunc` default to NOT use `localOutboundIP()`

**File:** `internal/registration/registration.go`, line 187-189

**Rationale:** Currently the default `IPFunc` returns `localOutboundIP()`, which puts the LAN IP into the `tailnet_ip` field. With the new `lan_ip` field, the `tailnet_ip` field should be empty when tsnet is not running. This prevents the backend from storing a LAN IP in the `tailnet_ip` column and ensures clean semantics.

Current:
```go
if c.IPFunc == nil {
	c.IPFunc = func() string { return localOutboundIP() }
}
```

New:
```go
if c.IPFunc == nil {
	c.IPFunc = func() string { return "" }
}
```

### 1.3 Fix `start.go` initial IPFunc

**File:** `internal/cli/start.go`, line 143

The initial `IPFunc` passed during manager creation already returns empty string, which is correct. No change needed -- but note that after tsnet starts (line 219), `SetIPFunc(tsnetSrv.IPv4)` correctly sets it to the real tsnet IP. This is fine.

**No changes required in `start.go`.**

---

## Part 2: Cloud Backend Changes

### 2.1 Database Migration

**File (new):** `internal/db/migrations/021_lan_ip.up.sql`

```sql
ALTER TABLE edges ADD COLUMN lan_ip TEXT;
```

**File (new):** `internal/db/migrations/021_lan_ip.down.sql`

```sql
ALTER TABLE edges DROP COLUMN IF EXISTS lan_ip;
```

The next migration number is `021` (highest existing is `020_chat_domain_mode`).

### 2.2 Edge Handler: Accept and Store `lan_ip`

**File:** `internal/handler/edge.go`

#### 2.2a Add `LanIP` to `RegisterEdgeRequest` (line 33)

```go
type RegisterEdgeRequest struct {
	Name        string            `json:"name"`
	Hostname    string            `json:"hostname"`
	TailnetIP   string            `json:"tailnet_ip"`
	LanIP       string            `json:"lan_ip"`
	GRPCPort    int               `json:"grpc_port"`
	WSPort      int               `json:"ws_port"`
	Version     string            `json:"version"`
	OSInfo      string            `json:"os_info"`
	Instruments []json.RawMessage `json:"instruments"`
}
```

#### 2.2b Add `LanIP` to `EdgeResponse` (line 46)

Add between `TailnetIP` and `GRPCPort`:
```go
LanIP         *string    `json:"lan_ip"`
```

#### 2.2c Update Register SQL (lines 157-171)

Current INSERT:
```sql
INSERT INTO edges (user_id, team_id, name, hostname, tailnet_ip, grpc_port, ws_port, version, os_info, status, last_heartbeat)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'online', now())
ON CONFLICT (user_id, name) DO UPDATE SET
   team_id = COALESCE(EXCLUDED.team_id, edges.team_id),
   hostname = EXCLUDED.hostname, tailnet_ip = EXCLUDED.tailnet_ip,
   grpc_port = EXCLUDED.grpc_port, ws_port = EXCLUDED.ws_port,
   version = EXCLUDED.version, os_info = EXCLUDED.os_info,
   status = 'online', last_heartbeat = now()
RETURNING id, user_id, team_id, name, hostname, tailnet_ip, grpc_port, ws_port, status, version, os_info, last_heartbeat, registered_at
```

New INSERT:
```sql
INSERT INTO edges (user_id, team_id, name, hostname, tailnet_ip, lan_ip, grpc_port, ws_port, version, os_info, status, last_heartbeat)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 'online', now())
ON CONFLICT (user_id, name) DO UPDATE SET
   team_id = COALESCE(EXCLUDED.team_id, edges.team_id),
   hostname = EXCLUDED.hostname, tailnet_ip = EXCLUDED.tailnet_ip,
   lan_ip = EXCLUDED.lan_ip,
   grpc_port = EXCLUDED.grpc_port, ws_port = EXCLUDED.ws_port,
   version = EXCLUDED.version, os_info = EXCLUDED.os_info,
   status = 'online', last_heartbeat = now()
RETURNING id, user_id, team_id, name, hostname, tailnet_ip, lan_ip, grpc_port, ws_port, status, version, os_info, last_heartbeat, registered_at
```

Update the parameter list to include `nilIfEmpty(body.LanIP)` and update the `.Scan()` call to include `&edge.LanIP`.

#### 2.2d Add `LanIP` to `HeartbeatRequest` (line 119)

```go
type HeartbeatRequest struct {
	TailnetIP   string              `json:"tailnet_ip,omitempty"`
	LanIP       string              `json:"lan_ip,omitempty"`
	Status      string              `json:"status,omitempty"`
	Instruments []InstrumentPayload `json:"instruments,omitempty"`
}
```

#### 2.2e Update Heartbeat handler (lines 244-251)

Current:
```go
if body.TailnetIP != "" {
	_, err = h.db.Exec(r.Context(),
		`UPDATE edges SET last_heartbeat = now(), status = 'online', tailnet_ip = $2 WHERE id = $1`,
		edgeID, body.TailnetIP)
} else {
	_, err = h.db.Exec(r.Context(),
		`UPDATE edges SET last_heartbeat = now(), status = 'online' WHERE id = $1`, edgeID)
}
```

New (always update both fields when provided):
```go
_, err = h.db.Exec(r.Context(),
	`UPDATE edges SET
	   last_heartbeat = now(),
	   status = 'online',
	   tailnet_ip = COALESCE(NULLIF($2, ''), tailnet_ip),
	   lan_ip = COALESCE(NULLIF($3, ''), lan_ip)
	 WHERE id = $1`,
	edgeID, body.TailnetIP, body.LanIP)
```

This preserves existing values when the daemon sends empty strings (e.g., tsnet not running sends empty `tailnet_ip` but non-empty `lan_ip`).

#### 2.2f Update List and Get queries

**List** (line 272): Add `lan_ip` to the SELECT column list and update `rows.Scan()`.

**Get** (line 301): Add `lan_ip` to the SELECT column list and update `QueryRow().Scan()`.

### 2.3 gRPC Manager: Three-tier Fallback

**File:** `internal/grpcclient/manager.go`

#### 2.3a Update `GetConnectionWithFallback` signature (line 69)

Current:
```go
func (m *Manager) GetConnectionWithFallback(edgeID, tailnetIP, directIP string, port int) (*grpc.ClientConn, error) {
```

New:
```go
func (m *Manager) GetConnectionWithFallback(edgeID, tailnetIP, lanIP, hostname string, port int) (*grpc.ClientConn, error) {
```

#### 2.3b Implement three-tier fallback

```go
func (m *Manager) GetConnectionWithFallback(edgeID, tailnetIP, lanIP, hostname string, port int) (*grpc.ClientConn, error) {
	// Tier 1: tailnet IP (most reliable when available)
	if tailnetIP != "" {
		conn, err := m.GetConnection(edgeID, tailnetIP, port)
		if err == nil {
			m.logger.Debug().
				Str("edge_id", edgeID).
				Str("tailnet_ip", tailnetIP).
				Msg("connected via tailnet")
			return conn, nil
		}
		m.logger.Warn().Err(err).
			Str("edge_id", edgeID).
			Str("tailnet_ip", tailnetIP).
			Msg("tailnet connection failed, trying LAN IP")
		m.RemoveConnection(edgeID)
	}

	// Tier 2: LAN IP (routable from Docker via bridge network)
	if lanIP != "" {
		conn, err := m.GetConnection(edgeID, lanIP, port)
		if err == nil {
			m.logger.Debug().
				Str("edge_id", edgeID).
				Str("lan_ip", lanIP).
				Msg("connected via LAN IP")
			return conn, nil
		}
		m.logger.Warn().Err(err).
			Str("edge_id", edgeID).
			Str("lan_ip", lanIP).
			Msg("LAN IP connection failed, trying hostname")
		m.RemoveConnection(edgeID)
	}

	// Tier 3: hostname (last resort, requires DNS resolution)
	if hostname != "" {
		conn, err := m.GetConnection(edgeID, hostname, port)
		if err == nil {
			m.logger.Debug().
				Str("edge_id", edgeID).
				Str("hostname", hostname).
				Msg("connected via hostname")
			return conn, nil
		}
		m.RemoveConnection(edgeID)
		return nil, fmt.Errorf("failed to connect to edge %s via hostname %s: %w", edgeID, hostname, err)
	}

	return nil, fmt.Errorf("no reachable address for edge %s (tailnet_ip=%q, lan_ip=%q, hostname=%q)",
		edgeID, tailnetIP, lanIP, hostname)
}
```

### 2.4 Update All Callers

Every place that queries for edge connection info and calls `GetConnection` or `GetConnectionWithFallback` must be updated to include `lan_ip`.

#### 2.4a `internal/handler/instrument.go`

**4 call sites** at lines ~182, ~297, ~418, ~519.

Each has a query like:
```sql
SELECT i.edge_id, i.address, e.tailnet_ip, e.hostname, e.grpc_port ...
```
or:
```sql
SELECT tailnet_ip, hostname, grpc_port FROM edges ...
```

For each:
1. Add `e.lan_ip` (or `lan_ip`) to the SELECT list
2. Add a `var lanIP *string` variable and include it in `.Scan()`
3. Update the `GetConnectionWithFallback` call:

```go
// Before:
conn, err := h.grpcManager.GetConnectionWithFallback(edgeID, derefStr(tailnetIP), derefStr(hostname), grpcPort)

// After:
conn, err := h.grpcManager.GetConnectionWithFallback(edgeID, derefStr(tailnetIP), derefStr(lanIP), derefStr(hostname), grpcPort)
```

**Specific locations:**
- Line 182: `SELECT i.edge_id, i.address, e.tailnet_ip, e.hostname, e.grpc_port` -> add `e.lan_ip`
- Line 193: update `GetConnectionWithFallback` call
- Line 297: `SELECT i.edge_id, i.address, e.tailnet_ip, e.hostname, e.grpc_port` -> add `e.lan_ip`
- Line 309: update `GetConnectionWithFallback` call
- Line 418: `SELECT i.edge_id, i.address, e.tailnet_ip, e.hostname, e.grpc_port` -> add `e.lan_ip`
- Line 428: update `GetConnectionWithFallback` call
- Line 519: `SELECT tailnet_ip, hostname, grpc_port FROM edges` -> add `lan_ip`
- Line 527: update `GetConnectionWithFallback` call

#### 2.4b `internal/handler/stream.go`

**1 call site** at line ~57.

Current query uses `COALESCE(e.tailnet_ip, e.hostname, '')` which flattens everything to one address. Replace with proper three-column select:

```sql
-- Before:
SELECT i.edge_id, i.address, COALESCE(e.tailnet_ip, e.hostname, ''), e.grpc_port

-- After:
SELECT i.edge_id, i.address, e.tailnet_ip, e.lan_ip, e.hostname, e.grpc_port
```

Update the variables and scan:
```go
var edgeID, visaAddress string
var tailnetIP, lanIP, hostname *string
var grpcPort int
```

Then replace the `StreamManager.Start()` call to pass all three addresses. This requires updating the `StreamManager.Start` method signature (see 2.4f below).

#### 2.4c `internal/handler/kernel_proxy.go`

**Multiple call sites:**

1. **Line 61** (ListResources): `COALESCE(tailnet_ip, hostname, '')` -> `tailnet_ip, lan_ip, hostname`
   - Change the edge struct to carry all three fields
   - Update the `GetConnection` call at line 96 to use `GetConnectionWithFallback`

2. **Line 146** (CreateSession): `COALESCE(e.tailnet_ip, e.hostname, '')` -> `e.tailnet_ip, e.lan_ip, e.hostname`
   - Scan into three separate variables
   - Resolve best address using `GetConnectionWithFallback` or store all three in the session

3. **Line 517** (StreamStart): `COALESCE(tailnet_ip, hostname, '')` -> `tailnet_ip, lan_ip, hostname`
   - Same pattern: scan three columns, use `GetConnectionWithFallback`

4. **Lines 208, 284, 342, 385** (session-based calls): These use `session.TailnetIP` which was populated from COALESCE. See 2.4d below.

#### 2.4d `internal/service/kernel_session.go`

**File:** `internal/service/kernel_session.go`

Update `KernelSession` struct to carry `LanIP`:

```go
type KernelSession struct {
	EdgeID       string
	InstrumentID string
	TailnetIP    string
	LanIP        string
	GRPCPort     int
	VisaAddress  string
	LastResponse string
}
```

Update session creation in `kernel_proxy.go` (line 157) to populate `LanIP`.

Update all session-based `GetConnection` calls (lines 208, 284, 342, 385) to use `GetConnectionWithFallback` with `session.TailnetIP`, `session.LanIP`, and optionally empty hostname:

```go
// Before:
conn, err := h.grpcManager.GetConnection(session.EdgeID, session.TailnetIP, session.GRPCPort)

// After:
conn, err := h.grpcManager.GetConnectionWithFallback(session.EdgeID, session.TailnetIP, session.LanIP, "", session.GRPCPort)
```

#### 2.4e `internal/handler/driver_analysis.go`

**2 call sites** at lines ~642 and ~744.

Same pattern as instrument.go:
1. Add `lan_ip` to SELECT
2. Add `var lanIP *string` to scan targets
3. Update `GetConnectionWithFallback(edgeID, derefStr(tailnetIP), derefStr(lanIP), derefStr(hostname), grpcPort)`

#### 2.4f `internal/service/stream.go`

**File:** `internal/service/stream.go`, line 71

The `StreamManager.Start` method currently takes a single `address string`. It should be updated to accept the three-tier address set and use the grpc manager's fallback internally:

Option A (recommended): Change callers to resolve the connection before calling `Start`, passing the `*grpc.ClientConn` directly.

Option B: Add `tailnetIP`, `lanIP`, `hostname` parameters to `Start`.

Choose whichever minimizes churn. The key constraint is that `Start` must be able to reach the daemon via any of the three addresses.

#### 2.4g `internal/handler/camera.go`

**File:** `internal/handler/camera.go`, line 47

Current query only selects `tailnet_ip` (not even `hostname`):
```sql
SELECT tailnet_ip, grpc_port FROM edges WHERE id = $1
```

Update to:
```sql
SELECT tailnet_ip, lan_ip, hostname, grpc_port FROM edges WHERE id = $1
```

Update the variables, scan, and change `GetConnection` to `GetConnectionWithFallback`.

#### 2.4h `internal/sequences/engine.go`

**File:** `internal/sequences/engine.go`, lines ~678, ~696, ~707-713

Two queries select `e.tailnet_ip, e.hostname, e.grpc_port`. Add `e.lan_ip` to both.

The manual fallback logic at lines 707-713:
```go
address := hostname
if tailnetIP != nil && *tailnetIP != "" {
	address = *tailnetIP
}
conn, err := e.grpcManager.GetConnection(edgeID, address, grpcPort)
```

Replace with:
```go
conn, err := e.grpcManager.GetConnectionWithFallback(edgeID, derefStr(tailnetIP), derefStr(lanIP), derefStr(hostname), grpcPort)
```

Note: `derefStr` (or equivalent) may need to be available in the `sequences` package. Either extract it to a shared utility or duplicate the trivial helper:
```go
func derefStr(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}
```

---

## Summary of All Files to Modify

### Daemon repo (`/Users/alexhernandez/work/galois/daemon-clean/`)

| File | Change |
|------|--------|
| `internal/registration/registration.go` | Add `lan_ip` to payloads, populate from `localOutboundIP()`, fix default IPFunc |

### Backend repo (`~/work/galois/cloud/backend/`)

| File | Change |
|------|--------|
| `internal/db/migrations/021_lan_ip.up.sql` | NEW: `ALTER TABLE edges ADD COLUMN lan_ip TEXT` |
| `internal/db/migrations/021_lan_ip.down.sql` | NEW: `ALTER TABLE edges DROP COLUMN IF EXISTS lan_ip` |
| `internal/handler/edge.go` | Add `lan_ip` to request/response structs, register/heartbeat SQL, list/get queries |
| `internal/grpcclient/manager.go` | Update `GetConnectionWithFallback` to three-tier: tailnet -> lan_ip -> hostname |
| `internal/handler/instrument.go` | 4 sites: add `lan_ip` to queries and `GetConnectionWithFallback` calls |
| `internal/handler/stream.go` | Replace COALESCE with three-column select, pass to stream manager |
| `internal/handler/kernel_proxy.go` | Replace COALESCE patterns, update session creation, update session-based calls |
| `internal/service/kernel_session.go` | Add `LanIP` field to `KernelSession` struct |
| `internal/service/stream.go` | Update `Start` method to handle three-tier addressing |
| `internal/handler/camera.go` | Add `lan_ip` and `hostname` to query, use `GetConnectionWithFallback` |
| `internal/handler/driver_analysis.go` | 2 sites: add `lan_ip` to queries and `GetConnectionWithFallback` calls |
| `internal/sequences/engine.go` | Add `lan_ip` to queries, replace manual fallback with `GetConnectionWithFallback` |

---

## Acceptance Criteria

### Daemon

1. Registration payload JSON includes `"lan_ip"` set to the machine's outbound LAN IP (e.g., `192.168.1.81`)
2. Heartbeat payload JSON includes `"lan_ip"` set to the current outbound LAN IP
3. When tsnet is running: `tailnet_ip` contains 100.x.x.x, `lan_ip` contains 192.168.x.x (both present)
4. When tsnet is NOT running: `tailnet_ip` is empty/omitted, `lan_ip` contains 192.168.x.x
5. Existing `localOutboundIP()` helper is reused (no new network detection code needed)

### Backend

6. Migration `021_lan_ip` applies cleanly and adds `lan_ip TEXT` column to `edges`
7. Registration handler stores `lan_ip` from the daemon's payload
8. Heartbeat handler updates `lan_ip` when provided (preserves existing value when empty)
9. `GetConnectionWithFallback` tries tailnet_ip -> lan_ip -> hostname in order
10. All 13+ call sites across instrument.go, stream.go, kernel_proxy.go, camera.go, driver_analysis.go, and sequences/engine.go pass `lan_ip` to the connection function
11. No remaining `COALESCE(tailnet_ip, hostname, '')` patterns -- all replaced with explicit three-column selects
12. Edge list/get API responses include `lan_ip` field

### End-to-End

13. Backend running in Docker can connect to a daemon on the same LAN via `lan_ip` when tsnet is not configured
14. Backend running in Docker can connect to a daemon via tsnet when tailnet is available, with `lan_ip` as fallback
15. Existing tailnet-only deployments continue to work (lan_ip is simply an additional fallback)
16. `kernel_session_test.go` updated to include `LanIP` field

---

## Ordering / Dependencies

1. **Migration first** -- the `lan_ip` column must exist before the backend handler code can reference it
2. **Daemon and backend handler changes can be deployed independently** -- the backend ignores unknown JSON fields, and the daemon's new `lan_ip` field is simply dropped by an old backend. An old daemon that doesn't send `lan_ip` results in a NULL column, and `GetConnectionWithFallback` skips empty addresses gracefully
3. **Backend `GetConnectionWithFallback` signature change** must happen in the same commit as all caller updates (it's a compile-time break)

## Backward Compatibility

- Old daemons (no `lan_ip` in payload): backend stores NULL for `lan_ip`, fallback chain skips it, behaves exactly as before (tailnet_ip -> hostname)
- Old backend (no `lan_ip` column): ignores the new field in JSON, no error
- The `lan_ip` field is always `omitempty` in JSON, so old backends never see an unknown required field
