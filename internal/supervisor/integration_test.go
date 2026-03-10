//go:build integration

package supervisor

import (
	"context"
	"fmt"
	"log/slog"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"testing"
	"time"

	edgev1 "github.com/galois-labs/edge/proto/gen/go/edge/v1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// pythonBinary returns the Python 3 interpreter path, or skips the test.
func pythonBinary(t *testing.T) string {
	t.Helper()

	// Prefer python3 on Unix, fall back to python (Windows / venvs).
	candidates := []string{"python3", "python"}
	if runtime.GOOS == "windows" {
		candidates = []string{"python", "python3"}
	}

	for _, name := range candidates {
		path, err := exec.LookPath(name)
		if err == nil {
			return path
		}
	}
	t.Skip("python3 not found in PATH; skipping integration test")
	return ""
}

// srcDir returns the absolute path to the daemon-clean/src directory.
func srcDir(t *testing.T) string {
	t.Helper()

	// This file lives at internal/supervisor/integration_test.go.
	// Walk up two directories to reach the repo root, then into src/.
	_, thisFile, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("runtime.Caller failed")
	}
	repoRoot := filepath.Dir(filepath.Dir(filepath.Dir(thisFile)))
	dir := filepath.Join(repoRoot, "src")

	if _, err := os.Stat(filepath.Join(dir, "galois_edge", "__main__.py")); err != nil {
		t.Skipf("galois_edge package not found at %s; skipping", dir)
	}
	return dir
}

// checkPythonDeps verifies that the galois_edge package can be imported.
// Skips the test if required Python dependencies are missing.
func checkPythonDeps(t *testing.T, python, pythonPath string) {
	t.Helper()

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	cmd := exec.CommandContext(ctx, python, "-c", "import galois_edge")
	cmd.Env = append(os.Environ(), "PYTHONPATH="+pythonPath)

	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Skipf("Python deps not installed (import galois_edge failed): %v\n%s", err, out)
	}
}

// freePort picks an ephemeral port by briefly listening on :0 and then
// closing the listener. The OS will recycle the port shortly; the Python
// engine should bind it before the OS reuses it for something else.
func freePort(t *testing.T) int {
	t.Helper()

	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("failed to find free port: %v", err)
	}
	port := ln.Addr().(*net.TCPAddr).Port
	ln.Close()
	return port
}

// ---------------------------------------------------------------------------
// Integration test: full supervisor lifecycle with real Python engine
// ---------------------------------------------------------------------------

func TestIntegration_SupervisorLifecycle(t *testing.T) {
	// Overall test timeout to prevent hanging.
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()

	python := pythonBinary(t)
	src := srcDir(t)
	checkPythonDeps(t, python, src)

	grpcPort := freePort(t)
	wsPort := freePort(t)
	healthAddr := fmt.Sprintf("127.0.0.1:%d", grpcPort)

	logger := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{
		Level: slog.LevelDebug,
	}))

	sup := New(Config{
		BinaryPath: python,
		Args:       []string{"-m", "galois_edge"},
		Env: append(os.Environ(),
			"PYTHONPATH="+src,
			fmt.Sprintf("GRPC_PORT=%d", grpcPort),
			fmt.Sprintf("WS_PORT=%d", wsPort),
			"LOG_LEVEL=DEBUG",
			"GPIB_ENABLED=false",
			"SCAN_INTERVAL_S=0",
		),
		HealthAddr:     healthAddr,
		StartupTimeout: 30 * time.Second,
		HealthInterval: 500 * time.Millisecond,
	}, logger)

	// -----------------------------------------------------------------
	// Phase 1: Start — spawn Python engine, wait for gRPC port to open
	// -----------------------------------------------------------------

	t.Log("Starting supervisor (spawning Python engine)...")
	if err := sup.Start(ctx); err != nil {
		t.Fatalf("supervisor.Start failed: %v", err)
	}

	if sup.GetState() != StateRunning {
		t.Fatalf("expected state Running, got %s", sup.GetState())
	}
	if !sup.IsHealthy() {
		t.Fatal("expected healthy=true after Start")
	}
	pid := sup.PID()
	if pid == 0 {
		t.Fatal("expected non-zero PID after Start")
	}
	t.Logf("Python engine running (PID %d, gRPC on %s)", pid, healthAddr)

	// -----------------------------------------------------------------
	// Phase 2: gRPC call — ListInstruments to verify the engine responds
	// -----------------------------------------------------------------

	t.Log("Connecting gRPC client...")
	conn, err := grpc.NewClient(
		healthAddr,
		grpc.WithTransportCredentials(insecure.NewCredentials()),
	)
	if err != nil {
		t.Fatalf("grpc.NewClient: %v", err)
	}
	defer conn.Close()

	client := edgev1.NewEdgeDaemonServiceClient(conn)

	rpcCtx, rpcCancel := context.WithTimeout(ctx, 10*time.Second)
	defer rpcCancel()

	resp, err := client.ListInstruments(rpcCtx, &edgev1.ListInstrumentsRequest{})
	if err != nil {
		t.Fatalf("ListInstruments RPC failed: %v", err)
	}

	t.Logf("ListInstruments returned: edge_id=%q, instruments=%d",
		resp.GetEdgeId(), len(resp.GetInstruments()))

	// We don't expect any real instruments in CI, but the call must succeed.
	if resp.GetEdgeId() == "" {
		t.Error("expected non-empty edge_id in ListInstruments response")
	}

	// -----------------------------------------------------------------
	// Phase 3: Stop — close stdin, verify clean shutdown
	// -----------------------------------------------------------------

	t.Log("Stopping supervisor (closing stdin pipe)...")
	if err := sup.Stop(); err != nil {
		t.Fatalf("supervisor.Stop failed: %v", err)
	}

	if sup.GetState() != StateStopped {
		t.Errorf("expected state Stopped, got %s", sup.GetState())
	}
	if sup.IsHealthy() {
		t.Error("expected healthy=false after Stop")
	}

	t.Log("Integration test passed: full supervisor lifecycle verified.")
}

// ---------------------------------------------------------------------------
// Integration test: start + stop without gRPC call (minimal lifecycle)
// ---------------------------------------------------------------------------

func TestIntegration_SupervisorStartStop(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()

	python := pythonBinary(t)
	src := srcDir(t)
	checkPythonDeps(t, python, src)

	grpcPort := freePort(t)
	wsPort := freePort(t)
	healthAddr := fmt.Sprintf("127.0.0.1:%d", grpcPort)

	logger := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{
		Level: slog.LevelDebug,
	}))

	sup := New(Config{
		BinaryPath: python,
		Args:       []string{"-m", "galois_edge"},
		Env: append(os.Environ(),
			"PYTHONPATH="+src,
			fmt.Sprintf("GRPC_PORT=%d", grpcPort),
			fmt.Sprintf("WS_PORT=%d", wsPort),
			"LOG_LEVEL=DEBUG",
			"GPIB_ENABLED=false",
			"SCAN_INTERVAL_S=0",
		),
		HealthAddr:     healthAddr,
		StartupTimeout: 30 * time.Second,
		HealthInterval: 500 * time.Millisecond,
	}, logger)

	t.Log("Starting supervisor...")
	if err := sup.Start(ctx); err != nil {
		t.Fatalf("supervisor.Start failed: %v", err)
	}

	// Verify the process is actually alive.
	if sup.PID() == 0 {
		t.Fatal("PID should be non-zero after Start")
	}

	t.Log("Stopping supervisor...")
	if err := sup.Stop(); err != nil {
		t.Fatalf("supervisor.Stop failed: %v", err)
	}

	if sup.GetState() != StateStopped {
		t.Errorf("expected Stopped, got %s", sup.GetState())
	}

	t.Log("Minimal lifecycle test passed.")
}
