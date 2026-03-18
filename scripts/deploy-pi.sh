#!/usr/bin/env bash
# Deploy galois-edge to a Raspberry Pi
# Usage: ./scripts/deploy-pi.sh [pi-host] [--skip-go] [--skip-python] [--skip-restart]
set -euo pipefail

PI_HOST="${1:-pi@pi5}"
SKIP_GO=false
SKIP_PYTHON=false
SKIP_RESTART=false

for arg in "$@"; do
  case "$arg" in
    --skip-go)      SKIP_GO=true ;;
    --skip-python)  SKIP_PYTHON=true ;;
    --skip-restart) SKIP_RESTART=true ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REMOTE_SRC="/home/pi/galois-edge/src/galois_edge"
REMOTE_VENV="/home/pi/galois-edge/venv"

log() { printf "\033[1;34m==> %s\033[0m\n" "$1"; }
err() { printf "\033[1;31m==> ERROR: %s\033[0m\n" "$1"; exit 1; }
ok()  { printf "\033[1;32m    ✓ %s\033[0m\n" "$1"; }

# ── Step 1: Build Go binary ──────────────────────────────────────────────
if [ "$SKIP_GO" = false ]; then
  log "Building Go binary (linux/arm64)..."
  cd "$SCRIPT_DIR"
  GOOS=linux GOARCH=arm64 go build -o "$SCRIPT_DIR/galois-edge-linux-arm64" ./cmd/galois-edge
  ok "Go binary built"
else
  log "Skipping Go build (--skip-go)"
fi

# ── Step 2: Sync Python source ───────────────────────────────────────────
log "Syncing Python source to $PI_HOST..."
rsync -az --delete \
  --exclude='__pycache__' --exclude='*.pyc' --exclude='_cache.pkl' \
  "$SCRIPT_DIR/src/galois_edge/" \
  "$PI_HOST:$REMOTE_SRC/"
ok "Python source synced"

# ── Step 3: Copy Go binary ──────────────────────────────────────────────
if [ "$SKIP_GO" = false ]; then
  log "Copying Go binary to $PI_HOST..."
  scp -q "$SCRIPT_DIR/galois-edge-linux-arm64" "$PI_HOST:/tmp/galois-edge"
  ok "Go binary copied"
fi

# ── Step 4: Build frozen Python binary on Pi ─────────────────────────────
if [ "$SKIP_PYTHON" = false ]; then
  log "Building frozen Python binary on $PI_HOST (PyInstaller)..."
  # shellcheck disable=SC2029
  ssh "$PI_HOST" "cd /home/pi/galois-edge && source venv/bin/activate && make freeze 2>&1" | \
    grep -E 'INFO: Build|ERROR|Frozen binary' || true
  ok "Frozen binary built"
else
  log "Skipping Python build (--skip-python)"
fi

# ── Step 5: Install and restart ──────────────────────────────────────────
if [ "$SKIP_RESTART" = false ]; then
  log "Stopping service..."
  ssh "$PI_HOST" "sudo systemctl stop galois-edge 2>/dev/null || true"
  ok "Service stopped"

  log "Installing binaries..."
  INSTALL_CMDS="sudo cp /home/pi/galois-edge/dist/galois-edge-daemon /usr/local/bin/galois-edge-daemon && sudo chmod +x /usr/local/bin/galois-edge-daemon"
  if [ "$SKIP_GO" = false ]; then
    INSTALL_CMDS="sudo cp /tmp/galois-edge /usr/local/bin/galois-edge && sudo chmod +x /usr/local/bin/galois-edge && $INSTALL_CMDS"
  fi
  ssh "$PI_HOST" "$INSTALL_CMDS"
  ok "Binaries installed"

  log "Reinitializing GPIB board..."
  ssh "$PI_HOST" "sudo gpib_config 2>&1" || echo "    (gpib_config not available — skipping)"
  ok "GPIB ready"

  log "Starting service..."
  ssh "$PI_HOST" "sudo systemctl start galois-edge"
  ok "Service started"

  log "Waiting for startup (10s)..."
  sleep 10

  log "Service status:"
  ssh "$PI_HOST" "sudo systemctl status galois-edge --no-pager 2>&1" | head -8

  echo ""
  log "Startup logs:"
  ssh "$PI_HOST" "sudo journalctl -u galois-edge --no-pager --since '15 seconds ago' 2>&1" | \
    grep -iE 'healthy|registered|tsnet|gRPC server|trickle|USB.*monitor|GPIB.*board|instrument|error|aborted' | \
    grep -v 'profile_loader' | tail -15
else
  log "Skipping restart (--skip-restart)"
fi

echo ""
log "Deploy complete."
