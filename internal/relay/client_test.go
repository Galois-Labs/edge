package relay

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

// ---------------------------------------------------------------------------
// Backoff tests
// ---------------------------------------------------------------------------

func TestBackoffDelayDeterministic(t *testing.T) {
	tests := []struct {
		attempt  int
		expected time.Duration
	}{
		{0, 2 * time.Second},  // clamped to attempt 1
		{1, 2 * time.Second},  // 2s * 2^0 = 2s
		{2, 4 * time.Second},  // 2s * 2^1 = 4s
		{3, 8 * time.Second},  // 2s * 2^2 = 8s
		{4, 16 * time.Second}, // 2s * 2^3 = 16s
		{5, 32 * time.Second}, // 2s * 2^4 = 32s
		{6, 64 * time.Second}, // 2s * 2^5 = 64s
		{7, 128 * time.Second},
		{8, 256 * time.Second},
		{9, 5 * time.Minute}, // capped at 5min
		{10, 5 * time.Minute},
		{20, 5 * time.Minute},
	}

	for _, tt := range tests {
		got := BackoffDelayDeterministic(tt.attempt)
		if got != tt.expected {
			t.Errorf("BackoffDelayDeterministic(%d) = %v, want %v", tt.attempt, got, tt.expected)
		}
	}
}

func TestBackoffDelayHasJitter(t *testing.T) {
	// BackoffDelay should return a value between base and base * 1.25.
	for attempt := 1; attempt <= 5; attempt++ {
		base := BackoffDelayDeterministic(attempt)
		maxWithJitter := time.Duration(float64(base) * 1.25)

		for i := 0; i < 50; i++ {
			got := BackoffDelay(attempt)
			if got < base {
				t.Errorf("BackoffDelay(%d) = %v, want >= %v (base)", attempt, got, base)
			}
			if got > maxWithJitter {
				t.Errorf("BackoffDelay(%d) = %v, want <= %v (base*1.25)", attempt, got, maxWithJitter)
			}
		}
	}
}

func TestBackoffDelayCappedAt5Min(t *testing.T) {
	// Even with jitter, should not exceed 5min * 1.25 = 6.25min.
	maxAllowed := time.Duration(float64(5*time.Minute) * 1.25)

	for i := 0; i < 100; i++ {
		got := BackoffDelay(100)
		if got > maxAllowed {
			t.Errorf("BackoffDelay(100) = %v, exceeded max allowed %v", got, maxAllowed)
		}
	}
}

// ---------------------------------------------------------------------------
// JSON serialization tests
// ---------------------------------------------------------------------------

func TestMarshalHelloMessage(t *testing.T) {
	msg := &relayMessage{
		Type:     "hello",
		EdgeID:   "test-uuid",
		EdgeName: "pi5-demo",
		Version:  "1.0.0",
	}

	data, err := MarshalMessage(msg)
	if err != nil {
		t.Fatalf("MarshalMessage: %v", err)
	}

	// Verify it can be parsed back.
	got, err := UnmarshalMessage(data)
	if err != nil {
		t.Fatalf("UnmarshalMessage: %v", err)
	}

	if got.Type != "hello" {
		t.Errorf("Type = %q, want %q", got.Type, "hello")
	}
	if got.EdgeID != "test-uuid" {
		t.Errorf("EdgeID = %q, want %q", got.EdgeID, "test-uuid")
	}
	if got.EdgeName != "pi5-demo" {
		t.Errorf("EdgeName = %q, want %q", got.EdgeName, "pi5-demo")
	}
	if got.Version != "1.0.0" {
		t.Errorf("Version = %q, want %q", got.Version, "1.0.0")
	}
}

func TestMarshalHeartbeatMessage(t *testing.T) {
	now := time.Now().UnixMilli()
	msg := &relayMessage{
		Type:        "heartbeat",
		TimestampMs: now,
	}

	data, err := MarshalMessage(msg)
	if err != nil {
		t.Fatalf("MarshalMessage: %v", err)
	}

	got, err := UnmarshalMessage(data)
	if err != nil {
		t.Fatalf("UnmarshalMessage: %v", err)
	}

	if got.Type != "heartbeat" {
		t.Errorf("Type = %q, want %q", got.Type, "heartbeat")
	}
	if got.TimestampMs != now {
		t.Errorf("TimestampMs = %d, want %d", got.TimestampMs, now)
	}
}

func TestMarshalCommandResponse(t *testing.T) {
	msg := &relayMessage{
		Type:            "command_response",
		RequestID:       "req-123",
		Success:         true,
		Data:            "1.234",
		ScpiCommand:     "MEAS:VOLT:DC?",
		ExecutionTimeMs: 45,
	}

	data, err := MarshalMessage(msg)
	if err != nil {
		t.Fatalf("MarshalMessage: %v", err)
	}

	got, err := UnmarshalMessage(data)
	if err != nil {
		t.Fatalf("UnmarshalMessage: %v", err)
	}

	if got.Type != "command_response" {
		t.Errorf("Type = %q, want %q", got.Type, "command_response")
	}
	if got.RequestID != "req-123" {
		t.Errorf("RequestID = %q, want %q", got.RequestID, "req-123")
	}
	if !got.Success {
		t.Error("Success = false, want true")
	}
	if got.Data != "1.234" {
		t.Errorf("Data = %q, want %q", got.Data, "1.234")
	}
	if got.ScpiCommand != "MEAS:VOLT:DC?" {
		t.Errorf("ScpiCommand = %q, want %q", got.ScpiCommand, "MEAS:VOLT:DC?")
	}
	if got.ExecutionTimeMs != 45 {
		t.Errorf("ExecutionTimeMs = %d, want %d", got.ExecutionTimeMs, 45)
	}
}

func TestUnmarshalCommandRequest(t *testing.T) {
	raw := `{
		"type": "command_request",
		"request_id": "uuid-456",
		"instrument_id": "GPIB0::22::INSTR",
		"command_name": "measure_voltage",
		"parameters": {"range": "10"},
		"is_query": true
	}`

	got, err := UnmarshalMessage([]byte(raw))
	if err != nil {
		t.Fatalf("UnmarshalMessage: %v", err)
	}

	if got.Type != "command_request" {
		t.Errorf("Type = %q, want %q", got.Type, "command_request")
	}
	if got.RequestID != "uuid-456" {
		t.Errorf("RequestID = %q, want %q", got.RequestID, "uuid-456")
	}
	if got.InstrumentID != "GPIB0::22::INSTR" {
		t.Errorf("InstrumentID = %q, want %q", got.InstrumentID, "GPIB0::22::INSTR")
	}
	if got.CommandName != "measure_voltage" {
		t.Errorf("CommandName = %q, want %q", got.CommandName, "measure_voltage")
	}
	if !got.IsQuery {
		t.Error("IsQuery = false, want true")
	}
	if got.Parameters["range"] != "10" {
		t.Errorf("Parameters[range] = %q, want %q", got.Parameters["range"], "10")
	}
}

func TestOmitEmptyFields(t *testing.T) {
	// A hello message should not include command_response fields.
	msg := &relayMessage{
		Type:     "hello",
		EdgeID:   "test",
		EdgeName: "test-name",
		Version:  "1.0.0",
	}

	data, err := MarshalMessage(msg)
	if err != nil {
		t.Fatalf("MarshalMessage: %v", err)
	}

	// Parse into a generic map to check omitted fields.
	var m map[string]interface{}
	if err := json.Unmarshal(data, &m); err != nil {
		t.Fatalf("json.Unmarshal: %v", err)
	}

	// These fields should be omitted (omitempty).
	omitted := []string{
		"timestamp_ms", "request_id", "instrument_id", "command_name",
		"parameters", "is_query", "success", "data", "error_message",
		"scpi_command", "execution_time_ms",
	}
	for _, key := range omitted {
		if _, ok := m[key]; ok {
			t.Errorf("field %q should be omitted from hello message, but was present", key)
		}
	}
}

func TestMarshalErrorResponse(t *testing.T) {
	msg := &relayMessage{
		Type:            "command_response",
		RequestID:       "req-err",
		Success:         false,
		ErrorMessage:    "instrument not found",
		ExecutionTimeMs: 12,
	}

	data, err := MarshalMessage(msg)
	if err != nil {
		t.Fatalf("MarshalMessage: %v", err)
	}

	got, err := UnmarshalMessage(data)
	if err != nil {
		t.Fatalf("UnmarshalMessage: %v", err)
	}

	if got.Success {
		t.Error("Success = true, want false")
	}
	if got.ErrorMessage != "instrument not found" {
		t.Errorf("ErrorMessage = %q, want %q", got.ErrorMessage, "instrument not found")
	}
}

// ---------------------------------------------------------------------------
// Helpers for WebSocket integration tests
// ---------------------------------------------------------------------------

var wsUpgrader = websocket.Upgrader{
	CheckOrigin: func(r *http.Request) bool { return true },
}

// newTestServer creates an httptest.Server with a WebSocket handler driven by
// the provided serverFn. It returns the server and a ws:// URL.
func newTestServer(t *testing.T, serverFn func(ws *websocket.Conn)) *httptest.Server {
	t.Helper()
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := wsUpgrader.Upgrade(w, r, nil)
		if err != nil {
			t.Logf("upgrade error: %v", err)
			return
		}
		defer conn.Close()
		serverFn(conn)
	}))
	return ts
}

// wsURL converts an httptest.Server's http:// URL to a ws:// URL.
func wsURL(ts *httptest.Server) string {
	return "ws" + strings.TrimPrefix(ts.URL, "http")
}

// newTestClient creates a Client wired to the given server URL with
// a no-op local gRPC address (tests that exercise handleCommand need a real
// gRPC server; tests that don't can use any string).
func newTestClient(t *testing.T, serverURL string) *Client {
	t.Helper()
	return NewClient(
		"edge-uuid-test",
		"test-edge",
		"0.0.1",
		serverURL,
		"test-token",
		"127.0.0.1:19999", // unused in tests that don't exercise handleCommand
		newTestLogger(t),
	)
}

// newTestLogger returns a slog.Logger that writes to t.Log.
func newTestLogger(t *testing.T) *slog.Logger {
	t.Helper()
	return slog.New(slog.NewTextHandler(&testWriter{t: t}, &slog.HandlerOptions{Level: slog.LevelDebug}))
}

type testWriter struct{ t *testing.T }

func (w *testWriter) Write(p []byte) (int, error) {
	w.t.Logf("%s", p)
	return len(p), nil
}

// readMessages drains frames from the WebSocket into a channel until the
// connection closes or ctx is cancelled.
func readMessages(ctx context.Context, ws *websocket.Conn) <-chan relayMessage {
	ch := make(chan relayMessage, 32)
	go func() {
		defer close(ch)
		for {
			var msg relayMessage
			if err := ws.ReadJSON(&msg); err != nil {
				return
			}
			ch <- msg
		}
	}()
	return ch
}

// ---------------------------------------------------------------------------
// T1 — Happy-path connect: daemon sends hello; server responds with hello_ack;
// daemon starts sending heartbeats within heartbeatInterval+1s.
//
// Note: waitForHelloAck is a no-op unless compiled with -tags relay_hello_ack.
// This test validates the handshake and heartbeat regardless.
// ---------------------------------------------------------------------------

func TestT1_HappyPathConnect(t *testing.T) {
	helloCh := make(chan relayMessage, 1)
	heartbeatCh := make(chan relayMessage, 1)
	var hbOnce sync.Once

	ts := newTestServer(t, func(ws *websocket.Conn) {
		msgs := readMessages(context.Background(), ws)
		for msg := range msgs {
			switch msg.Type {
			case "hello":
				helloCh <- msg
				// Send hello_ack so the ack-enabled build doesn't time out.
				ack := relayMessage{Type: "hello_ack", SessionID: "sess-abc"}
				_ = ws.WriteJSON(ack)
			case "heartbeat":
				hbOnce.Do(func() { heartbeatCh <- msg })
			}
		}
	})
	defer ts.Close()

	ctx, cancel := context.WithTimeout(context.Background(), heartbeatInterval+2*time.Second)
	defer cancel()

	client := newTestClient(t, wsURL(ts))
	go client.Run(ctx)

	// Verify hello was received.
	select {
	case hello := <-helloCh:
		if hello.Type != "hello" {
			t.Fatalf("first frame type = %q, want hello", hello.Type)
		}
		if hello.EdgeID != "edge-uuid-test" {
			t.Errorf("EdgeID = %q, want edge-uuid-test", hello.EdgeID)
		}
	case <-ctx.Done():
		t.Fatal("timeout waiting for hello from daemon")
	}

	// Verify heartbeat arrives within heartbeatInterval + 1s.
	select {
	case hb := <-heartbeatCh:
		if hb.Type != "heartbeat" {
			t.Fatalf("heartbeat frame type = %q, want heartbeat", hb.Type)
		}
		if hb.TimestampMs == 0 {
			t.Error("heartbeat timestamp_ms is zero")
		}
	case <-ctx.Done():
		t.Fatal("timeout waiting for heartbeat from daemon")
	}
}

// ---------------------------------------------------------------------------
// T2 — Bad token: server closes with code 4401 immediately after upgrade;
// daemon should not reconnect; Run returns after one attempt.
// ---------------------------------------------------------------------------

func TestT2_BadToken_NoRetry(t *testing.T) {
	connectCount := int32(0)

	ts := newTestServer(t, func(ws *websocket.Conn) {
		atomic.AddInt32(&connectCount, 1)
		// Close immediately with the "bad token" code.
		_ = ws.WriteMessage(websocket.CloseMessage,
			websocket.FormatCloseMessage(4401, "bad token"))
		// Drain to allow the close handshake to complete.
		for {
			if _, _, err := ws.ReadMessage(); err != nil {
				return
			}
		}
	})
	defer ts.Close()

	// Give Run a generous deadline — it should exit well before this.
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	done := make(chan struct{})
	client := newTestClient(t, wsURL(ts))
	go func() {
		client.Run(ctx)
		close(done)
	}()

	select {
	case <-done:
		// Run exited — good.
	case <-ctx.Done():
		t.Fatal("Run did not exit after 4401 close code (still running after 5s)")
	}

	// Server should have been contacted exactly once.
	if n := atomic.LoadInt32(&connectCount); n != 1 {
		t.Errorf("server received %d connection(s), want exactly 1", n)
	}
}

// ---------------------------------------------------------------------------
// T3 — Mid-stream disconnect with reconnect: server closes unexpectedly after
// 1 heartbeat; daemon reconnects with backoff and re-sends hello with the same
// edge_id.
// ---------------------------------------------------------------------------

func TestT3_MidStreamDisconnectReconnect(t *testing.T) {
	connectCount := int32(0)
	edgeIDs := make(chan string, 10)

	ts := newTestServer(t, func(ws *websocket.Conn) {
		n := atomic.AddInt32(&connectCount, 1)
		msgs := readMessages(context.Background(), ws)

		for msg := range msgs {
			switch msg.Type {
			case "hello":
				edgeIDs <- msg.EdgeID
				// Send hello_ack so the optional feature is satisfied.
				_ = ws.WriteJSON(relayMessage{Type: "hello_ack", SessionID: fmt.Sprintf("sess-%d", n)})
				if n == 1 {
					// First connection: wait for one heartbeat then close unexpectedly.
				}
			case "heartbeat":
				if n == 1 {
					// Drop the connection abruptly after the first heartbeat.
					ws.Close()
					return
				}
			}
		}
	})
	defer ts.Close()

	// Allow enough time for: connect → heartbeat → disconnect → backoff (2s) → reconnect → hello.
	ctx, cancel := context.WithTimeout(context.Background(), heartbeatInterval+10*time.Second)
	defer cancel()

	client := newTestClient(t, wsURL(ts))
	go client.Run(ctx)

	// Collect two hello frames (one per connection attempt).
	var ids []string
	deadline := time.After(heartbeatInterval + 8*time.Second)
	for len(ids) < 2 {
		select {
		case id := <-edgeIDs:
			ids = append(ids, id)
		case <-deadline:
			t.Fatalf("only got %d hello(s), want 2", len(ids))
		}
	}
	cancel() // stop Run

	// Both hellos must carry the same edge_id.
	if ids[0] != ids[1] {
		t.Errorf("edge_id changed across reconnect: first=%q second=%q", ids[0], ids[1])
	}
	if n := atomic.LoadInt32(&connectCount); n < 2 {
		t.Errorf("server received %d connection(s), want >= 2", n)
	}
}

// ---------------------------------------------------------------------------
// T4 — Command received before hello_ack (stub path): server sends a
// command_request immediately; verify daemon does not panic. When the
// relay_hello_ack tag is off (default) the daemon accepts the frame. When the
// tag is on the daemon closes the session and the command is dropped.
// ---------------------------------------------------------------------------

func TestT4_CommandBeforeHelloAck_NoPanic(t *testing.T) {
	// This test verifies no panic occurs. We send a command_request before any
	// hello_ack and expect the daemon to handle it gracefully (either process
	// it if the gRPC server is absent — returning an error response — or drop
	// it). The exact outcome depends on the build tag; we just verify liveness.

	ts := newTestServer(t, func(ws *websocket.Conn) {
		msgs := readMessages(context.Background(), ws)

		for msg := range msgs {
			if msg.Type == "hello" {
				// Send a command_request before hello_ack.
				cmdReq := relayMessage{
					Type:         "command_request",
					RequestID:    "req-t4",
					InstrumentID: "GPIB0::1::INSTR",
					CommandName:  "test",
					IsQuery:      false,
				}
				_ = ws.WriteJSON(cmdReq)
				// Give the daemon a moment to process then close cleanly.
				time.Sleep(500 * time.Millisecond)
				_ = ws.WriteMessage(websocket.CloseMessage,
					websocket.FormatCloseMessage(websocket.CloseNormalClosure, ""))
				return
			}
		}
	})
	defer ts.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()

	client := newTestClient(t, wsURL(ts))

	// Run in a goroutine; if there's a panic the test will fail.
	done := make(chan struct{})
	go func() {
		defer func() {
			if r := recover(); r != nil {
				t.Errorf("panic in Run: %v", r)
			}
			close(done)
		}()
		client.Run(ctx)
	}()

	select {
	case <-done:
		// Exited cleanly — pass.
	case <-ctx.Done():
		// Timed out — also acceptable (daemon reconnecting after close).
	}
}

// ---------------------------------------------------------------------------
// T5 — resolveRelayURL: empty RELAY_URL and BACKEND_URL → returns ""; relay
// goroutine is never started.
// ---------------------------------------------------------------------------

func TestT5_RelayDisabled_EmptyURLs(t *testing.T) {
	// Test resolveRelayURL directly (it lives in internal/cli/start.go but the
	// logic is trivial and the relay package itself is what we test here).
	// We validate that an empty backendURL causes the Client to fail
	// immediately on dial without panicking.

	client := NewClient(
		"edge-uuid",
		"test-edge",
		"0.0.1",
		"", // empty backendURL
		"tok",
		"127.0.0.1:19999",
		newTestLogger(t),
	)

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	// Run with an empty URL should either return immediately or the ctx
	// should time out. Either way there must be no panic.
	done := make(chan struct{})
	go func() {
		defer func() {
			if r := recover(); r != nil {
				t.Errorf("panic with empty URL: %v", r)
			}
			close(done)
		}()
		client.Run(ctx)
	}()

	// Cancel quickly — we just want to verify no panic and no infinite block.
	cancel()
	select {
	case <-done:
		// Good.
	case <-time.After(3 * time.Second):
		t.Fatal("Run did not return after ctx cancel with empty URL")
	}
}

// ---------------------------------------------------------------------------
// T2b — Other unrecoverable close codes (4403, 4426, 1008)
// ---------------------------------------------------------------------------

func TestT2b_UnrecoverableCloseCodes(t *testing.T) {
	for _, code := range []int{4403, 4426, 1008} {
		code := code
		t.Run(fmt.Sprintf("close_%d", code), func(t *testing.T) {
			connectCount := int32(0)

			ts := newTestServer(t, func(ws *websocket.Conn) {
				atomic.AddInt32(&connectCount, 1)
				_ = ws.WriteMessage(websocket.CloseMessage,
					websocket.FormatCloseMessage(code, "auth failure"))
				for {
					if _, _, err := ws.ReadMessage(); err != nil {
						return
					}
				}
			})
			defer ts.Close()

			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()

			done := make(chan struct{})
			client := newTestClient(t, wsURL(ts))
			go func() {
				client.Run(ctx)
				close(done)
			}()

			select {
			case <-done:
				// Good — Run exited.
			case <-ctx.Done():
				t.Fatalf("Run did not exit for close code %d", code)
			}

			if n := atomic.LoadInt32(&connectCount); n != 1 {
				t.Errorf("server received %d connection(s) for code %d, want exactly 1", n, code)
			}
		})
	}
}

// ---------------------------------------------------------------------------
// Test: Auth header is sent in dial (F3)
// ---------------------------------------------------------------------------

func TestAuthorizationHeader(t *testing.T) {
	authHeaderCh := make(chan string, 1)

	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		authHeaderCh <- r.Header.Get("Authorization")
		conn, err := wsUpgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer conn.Close()
		// Send close to terminate the daemon's session.
		_ = conn.WriteMessage(websocket.CloseMessage,
			websocket.FormatCloseMessage(websocket.CloseNormalClosure, ""))
		for {
			if _, _, err := conn.ReadMessage(); err != nil {
				return
			}
		}
	}))
	defer ts.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()

	client := newTestClient(t, wsURL(ts))
	go client.Run(ctx)

	select {
	case auth := <-authHeaderCh:
		want := "Bearer test-token"
		if auth != want {
			t.Errorf("Authorization header = %q, want %q", auth, want)
		}
	case <-ctx.Done():
		t.Fatal("timeout waiting for dial request to server")
	}
}

// ---------------------------------------------------------------------------
// Test: Malformed frame is dropped, session continues (F2)
// ---------------------------------------------------------------------------

func TestMalformedFrameDropped(t *testing.T) {
	heartbeatAfterMalformed := make(chan struct{}, 1)
	var hbOnce sync.Once

	ts := newTestServer(t, func(ws *websocket.Conn) {
		msgs := readMessages(context.Background(), ws)
		for msg := range msgs {
			switch msg.Type {
			case "hello":
				// Send hello_ack then a malformed JSON frame.
				_ = ws.WriteJSON(relayMessage{Type: "hello_ack", SessionID: "s1"})
				// Write a raw non-JSON text frame.
				_ = ws.WriteMessage(websocket.TextMessage, []byte("{not valid json!!!"))
			case "heartbeat":
				hbOnce.Do(func() { close(heartbeatAfterMalformed) })
			}
		}
	})
	defer ts.Close()

	ctx, cancel := context.WithTimeout(context.Background(), heartbeatInterval+2*time.Second)
	defer cancel()

	client := newTestClient(t, wsURL(ts))
	go client.Run(ctx)

	select {
	case <-heartbeatAfterMalformed:
		// Heartbeat arrived after the malformed frame — session was not closed.
	case <-ctx.Done():
		t.Fatal("session closed after malformed frame (expected it to stay open)")
	}
}
