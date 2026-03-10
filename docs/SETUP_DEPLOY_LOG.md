# Edge Daemon Setup & Deploy — Implementation Log

Date: 2026-03-09

## What Was Built

Single-command edge registration: `galois-edge setup <TOKEN>` replaces 5+ manual
config steps with one copy-paste from the dashboard.

## Changes by File

### daemon-clean

| File | Action | What |
|---|---|---|
| `internal/registration/registration.go` | Edited | Fixed auth bug: `X-API-Key` now sent on heartbeat and unregister. Extracted `setAuthHeader()` helper. Added `doRegister()` (shared HTTP logic), `RegisterOnce()` (one-shot register returning pre_auth_key/headscale_url), `SetIPFunc()`, `RegisterResult` struct. Loop skips to Connected when edgeID already set. Unregister uses `POST /{id}/unregister` instead of `DELETE /{id}` (avoids Chi route conflict). |
| `internal/registration/registration_test.go` | Edited | Added tests: `TestHeartbeat_Success` now asserts `X-API-Key` header. New: `TestRegisterOnce_Success`, `TestRegisterOnce_SetsEdgeID`, `TestLoopSkipsRegistrationAfterRegisterOnce`, `TestSetIPFunc`. 16 tests total, all pass. |
| `internal/cli/setup.go` | Created | New `galois-edge setup <TOKEN>` command. Flags: `--backend` (default `https://cloud.galoislabs.ai`), `--name` (default hostname), `--config`. Registers via HTTP, writes config.env with read-modify-write. |
| `internal/cli/start.go` | Edited | Reordered startup: Python engine -> `RegisterOnce` -> persist pre_auth_key to config if returned -> tsnet -> proxies -> heartbeat loop. Added `persistTailnetCredentials()` helper. |
| `internal/cli/root.go` | Edited | Added `rootCmd.AddCommand(setupCmd)`. |
| `src/galois_edge/__main__.py` | Edited | Changed `from .main import main` to `from galois_edge.main import main` (fixes PyInstaller relative import error). |
| `galois-edge-daemon.spec` | Edited | Added `collect_all()` for pyvisa, pyvisa_py, aiohttp. Removed `email` from excludes (was breaking `importlib.metadata`, causing both pyvisa and aiohttp to fail silently at runtime). Added aiohttp transitive deps to hidden imports. |
| `docs/SETUP_COMMAND_PLAN.md` | Created | 4-phase implementation plan with file lists and subagent instructions. |

### cloud/backend

| File | Action | What |
|---|---|---|
| `internal/handler/edge.go` | Edited | Added `headscaleURL string` field to `EdgeHandler`. Added `HeadscaleURL` to `RegisterEdgeResponse`. Populated when Headscale is configured. |
| `internal/server/routes.go` | Edited | Passed `s.config.HeadscaleURL` to `NewEdgeHandler`. Added `POST /{id}/unregister` to API key auth group (daemon-facing). Kept `DELETE /{id}` in Firebase auth group (dashboard-facing). |

### cloud/web (frontend)

| File | Action | What |
|---|---|---|
| `src/components/edges/AddEdgeDialog.tsx` | Created | "Add Edge" dialog. Creates API key via `POST /api-keys`, shows `galois-edge setup <key>` one-liner with copy button and one-time display warning. |
| `src/pages/Daemons.tsx` | Edited | Added "Add Edge" button and `AddEdgeDialog` integration. |

## Bugs Found and Fixed

### 1. Auth missing on heartbeat/unregister (Critical)

The registration manager sent `X-API-Key` only on the initial register call,
then cleared the token. Heartbeat and unregister calls had no auth header,
causing 401s from the backend's `APIKeyAuth` middleware.

**Fix**: Stop clearing the token. Send `X-API-Key` on all three HTTP methods
via shared `setAuthHeader()`.

### 2. Chi route conflict on DELETE (Critical)

`r.Delete("/{id}", edgeHandler.Delete)` was registered in both the API key
and Firebase auth middleware groups. Chi `r.Group()` doesn't create isolated
route namespaces — the second registration wins. The daemon's DELETE always
hit Firebase auth and got 401.

**Fix**: Daemon uses `POST /{id}/unregister` (unique route in API key group).
Dashboard keeps `DELETE /{id}` (Firebase group).

### 3. PyInstaller excludes `email` stdlib (Medium)

The spec excluded the `email` stdlib package to save space. But
`importlib.metadata` depends on `email` to parse package metadata. Both
`pyvisa` and `aiohttp` call `importlib.metadata` at import time for
`__version__`. The resulting `ImportError` was silently caught by try/except
guards, disabling VISA transport and WebSocket streaming.

**Fix**: Removed `email` from the excludes list.

### 4. PyInstaller relative import (Low)

`__main__.py` used `from .main import main` — a relative import that fails
when PyInstaller runs the file as a script entry point.

**Fix**: Changed to absolute import `from galois_edge.main import main`.

## Deployment Tested

### Target: Raspberry Pi 5

- Debian 13 (trixie), aarch64, Python 3.13.5
- Tailscale 1.94.2 running as system daemon

### Artifacts

| Binary | Size | Description |
|---|---|---|
| `bin/galois-edge` | 45MB | Go supervisor, cross-compiled `linux/arm64` |
| `dist/galois-edge-daemon` | 29MB | Frozen Python engine (PyInstaller onefile) |

### Verified Flows

**`galois-edge setup glc_XXXXX`** against production (`cloud.galoislabs.ai`):
```
Registered as "pi5" (6848b9d5-683c-498d-8d3d-7e1995681183)
Config written to /home/pi/.config/galois-edge/config.env
```

**`galois-edge start`** with raw Python engine:
```
Python engine is healthy
[registration] registered as edge 6848b9d5-...
tsnet not configured, using direct listeners only
fallback proxy created: grpc 0.0.0.0:50051
fallback proxy created: ws   0.0.0.0:8765
daemon ready
[registration] state: Disconnected -> Connected
[registration] unregistered edge ... (status 204)   # clean shutdown
```

**`galois-edge start`** with frozen Python binary:
```
PyVISA resource manager initialised (backend=@py)
WebSocket server listening on 127.0.0.1:8766
[registration] registered as edge 5ed72291-...
daemon ready
[registration] unregistered edge ... (status 204)
```

All 33 instrument profiles loaded. gRPC, WebSocket, registration,
heartbeat, and graceful unregister all confirmed working.

## tsnet Notes

tsnet was not tested (no Headscale server, no Tailscale pre-auth key provided).
The daemon falls back to `0.0.0.0` TCP listeners which work for direct/LAN
access. tsnet will activate when either:

- The backend has Headscale configured and returns a `pre_auth_key` during registration
- The user manually sets `TAILSCALE_AUTH_KEY` in config (for personal Tailscale accounts)

tsnet runs as a separate userspace node alongside the system `tailscaled` — no
conflict, no admin required, no kernel tun device. The Pi appears twice in the
tailnet (one for SSH/admin, one for galois-edge gRPC).

## Startup Order (after changes)

```
1. Load config.env, validate
2. Init logger, print banner
3. Signal context (SIGINT/SIGTERM)
4. Resolve Python binary (PYTHON_BIN or auto-detect)
5. Start Python engine (supervisor waits for gRPC health)
6. RegisterOnce with backend (if BACKEND_URL set)
   -> If pre_auth_key returned: save to config, update in-memory
7. Start tsnet (if auth key available)
8. Create TCP proxies (tsnet + fallback on 0.0.0.0)
9. Start heartbeat loop (skips to Connected since already registered)
10. <running>
11. Shutdown: stop heartbeats -> unregister -> drain proxies -> stop tsnet -> stop Python
```
