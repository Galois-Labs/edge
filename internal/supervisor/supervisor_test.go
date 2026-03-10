package supervisor

import (
	"context"
	"log/slog"
	"net"
	"os"
	"testing"
	"time"
)

// ---------------------------------------------------------------------------
// State.String()
// ---------------------------------------------------------------------------

func TestStateString(t *testing.T) {
	tests := []struct {
		state State
		want  string
	}{
		{StateStopped, "stopped"},
		{StateStarting, "starting"},
		{StateRunning, "running"},
		{StateStopping, "stopping"},
		{StateBackoff, "backoff"},
		{State(99), "unknown(99)"},
	}
	for _, tt := range tests {
		if got := tt.state.String(); got != tt.want {
			t.Errorf("State(%d).String() = %q, want %q", int(tt.state), got, tt.want)
		}
	}
}

// ---------------------------------------------------------------------------
// State transitions
// ---------------------------------------------------------------------------

func TestNew_InitialState(t *testing.T) {
	s := New(Config{BinaryPath: "/bin/echo"}, nil)
	if s.GetState() != StateStopped {
		t.Errorf("initial state: got %s, want stopped", s.GetState())
	}
	if s.IsHealthy() {
		t.Error("initial healthy should be false")
	}
	if s.Restarts() != 0 {
		t.Errorf("initial restarts: got %d, want 0", s.Restarts())
	}
	if s.PID() != 0 {
		t.Errorf("initial PID: got %d, want 0", s.PID())
	}
}

func TestStart_CannotStartTwice(t *testing.T) {
	// Start a process that listens on TCP so health checks pass.
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	defer ln.Close()

	s := New(Config{
		BinaryPath:     findSleepBinary(),
		Args:           []string{"30"},
		HealthAddr:     ln.Addr().String(),
		StartupTimeout: 5 * time.Second,
		HealthInterval: 100 * time.Millisecond,
	}, slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelError})))

	ctx := context.Background()
	if err := s.Start(ctx); err != nil {
		t.Fatalf("Start: %v", err)
	}
	defer s.Stop()

	if s.GetState() != StateRunning {
		t.Errorf("state after Start: got %s, want running", s.GetState())
	}

	// Second Start should fail.
	err = s.Start(ctx)
	if err == nil {
		t.Fatal("expected error on second Start")
	}
}

// ---------------------------------------------------------------------------
// Start + Stop lifecycle with a real subprocess
// ---------------------------------------------------------------------------

func TestStartStop_Lifecycle(t *testing.T) {
	// Create a TCP listener for the health check.
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	defer ln.Close()

	logger := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelError}))

	s := New(Config{
		BinaryPath:     findSleepBinary(),
		Args:           []string{"30"},
		HealthAddr:     ln.Addr().String(),
		StartupTimeout: 5 * time.Second,
		HealthInterval: 100 * time.Millisecond,
	}, logger)

	// Pre-start.
	if s.GetState() != StateStopped {
		t.Fatalf("pre-start state: %s", s.GetState())
	}

	// Start.
	ctx := context.Background()
	if err := s.Start(ctx); err != nil {
		t.Fatalf("Start: %v", err)
	}

	// Running checks.
	if s.GetState() != StateRunning {
		t.Errorf("state after Start: got %s, want running", s.GetState())
	}
	if !s.IsHealthy() {
		t.Error("should be healthy after Start")
	}
	if s.PID() == 0 {
		t.Error("PID should be non-zero after Start")
	}

	// Stop.
	if err := s.Stop(); err != nil {
		t.Fatalf("Stop: %v", err)
	}

	if s.GetState() != StateStopped {
		t.Errorf("state after Stop: got %s, want stopped", s.GetState())
	}
	if s.IsHealthy() {
		t.Error("should not be healthy after Stop")
	}
}

func TestStop_AlreadyStopped(t *testing.T) {
	s := New(Config{BinaryPath: "/bin/echo"}, nil)
	// Stopping a never-started supervisor should be harmless.
	if err := s.Stop(); err != nil {
		t.Fatalf("Stop on stopped supervisor: %v", err)
	}
}

// ---------------------------------------------------------------------------
// Health check with a real TCP listener
// ---------------------------------------------------------------------------

func TestProbeHealth_Success(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	defer ln.Close()

	s := New(Config{HealthAddr: ln.Addr().String()}, nil)
	if !s.probeHealth() {
		t.Error("probeHealth should succeed when listener is active")
	}
}

func TestProbeHealth_Failure(t *testing.T) {
	// Use a port that is almost certainly not listening.
	s := New(Config{HealthAddr: "127.0.0.1:1"}, nil)
	if s.probeHealth() {
		t.Error("probeHealth should fail when nothing is listening")
	}
}

// ---------------------------------------------------------------------------
// Start with failed health check (process exits immediately)
// ---------------------------------------------------------------------------

func TestStart_HealthCheckTimeout(t *testing.T) {
	// Use a binary that exits immediately (echo) and an address that won't
	// be listening, so health check times out.
	s := New(Config{
		BinaryPath:     "/bin/echo",
		Args:           []string{"hello"},
		HealthAddr:     "127.0.0.1:1", // nothing listening
		StartupTimeout: 500 * time.Millisecond,
		HealthInterval: 50 * time.Millisecond,
	}, slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelError})))

	err := s.Start(context.Background())
	if err == nil {
		t.Fatal("expected Start to fail when health check cannot pass")
	}

	if s.GetState() != StateStopped {
		t.Errorf("state after failed Start: got %s, want stopped", s.GetState())
	}
}

// ---------------------------------------------------------------------------
// Start with context cancellation
// ---------------------------------------------------------------------------

func TestStart_ContextCancelled(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	// Close the listener immediately so health check won't pass.
	ln.Close()

	ctx, cancel := context.WithCancel(context.Background())

	s := New(Config{
		BinaryPath:     findSleepBinary(),
		Args:           []string{"30"},
		HealthAddr:     ln.Addr().String(),
		StartupTimeout: 10 * time.Second,
		HealthInterval: 100 * time.Millisecond,
	}, slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelError})))

	// Cancel the context after a short delay so waitHealthy returns.
	go func() {
		time.Sleep(300 * time.Millisecond)
		cancel()
	}()

	err = s.Start(ctx)
	if err == nil {
		s.Stop()
		t.Fatal("expected Start to fail when context is cancelled")
	}

	if s.GetState() != StateStopped {
		t.Errorf("state after cancelled Start: got %s, want stopped", s.GetState())
	}
}

// ---------------------------------------------------------------------------
// Exponential backoff calculation
// ---------------------------------------------------------------------------

func TestBackoffDelay(t *testing.T) {
	tests := []struct {
		attempt int
		minMS   int64
		maxMS   int64
	}{
		{1, 2000, 2500},   // 2s * (1 + 0..0.25)
		{2, 4000, 5000},   // 4s
		{3, 8000, 10000},  // 8s
		{4, 16000, 20000}, // 16s
	}

	for _, tt := range tests {
		d := BackoffDelay(tt.attempt)
		ms := d.Milliseconds()
		if ms < tt.minMS || ms > tt.maxMS {
			t.Errorf("BackoffDelay(%d) = %dms, want [%d, %d]",
				tt.attempt, ms, tt.minMS, tt.maxMS)
		}
	}
}

func TestBackoffDelay_Capped(t *testing.T) {
	// At attempt 20, the raw exponent would far exceed backoffMax (5 min).
	d := BackoffDelay(20)
	maxWithJitter := time.Duration(float64(backoffMax) * 1.25)
	if d > maxWithJitter {
		t.Errorf("BackoffDelay(20) = %v, should be capped near %v", d, backoffMax)
	}
}

func TestBackoffDelay_ZeroOrNegative(t *testing.T) {
	// Attempt < 1 should be treated as attempt 1.
	d := BackoffDelay(0)
	if d < backoffInitial {
		t.Errorf("BackoffDelay(0) = %v, should be >= %v", d, backoffInitial)
	}
	d2 := BackoffDelay(-5)
	if d2 < backoffInitial {
		t.Errorf("BackoffDelay(-5) = %v, should be >= %v", d2, backoffInitial)
	}
}

// ---------------------------------------------------------------------------
// Config defaults
// ---------------------------------------------------------------------------

func TestConfig_StartupTimeoutDefault(t *testing.T) {
	c := &Config{}
	if c.startupTimeout() != defaultStartupTimeout {
		t.Errorf("startupTimeout: got %v, want %v", c.startupTimeout(), defaultStartupTimeout)
	}

	c.StartupTimeout = 10 * time.Second
	if c.startupTimeout() != 10*time.Second {
		t.Errorf("startupTimeout override: got %v, want 10s", c.startupTimeout())
	}
}

func TestConfig_HealthIntervalDefault(t *testing.T) {
	c := &Config{}
	if c.healthInterval() != defaultHealthInterval {
		t.Errorf("healthInterval: got %v, want %v", c.healthInterval(), defaultHealthInterval)
	}

	c.HealthInterval = 500 * time.Millisecond
	if c.healthInterval() != 500*time.Millisecond {
		t.Errorf("healthInterval override: got %v, want 500ms", c.healthInterval())
	}
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// findSleepBinary returns the path to a "sleep" binary.
func findSleepBinary() string {
	// macOS and Linux both have /bin/sleep.
	for _, path := range []string{"/bin/sleep", "/usr/bin/sleep"} {
		if _, err := os.Stat(path); err == nil {
			return path
		}
	}
	return "sleep" // rely on PATH
}
