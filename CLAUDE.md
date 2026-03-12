# Galois Edge Daemon — Implementation Directives

## Active Implementation Plan

There is a detailed implementation plan at `docs/implementation-plan.md` that must be executed in phases.
Supporting context is in `docs/agentfindings.md` and `docs/architecture-extensions.md`.

### Execution Instructions

When asked to implement the plan (or any phase/task from it):

1. **Read `docs/implementation-plan.md` first** — it contains exact file paths, line numbers, code to write, acceptance criteria, and dependency ordering.
2. **Use opus subagents** (`model: "opus"`) for each independent task. Launch tasks that have no dependencies on each other in parallel.
3. **Each subagent prompt must include:**
   - The full task description from the plan (copy it verbatim)
   - The list of "context files to read" and "files to edit"
   - The acceptance criteria
   - A directive to run tests after making changes (`pytest tests/ -v`)
4. **Do NOT start a later phase until all tasks in the current phase pass tests.**
5. **Phase 0 is the top priority** — it fixes pre-existing crashes. Daemon tasks (0.1-0.4) can parallel with cloud tasks (0.5-0.6).

### Key Architecture Facts

- The daemon uses `grpc.aio` (async gRPC) with a `ThreadPoolExecutor` for blocking VISA calls
- `instrument_id` IS the VISA address (same value, confirmed)
- The cloud relay always JSON-encodes gRPC responses (bytes become base64)
- The cloud's Go proto stubs at `~/work/galois/cloud/backend/internal/gen/proto/galois/edge/v1/edge_grpc.go` are hand-written (not generated) and have phantom fields that cause panics
- `ParameterConfig.map` is used in 43 YAML profiles but silently dropped — apply forward-map only (label → wire value on writes), do NOT reverse-map numeric returns
- The daemon (`profile_schema.py`) uses plain dataclasses; the cloud uses Pydantic with strict enums

### Project Structure

- Edge daemon: `src/galois_edge/` (Python, gRPC server)
- Cloud backend: `~/work/galois/cloud/backend/` (Go, HTTP+gRPC proxy)
- Cloud frontend: `~/work/galois/cloud/web/` (React/TypeScript)
- Proto source: `proto/edge/v1/edge.proto`
- Instrument profiles: `src/galois_edge/profiles/*.yaml`
- SDK-pending profiles: `src/galois_edge/profiles/_needs_python/`
- Tests: `tests/`
