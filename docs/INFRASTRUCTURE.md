# Galois Edge — Infrastructure & Deployment Guide

> Audience: Engineers who need to understand how the edge daemon is built,
> deployed, and connected to the cloud platform. Covers architecture,
> CI/CD, hosting, DNS, and production operations.

---

## Table of Contents

1. [System Overview](#system-overview)
2. [Edge Daemon Architecture](#edge-daemon-architecture)
3. [Cloud Backend](#cloud-backend)
4. [Networking: Tailscale Mesh](#networking-tailscale-mesh)
5. [CI/CD Pipeline](#cicd-pipeline)
6. [Release Hosting (Cloudflare R2)](#release-hosting-cloudflare-r2)
7. [Install Script](#install-script)
8. [DNS & Domains](#dns--domains)
9. [Production Deployment](#production-deployment)
10. [End-to-End Flows](#end-to-end-flows)
11. [Key Files Reference](#key-files-reference)
12. [Common Operations](#common-operations)

---

## System Overview

```
┌─────────────────────────────────┐     ┌──────────────────────────────────────┐
│  Lab Machine (Pi / workstation) │     │  Cloud (Azure ARM64 VM)              │
│                                 │     │                                      │
│  galois-edge        (Go)    ◄───┼─────┼── backend   (Go, Chi, Postgres)     │
│    ├─ registration + heartbeat  │ HTTP│    ├─ /api/v1/edges/*   (API key)    │
│    ├─ tsnet (embedded Tailscale)│     │    ├─ /api/v1/instruments/* (Firebase)│
│    ├─ TCP proxy :50051 → :50052│     │    ├─ /api/v1/kernel/*  (Kernel JWT) │
│    └─ TCP proxy :8765  → :8766 │     │    └─ gRPC dial to edges via Tailnet │
│                                 │     │                                      │
│  galois-edge-daemon (Python)    │     │  web        (React, Vite)            │
│    ├─ gRPC server   :50052     │     │    └─ dashboard at cloud.galoislabs.ai│
│    ├─ WebSocket     :8766      │     │                                      │
│    ├─ PyVISA (USB/GPIB/LAN)    │     │  postgres   (16)                     │
│    └─ 50+ instrument profiles   │     │  agent      (Python, Anthropic)      │
└─────────────────────────────────┘     └──────────────────────────────────────┘
         │                                        │
         │  gRPC over Tailscale mesh VPN          │
         └────────────────────────────────────────┘

┌─────────────────────────┐     ┌─────────────────────────────┐
│  Cloudflare             │     │  GitHub (Galois-Labs org)    │
│  ├─ DNS for *.galoislabs│     │  ├─ /edge     (private)      │
│  ├─ R2: releases bucket │     │  ├─ /cloud    (private)      │
│  └─ CDN/proxy           │     │  ├─ /site     (private)      │
└─────────────────────────┘     │  └─ /get      (can delete)   │
                                └─────────────────────────────┘
```

---

## Edge Daemon Architecture

**Repo:** `Galois-Labs/edge` (private) — local path: `~/work/galois/daemon-clean/`
**Module:** `github.com/galois-labs/edge`

### Dual-Process Design

Two binaries ship together:

| Binary | Language | Size | Role |
|---|---|---|---|
| `galois-edge` | Go | ~45 MB | Supervisor: CLI, Tailscale, TCP proxy, registration, systemd |
| `galois-edge-daemon` | Python (PyInstaller) | ~29 MB | Instrument engine: PyVISA, gRPC server, WebSocket, profiles |

The Go process is always the parent. It spawns Python as a child and owns its
lifecycle. Python listens on `127.0.0.1` only — Go's TCP proxy exposes it
externally on the Tailnet and/or `0.0.0.0`.

### Go Source Layout

```
cmd/galois-edge/main.go              CLI entry point
internal/
  cli/
    root.go                          Cobra root command
    start.go                         "galois-edge start" — full startup
    setup.go                         "galois-edge setup <TOKEN>" — registration
    install.go                       "galois-edge install" — systemd/SCM
    configure.go                     "galois-edge config get/set"
    status.go                        "galois-edge status"
    doctor.go                        "galois-edge doctor" — diagnostics
  config/config.go                   KEY=VALUE config, env overrides, validation
  supervisor/supervisor.go           Python child process lifecycle
  network/tsnet.go                   Embedded Tailscale (userspace, no root)
  proxy/tcpproxy.go                  Bidirectional TCP proxy
  registration/registration.go       HTTP state machine (register/heartbeat/unregister)
  grpcclient/client.go               Local gRPC client to Python engine
  service/
    service_linux.go                 systemd unit generation + udev rules
    service_windows.go               Windows SCM registration
  doctor/doctor.go                   System health checks
```

### Python Source Layout

```
src/galois_edge/
  main.py                            EdgeDaemon class, async lifecycle
  grpc_server.py                     All EdgeDaemonService RPC implementations
  instrument_manager.py              Aggregates PyVISA, GPIB, USB, LAN transports
  capability_manager.py              Loads YAML profiles → CommandCapability protos
  command_handler.py                 SCPI dispatch (profile-based or raw)
  ws_server.py                       aiohttp WebSocket streaming
  profile_loader.py                  YAML profile parser
  sdk_executor.py                    ProxySDKCall handler
  config.py                          Python-side config (reads env vars from Go)
  profiles/                          ~50 instrument YAML profiles
    keysight_34461a.yaml
    tektronix_mso4000.yaml
    ...
```

### Startup Sequence (`start.go`)

```
1.  Load config.env + env overrides
2.  Resolve Python binary path
3.  Start Python engine (supervisor polls :50052 until healthy)
4.  RegisterOnce → POST /api/v1/edges/register
    └─ If backend returns pre_auth_key: save to config
5.  Start tsnet (if auth key available)
6.  Create TCP proxies (tsnet + fallback 0.0.0.0)
7.  Start heartbeat loop (every 30s)
8.  <running — block on SIGINT/SIGTERM>
9.  Shutdown: stop heartbeats → unregister → drain proxies → stop tsnet → stop Python
```

### Proto Definition

Single file: `proto/edge/v1/edge.proto`

Service `EdgeDaemonService` — key RPCs:
- `SendCommand` / `StreamCommands` — raw SCPI
- `ListInstruments` / `ScanInstruments` — discovery
- `GetCapabilities` / `ExecuteCommand` — profile-based
- `StreamMeasurement` / `StopStream` — server-streaming
- `ProxySDKCall` — forward vendor SDK calls (RPyC-style)

Regenerate stubs: `make proto` (requires `buf` CLI).

### Config File

Location: `/etc/galois-edge/config.env` (system) or `~/.config/galois-edge/config.env` (user).

Key variables:

| Key | Default | Written by |
|---|---|---|
| `BACKEND_URL` | (empty) | `galois-edge setup` |
| `REGISTRATION_TOKEN` | (empty) | `galois-edge setup` |
| `EDGE_NAME` | hostname | `galois-edge setup` |
| `TAILSCALE_AUTH_KEY` | (empty) | `galois-edge setup` (from backend) |
| `HEADSCALE_URL` | (empty) | `galois-edge setup` (from backend) |
| `GRPC_PORT` | 50051 | Manual |
| `GRPC_INTERNAL_PORT` | 50052 | Manual |
| `WS_PORT` | 8765 | Manual |
| `WS_INTERNAL_PORT` | 8766 | Manual |
| `PROFILES_ENABLED` | true | Manual |
| `GPIB_ENABLED` | auto | Manual |
| `LOG_LEVEL` | info | Manual |

Full list: `internal/config/config.go` — every key is documented there.

---

## Cloud Backend

**Repo:** `Galois-Labs/cloud` — `cloud/backend/`

### Stack

Go, Chi router, PostgreSQL 16, Firebase Auth, gRPC client to edges.

### Auth Model

Three auth systems coexist:

| Auth | Header | Used by | Routes |
|---|---|---|---|
| API Key | `X-API-Key: glc_...` | Edge daemons | `/edges/register`, `/{id}/heartbeat`, `/{id}/unregister` |
| Firebase JWT | `Authorization: Bearer ...` | Dashboard, VS Code extension | Everything else under `/api/v1/` |
| Kernel JWT | `Authorization: Bearer ...` | pyvisa-galois in Jupyter containers | `/api/v1/kernel/*` |

### How Backend Talks to Edges

`internal/grpcclient/manager.go` is a lazy connection pool keyed by edge ID.
On first access, it dials `{tailnet_ip}:{grpc_port}` via the Tailscale mesh.
Transport security is provided by Tailscale (WireGuard) — gRPC uses insecure
credentials.

### Edge Registration Handler (`internal/handler/edge.go`)

1. Edge POSTs `{name, hostname, grpc_port, ws_port, version, os_info}` with `X-API-Key`
2. Middleware validates the API key against bcrypt hash in `api_keys` table
3. Handler upserts into `edges` table
4. If Headscale is configured: creates pre-auth key, returns it in response
5. Returns `{id, pre_auth_key, headscale_url}`

### Route Map

See `cloud/backend/internal/server/routes.go` for the complete map. Major groups:
- `/api/v1/edges/*` — edge lifecycle
- `/api/v1/instruments/*` — instrument CRUD + commands
- `/api/v1/streams/*` — measurement streaming + SSE
- `/api/v1/teams/{teamId}/projects/{projectId}/...` — teams, projects, sequences, datasets, experiments
- `/api/v1/kernel/*` — kernel proxy (HTTP → gRPC bridge)
- `/api/v1/resources/{resourceId}/kernels/*` — Jupyter kernel lifecycle

---

## Networking: Tailscale Mesh

The edge daemon uses **tsnet** — an embedded Tailscale node that runs entirely
in userspace (gvisor netstack). Key properties:

- **No root required** at runtime (only at install time for systemd)
- **No conflict** with system `tailscaled` — separate state dir, separate tailnet node
- **No kernel tun device** — pure userspace networking
- The Pi appears twice in the tailnet: once for SSH (system tailscale), once for galois-edge (tsnet)

When tsnet is not available (no auth key), the daemon falls back to `0.0.0.0`
TCP listeners for direct/LAN access.

---

## CI/CD Pipeline

**File:** `.github/workflows/release.yml`
**Trigger:** Push a `v*` tag to `Galois-Labs/edge`

### Jobs

```
┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│ build-go     │  │ build-python │  │ test         │
│ (cross-comp) │  │ (native/arch)│  │ (go + pytest)│
│ amd64 + arm64│  │ amd64 + arm64│  │              │
└──────┬───────┘  └──────┬───────┘  └──────┬───────┘
       │                 │                 │
       └────────┬────────┘─────────────────┘
                │
        ┌───────▼────────┐
        │ release        │
        │ - checksums    │
        │ - GitHub Release│
        │ - R2 upload    │
        └────────────────┘
```

**Important:** Go cross-compiles from any runner (`GOOS=linux GOARCH=arm64 CGO_ENABLED=0`).
Python **must** be frozen natively per architecture — PyInstaller does not cross-compile.
The arm64 Python build runs on `ubuntu-24.04-arm` runners.

### Artifacts Produced

Per release tag (e.g. `v0.1.0`):
```
galois-edge-linux-amd64
galois-edge-linux-arm64
galois-edge-daemon-linux-amd64
galois-edge-daemon-linux-arm64
checksums-linux-amd64.sha256
checksums-linux-arm64.sha256
checksums.sha256
install.sh
```

### How to Release

```bash
# 1. Commit your changes
git add -A && git commit -m "your message"
git push

# 2. Tag and push
git tag v0.2.0
git push origin v0.2.0

# 3. CI builds, tests, and publishes automatically
# Watch: gh run list --repo Galois-Labs/edge
```

---

## Release Hosting (Cloudflare R2)

The GitHub repo is **private**, so GitHub release assets are not publicly
downloadable. Release binaries are uploaded to Cloudflare R2 by CI.

### Bucket Details

| Property | Value |
|---|---|
| Bucket name | `galois-edge-releases` |
| Custom domain | `releases.galoislabs.ai` |
| Public r2.dev URL | `pub-10bb6becf7d045f091ee31e53a30fca9.r2.dev` |
| Account ID | `17556a1f5d0b68b9794340e1b4993d54` |
| Region | Auto |

### Object Layout

```
galois-edge-releases/
  latest                                    ← plain text: "v0.1.0"
  v0.1.0/
    galois-edge-linux-amd64
    galois-edge-linux-arm64
    galois-edge-daemon-linux-amd64
    galois-edge-daemon-linux-arm64
    checksums-linux-amd64.sha256
    checksums-linux-arm64.sha256
```

### CI Secrets (GitHub → R2)

Set on `Galois-Labs/edge` repo:

| Secret | Purpose |
|---|---|
| `R2_ACCOUNT_ID` | Cloudflare account ID |
| `R2_ACCESS_KEY_ID` | R2 S3-compatible access key |
| `R2_SECRET_ACCESS_KEY` | R2 S3-compatible secret key |

### Manual R2 Operations

```bash
# List objects (requires wrangler login)
npx wrangler r2 object get galois-edge-releases/latest --pipe --remote

# Upload a file
echo -n "v0.2.0" | npx wrangler r2 object put galois-edge-releases/latest \
  --pipe --content-type "text/plain" --remote

# IMPORTANT: Always use --remote flag. Without it, wrangler operates on
# a local dev instance and uploads go nowhere.
```

---

## Install Script

**Canonical copy:** `site/public/install.sh` (served at `https://galoislabs.ai/install.sh`)
**Repo copy:** `daemon-clean/install.sh` (identical, shipped in GitHub releases)

### What It Does

```
1. Check root (exit if not sudo)
2. Detect arch (uname -m → amd64/arm64/armv7)
3. Fetch https://releases.galoislabs.ai/latest → resolve version
4. Download binaries + checksums from R2
5. Verify SHA-256
6. Stop existing service (if upgrading)
7. Install to /usr/local/bin/
8. Write udev rules for T&M instruments (Keysight, Tektronix, R&S, NI, Rigol, Siglent)
9. galois-edge setup <TOKEN> (if --token provided)
10. galois-edge install (systemd unit + service user)
11. systemctl start galois-edge
12. Write /usr/local/bin/galois-edge-uninstall helper
```

### Usage

```bash
# Full install + register:
curl -fsSL https://galoislabs.ai/install.sh | sudo sh -s -- --token glc_XXXXX

# Install without registering (register later):
curl -fsSL https://galoislabs.ai/install.sh | sudo sh

# Specific version:
curl -fsSL https://galoislabs.ai/install.sh | sudo sh -s -- --version v0.1.0 --token glc_XXXXX

# Uninstall:
sudo galois-edge-uninstall
```

### Updating the Install Script

The script lives in two places — keep them in sync:

1. `~/work/galois/daemon-clean/install.sh` — repo copy, included in GitHub releases
2. `~/work/galois/site/public/install.sh` — the one users actually download

After editing, push both repos.

---

## DNS & Domains

All DNS is managed by **Cloudflare** (zone: `galoislabs.ai`).

| Domain | Points to | Purpose |
|---|---|---|
| `galoislabs.ai` | Vercel | Marketing site + `/install.sh` |
| `cloud.galoislabs.ai` | Azure VM (Nginx) | Dashboard + API |
| `releases.galoislabs.ai` | Cloudflare R2 bucket | Binary downloads |

Cloudflare zone ID: `ebd1ef63798b2521547ac85236dc4489`

---

## Production Deployment

### Cloud Backend (Azure VM)

- **VM:** `galois-cloud-vm` — Azure Standard_B2pls_v2 (ARM64, 2 vCPU, 4 GB RAM), westus2
- **SSH:** `galois@20.230.218.54`
- **Stack:** Nginx (host) → Docker containers

```
Internet → Cloudflare → :443 → Nginx
  /            → web container     :3001
  /api/        → backend container :8000
  /agent/      → agent container   :8001
  postgres container               :5432
```

Deploy procedure (see `cloud/DEPLOY.md` for full details):
```bash
# From your machine:
rsync -avz --exclude node_modules --exclude .git cloud/ galois@20.230.218.54:~/cloud/

# On the VM:
cd ~/cloud
docker compose -f docker-compose.prod.yml up -d --build
```

Secrets live in `~/cloud/.env` on the VM — **never committed to git**.

### Edge Daemon (Lab Machines)

Installed via the curl one-liner. Runs as `systemctl` service. Managed remotely
by re-running the install script (idempotent) or by `galois-edge setup` for
re-registration.

```bash
# Check status on a Pi:
ssh pi@pi5 'systemctl status galois-edge'
ssh pi@pi5 'journalctl -u galois-edge -f'

# Restart:
ssh pi@pi5 'sudo systemctl restart galois-edge'
```

---

## End-to-End Flows

### New Edge Installation

```
1. User: Dashboard → "Add Edge" → POST /api/v1/api-keys
   ← Returns glc_XXXXX (one-time display)

2. User runs on lab machine:
   curl -fsSL https://galoislabs.ai/install.sh | sudo sh -s -- --token glc_XXXXX
     ├─ Downloads binaries from releases.galoislabs.ai
     ├─ galois-edge setup glc_XXXXX
     │    └─ POST /api/v1/edges/register (X-API-Key: glc_XXXXX)
     │    ← {id, pre_auth_key, headscale_url}
     │    └─ Writes config.env
     ├─ galois-edge install (systemd + udev + service user)
     └─ systemctl start galois-edge
          ├─ Spawns Python engine
          ├─ Joins Tailnet
          └─ Starts heartbeat loop
```

### Instrument Command (Dashboard → Edge)

```
Browser → POST /api/v1/instruments/{id}/command (Firebase JWT)
  → backend looks up edge for instrument
  → grpcManager.GetConnection(edgeID) → dials tailnet_ip:50051
  → EdgeDaemonService.ExecuteCommand (gRPC over Tailscale)
  → Go TCP proxy :50051 → :50052
  → Python command_handler → PyVISA → instrument
  ← response bubbles back up
```

### Kernel Instrument Access (Jupyter → Edge)

```
Jupyter kernel (pyvisa-galois)
  → POST /api/v1/kernel/sessions/{id}/query (Kernel JWT)
  → KernelProxyHandler
  → grpcManager → EdgeDaemonService RPC (gRPC over Tailscale)
  → Python → PyVISA → instrument
  ← HTTP response to kernel
```

---

## Key Files Reference

### Edge Daemon (`Galois-Labs/edge`)

| File | What to know |
|---|---|
| `cmd/galois-edge/main.go` | Entry point — CLI mode or Windows service mode |
| `internal/cli/start.go` | The startup sequence — read this first |
| `internal/cli/setup.go` | One-time registration flow |
| `internal/config/config.go` | Every config key, defaults, validation |
| `internal/supervisor/supervisor.go` | How Python child process is managed |
| `internal/registration/registration.go` | HTTP state machine for register/heartbeat |
| `internal/service/service_linux.go` | systemd unit template + udev rules |
| `internal/network/tsnet.go` | Embedded Tailscale wrapper |
| `internal/proxy/tcpproxy.go` | TCP proxy (external → Python) |
| `proto/edge/v1/edge.proto` | The gRPC contract |
| `src/galois_edge/grpc_server.py` | All Python RPC implementations |
| `src/galois_edge/instrument_manager.py` | PyVISA + transport aggregation |
| `.github/workflows/release.yml` | CI pipeline |
| `install.sh` | Linux installer |
| `Makefile` | Build targets |
| `galois-edge-daemon.spec` | PyInstaller freeze config |

### Cloud (`Galois-Labs/cloud`)

| File | What to know |
|---|---|
| `backend/internal/server/routes.go` | Complete route map |
| `backend/internal/handler/edge.go` | Edge register/heartbeat handlers |
| `backend/internal/handler/kernel_proxy.go` | Kernel → gRPC → edge bridge |
| `backend/internal/grpcclient/manager.go` | gRPC connection pool to edges |
| `backend/internal/middleware/apikey.go` | API key validation (bcrypt) |
| `web/src/components/edges/AddEdgeDialog.tsx` | Edge onboarding UI |
| `DEPLOY.md` | Production deployment runbook |

### Infrastructure

| File / Service | What to know |
|---|---|
| `site/public/install.sh` | Canonical install script served to users |
| `site/next.config.ts` | Content-Type header for install.sh |
| Cloudflare R2 `galois-edge-releases` | Binary hosting (public reads) |
| Cloudflare DNS zone `galoislabs.ai` | All domain routing |
| GitHub Actions secrets on `Galois-Labs/edge` | R2 credentials for CI uploads |

---

## Common Operations

### Releasing a New Version

```bash
cd ~/work/galois/daemon-clean
git tag v0.2.0
git push origin v0.2.0
# CI runs automatically. Monitor:
gh run list --repo Galois-Labs/edge
```

### Updating the Install Script

```bash
# Edit daemon-clean/install.sh
# Copy to site:
cp daemon-clean/install.sh site/public/install.sh
# Push both repos
```

### Checking R2 Releases

```bash
# What version is "latest"?
curl -fsSL https://releases.galoislabs.ai/latest

# Download a specific binary:
curl -fsSL https://releases.galoislabs.ai/v0.1.0/galois-edge-linux-arm64 -o galois-edge

# Via wrangler (needs login):
npx wrangler r2 object get galois-edge-releases/latest --pipe --remote
```

### Deploying Backend Changes

```bash
rsync -avz --exclude node_modules --exclude .git \
  ~/work/galois/cloud/ galois@20.230.218.54:~/cloud/
ssh galois@20.230.218.54 'cd ~/cloud && docker compose -f docker-compose.prod.yml up -d --build'
```

### Debugging an Edge

```bash
# On the edge machine:
sudo systemctl status galois-edge
sudo journalctl -u galois-edge -f
galois-edge doctor                    # runs diagnostics
galois-edge status                    # shows registration state

# Re-register:
sudo galois-edge setup glc_NEWTOKEN --config /etc/galois-edge/config.env
sudo systemctl restart galois-edge

# Full reinstall:
sudo galois-edge-uninstall
curl -fsSL https://galoislabs.ai/install.sh | sudo sh -s -- --token glc_XXXXX
```

### Adding a New Instrument Profile

```bash
# Create src/galois_edge/profiles/vendor_model.yaml
# Follow existing profiles as templates (e.g., keysight_34461a.yaml)
# The profile is auto-loaded at startup when PROFILES_ENABLED=true
# For PyInstaller: profiles are bundled via the .spec file's profile_datas glob
```

### Rotating R2 Credentials

1. Cloudflare Dashboard → R2 → Manage R2 API Tokens → Create new token
2. Update GitHub secrets: `gh secret set R2_ACCESS_KEY_ID --repo Galois-Labs/edge --body "..."`
3. Delete old token in Cloudflare Dashboard
