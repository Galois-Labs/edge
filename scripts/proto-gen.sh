#!/usr/bin/env bash
# proto-gen.sh — Regenerate protobuf stubs for both daemon and cloud repos.
#
# Source of truth: daemon-clean/proto/edge/v1/edge.proto
#
# Usage:
#   ./scripts/proto-gen.sh          # full: sync proto + generate stubs
#   ./scripts/proto-gen.sh sync     # sync proto to cloud only (no codegen)
#   ./scripts/proto-gen.sh gen      # generate stubs only (assumes proto is synced)
#
# Prerequisites:
#   - buf CLI (https://buf.build/docs/installation) OR protoc + plugins
#   - For Go:   protoc-gen-go, protoc-gen-go-grpc
#   - For Python: grpcio-tools (pip install grpcio-tools)
#
# If buf/protoc are not installed, the script will still sync the proto file
# and print instructions for manual generation.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DAEMON_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CLOUD_ROOT="$(cd "$DAEMON_ROOT/../cloud" 2>/dev/null && pwd)" || true

PROTO_SRC="$DAEMON_ROOT/proto/edge/v1/edge.proto"
CLOUD_PROTO_DIR="$CLOUD_ROOT/proto/galois/edge/v1"
CLOUD_PROTO_DST="$CLOUD_PROTO_DIR/edge.proto"

# Colors (disable if not a terminal)
if [[ -t 1 ]]; then
  GREEN='\033[0;32m'
  YELLOW='\033[1;33m'
  RED='\033[0;31m'
  NC='\033[0m'
else
  GREEN='' YELLOW='' RED='' NC=''
fi

info()  { echo -e "${GREEN}[proto-gen]${NC} $*"; }
warn()  { echo -e "${YELLOW}[proto-gen]${NC} $*"; }
error() { echo -e "${RED}[proto-gen]${NC} $*" >&2; }

# ──────────────────────────────────────────────────────────────────────────────
# sync_proto: Copy canonical proto to cloud, rewriting go_package.
# ──────────────────────────────────────────────────────────────────────────────
sync_proto() {
  if [[ -z "$CLOUD_ROOT" || ! -d "$CLOUD_ROOT" ]]; then
    warn "Cloud repo not found at $DAEMON_ROOT/../cloud — skipping sync."
    warn "Set CLOUD_ROOT env var to override."
    return 1
  fi

  info "Syncing proto: $PROTO_SRC -> $CLOUD_PROTO_DST"
  mkdir -p "$CLOUD_PROTO_DIR"

  # Copy and rewrite go_package for the cloud's module path.
  sed 's|option go_package = "github.com/galois-labs/daemon/proto/gen/go/edge/v1";|// NOTE: go_package is overridden here for the cloud backend'\''s import path.\n// The canonical source of truth is daemon-clean/proto/edge/v1/edge.proto.\n// See proto/README.md in daemon-clean for the regeneration workflow.\noption go_package = "github.com/galois-labs/cloud/backend/internal/gen/proto/galois/edge/v1;edgev1";|' \
    "$PROTO_SRC" > "$CLOUD_PROTO_DST"

  info "Proto synced successfully."
}

# ──────────────────────────────────────────────────────────────────────────────
# generate_stubs: Run buf or protoc to generate language stubs.
# ──────────────────────────────────────────────────────────────────────────────
generate_stubs() {
  local has_buf=false has_protoc=false
  command -v buf >/dev/null 2>&1 && has_buf=true
  command -v protoc >/dev/null 2>&1 && has_protoc=true

  # --- Daemon (Python) stubs ---
  if $has_buf; then
    info "Generating daemon Python stubs with buf..."
    (cd "$DAEMON_ROOT/proto" && buf generate)
    info "Daemon stubs generated."
  elif $has_protoc; then
    info "Generating daemon Python stubs with protoc..."
    python -m grpc_tools.protoc \
      -I"$DAEMON_ROOT/proto" \
      --python_out="$DAEMON_ROOT/proto/gen/python" \
      --grpc_python_out="$DAEMON_ROOT/proto/gen/python" \
      "$PROTO_SRC" 2>/dev/null || warn "Python stub generation failed (grpcio-tools may not be installed)."
  else
    warn "Neither buf nor protoc found — skipping daemon stub generation."
    warn "Install buf: https://buf.build/docs/installation"
  fi

  # --- Cloud (Go) stubs ---
  if [[ -z "$CLOUD_ROOT" || ! -d "$CLOUD_ROOT" ]]; then
    warn "Cloud repo not found — skipping cloud stub generation."
    return
  fi

  if $has_buf; then
    info "Generating cloud Go stubs with buf..."
    (cd "$CLOUD_ROOT/proto" && buf generate)
    info "Cloud stubs generated."
  elif $has_protoc && command -v protoc-gen-go >/dev/null 2>&1 && command -v protoc-gen-go-grpc >/dev/null 2>&1; then
    info "Generating cloud Go stubs with protoc..."
    local out_dir="$CLOUD_ROOT/backend/internal/gen/proto"
    mkdir -p "$out_dir/galois/edge/v1"
    protoc \
      -I"$CLOUD_ROOT/proto" \
      --go_out="$out_dir" --go_opt=paths=source_relative \
      --go-grpc_out="$out_dir" --go-grpc_opt=paths=source_relative \
      "$CLOUD_PROTO_DST"
    info "Cloud Go stubs generated."
  else
    warn "Cannot generate cloud Go stubs — buf or protoc + Go plugins not found."
    warn "The hand-written stubs in $CLOUD_ROOT/backend/internal/gen/proto/ must be updated manually."
    warn "After manual update, verify with: cd $CLOUD_ROOT/backend && go build ./..."
  fi
}

# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────
main() {
  local mode="${1:-all}"

  case "$mode" in
    sync)
      sync_proto
      ;;
    gen)
      generate_stubs
      ;;
    all|"")
      sync_proto || true
      generate_stubs
      ;;
    *)
      error "Unknown mode: $mode"
      echo "Usage: $0 [sync|gen|all]"
      exit 1
      ;;
  esac

  info "Done."
}

main "$@"
