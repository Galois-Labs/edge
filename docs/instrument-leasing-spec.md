# Instrument Leasing & Edge Sharing — Technical Spec

## Status: DRAFT
**Date:** 2026-04-07
**Authors:** Gemini 3 Pro, GPT 5.2, Claude Opus (synthesis)

---

## 1. Problem Statement

Physical lab instruments can only serve one operator at a time. When multiple
teams share a workbench (common in research labs), concurrent writes cause:

- Garbled SCPI responses (interleaved commands on GPIB/serial)
- Corrupted experiments (User B changes VOA attenuation during User A's sweep)
- Silent data contamination (streaming readings change unexpectedly)

Additionally, edges (daemons) are currently user-owned (`edges.user_id`),
preventing team members from seeing or controlling shared equipment.

This spec addresses both problems with two complementary features:
1. **Edge sharing** — make edges visible/controllable across teams
2. **Instrument leasing** — per-instrument write locks with TTL

---

## 2. Edge Sharing (Team Access)

### 2.1 Design: User Ownership + Explicit Sharing

Keep `edges.user_id` as the registrant/creator. Add a junction table for
team visibility. This is the least-breakage path from the current model.

### 2.2 Schema

```sql
-- Migration: 022_edge_team_access.up.sql

CREATE TABLE edge_team_access (
    edge_id    UUID NOT NULL REFERENCES edges(id) ON DELETE CASCADE,
    team_id    UUID NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    role       TEXT NOT NULL DEFAULT 'controller',  -- 'viewer' | 'controller'
    granted_by UUID     NULL REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (edge_id, team_id)
);

CREATE INDEX idx_edge_team_access_team ON edge_team_access(team_id, edge_id);

-- Backfill from existing edges.team_id
INSERT INTO edge_team_access (edge_id, team_id, role, granted_by)
SELECT id, team_id, 'controller', user_id
FROM edges
WHERE team_id IS NOT NULL
ON CONFLICT DO NOTHING;
```

```sql
-- Migration: 022_edge_team_access.down.sql

DROP TABLE IF EXISTS edge_team_access;
```

### 2.3 Query Changes

**List edges** (currently `WHERE user_id = $1`):
```sql
SELECT DISTINCT e.*
FROM edges e
LEFT JOIN edge_team_access eta ON eta.edge_id = e.id
WHERE e.user_id = $1
   OR eta.team_id = $2;  -- $2 = current active team from X-Team-ID header
```

**Instrument authorization** (currently joins `edges.user_id`):
```sql
SELECT EXISTS(
    SELECT 1 FROM instruments i
    JOIN edges e ON i.edge_id = e.id
    LEFT JOIN edge_team_access eta ON eta.edge_id = e.id
    WHERE i.id = $1
      AND (e.user_id = $2 OR eta.team_id = $3)
)
```

### 2.4 API Changes

#### New endpoints
- `POST /edges/{id}/share` — body: `{ "team_id": "...", "role": "controller" }`
  Auth: edge owner only
- `DELETE /edges/{id}/share/{team_id}` — revoke team access
  Auth: edge owner only
- `GET /edges/{id}/shares` — list teams with access
  Auth: edge owner or team member with access

#### Modified endpoints
- `GET /api/v1/edges` — add optional `?team_id=` filter, or use `X-Team-ID`
  header to include shared edges
- `GET /api/v1/instruments` — join through `edge_team_access` for team-scoped
  queries

#### Registration auto-share
When a daemon registers with an API key that has `team_id`, auto-upsert into
`edge_team_access(edge_id, api_key.team_id, 'controller', api_key.user_id)`.

### 2.5 Files to Modify

**Backend (`~/work/galois/cloud/backend/`):**
- `internal/db/migrations/022_edge_team_access.{up,down}.sql` — new
- `internal/handler/edge.go` — List/Get queries, new share endpoints
- `internal/handler/instrument.go` — authorization joins
- `internal/handler/instrument_asset.go` — authorization joins
- `internal/handler/stream.go` — authorization joins
- `internal/server/routes.go` — register share endpoints

**Frontend (`~/work/galois/cloud/web/`):**
- `src/pages/Daemons.tsx` — pass team context to edge listing
- `src/pages/DaemonDetail.tsx` — show share controls for owner
- `src/hooks/use-edges.ts` — pass team_id to API call

### 2.6 Daemon Changes

**None.** The daemon is team-unaware. Sharing is a cloud-side concern.

---

## 3. Instrument Leasing (Concurrency Control)

### 3.1 Design: Per-Instrument Write Leases with TTL

- **Granularity:** per-instrument (not per-edge)
- **Lock type:** hard block for writes, free for reads
- **Source of truth:** cloud DB (`instrument_leases` table)
- **Enforcement:** daemon checks lease before executing write commands
- **Expiry:** TTL-based leases (default 30s, renewable)
- **Streaming:** no lease required (read-only)
- **Sequences:** acquire all instrument leases atomically before execution

### 3.2 Schema

```sql
-- Migration: 023_instrument_leases.up.sql

CREATE TABLE instrument_leases (
    instrument_id      UUID PRIMARY KEY REFERENCES instruments(id) ON DELETE CASCADE,
    lease_type         TEXT NOT NULL DEFAULT 'write'
                       CHECK (lease_type IN ('write')),
    holder_user_id     UUID     NULL REFERENCES users(id) ON DELETE SET NULL,
    holder_team_id     UUID     NULL REFERENCES teams(id) ON DELETE SET NULL,
    holder_session_id  UUID NOT NULL,
    purpose            TEXT     NULL,  -- 'manual', 'sequence', 'calibration'
    acquired_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at         TIMESTAMPTZ NOT NULL,
    lease_version      BIGINT NOT NULL DEFAULT 0
);

CREATE INDEX idx_instrument_leases_expires ON instrument_leases(expires_at);
```

```sql
-- Migration: 023_instrument_leases.down.sql

DROP TABLE IF EXISTS instrument_leases;
```

### 3.3 Lease Operations (SQL)

**Acquire** (succeed only if no active lease, or same session, or expired):
```sql
INSERT INTO instrument_leases
    (instrument_id, holder_user_id, holder_team_id, holder_session_id, purpose, expires_at)
VALUES ($1, $2, $3, $4, $5, now() + ($6 || ' seconds')::interval)
ON CONFLICT (instrument_id) DO UPDATE SET
    holder_user_id = EXCLUDED.holder_user_id,
    holder_team_id = EXCLUDED.holder_team_id,
    holder_session_id = EXCLUDED.holder_session_id,
    purpose = EXCLUDED.purpose,
    acquired_at = now(),
    expires_at = EXCLUDED.expires_at,
    lease_version = instrument_leases.lease_version + 1
WHERE instrument_leases.expires_at < now()
   OR instrument_leases.holder_session_id = $4;
-- If rowcount == 0 => conflict, return 409 with current holder info
```

**Renew:**
```sql
UPDATE instrument_leases
SET expires_at = now() + ($2 || ' seconds')::interval,
    lease_version = lease_version + 1
WHERE instrument_id = $1
  AND holder_session_id = $3
  AND expires_at >= now();
```

**Release:**
```sql
DELETE FROM instrument_leases
WHERE instrument_id = $1 AND holder_session_id = $2;
```

**Multi-instrument acquire** (for sequences):
```sql
-- In a single transaction:
-- 1. Check no unexpired conflicting leases exist
SELECT instrument_id, holder_user_id, expires_at
FROM instrument_leases
WHERE instrument_id = ANY($instrument_ids)
  AND expires_at >= now()
  AND holder_session_id != $session_id;
-- If any rows returned => 409 Conflict

-- 2. Upsert all (sorted by instrument_id to prevent deadlocks)
INSERT INTO instrument_leases (instrument_id, ...) VALUES ...
ON CONFLICT (instrument_id) DO UPDATE SET ...
WHERE instrument_leases.expires_at < now()
   OR instrument_leases.holder_session_id = $session_id;
```

### 3.4 API Endpoints

- `POST /instruments/{id}/lease` — acquire write lease
  Body: `{ "ttl_seconds": 30, "purpose": "manual" }`
  Returns: `{ "session_id": "...", "expires_at": "..." }`
  Auth: user must have access to instrument's edge

- `POST /instruments/{id}/lease/renew` — extend TTL
  Body: `{ "session_id": "...", "ttl_seconds": 30 }`

- `DELETE /instruments/{id}/lease` — release
  Body: `{ "session_id": "..." }`

- `GET /instruments/{id}/lease` — introspect current lease
  Returns: holder info + expiry, or null

- `POST /sequences/lease` — atomic multi-instrument acquire
  Body: `{ "instrument_ids": [...], "ttl_seconds": 60, "purpose": "sequence" }`

### 3.5 Daemon Enforcement

#### Read vs Write classification
- Commands ending in `?` → READ (no lease required)
- Everything else → WRITE (lease required)
- Override: profile YAML can tag specific commands as `mutating: true/false`

#### Proto changes
Add optional `session_id` to command/sequence RPCs:
```protobuf
message ExecuteCommandRequest {
    // ... existing fields ...
    string session_id = 10;  // Optional lease session ID
}

message StreamMeasurementRequest {
    // ... existing fields ...
    // No session_id — streams are read-only
}

message ExecuteSequenceRequest {
    // ... existing fields ...
    string session_id = 10;  // Required for sequences
}
```

#### Enforcement logic in grpc_server.py
Before executing a write command:
1. Check if instrument has an active lease (cache from heartbeat or DB call)
2. If lease exists and `request.session_id` doesn't match → reject with
   `FAILED_PRECONDITION` ("Instrument controlled by {holder}")
3. If no lease or matching session → proceed

#### Lease state in heartbeat
Daemon reports active leases in heartbeat payload so cloud DB stays in sync
even if direct lease API calls fail:
```json
{
    "instruments": [
        {
            "address": "GPIB0::28::INSTR",
            "locked_by_session": "uuid",
            "lock_expires": "2026-04-07T23:30:00Z"
        }
    ]
}
```

### 3.6 Frontend Changes

- Instrument cards show lock badge: "Controlled by Alice" with expiry countdown
- Write command buttons are disabled when another user holds the lease
- "Take Control" button acquires a lease (with confirmation if stealing from expired)
- Streaming charts show a banner: "Instrument controlled by Bob" (informational)
- Sequence execution auto-acquires leases and shows progress

### 3.7 Files to Modify

**Backend:**
- `internal/db/migrations/023_instrument_leases.{up,down}.sql` — new
- `internal/handler/instrument_lease.go` — new handler
- `internal/server/routes.go` — register lease endpoints
- `internal/handler/instrument.go` — check lease on ExecuteCommand
- `internal/handler/stream.go` — pass session_id to daemon
- `internal/sequences/engine.go` — acquire leases before sequence

**Daemon:**
- `proto/edge/v1/edge.proto` — add session_id to RPCs
- `src/galois_edge/grpc_server.py` — enforce lease on writes
- `internal/registration/registration.go` — report lease state in heartbeat

**Frontend:**
- `web/src/components/instruments/InstrumentCard.tsx` — lock badge
- `web/src/pages/InstrumentDetail.tsx` — take control button
- `web/src/hooks/use-instrument-lease.ts` — new hook
- `web/src/pages/Monitor.tsx` — lease status in stream cards

---

## 4. Implementation Order

### Phase 1: Edge Sharing (1-2 days)
- Migration 022
- Backend query changes + share API
- Frontend team-scoped edge listing
- Auto-share on registration

### Phase 2: Instrument Leasing (2-3 days)
- Migration 023
- Lease API endpoints
- Proto changes + daemon enforcement
- Frontend lock UI

### Phase 3: Polish
- Force-take-over (admin only)
- Lease history/audit table
- Per-team display names for shared edges

---

## 5. Acceptance Criteria

### Edge Sharing
- [ ] User A registers an edge; only User A sees it initially
- [ ] User A shares edge to Team X; all Team X members see it
- [ ] Team X member can execute commands on shared edge's instruments
- [ ] Removing share revokes access immediately
- [ ] Deleting the edge cascades to all shares
- [ ] API key with team_id auto-shares on registration

### Instrument Leasing
- [ ] User acquires write lease on instrument; writes succeed
- [ ] Another user's writes are rejected with clear error
- [ ] Reads/streams work regardless of lease state
- [ ] Lease expires after TTL; instrument becomes available
- [ ] Renewing extends the TTL
- [ ] Sequence acquires all leases atomically; partial acquire fails cleanly
- [ ] UI shows who controls each instrument
- [ ] Crashed client's lease expires without manual intervention

---

## 6. Open Questions

1. Should reads during an active write lease show a warning? Or silently proceed?
2. Should the `?` SCPI heuristic be the default, or should profiles explicitly
   tag `mutating: true/false` per command?
3. Should `instrument_assets.team_id` be dropped in favor of deriving access
   through edge sharing? (Gemini 3 recommends yes)
4. Do users call the daemon directly (WebSocket/gRPC) or only via cloud proxy?
   Determines if daemon-side enforcement is mandatory.
