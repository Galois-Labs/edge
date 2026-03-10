# `galois-edge setup` Implementation Plan

## Goal

Reduce edge daemon setup from 5+ manual config steps to a single command:

```
galois-edge setup glc_XXXXX
```

## Architecture

```
Dashboard                         Pi (or any device)
─────────                         ──────────────────
User clicks "Add Edge"
  → creates API key
  → shows one-liner:
    galois-edge setup glc_XXX
                                  User runs:
                                  $ galois-edge setup glc_XXX
                                    │
                                    ├─ POST /api/v1/edges/register
                                    │    X-API-Key: glc_XXX
                                    │    body: { name, hostname, ... }
                                    │
                                    │  ← { id, pre_auth_key, headscale_url }
                                    │
                                    ├─ Write config.env
                                    ├─ Start tsnet with pre_auth_key
                                    ├─ Start Python engine
                                    ├─ Start heartbeat loop
                                    └─ ✓ "Edge registered, daemon running"
```

## Phases

---

### Phase 1 — Backend: Return `headscale_url` in register response

**Why**: The daemon needs to know the Headscale control URL to start tsnet, but
currently the user must configure it manually. The backend already knows it.

**Files to read**:
- `cloud/backend/internal/handler/edge.go` — `Register` handler, `RegisterEdgeResponse` struct
- `cloud/backend/internal/config/config.go` — where `HEADSCALE_URL` is loaded
- `cloud/backend/internal/server/server.go` — how config is wired to handler

**Files to edit**:
- `cloud/backend/internal/handler/edge.go`
  - Add `HeadscaleURL string \`json:"headscale_url,omitempty"\`` to `RegisterEdgeResponse`
  - In `Register()`, populate it from the handler's config/injected value
  - The headscale URL is a server config value, not per-edge — need to pass it to `EdgeHandler`

**Files to create**: none

**Acceptance**:
- `POST /api/v1/edges/register` response includes `headscale_url` when Headscale is configured
- Existing behavior unchanged when Headscale is not configured (field omitted)

---

### Phase 2 — Daemon: `galois-edge setup <TOKEN>` command

**Why**: Single command replaces manual config.env editing.

**Files to read** (context for subagent):
- `daemon-clean/internal/cli/root.go` — command registration pattern
- `daemon-clean/internal/cli/install.go` — reference for system-state CLI command
- `daemon-clean/internal/cli/configure.go` — config file write patterns (`config.WriteFileMap`, `resolveConfigPath`)
- `daemon-clean/internal/config/config.go` — `Config` struct, `Save()`, `FindConfigFile()`, `SystemConfigDir()`, `UserConfigDir()`
- `daemon-clean/internal/registration/registration.go` — `registerPayload`, `registerResponse`, HTTP call shape

**Files to edit**:
- `daemon-clean/internal/cli/root.go` — add `rootCmd.AddCommand(setupCmd)` in `init()`

**Files to create**:
- `daemon-clean/internal/cli/setup.go`

**`setup.go` specification**:

```
galois-edge setup <TOKEN> [flags]

Flags:
  --backend    Backend URL (default: https://cloud.galoislabs.ai, env: BACKEND_URL)
  --name       Edge name (default: hostname)
  --config     Config file path (default: auto-detect or ~/.config/galois-edge/config.env)
  --start      Start daemon after setup (default: false)
```

**Logic**:
1. Parse token from args (must start with `glc_`)
2. Resolve `--backend` (flag → env → default)
3. Resolve `--name` (flag → hostname)
4. Build `registerPayload` with name, hostname, version, os_info, empty instruments
5. `POST {backend}/api/v1/edges/register` with `X-API-Key: <token>`
6. Parse response: `id`, `pre_auth_key`, `headscale_url`
7. Write config.env via `config.WriteFileMap`:
   ```
   BACKEND_URL=https://cloud.galoislabs.ai
   REGISTRATION_TOKEN=glc_XXXXX
   EDGE_NAME=pi5
   TAILSCALE_AUTH_KEY=tskey-auth-XXXXX    (if returned)
   HEADSCALE_URL=https://headscale.xxx    (if returned)
   ```
8. Print success:
   ```
   ✓ Registered as "pi5" (edge-abc123)
   ✓ Config written to ~/.config/galois-edge/config.env

   Start the daemon:
     galois-edge start
   ```
9. If `--start`, exec into `runStart` (or print instruction to use systemd)

**Does NOT start tsnet or Python engine** — that's `start`'s job. Setup is pure
registration + config persistence. This keeps the command fast and testable.

**Acceptance**:
- `galois-edge setup glc_test123` against a test backend writes a valid config.env
- Config includes TAILSCALE_AUTH_KEY when backend returns pre_auth_key
- Errors clearly if token is invalid (401), backend unreachable, etc.
- Idempotent: running setup again overwrites config (re-registration upserts)

---

### Phase 3 — Daemon: Reorder `start.go` for pre_auth_key from config

**Why**: After `setup` writes TAILSCALE_AUTH_KEY to config, `start` must use it.
Currently this already works — `start.go` reads `cfg.TailscaleAuthKey` and passes
it to tsnet. The only issue was that registration happened AFTER tsnet, meaning
a fresh setup wouldn't have the key yet. With Phase 2, `setup` writes the key
before `start` is ever called, so the ordering in `start.go` already works.

**Files to read**:
- `daemon-clean/internal/cli/start.go` — full file, understand the startup sequence

**Files to edit**:
- `daemon-clean/internal/cli/start.go`
  - Move registration to happen BEFORE tsnet start (lines ~118-200)
  - After registration succeeds, if config doesn't have a TAILSCALE_AUTH_KEY but
    registration returned a pre_auth_key, update the in-memory config and persist
  - This handles the edge case where someone runs `start` directly without `setup`
    (e.g., they only set BACKEND_URL and REGISTRATION_TOKEN manually)

**New startup order**:
```
1. Load config, validate, init logger
2. Resolve Python binary
3. Register with backend (if BACKEND_URL set)
   → If response has pre_auth_key and config lacks TAILSCALE_AUTH_KEY:
     update cfg.TailscaleAuthKey in memory + persist to config.env
4. Start tsnet (using cfg.TailscaleAuthKey, which may now be populated)
5. Start supervisor (Python engine)
6. Create TCP proxies
7. Start heartbeat loop (registration.Manager, but skip initial register — already done)
8. Wait for signal, graceful shutdown
```

**Tricky bit**: The registration Manager currently does both initial registration
AND heartbeat loop. We need to split these concerns:
- Option A: Add a `Manager.RegisterOnce(ctx)` method that does one registration
  call and returns the response (including pre_auth_key). Then `Start()` skips
  the initial registration if already registered (edgeID is set).
- Option B: Do the initial register call directly in `start.go` (inline HTTP),
  then feed the result into the Manager for heartbeats only.

**Recommendation**: Option A — cleaner, keeps HTTP logic in the registration package.

**Files to edit**:
- `daemon-clean/internal/registration/registration.go`
  - Add `RegisterOnce(ctx) (*RegisterResult, error)` method
  - `RegisterResult` contains `EdgeID`, `PreAuthKey`, `HeadscaleURL`
  - Modify `loop()` to skip initial registration if `m.edgeID != ""` (already set by RegisterOnce)
- `daemon-clean/internal/cli/start.go`
  - Call `RegisterOnce` before tsnet
  - Use result to populate tsnet config
  - Then call `regMgr.Start(ctx)` for heartbeat loop

**Files to create**: none

**Acceptance**:
- `galois-edge start` with only BACKEND_URL + REGISTRATION_TOKEN (no TAILSCALE_AUTH_KEY)
  successfully registers, gets pre_auth_key, starts tsnet, begins heartbeats
- `galois-edge start` with full config (post-setup) works as before
- `galois-edge start` without BACKEND_URL works in standalone mode (no registration, no tsnet)

---

### Phase 4 — Frontend: "Add Edge" flow in dashboard

**Why**: User needs a guided path from dashboard to get the setup command.

**Files to read**:
- `cloud/web/src/pages/Daemons.tsx` — current edge list page
- `cloud/web/src/pages/Settings.tsx` — API key creation pattern to follow
- `cloud/web/src/hooks/use-edges.ts` — current hook shape
- `cloud/web/src/components/edges/EdgeCard.tsx` — existing component
- `cloud/web/src/lib/api.ts` — API call pattern

**Files to edit**:
- `cloud/web/src/pages/Daemons.tsx`
  - Add "Add Edge" button (top right, next to heading)
  - On click: open `AddEdgeDialog`

**Files to create**:
- `cloud/web/src/components/edges/AddEdgeDialog.tsx`

**`AddEdgeDialog` specification**:

1. User enters an edge name (e.g., "lab-bench-1") — optional, defaults to "my-edge"
2. On submit: `POST /api/v1/api-keys` with `{ name: "edge-<name>" }`
3. On success, show a one-time panel:
   ```
   Run this command on your device:

   ┌──────────────────────────────────────────────┐
   │ galois-edge setup glc_XXXXXXXXXXXXXXXXXXXXX  │  [Copy]
   └──────────────────────────────────────────────┘

   The token is shown only once. If you lose it, delete this
   edge and create a new one.
   ```
4. "Done" button closes dialog and refreshes edge list

**UI pattern**: Follow the exact one-time-display pattern from `Settings.tsx` —
amber warning box, copy button, "shown only once" message.

**Acceptance**:
- "Add Edge" button visible on Daemons page
- Dialog creates API key and shows setup command
- Copy button works
- Edge appears in list once daemon registers (no immediate list entry)

---

## Phase Dependency Graph

```
Phase 1 (backend)  ──┐
                     ├──→ Phase 3 (start.go reorder)
Phase 2 (setup cmd) ─┘
                           Phase 4 (frontend) — independent
```

Phases 1 and 2 can run in parallel.
Phase 3 depends on Phase 1 (needs headscale_url in response) and Phase 2 (uses same registration flow).
Phase 4 is fully independent — only needs the existing POST /api/v1/api-keys endpoint.

## Default Backend URL

Hardcoded in `setup.go` as a const:

```go
const DefaultBackendURL = "https://cloud.galoislabs.ai"
```

Overridable via `--backend` flag or `BACKEND_URL` env var. This is standard for
SaaS CLIs (cf. `gh`, `fly`, `heroku`).

## Pre-auth Key Lifecycle

| Scenario | What happens |
|---|---|
| First `setup` | Backend creates Headscale namespace + 1hr pre-auth key, daemon saves it |
| `start` after reboot | Daemon reads saved key from config, tsnet reuses persisted state (key only needed for first join) |
| Key expired, state lost | tsnet fails to start → daemon logs warning, falls back to direct TCP |
| Re-run `setup` | Backend issues new pre-auth key (new namespace or reuse), overwrites config |
| No Headscale on backend | `pre_auth_key` omitted in response, tsnet not started, direct TCP only |

## Testing Notes

- Phase 1: Add test in `cloud/backend` that asserts `headscale_url` in register response
- Phase 2: Add test for `setup` command using httptest (mock backend)
- Phase 3: Add integration test for register-then-tsnet ordering (can use mock tsnet)
- Phase 4: Manual testing in browser (or Playwright if available)
