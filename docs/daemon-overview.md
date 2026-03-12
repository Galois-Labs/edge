# Galois Edge Daemon

## Spec Sheet

| | |
|---|---|
| **What it is** | A Python gRPC service that sits on a lab machine and acts as a unified gateway to scientific instruments |
| **Runtime** | Python 3.10+, async event loop (`grpc.aio`) with a `ThreadPoolExecutor` for blocking instrument I/O |
| **Transports** | PyVISA (USB-TMC, TCPIP/VXI-11, HiSLIP, Serial), linux-gpib, raw USB (pyusb), LAN (static + mDNS) |
| **Interfaces out** | gRPC on `127.0.0.1:50052`, optional WebSocket on `127.0.0.1:8766` |
| **Instrument profiles** | ~70 YAML files describing commands, parameters, return types, sequences, and SDK bindings per instrument model |
| **Discovery** | Automatic on startup + periodic background rescan; matches instruments to profiles via `*IDN?` regex patterns |
| **Streaming** | Server-streaming gRPC for periodic measurement polling; bidirectional streaming for command batches |
| **Sweep/ramp** | First-class support for long-running hardware sweeps (magnets, temperature controllers) with safety interlocks |
| **SDK support** | Runtime `importlib` loading of vendor Python SDKs for instruments that don't speak SCPI |
| **Cloud link** | A Go supervisor on the same machine proxies the gRPC port over Tailscale to the Galois cloud backend |

### Capabilities at a glance

- **Send raw SCPI** to any connected instrument (query or write)
- **Execute named commands** from a profile with typed parameters, units, and validation
- **Run multi-step sequences** (e.g., IV sweep: set voltage, wait, read current, repeat)
- **Stream measurements** at a configurable interval with automatic timestamping
- **Start/monitor/stop hardware sweeps** with polling, cancellation, and abort commands
- **Discover instruments** across GPIB, USB, LAN, and serial buses
- **Advertise capabilities** so the cloud UI can render controls without knowing instrument details
- **Proxy vendor SDK calls** for non-SCPI instruments (digitizers, custom cryogenics, etc.)

---

## How It Works

### Startup sequence

```
main.py: EdgeDaemon.start()
  |
  +--> Load config from env vars (config.py)
  +--> Create InstrumentManager (PyVISA + GPIB + USB + LAN backends)
  +--> Create SDKExecutor (empty, clients loaded on demand)
  +--> Load all YAML profiles from disk (ProfileLoader)
  +--> Scan for instruments:
  |      for each VISA resource found:
  |        connect -> send *IDN? -> regex-match against profiles
  |        -> register in CapabilityManager
  |        -> run init_commands if profile defines them
  |        -> connect SDK client if profile has an sdk: block
  |
  +--> Start gRPC server on :50052
  +--> Start WebSocket server on :8766 (optional)
  +--> Launch background tasks:
         - GPIB bus scan (slow, runs in thread pool)
         - Periodic rescan (every scan_interval_s, detects new/removed instruments)
         - stdin watcher (Go supervisor sends EOF to signal shutdown)
```

### Core execution model

The daemon is an **async Python process** running a single event loop. All gRPC handlers are async coroutines. Instrument I/O (PyVISA `query()`, `write()`) is blocking, so it's dispatched to a `ThreadPoolExecutor` (default 10 threads) via `asyncio.run_in_executor()`.

Each instrument has its own `threading.Lock` inside `CommandHandler`. This serializes all SCPI access per instrument, which matches GPIB bus semantics (one talker/listener at a time) and prevents interleaved reads on shared buses.

```
gRPC request arrives (async)
  |
  +--> CapabilityManager resolves the command name to an SCPI string
  |      (substitutes parameters, applies value mappings)
  |
  +--> Dispatched to ThreadPoolExecutor:
  |      CommandHandler acquires per-instrument lock
  |      InstrumentManager.query() or .write()
  |      Lock released
  |
  +--> Response packed into protobuf, returned to caller
```

### Profile matching

When an instrument is discovered, the daemon sends `*IDN?` and gets back something like:

```
KEITHLEY INSTRUMENTS INC.,MODEL 2400,1234567,C30
```

This is matched against `identity.patterns` in each loaded YAML profile using case-insensitive regex. The first match wins. Once matched, the profile's commands, sequences, and settings are registered in `CapabilityManager` and become available through the gRPC API.

Instruments without a matching profile still work — they just only support raw SCPI via `SendCommand`, with no named commands or capability advertisement.

### Sweep lifecycle

For instruments where abrupt value changes are dangerous (superconducting magnets, temperature controllers), the daemon implements a sweep/ramp system:

1. Client calls `StartSweep(instrument_id, command_name, target_value, sweep_rate)`
2. Daemon reserves the instrument (prevents concurrent sweeps), sends the SCPI ramp command with substituted `{value}` and `{sweep_rate}`
3. A background task polls a status query at `poll_interval_ms` intervals
4. When the response matches `check_idle_match`, the sweep is complete
5. If the client calls `StopSweep`, the daemon sends the abort command and releases the instrument

Commands marked `requires_sweep: true` in a profile are rejected by `ExecuteCommand` — they can only be executed through the sweep system.

---

## How It Talks to the Cloud

### Network topology

```
Lab machine                              Cloud
+---------------------------------+      +---------------------------+
| Galois Edge Daemon              |      | Go backend (HTTP + gRPC)  |
|   gRPC on 127.0.0.1:50052      |      |                           |
|          |                      |      |   /api/instruments        |
|   Go supervisor (Tailscale)  ----TCP------> gRPC client            |
|          |                      |      |   proxies to HTTP/JSON    |
|   WebSocket on 127.0.0.1:8766  |      |                           |
+---------------------------------+      |   React frontend          |
                                         +---------------------------+
```

The daemon listens only on `127.0.0.1`. A **Go supervisor process** on the same lab machine:
- Launches and manages the daemon process (stdin EOF = shutdown signal)
- Exposes the daemon's gRPC port over **Tailscale** (encrypted WireGuard mesh VPN)
- The cloud backend connects to this Tailscale address as a standard gRPC client

### Cloud-side protocol flow

The Go cloud backend acts as a **gRPC-to-HTTP proxy**:

1. **Frontend** (React) makes REST calls like `POST /api/instruments/{id}/commands/{name}`
2. **Backend** (Go) translates these into gRPC calls to the daemon: `ExecuteCommand`, `ListInstruments`, `GetCapabilities`, etc.
3. **Responses** are JSON-encoded. The Go backend serializes gRPC protobuf responses to JSON, with `bytes` fields becoming base64
4. **Streaming** is handled via the `StreamMeasurement` RPC — the Go backend holds a server-stream open and forwards data points to the frontend over WebSocket or SSE

### Key protocol details

| Aspect | Detail |
|---|---|
| **Serialization** | Protobuf over gRPC (daemon <-> cloud), JSON over HTTP (cloud <-> frontend) |
| **Auth** | None at the gRPC level — Tailscale handles machine-level auth and encryption |
| **Max message size** | Daemon can send up to ~50 MB (binary traces); cloud defaults to 4 MB receive limit (known bug) |
| **Heartbeat** | Daemon sends periodic `Heartbeat` RPCs with instrument count; cloud uses this for online/offline status |
| **Registration** | Daemon calls `RegisterEdge` on startup to announce itself to the cloud with a capability summary |

### Proto definition

The single proto file (`proto/edge/v1/edge.proto`) defines the `EdgeDaemonService` with these RPC groups:

```protobuf
service EdgeDaemonService {
  // Core SCPI
  rpc SendCommand(SendCommandRequest) returns (SendCommandResponse);
  rpc StreamCommands(stream SendCommandRequest) returns (stream SendCommandResponse);

  // Discovery
  rpc ListInstruments(ListInstrumentsRequest) returns (ListInstrumentsResponse);
  rpc GetInstrument(GetInstrumentRequest) returns (GetInstrumentResponse);
  rpc ScanInstruments(ScanInstrumentsRequest) returns (ScanInstrumentsResponse);

  // Profile-based
  rpc GetCapabilities(GetCapabilitiesRequest) returns (GetCapabilitiesResponse);
  rpc ExecuteCommand(ExecuteCommandRequest) returns (ExecuteCommandResponse);
  rpc ExecuteSequence(ExecuteSequenceRequest) returns (ExecuteSequenceResponse);

  // Streaming
  rpc StreamMeasurement(StreamMeasurementRequest) returns (stream MeasurementDataPoint);
  rpc StopStream(StopStreamRequest) returns (StopStreamResponse);

  // Sweep/Ramp
  rpc StartSweep(StartSweepRequest) returns (StartSweepResponse);
  rpc GetSweepStatus(GetSweepStatusRequest) returns (GetSweepStatusResponse);
  rpc StopSweep(StopSweepRequest) returns (StopSweepResponse);

  // Health
  rpc Ping(PingRequest) returns (PingResponse);
  rpc GetStatus(GetStatusRequest) returns (GetStatusResponse);
  rpc Heartbeat(HeartbeatRequest) returns (HeartbeatResponse);
  rpc RegisterEdge(RegisterEdgeRequest) returns (RegisterEdgeResponse);
}
```

### What the cloud knows vs. what the daemon knows

| Cloud knows | Daemon knows |
|---|---|
| Instrument list (names, IDs, classes) | Actual VISA addresses and bus topology |
| Available commands per instrument (names, params, types) | SCPI strings, timing, bus locking |
| Measurement data streams (timestamped values) | Raw instrument responses, binary parsing |
| Sweep status (percentage, current value) | Hardware ramp commands, abort sequences |
| Nothing about GPIB/USB/Serial protocols | Everything about transport-level details |

The cloud treats the daemon as a black box. It never sends raw SCPI — it uses named commands from the capability advertisement. This means the frontend can render parameter sliders, unit labels, and sweep controls without any instrument-specific knowledge.

---

## Code Structure

```
src/galois_edge/
|
|-- main.py                    Daemon lifecycle (start/stop/background tasks)
|-- config.py                  Env var config loading (frozen dataclass)
|
|-- grpc_server.py             gRPC servicer — all RPC handlers (~1400 lines)
|-- ws_server.py               WebSocket server for real-time data (optional)
|
|-- instrument_manager.py      Transport abstraction (PyVISA/GPIB/USB/LAN)
|-- command_handler.py         SCPI dispatch with per-instrument locking
|-- capability_manager.py      Profile registry + command resolution
|
|-- profile_schema.py          Dataclass definitions for YAML profile structure
|-- profile_loader.py          YAML loading + *IDN? pattern matching
|
|-- sdk_executor.py            Vendor SDK lifecycle (importlib + reflection)
|-- sdk_wrappers/              Thin wrappers around vendor SDKs (future)
|
|-- gpib_manager.py            linux-gpib wrapper
|-- usb_transport.py           Raw USB (pyusb) wrapper
|-- lan_discovery.py           LAN discovery (static IPs + mDNS)
|
|-- edge_pb2.py                Generated protobuf messages (committed)
|-- edge_pb2_grpc.py           Generated gRPC stubs (committed)
|
|-- profiles/                  ~70 YAML instrument profiles
|   |-- keithley_2400.yaml
|   |-- oxford_ips120.yaml
|   |-- triton.yaml
|   |-- ...
|
|-- profiles/_needs_python/    Profiles requiring vendor SDK (not yet wired)
```

### Module roles and relationships

```
                        main.py
                     (orchestrator)
                          |
          +-------+-------+-------+--------+
          |       |       |       |        |
       config  profile  instr   grpc     ws
        .py    loader   mgr    server   server
                 |       |       |
              profile    |    command    capability   sdk
              schema     |    handler    manager    executor
                         |       |          |
                    gpib_mgr  (lock per   (profile
                    usb_xport  instrument) registry)
                    lan_disc
```

**`main.py`** creates all components and wires them together via constructor injection. Nothing imports globals — every dependency is passed explicitly, making the system testable with mocks.

**`grpc_server.py`** is the largest file. It implements every RPC as an async method on `EdgeDaemonServicer`. It owns the `ThreadPoolExecutor`, manages active streams and sweeps, and builds protobuf response messages. This is where most of the application logic lives.

**`instrument_manager.py`** is the transport layer. It hides the differences between PyVISA, linux-gpib, raw USB, and LAN instruments behind `connect()`, `query()`, `write()`, and `disconnect()`. Higher layers never touch transport details.

**`command_handler.py`** is thin — it acquires a per-instrument lock, calls `instrument_manager.query()` or `.write()`, and returns a result dict. Its job is serialization and timeout enforcement.

**`capability_manager.py`** is the bridge between profiles and the gRPC API. It resolves a `(instrument_id, command_name, params)` tuple into either an SCPI string (for `CommandHandler`) or an `SDKCommandRequest` (for `SDKExecutor`).

**`profile_schema.py`** defines the data model as plain Python dataclasses: `InstrumentProfile` > `CommandConfig` > `ParameterConfig` > etc. The `profile_from_dict()` function parses a YAML dict into this tree.

**`profile_loader.py`** globs `*.yaml` from the profiles directory, parses each one via `profile_from_dict()`, and provides `match_instrument(idn_response)` to find the right profile for a given `*IDN?` string.

**`sdk_executor.py`** handles instruments that use vendor Python libraries instead of SCPI. It dynamically imports the SDK module, instantiates a client object, and calls methods on it via reflection. The SDK lifecycle (connect/disconnect) is managed alongside the instrument connection lifecycle.

### Test structure

```
tests/
|-- test_grpc_server.py       Integration tests — mock InstrumentManager, test RPCs end-to-end
|-- test_command_handler.py   Unit tests — mock InstrumentManager, test locking and dispatch
|-- test_profile_loader.py    Unit tests — load real YAML files, test pattern matching
|-- test_profile_schema.py    Unit tests — validate dataclass parsing and validation
|-- test_config.py            Unit tests — env var loading with defaults
```

Tests mock at the `InstrumentManager` boundary — everything above it (gRPC handlers, command handler, capability manager) is tested with real logic, while everything below (VISA, GPIB, USB) is mocked out.

### Configuration

All config is via environment variables with sensible defaults:

| Variable | Default | Purpose |
|---|---|---|
| `GRPC_PORT` | `50052` | gRPC listen port |
| `GRPC_MAX_WORKERS` | `10` | Thread pool size for blocking I/O |
| `WS_PORT` | `8766` | WebSocket listen port |
| `LOG_LEVEL` | `INFO` | Logging verbosity |
| `PROFILE_DIR` | `./src/galois_edge/profiles` | YAML profile directory |
| `GPIB_ENABLED` | `true` (Linux) | Enable linux-gpib backend |
| `INCLUDE_SERIAL_PORTS` | `false` | Include ASRL ports in discovery |
| `LAN_INSTRUMENTS` | `""` | Comma-separated instrument IPs |
| `SCAN_INTERVAL_S` | `60` | Background rescan interval (0 = disabled) |

### Dependencies

**Required:** `grpcio`, `protobuf`, `pyvisa`, `pyvisa-py`, `aiohttp`, `pyyaml`, `python-dotenv`

**Optional:** `gpib-ctypes` (GPIB), `pyusb` (raw USB), `zeroconf` (mDNS), `pyzmq` (ZMQ streaming), vendor instrument SDKs (loaded at runtime)
