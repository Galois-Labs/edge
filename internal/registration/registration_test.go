package registration

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"
)

// ---------------------------------------------------------------------------
// Mock InstrumentGetter
// ---------------------------------------------------------------------------

type mockGetter struct {
	instruments []InstrumentInfo
	err         error
}

func (m *mockGetter) GetInstruments(_ context.Context) ([]InstrumentInfo, error) {
	return m.instruments, m.err
}

// ---------------------------------------------------------------------------
// State.String()
// ---------------------------------------------------------------------------

func TestStateString(t *testing.T) {
	tests := []struct {
		s    State
		want string
	}{
		{StateDisconnected, "Disconnected"},
		{StateRegistering, "Registering"},
		{StateConnected, "Connected"},
		{StateBackoff, "Backoff"},
		{State(99), "State(99)"},
	}
	for _, tt := range tests {
		if got := tt.s.String(); got != tt.want {
			t.Errorf("State(%d).String() = %q, want %q", int(tt.s), got, tt.want)
		}
	}
}

// ---------------------------------------------------------------------------
// NewManager — defaults
// ---------------------------------------------------------------------------

func TestNewManager_Defaults(t *testing.T) {
	m := NewManager(Config{}, &mockGetter{})

	if m.State() != StateDisconnected {
		t.Errorf("initial state: got %s, want Disconnected", m.State())
	}
	if m.EdgeID() != "" {
		t.Errorf("initial EdgeID: got %q, want empty", m.EdgeID())
	}
	if m.cfg.GRPCPort != 50051 {
		t.Errorf("default GRPCPort: got %d, want 50051", m.cfg.GRPCPort)
	}
	if m.cfg.HeartbeatInterval != 30*time.Second {
		t.Errorf("default HeartbeatInterval: got %v, want 30s", m.cfg.HeartbeatInterval)
	}
	if m.cfg.InitialBackoff != 2*time.Second {
		t.Errorf("default InitialBackoff: got %v, want 2s", m.cfg.InitialBackoff)
	}
	if m.cfg.MaxBackoff != 300*time.Second {
		t.Errorf("default MaxBackoff: got %v, want 300s", m.cfg.MaxBackoff)
	}
	if m.cfg.FailureThreshold != 3 {
		t.Errorf("default FailureThreshold: got %d, want 3", m.cfg.FailureThreshold)
	}
}

// ---------------------------------------------------------------------------
// State transitions: Disconnected -> Registering -> Connected
// ---------------------------------------------------------------------------

func TestRegister_Success(t *testing.T) {
	var gotPayload registerPayload
	var gotToken string

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotToken = r.Header.Get("X-API-Key")

		if err := json.NewDecoder(r.Body).Decode(&gotPayload); err != nil {
			http.Error(w, "bad request", http.StatusBadRequest)
			return
		}

		resp := registerResponse{ID: "edge-123"}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		json.NewEncoder(w).Encode(resp)
	}))
	defer srv.Close()

	getter := &mockGetter{
		instruments: []InstrumentInfo{
			{ID: "inst-1", VisaAddress: "TCPIP::1.1.1.1", Name: "DMM"},
		},
	}

	m := NewManager(Config{
		BackendURL: srv.URL,
		EdgeName:   "test-edge",
		Token:      "reg-token-abc",
		Version:    "1.0.0",
	}, getter)

	// Call register directly.
	ctx := context.Background()
	if err := m.register(ctx); err != nil {
		t.Fatalf("register: %v", err)
	}

	if m.EdgeID() != "edge-123" {
		t.Errorf("EdgeID: got %q, want %q", m.EdgeID(), "edge-123")
	}
	if gotToken != "reg-token-abc" {
		t.Errorf("X-API-Key header: got %q, want %q", gotToken, "reg-token-abc")
	}
	if gotPayload.Name != "test-edge" {
		t.Errorf("payload name: got %q, want %q", gotPayload.Name, "test-edge")
	}
	if len(gotPayload.Instruments) != 1 {
		t.Errorf("payload instruments: got %d, want 1", len(gotPayload.Instruments))
	}
}

func TestRegister_ServerError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "internal error", http.StatusInternalServerError)
	}))
	defer srv.Close()

	m := NewManager(Config{BackendURL: srv.URL}, &mockGetter{})

	if err := m.register(context.Background()); err == nil {
		t.Fatal("expected error on 500 response")
	}
}

// ---------------------------------------------------------------------------
// Heartbeat
// ---------------------------------------------------------------------------

func TestHeartbeat_Success(t *testing.T) {
	var gotToken string

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotToken = r.Header.Get("X-API-Key")
		if r.URL.Path == "/api/v1/edges/edge-42/heartbeat" && r.Method == http.MethodPost {
			w.WriteHeader(http.StatusOK)
			return
		}
		http.Error(w, "not found", http.StatusNotFound)
	}))
	defer srv.Close()

	m := NewManager(Config{BackendURL: srv.URL, Token: "hb-token-xyz"}, &mockGetter{})
	m.mu.Lock()
	m.edgeID = "edge-42"
	m.state = StateConnected
	m.mu.Unlock()

	if err := m.heartbeat(context.Background()); err != nil {
		t.Fatalf("heartbeat: %v", err)
	}
	if gotToken != "hb-token-xyz" {
		t.Errorf("heartbeat X-API-Key: got %q, want %q", gotToken, "hb-token-xyz")
	}
}

func TestHeartbeat_404_TriggersReregistration(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "not found", http.StatusNotFound)
	}))
	defer srv.Close()

	m := NewManager(Config{BackendURL: srv.URL}, &mockGetter{})
	m.mu.Lock()
	m.edgeID = "edge-gone"
	m.state = StateConnected
	m.mu.Unlock()

	err := m.heartbeat(context.Background())
	if err == nil {
		t.Fatal("expected error on 404")
	}

	if m.State() != StateDisconnected {
		t.Errorf("state after 404: got %s, want Disconnected", m.State())
	}
	if m.EdgeID() != "" {
		t.Errorf("EdgeID should be cleared after 404: got %q", m.EdgeID())
	}
}

func TestHeartbeat_NoEdgeID(t *testing.T) {
	m := NewManager(Config{BackendURL: "http://unused"}, &mockGetter{})
	m.mu.Lock()
	m.state = StateConnected
	m.mu.Unlock()
	// edgeID is empty.

	err := m.heartbeat(context.Background())
	if err == nil {
		t.Fatal("expected error when edgeID is empty")
	}
	if m.State() != StateDisconnected {
		t.Errorf("state: got %s, want Disconnected", m.State())
	}
}

// ---------------------------------------------------------------------------
// handleHeartbeatFailure — threshold-based backoff
// ---------------------------------------------------------------------------

func TestHandleHeartbeatFailure_Threshold(t *testing.T) {
	m := NewManager(Config{
		BackendURL:       "http://unused",
		FailureThreshold: 3,
	}, &mockGetter{})
	m.mu.Lock()
	m.state = StateConnected
	m.mu.Unlock()

	dummyErr := http.ErrServerClosed

	// First two failures should stay Connected.
	m.handleHeartbeatFailure(dummyErr)
	if m.State() != StateConnected {
		t.Errorf("after 1 failure: got %s, want Connected", m.State())
	}
	m.handleHeartbeatFailure(dummyErr)
	if m.State() != StateConnected {
		t.Errorf("after 2 failures: got %s, want Connected", m.State())
	}

	// Third failure should trigger backoff.
	m.handleHeartbeatFailure(dummyErr)
	if m.State() != StateBackoff {
		t.Errorf("after 3 failures: got %s, want Backoff", m.State())
	}
}

// ---------------------------------------------------------------------------
// Exponential backoff with jitter
// ---------------------------------------------------------------------------

func TestCalcBackoff(t *testing.T) {
	m := NewManager(Config{
		BackendURL:     "http://unused",
		InitialBackoff: 1 * time.Second,
		MaxBackoff:     60 * time.Second,
	}, &mockGetter{})

	// attempt 0 -> base = min(1*2^0, 60) = 1s; jitter up to 0.25s
	m.mu.Lock()
	m.attempts = 0
	m.mu.Unlock()
	d := m.calcBackoff()
	if d < 1*time.Second || d > 1250*time.Millisecond {
		t.Errorf("attempt 0 backoff: got %v, want [1s, 1.25s]", d)
	}

	// attempt 3 -> base = min(1*2^3, 60) = 8s; jitter up to 2s
	m.mu.Lock()
	m.attempts = 3
	m.mu.Unlock()
	d = m.calcBackoff()
	if d < 8*time.Second || d > 10*time.Second {
		t.Errorf("attempt 3 backoff: got %v, want [8s, 10s]", d)
	}

	// attempt 20 -> should be capped at 60s.
	m.mu.Lock()
	m.attempts = 20
	m.mu.Unlock()
	d = m.calcBackoff()
	maxWithJitter := time.Duration(60.0*1.25*float64(time.Second))
	if d > maxWithJitter {
		t.Errorf("attempt 20 backoff: got %v, should be capped near 60s", d)
	}
}

// ---------------------------------------------------------------------------
// Full Start/Stop loop with httptest
// ---------------------------------------------------------------------------

func TestStartStop_FullLoop(t *testing.T) {
	var registerCalls atomic.Int32
	var heartbeatCalls atomic.Int32

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/api/v1/edges/register":
			registerCalls.Add(1)
			resp := registerResponse{ID: "edge-loop-1"}
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusCreated)
			json.NewEncoder(w).Encode(resp)

		case r.Method == http.MethodPost && r.URL.Path == "/api/v1/edges/edge-loop-1/heartbeat":
			heartbeatCalls.Add(1)
			w.WriteHeader(http.StatusOK)

		case r.Method == http.MethodDelete && r.URL.Path == "/api/v1/edges/edge-loop-1":
			w.WriteHeader(http.StatusOK)

		default:
			http.Error(w, "unexpected", http.StatusBadRequest)
		}
	}))
	defer srv.Close()

	getter := &mockGetter{instruments: []InstrumentInfo{}}

	m := NewManager(Config{
		BackendURL:        srv.URL,
		EdgeName:          "loop-test",
		HeartbeatInterval: 100 * time.Millisecond,
	}, getter)

	ctx := context.Background()
	m.Start(ctx)

	// Wait for registration + at least one heartbeat.
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if registerCalls.Load() >= 1 && heartbeatCalls.Load() >= 1 {
			break
		}
		time.Sleep(50 * time.Millisecond)
	}

	m.Stop()

	if registerCalls.Load() < 1 {
		t.Error("expected at least 1 register call")
	}
	if heartbeatCalls.Load() < 1 {
		t.Error("expected at least 1 heartbeat call")
	}
	if m.EdgeID() != "edge-loop-1" {
		t.Errorf("EdgeID: got %q, want %q", m.EdgeID(), "edge-loop-1")
	}
}

// ---------------------------------------------------------------------------
// Register failure -> backoff -> retry loop
// ---------------------------------------------------------------------------

func TestRegisterRetry_AfterFailure(t *testing.T) {
	var calls atomic.Int32

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		n := calls.Add(1)
		if n <= 2 {
			http.Error(w, "server error", http.StatusInternalServerError)
			return
		}
		// Third call succeeds.
		resp := registerResponse{ID: "edge-retry"}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		json.NewEncoder(w).Encode(resp)
	}))
	defer srv.Close()

	m := NewManager(Config{
		BackendURL:        srv.URL,
		EdgeName:          "retry-test",
		HeartbeatInterval: 100 * time.Millisecond,
		InitialBackoff:    50 * time.Millisecond, // fast backoff for test
		MaxBackoff:        200 * time.Millisecond,
	}, &mockGetter{})

	ctx := context.Background()
	m.Start(ctx)

	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if m.State() == StateConnected {
			break
		}
		time.Sleep(50 * time.Millisecond)
	}

	m.Stop()

	if m.State() != StateDisconnected {
		// After stop, the loop exits. State may vary, but we check the edge
		// was eventually connected.
	}

	if calls.Load() < 3 {
		t.Errorf("expected at least 3 register calls (2 failures + 1 success), got %d", calls.Load())
	}
	if m.EdgeID() != "edge-retry" {
		t.Errorf("EdgeID: got %q, want %q", m.EdgeID(), "edge-retry")
	}
}

// ---------------------------------------------------------------------------
// RegisterOnce — success with pre_auth_key + headscale_url
// ---------------------------------------------------------------------------

func TestRegisterOnce_Success(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		resp := map[string]string{
			"id":            "edge-once-1",
			"pre_auth_key":  "tskey-auth-abc123",
			"headscale_url": "https://headscale.example.com",
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		json.NewEncoder(w).Encode(resp)
	}))
	defer srv.Close()

	getter := &mockGetter{
		instruments: []InstrumentInfo{
			{ID: "inst-1", VisaAddress: "TCPIP::1.1.1.1", Name: "DMM"},
		},
	}

	m := NewManager(Config{
		BackendURL: srv.URL,
		EdgeName:   "test-once",
		Token:      "once-token",
		Version:    "1.0.0",
	}, getter)

	result, err := m.RegisterOnce(context.Background())
	if err != nil {
		t.Fatalf("RegisterOnce: %v", err)
	}

	if result.EdgeID != "edge-once-1" {
		t.Errorf("EdgeID: got %q, want %q", result.EdgeID, "edge-once-1")
	}
	if result.PreAuthKey != "tskey-auth-abc123" {
		t.Errorf("PreAuthKey: got %q, want %q", result.PreAuthKey, "tskey-auth-abc123")
	}
	if result.HeadscaleURL != "https://headscale.example.com" {
		t.Errorf("HeadscaleURL: got %q, want %q", result.HeadscaleURL, "https://headscale.example.com")
	}
}

// ---------------------------------------------------------------------------
// RegisterOnce — sets EdgeID on Manager
// ---------------------------------------------------------------------------

func TestRegisterOnce_SetsEdgeID(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		resp := map[string]string{"id": "edge-set-id"}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		json.NewEncoder(w).Encode(resp)
	}))
	defer srv.Close()

	m := NewManager(Config{
		BackendURL: srv.URL,
		EdgeName:   "set-id-test",
	}, &mockGetter{})

	if m.EdgeID() != "" {
		t.Fatalf("EdgeID should be empty before RegisterOnce, got %q", m.EdgeID())
	}

	_, err := m.RegisterOnce(context.Background())
	if err != nil {
		t.Fatalf("RegisterOnce: %v", err)
	}

	if m.EdgeID() != "edge-set-id" {
		t.Errorf("EdgeID after RegisterOnce: got %q, want %q", m.EdgeID(), "edge-set-id")
	}
}

// ---------------------------------------------------------------------------
// Loop skips registration after RegisterOnce
// ---------------------------------------------------------------------------

func TestLoopSkipsRegistrationAfterRegisterOnce(t *testing.T) {
	var registerCalls atomic.Int32
	var heartbeatCalls atomic.Int32

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/api/v1/edges/register":
			registerCalls.Add(1)
			resp := registerResponse{ID: "edge-skip-1"}
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusCreated)
			json.NewEncoder(w).Encode(resp)

		case r.Method == http.MethodPost && r.URL.Path == "/api/v1/edges/edge-skip-1/heartbeat":
			heartbeatCalls.Add(1)
			w.WriteHeader(http.StatusOK)

		case r.Method == http.MethodDelete && r.URL.Path == "/api/v1/edges/edge-skip-1":
			w.WriteHeader(http.StatusOK)

		default:
			http.Error(w, "unexpected", http.StatusBadRequest)
		}
	}))
	defer srv.Close()

	m := NewManager(Config{
		BackendURL:        srv.URL,
		EdgeName:          "skip-test",
		HeartbeatInterval: 50 * time.Millisecond,
	}, &mockGetter{})

	// RegisterOnce should make exactly 1 register call.
	_, err := m.RegisterOnce(context.Background())
	if err != nil {
		t.Fatalf("RegisterOnce: %v", err)
	}

	if registerCalls.Load() != 1 {
		t.Fatalf("expected exactly 1 register call from RegisterOnce, got %d", registerCalls.Load())
	}

	// Start the heartbeat loop.
	ctx := context.Background()
	m.Start(ctx)

	// Wait for at least one heartbeat.
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if heartbeatCalls.Load() >= 1 {
			break
		}
		time.Sleep(25 * time.Millisecond)
	}

	m.Stop()

	// The loop should NOT have made any additional register calls — it
	// should have gone straight to heartbeats.
	if registerCalls.Load() != 1 {
		t.Errorf("expected exactly 1 register call total (from RegisterOnce), got %d", registerCalls.Load())
	}
	if heartbeatCalls.Load() < 1 {
		t.Error("expected at least 1 heartbeat call after Start")
	}
}

// ---------------------------------------------------------------------------
// SetIPFunc
// ---------------------------------------------------------------------------

func TestSetIPFunc(t *testing.T) {
	m := NewManager(Config{}, &mockGetter{})

	// Default IPFunc should return empty string.
	if got := m.cfg.IPFunc(); got != "" {
		t.Errorf("default IPFunc: got %q, want empty", got)
	}

	m.SetIPFunc(func() string { return "100.64.0.1" })

	m.mu.Lock()
	got := m.cfg.IPFunc()
	m.mu.Unlock()
	if got != "100.64.0.1" {
		t.Errorf("after SetIPFunc: got %q, want %q", got, "100.64.0.1")
	}
}

// ---------------------------------------------------------------------------
// InstrumentInfo JSON round-trip
// ---------------------------------------------------------------------------

func TestInstrumentInfo_JSON(t *testing.T) {
	info := InstrumentInfo{
		ID:           "inst-1",
		VisaAddress:  "GPIB0::1::INSTR",
		Name:         "DMM",
		Manufacturer: "Keysight",
		Model:        "34465A",
		Status:       "connected",
	}

	data, err := json.Marshal(info)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}

	var decoded InstrumentInfo
	if err := json.Unmarshal(data, &decoded); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}

	if decoded != info {
		t.Errorf("round-trip mismatch: got %+v, want %+v", decoded, info)
	}
}
