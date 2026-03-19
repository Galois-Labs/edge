# Ring-Buffered Record Stream — Implementation Plan

## Critical Findings (from code audit)

1. **`StreamMeasurement` never yields `vector_data`** — the polling loop (grpc_server.py:1208-1321)
   only fills scalar `value`/`values_map`. Scope commands return JSON strings that get parsed as
   scalars (usually 0.0). This is why waveform streaming doesn't work in the monitor tab.

2. **`SDKExecutor.execute()` always returns strings** — no bytes path. The `RecordEngine` must
   bypass it entirely and access the dwfpy device handle directly via `wrapper.device`.

3. **`dwfpy.AnalogRecorder.process()`** is the correct non-blocking integration point — can be
   polled from a `ThreadPoolExecutor` thread. The alternative `record(callback=fn)` blocks for
   the full duration and is unsuitable for our async architecture.

4. **Cloud gRPC client has no `MaxRecvMsgSize`** (known bug, `manager.go`) — default 4MB limit.
   256K float64 samples = 2MB per chunk, which is safe. But 512K chunks (4MB) would hit the limit.
   **Keep chunks at 256K samples maximum.**

5. **Frontend waveform rendering already works** — `ChartWidget.tsx` has `decodeYData()` (base64 →
   typed array) and `buildWaveformData()` (x_start + i*x_increment). Just need the daemon to
   actually populate the `vector_data` field.

## Overview

Add a high-throughput continuous recording mode to the daemon that uses
triple ring-buffering to stream oscilloscope/digitizer data at sustained
rates (4-8 MS/s for Analog Discovery, higher for USB 3.0 devices). Data
is written to local disk (primary) and streamed to the cloud via gRPC
(secondary, with optional decimation for live preview).

This is fundamentally different from `StreamMeasurement` (which polls a
command at N ms intervals). `RecordStream` captures a gap-free data stream
from the hardware's record mode with zero-copy buffer management.

### Architecture

```
┌────────────── Daemon (Pi5) ──────────────────────────────────────┐
│                                                                    │
│  ┌──────────┐    ┌───────────────────────┐    ┌──────────────┐    │
│  │  Device   │    │    Ring Buffer (3)     │    │  Disk Writer │    │
│  │  Thread   │───→│  [A] [B] [C]          │───→│  (HDF5/raw)  │    │
│  │ (dwfpy    │    │  pre-alloc np.int16   │    └──────────────┘    │
│  │  record)  │    │  256K samples/slot    │                        │
│  └──────────┘    └──────────┬────────────┘    ┌──────────────┐    │
│                              │                  │  gRPC Stream │    │
│                              └────────────────→│  (decimate   │───→ Cloud
│                                                 │   + binary)  │    │
│  INVARIANT: Device thread NEVER blocks.         └──────────────┘    │
│  Ring full → overwrite oldest + set overrun flag.                   │
└────────────────────────────────────────────────────────────────────┘
```

### Bandwidth budget

| Component | Throughput | Notes |
|---|---|---|
| Analog Discovery record mode | 4-8 MS/s = 8-16 MB/s | USB 2.0 bottleneck |
| Pi5 SD/NVMe write | 40-400 MB/s | SD card is ~40, NVMe is 400+ |
| Pi5 Gigabit Ethernet | ~100 MB/s | Plenty of headroom |
| gRPC binary stream | 50+ MB/s | Single channel |

---

## Phase 0: Proto & Regeneration

### Task 0.1: Add RecordStream RPC and messages to edge.proto

**Context files to read:**
- `proto/edge/v1/edge.proto` (lines 48-54 for existing streaming RPCs, lines 320-362 for existing messages)
- `proto/buf.gen.yaml` (proto generation config)

**File to edit:** `proto/edge/v1/edge.proto`

**Changes:**
1. Add new RPC to `EdgeDaemonService` (after `StopStream`, before Status section ~line 56):
   ```protobuf
   // RecordStream starts a high-throughput continuous recording from a
   // digitizer/oscilloscope. Data flows as binary chunks via ring buffer.
   // The daemon writes to local disk and streams chunks to the caller.
   rpc RecordStream(RecordStreamRequest) returns (stream RecordChunk);

   // StopRecording terminates an active recording session.
   rpc StopRecording(StopRecordingRequest) returns (StopRecordingResponse);
   ```

2. Add new messages (after `StopStreamResponse`, ~line 363):
   ```protobuf
   message RecordStreamRequest {
     string record_id = 1;            // Unique recording session ID
     string instrument_id = 2;        // Instrument (VISA addr or SDK ID)
     int32 channel = 3;               // Scope/digitizer channel index
     double sample_rate = 4;          // Requested sample rate (Hz)
     double duration_seconds = 5;     // Recording length (0 = until stopped)
     double voltage_range = 6;        // Full-scale voltage range
     string coupling = 7;             // "dc" or "ac"
     bool store_locally = 8;          // Write to local disk on daemon
     string file_format = 9;          // "raw", "hdf5", "csv"
     int32 preview_decimation = 10;   // Send every Nth sample to cloud (0 = full rate)
   }

   message RecordChunk {
     string record_id = 1;            // Recording session ID
     bytes samples = 2;               // Raw binary samples (int16 or float32)
     string dtype = 3;                // "int16", "float32", "float64"
     uint32 sequence_number = 4;      // Monotonic chunk counter (detect drops)
     uint32 sample_count = 5;         // Number of samples in this chunk
     double sample_rate = 6;          // Actual sample rate
     bool buffer_overrun = 7;         // True if chunks were dropped
     bool is_final = 8;              // True for the last chunk
     string local_file_path = 9;      // Set on final chunk if store_locally=true
   }

   message StopRecordingRequest {
     string record_id = 1;
   }

   message StopRecordingResponse {
     bool success = 1;
     string file_path = 2;           // Local file path if stored
     uint64 total_samples = 3;
     double duration_seconds = 4;
     uint32 chunks_dropped = 5;
   }
   ```

**Verification:**
- `buf lint` passes
- `buf generate` produces updated Python stubs in `proto/gen/python/edge/v1/`

**Dependencies:** None (first task)

### Task 0.2: Copy regenerated Python stubs into daemon

**Context files to read:**
- `scripts/proto-gen.sh` (existing regeneration workflow)
- `src/galois_edge/edge_pb2.py`, `src/galois_edge/edge_pb2_grpc.py` (current stubs)
- `src/galois_edge/edge/v1/edge_pb2.py` (nested stubs for PyInstaller)

**Files to edit:**
- `src/galois_edge/edge_pb2.py` — replace with regenerated
- `src/galois_edge/edge_pb2_grpc.py` — replace with regenerated
- `src/galois_edge/edge/v1/edge_pb2.py` — replace with regenerated
- `src/galois_edge/edge/v1/edge_pb2_grpc.py` — replace with regenerated

**End state:** Both stub locations contain the new `RecordStreamRequest`,
`RecordChunk`, `StopRecordingRequest`, `StopRecordingResponse` messages
and the `RecordStream`/`StopRecording` service methods.

**Verification:**
```python
from galois_edge import edge_pb2
req = edge_pb2.RecordStreamRequest(instrument_id="test", sample_rate=1e6)
chunk = edge_pb2.RecordChunk(sequence_number=1, sample_count=256000)
assert hasattr(edge_pb2, 'RecordStreamRequest')
assert hasattr(edge_pb2, 'RecordChunk')
```

**Dependencies:** Task 0.1

---

## Phase 1: Daemon Ring Buffer & Record Engine

### Task 1.1: Create `record_engine.py` — ring buffer + threading

**Context files to read:**
- `src/galois_edge/sdk_wrappers/digilent_dwf_wrapper.py` (dwfpy device access patterns)
- `src/galois_edge/sdk_executor.py` (how SDK clients are managed, `_SDKClient.lock`)
- `src/galois_edge/grpc_server.py:1118-1322` (existing `StreamMeasurement` loop)

**File to create:** `src/galois_edge/record_engine.py`

**Design:**

```python
class RecordEngine:
    """Triple ring-buffered continuous recording from a DWF device.

    Three threads:
      - device_thread: reads from dwfpy AnalogRecorder, fills ring buffer
      - disk_thread: drains ring buffer to local file (optional)
      - The gRPC coroutine reads from an asyncio.Queue for network streaming
    """

    CHUNK_SAMPLES = 262144  # 256K samples per ring slot
    NUM_SLOTS = 3           # triple buffer

    def __init__(self, device, channel, sample_rate, voltage_range,
                 coupling, store_locally, file_format, preview_decimation):
        ...

    def start(self) -> None:
        """Configure scope once. Start device + disk threads."""

    def stop(self) -> StopResult:
        """Signal threads to stop, join, return summary."""

    async def chunks(self) -> AsyncIterator[ChunkResult]:
        """Async generator yielding RecordChunk data for gRPC streaming.
        Called by the gRPC handler's RecordStream method."""

    # Internal:
    def _device_loop(self) -> None:
        """Thread: dwfpy record mode callback → ring buffer.
        NEVER blocks. If ring full, overwrite oldest + set overrun flag."""

    def _disk_loop(self) -> None:
        """Thread: drain ring buffer → append to local file."""
```

**Ring buffer implementation:**
- Pre-allocate 3 numpy arrays: `[np.empty(CHUNK_SAMPLES, dtype=np.float64) for _ in range(3)]`
- Device thread writes into slot `write_idx`, increments when full
- Completed chunks pushed to `queue.Queue(maxsize=3)` (non-blocking put)
- If queue is full (backpressure), overwrite oldest and set `overrun_count += 1`
- Disk thread and gRPC coroutine both consume from the queue
  - Use a fanout: device thread puts into two queues (disk_queue, net_queue)
  - net_queue gets decimated data if `preview_decimation > 0`

**Device thread — dwfpy integration:**
```python
def _device_loop(self):
    ai = self._device.analog_input
    ai.setup_channel(self._channel, range=self._voltage_range, coupling=self._coupling)
    # Use record mode with callback
    recorder = ai.record(
        sample_rate=self._sample_rate,
        length=self._duration or 0,  # 0 = until stopped
        start=True,
    )
    # AnalogRecorder.record() blocks, calling our callback with chunks
    # We need to intercept the data as it arrives
```

dwfpy provides two integration points for record mode:
a) `record(callback=fn)` — blocks for full duration, callback per chunk. Unsuitable for async.
b) `AnalogRecorder.process()` — non-blocking, returns True if more data pending. **Use this.**

The device loop uses `process()` for non-blocking polling:
```python
ai = device.analog_input
ai.setup_channel(channel, range=voltage_range, coupling=coupling)
recorder = ai.record(sample_rate=rate, length=duration, start=False)
ai.configure(start=True)

while self._running:
    has_more = recorder.process()  # non-blocking: reads available chunk
    # After process(), recorder.channels[ch].data_samples has accumulated data
    # We need to extract the NEW samples since last call
    available, lost, corrupted = ai.record_status
    if available > 0:
        data = ai.channels[channel].get_data(sample_count=available)
        self._push_to_ring(data)
    if lost > 0 or corrupted > 0:
        self._overrun_count += 1
    if not has_more:
        break  # recording complete (duration reached)
    time.sleep(0.001)  # 1ms poll — fast enough for 4-8 MS/s
```

**Why `process()` over `record(callback)`:** The callback variant blocks the
thread for the entire recording duration. With `process()`, we poll in our
own loop and can check `self._running` for clean shutdown, push to the ring
buffer at our own pace, and interleave with other work if needed.

**Disk writer:**
```python
def _disk_loop(self):
    with open(self._file_path, 'wb') as f:
        while self._running or not self._disk_queue.empty():
            try:
                chunk_bytes = self._disk_queue.get(timeout=0.5)
                f.write(chunk_bytes)
            except queue.Empty:
                continue
```

**gRPC async generator:**
```python
async def chunks(self):
    loop = asyncio.get_running_loop()
    seq = 0
    while self._running:
        try:
            chunk = await loop.run_in_executor(
                None, lambda: self._net_queue.get(timeout=0.5))
            seq += 1
            yield ChunkResult(
                samples=chunk.tobytes(),
                dtype='float64',
                sequence_number=seq,
                sample_count=len(chunk),
                sample_rate=self._actual_sample_rate,
                buffer_overrun=self._overrun_count > 0,
            )
        except queue.Empty:
            continue
    # Final chunk
    yield ChunkResult(is_final=True, ...)
```

**Acceptance criteria:**
- `RecordEngine` can be instantiated with a dwfpy device
- Device thread fills ring buffer from record mode at sustained rate
- Disk thread writes to local file with zero gaps
- Async `chunks()` yields data for gRPC consumption
- Overrun flag is set when backpressure occurs (never blocks device thread)
- Clean shutdown: `stop()` joins threads, flushes queues, returns summary

**Dependencies:** Task 0.2 (needs proto types for ChunkResult shape)

### Task 1.2: Add `RecordStream` and `StopRecording` RPCs to gRPC server

**Context files to read:**
- `src/galois_edge/grpc_server.py` (full file — especially `StreamMeasurement` at 1118-1322, `StopStream` at 1324-1350, class constructor, `_active_streams` dict)
- `src/galois_edge/record_engine.py` (from Task 1.1)
- `src/galois_edge/sdk_executor.py` (to get the dwfpy device handle)

**File to edit:** `src/galois_edge/grpc_server.py`

**Changes:**

1. Add `_active_recordings: Dict[str, RecordEngine] = {}` to `__init__`

2. Add `RecordStream` method (after `StopStream`, ~line 1350):
   ```python
   async def RecordStream(self, request, context):
       record_id = request.record_id
       instrument_id = request.instrument_id

       # Get the SDK device handle (only SDK instruments support recording)
       if not self._sdk_executor:
           yield error chunk; return
       entry = self._sdk_executor._clients.get(instrument_id)
       if entry is None:
           yield error chunk; return

       # The SDK client wraps a dwfpy.Device — extract it
       device = getattr(entry.client, '_device', None)
       if device is None:
           yield error chunk; return

       engine = RecordEngine(
           device=device,
           channel=request.channel,
           sample_rate=request.sample_rate,
           voltage_range=request.voltage_range,
           coupling=request.coupling or 'dc',
           store_locally=request.store_locally,
           file_format=request.file_format or 'raw',
           preview_decimation=request.preview_decimation,
       )

       self._active_recordings[record_id] = engine
       engine.start()

       try:
           async for chunk in engine.chunks():
               if context.cancelled():
                   break
               yield edge_pb2.RecordChunk(
                   record_id=record_id,
                   samples=chunk.samples,
                   dtype=chunk.dtype,
                   sequence_number=chunk.sequence_number,
                   sample_count=chunk.sample_count,
                   sample_rate=chunk.sample_rate,
                   buffer_overrun=chunk.buffer_overrun,
                   is_final=chunk.is_final,
                   local_file_path=chunk.local_file_path or '',
               )
       finally:
           result = engine.stop()
           self._active_recordings.pop(record_id, None)
   ```

3. Add `StopRecording` method:
   ```python
   async def StopRecording(self, request, context):
       record_id = request.record_id
       engine = self._active_recordings.get(record_id)
       if engine is None:
           return edge_pb2.StopRecordingResponse(success=False)
       result = engine.stop()
       self._active_recordings.pop(record_id, None)
       return edge_pb2.StopRecordingResponse(
           success=True,
           file_path=result.file_path or '',
           total_samples=result.total_samples,
           duration_seconds=result.duration_seconds,
           chunks_dropped=result.chunks_dropped,
       )
   ```

**End state:** Daemon accepts `RecordStream` RPC, creates a `RecordEngine`, streams
`RecordChunk` messages to the caller until stopped or duration reached.

**Verification:**
- gRPC server starts without import errors
- `RecordStream` RPC is registered in the servicer
- Existing `StreamMeasurement` still works (no regressions)
- Test with grpcurl or a Python client script on the Pi5

**Dependencies:** Tasks 0.2, 1.1

### Task 1.3: Expose `_device` from DigilentDwfClient for RecordEngine access

**Context files to read:**
- `src/galois_edge/sdk_wrappers/digilent_dwf_wrapper.py` (the `_device` attribute)
- `src/galois_edge/sdk_executor.py` (`_SDKClient` dataclass, `_clients` dict)

**File to edit:** `src/galois_edge/sdk_wrappers/digilent_dwf_wrapper.py`

**Changes:** Add a public property:
```python
@property
def device(self):
    """Expose the underlying dwfpy.Device for RecordEngine access."""
    return self._device
```

**Rationale:** The `RecordEngine` needs direct access to the `dwfpy.Device`
to call `analog_input.record()` and `read_status()`. The SDK executor's
`_SDKClient.client` gives us the wrapper; `.device` gives us the raw handle.
The existing `_dev` property raises on None, which is correct for commands
but the gRPC handler needs a non-throwing accessor to check availability.

**End state:** `wrapper_instance.device` returns the dwfpy.Device or None.

**Verification:** `DigilentDwfClient().device` returns None (not connected),
connected client returns a dwfpy.Device instance.

**Dependencies:** None (can run in parallel with 1.1 and 1.2)

---

## Phase 2: Optimize Existing StreamMeasurement for Waveform Data

This phase improves the existing live monitoring path (separate from recording).

### Task 2.1: Emit VectorData for scope commands in StreamMeasurement

**Context files to read:**
- `src/galois_edge/grpc_server.py:1118-1322` (StreamMeasurement loop)
- `src/galois_edge/grpc_server.py:1239-1283` (result handling — currently only emits scalar `value`)
- `src/galois_edge/sdk_wrappers/digilent_dwf_wrapper.py` (`scope_acquire` returns JSON string)
- `proto/edge/v1/edge.proto:329-340` (VectorData message)

**File to edit:** `src/galois_edge/grpc_server.py`

**Changes to StreamMeasurement loop (lines 1239-1283):**

After the SDK command executes successfully, detect if the response is a
JSON array (scope data) and pack it as `VectorData` instead of trying to
parse it as a scalar:

```python
if result["success"]:
    raw = result["response"].strip()
    raw = self._apply_response_processing(raw, instrument_id, command_name)
    ts_ms = int(time.time() * 1000)

    # Detect array response (scope data) → emit as VectorData
    if raw.startswith('[') and cmd.returns and cmd.returns.type == 'string':
        try:
            import json, struct
            values = json.loads(raw)
            if isinstance(values, list) and len(values) > 0:
                packed = struct.pack(f'<{len(values)}d', *values)
                vector = edge_pb2.VectorData(
                    y_data=packed,
                    y_dtype='float64',
                    y_length=len(values),
                    x_start=0.0,
                    x_increment=1.0 / (cmd.params.get('sample_rate', {}).default or 1e6) if hasattr(cmd, 'params') else 1e-6,
                    x_unit='s',
                    y_unit=unit or 'V',
                    x_name='Time',
                )
                yield edge_pb2.MeasurementDataPoint(
                    stream_id=stream_id,
                    value=float(values[0]),
                    timestamp_ms=ts_ms,
                    unit=unit,
                    status="ok",
                    vector_data=vector,
                )
                continue  # skip normal scalar emission
        except (json.JSONDecodeError, struct.error):
            pass  # fall through to scalar handling

    # ... existing scalar handling ...
```

**Rationale:** The cloud `stream.go:125-138` already forwards `vector_data`
via SSE, and the frontend `ChartWidget.tsx` already renders waveforms from
VectorData. This change completes the pipeline without requiring any
cloud or frontend changes.

**End state:** Streaming `scope_acquire` in the Monitor tab displays a
real-time updating waveform chart instead of trying to plot a scalar 0.

**Verification:**
1. Start a stream on the Digilent's `scope_acquire` command
2. Cloud receives `MeasurementDataPoint` with `vector_data` populated
3. Frontend renders a waveform line chart (not a flat scalar line)
4. Existing scalar streams (temperature, voltage) still work unchanged

**Dependencies:** None (independent of Phase 1)

### Task 2.2: "Configure once, re-arm in loop" optimization for scope streaming

**Context files to read:**
- `src/galois_edge/sdk_wrappers/digilent_dwf_wrapper.py` (`scope_acquire` method)

**File to edit:** `src/galois_edge/sdk_wrappers/digilent_dwf_wrapper.py`

**Changes:** Add a `scope_acquire_fast` method that skips reconfiguration
if the scope is already configured with the same parameters:

```python
def scope_acquire_fast(self, channel=0, v_range=5.0, samples=4096,
                       sample_rate=1000000.0) -> str:
    """Optimized acquisition for streaming — configures once, then re-arms."""
    ai = self._dev.analog_input
    ch = int(channel)
    # Only reconfigure if parameters changed
    key = (ch, float(v_range), int(samples), float(sample_rate))
    if getattr(self, '_last_scope_config', None) != key:
        ai.setup_channel(ch, range=float(v_range), coupling='dc', enabled=True)
        ai.trigger.auto_timeout = 2.0
        self._last_scope_config = key
    # Re-arm and acquire (skip setup_channel / setup_acquisition)
    ai.single(sample_rate=float(sample_rate), buffer_size=int(samples), start=True)
    data = ai.channels[ch].get_data()
    if data is None:
        return "[]"
    try:
        return json.dumps(data.tolist())
    except AttributeError:
        return json.dumps(list(data))
```

Add corresponding profile command marked `streamable: true`.

**End state:** Streaming scope data at 10 Hz skips USB configuration
overhead on every iteration, reducing per-acquisition time.

**Verification:** Time 100 `scope_acquire` vs 100 `scope_acquire_fast` calls.
Fast variant should be measurably quicker (fewer USB config round-trips).

**Dependencies:** None (can run in parallel with 2.1)

---

## Phase 3: Cloud Backend Support

### Task 3.1: Add RecordStream proxy handler to cloud backend

**Context files to read:**
- `~/work/galois/cloud/backend/internal/handler/stream.go` (existing stream handler)
- `~/work/galois/cloud/backend/internal/service/stream.go` (StreamManager)
- `~/work/galois/cloud/backend/internal/grpcclient/client.go` (gRPC client wrapper)
- `~/work/galois/cloud/backend/internal/server/routes.go` (route registration)

**Files to create/edit:**
- `~/work/galois/cloud/backend/internal/handler/record.go` (new handler)
- `~/work/galois/cloud/backend/internal/server/routes.go` (add routes)
- `~/work/galois/cloud/backend/internal/grpcclient/client.go` (add RecordStream method)

**Changes:**

New handler with endpoints:
- `POST /api/v1/recordings` — start recording (calls daemon RecordStream, stores chunks to temp file, streams decimated preview via WebSocket)
- `DELETE /api/v1/recordings/{id}` — stop recording
- `GET /api/v1/recordings/{id}/download` — download completed recording file
- `GET /api/v1/recordings/{id}/ws` — WebSocket for live preview (binary frames)

**End state:** Cloud can proxy RecordStream from daemon, store raw data,
serve live preview and completed file download.

**Dependencies:** Phase 0 (needs updated proto stubs in Go)

### Task 3.2: Frontend recording UI

**Context files to read:**
- `~/work/galois/cloud/web/src/pages/Monitor.tsx`
- `~/work/galois/cloud/web/src/components/monitor/ChartWidget.tsx`
- `~/work/galois/cloud/web/src/hooks/use-sse.ts`

**Files to create/edit:**
- `~/work/galois/cloud/web/src/pages/Recording.tsx` (new page)
- `~/work/galois/cloud/web/src/hooks/use-recording-ws.ts` (WebSocket binary hook)
- `~/work/galois/cloud/web/src/components/layout/Sidebar.tsx` (add nav entry)
- `~/work/galois/cloud/web/src/App.tsx` (add route)

**End state:** New "Recording" page where users can start/stop continuous
recordings, see a live decimated preview, and download completed data files.

**Dependencies:** Task 3.1

---

## Execution Order & Parallelism

```
Phase 0 (sequential):
  0.1  Proto changes ──→ 0.2  Regenerate stubs

Phase 1 (parallel after 0.2):
  ┌── 1.1  RecordEngine (ring buffer + threads)
  │
  ├── 1.2  gRPC RecordStream handler (depends on 1.1)
  │
  └── 1.3  DigilentDwfClient.device property (parallel with 1.1)

Phase 2 (parallel with Phase 1, no dependencies):
  ┌── 2.1  VectorData emission in StreamMeasurement
  │
  └── 2.2  scope_acquire_fast optimization

Phase 3 (after Phase 1):
  3.1  Cloud backend handler ──→ 3.2  Frontend UI
```

**Minimum viable demo:** Phases 0 + 1 — daemon can record and stream
binary chunks over gRPC. Test with a Python gRPC client on the Pi5.

**Full end-to-end:** All phases — record from cloud UI, see live preview,
download completed data file.

---

## Testing Strategy

### Unit tests (daemon)
- `tests/test_record_engine.py`:
  - Ring buffer overflow behavior (device thread faster than consumer)
  - Clean shutdown with pending chunks
  - Chunk sequence numbering
  - Overrun flag propagation
  - Mock dwfpy device with synthetic data

### Integration test (Pi5)
- Start RecordStream via gRPC, verify chunks arrive with correct sequence numbers
- Verify local file contains correct number of samples
- Verify no data loss: `sum(chunk.sample_count) == file_size / sizeof(dtype)`
- Run for 10 seconds at max rate, check overrun count

### End-to-end test (cloud)
- Start recording from frontend, verify waveform preview updates
- Stop recording, download file, verify sample count matches duration × rate
