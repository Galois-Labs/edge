# Protobuf Source of Truth

## Location

The canonical `.proto` file for all edge-to-cloud communication lives here:

```
daemon-clean/proto/edge/v1/edge.proto
```

This is the **single source of truth**. The cloud repo's copy at
`cloud/proto/galois/edge/v1/edge.proto` is a derived artifact — it is
synced from this file with the `go_package` option rewritten for the
cloud's Go module path.

## Regenerating Stubs

From the daemon-clean root:

```bash
./scripts/proto-gen.sh          # sync proto + generate all stubs
./scripts/proto-gen.sh sync     # sync proto to cloud only (no codegen)
./scripts/proto-gen.sh gen      # generate stubs only (assumes proto synced)
```

### What the script does

1. **Sync**: Copies `edge.proto` to `cloud/proto/galois/edge/v1/edge.proto`,
   rewriting `go_package` from the daemon path to the cloud's import path.

2. **Generate (daemon)**: Runs `buf generate` (or `protoc`) in `daemon-clean/proto/`
   to produce Python stubs in `proto/gen/python/`.

3. **Generate (cloud)**: Runs `buf generate` (or `protoc`) in `cloud/proto/`
   to produce Go stubs in `cloud/backend/internal/gen/proto/`.

### Prerequisites

| Tool | Install | Used for |
|------|---------|----------|
| `buf` | https://buf.build/docs/installation | Preferred generator |
| `protoc` | `brew install protobuf` | Fallback generator |
| `protoc-gen-go` | `go install google.golang.org/protobuf/cmd/protoc-gen-go@latest` | Go messages |
| `protoc-gen-go-grpc` | `go install google.golang.org/grpc/cmd/protoc-gen-go-grpc@latest` | Go gRPC stubs |
| `grpcio-tools` | `pip install grpcio-tools` | Python stubs |

If neither `buf` nor `protoc` is available, the script will still sync the
proto file and print instructions for manual stub updates.

## Important Rules

1. **Never hand-edit `cloud/backend/internal/gen/proto/.../edge_grpc.go`** — it
   should be generated. If you must manually update it (because tooling is not
   installed), always verify with `cd cloud/backend && go build ./...`.

2. **Edit only `daemon-clean/proto/edge/v1/edge.proto`**, then re-run the
   script. Never edit `cloud/proto/galois/edge/v1/edge.proto` directly.

3. **The `go_package` differs intentionally** between the daemon and cloud
   copies. The sync script handles this rewrite automatically.

4. After regeneration, check that both repos compile:
   ```bash
   # Daemon (Python)
   cd daemon-clean && pytest tests/ -v

   # Cloud (Go)
   cd cloud/backend && go build ./...
   ```
