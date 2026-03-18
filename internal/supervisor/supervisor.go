// Package supervisor manages the lifecycle of the Python instrument engine
// child process. It implements a state machine (Stopped -> Starting -> Running
// -> Stopping) with health checking via TCP probes, automatic restart with
// exponential backoff on unexpected exit, and a multi-stage graceful shutdown
// sequence using stdin pipe closure.
package supervisor

import (
	"bufio"
	"context"
	"fmt"
	"io"
	"log/slog"
	"math"
	"math/rand/v2"
	"net"
	"os/exec"
	"sync"
	"time"
)

// --------------------------------------------------------------------------
// State machine
// --------------------------------------------------------------------------

// State represents the current lifecycle state of the supervised process.
type State int

const (
	// StateStopped means the child process is not running.
	StateStopped State = iota

	// StateStarting means the child has been spawned and we are waiting
	// for it to become healthy.
	StateStarting

	// StateRunning means the child is running and the last health check
	// passed.
	StateRunning

	// StateStopping means a graceful shutdown is in progress.
	StateStopping

	// StateBackoff means the child crashed and we are waiting before
	// the next restart attempt.
	StateBackoff
)

// String returns a human-readable name for the state.
func (s State) String() string {
	switch s {
	case StateStopped:
		return "stopped"
	case StateStarting:
		return "starting"
	case StateRunning:
		return "running"
	case StateStopping:
		return "stopping"
	case StateBackoff:
		return "backoff"
	default:
		return fmt.Sprintf("unknown(%d)", int(s))
	}
}

// --------------------------------------------------------------------------
// Timing constants
// --------------------------------------------------------------------------

const (
	// defaultStartupTimeout is the maximum time to wait for the child to
	// become healthy after spawning.
	defaultStartupTimeout = 120 * time.Second

	// defaultHealthInterval is the time between TCP health probes during
	// startup.
	defaultHealthInterval = 2 * time.Second

	// defaultHealthDialTimeout is the TCP dial timeout for a single probe.
	defaultHealthDialTimeout = 2 * time.Second

	// backoffInitial is the starting delay for exponential backoff after
	// a crash.
	backoffInitial = 2 * time.Second

	// backoffMax is the ceiling for exponential backoff.
	backoffMax = 5 * time.Minute

	// backoffJitter is the fraction of random jitter added to backoff
	// delays (e.g. 0.25 = up to 25% extra).
	backoffJitter = 0.25

	// shutdownGrace is how long to wait after closing stdin before
	// escalating to SIGTERM / TerminateProcess.
	shutdownGrace = 10 * time.Second

	// shutdownTermWait is how long to wait after SIGTERM before
	// escalating to SIGKILL / force kill.
	shutdownTermWait = 5 * time.Second
)

// --------------------------------------------------------------------------
// Config
// --------------------------------------------------------------------------

// Config holds the parameters needed to launch and supervise a child process.
type Config struct {
	// BinaryPath is the path to the frozen Python engine binary.
	BinaryPath string

	// Args are command-line arguments passed to the child.
	Args []string

	// Env are additional environment variables (KEY=VALUE) for the child.
	// The child inherits these plus PYTHONUNBUFFERED=1 and PLANE0_MANAGED=1.
	Env []string

	// HealthAddr is the TCP address to probe for readiness (e.g. "127.0.0.1:50052").
	HealthAddr string

	// StartupTimeout overrides the default startup timeout. Zero uses the
	// default (30 seconds).
	StartupTimeout time.Duration

	// HealthInterval overrides the default interval between health probes.
	// Zero uses the default (2 seconds).
	HealthInterval time.Duration
}

func (c *Config) startupTimeout() time.Duration {
	if c.StartupTimeout > 0 {
		return c.StartupTimeout
	}
	return defaultStartupTimeout
}

func (c *Config) healthInterval() time.Duration {
	if c.HealthInterval > 0 {
		return c.HealthInterval
	}
	return defaultHealthInterval
}

// --------------------------------------------------------------------------
// Supervisor
// --------------------------------------------------------------------------

// Supervisor manages a single child process with health monitoring,
// automatic restart on crash, and graceful shutdown.
type Supervisor struct {
	cfg    Config
	logger *slog.Logger

	// mu protects all mutable state below.
	mu       sync.Mutex
	state    State
	restarts int
	cmd      *exec.Cmd
	stdin    io.WriteCloser

	// waitDone is closed when the current child's Wait() goroutine finishes.
	// waitErr holds the resulting error. These ensure Wait() is called
	// exactly once per spawned process, preventing data races between the
	// monitor loop and gracefulShutdown.
	waitDone chan struct{}
	waitErr  error

	// cancel stops the background monitor goroutine.
	cancel context.CancelFunc

	// done is closed when the monitor goroutine exits.
	done chan struct{}

	// healthy tracks the most recent health probe result.
	healthy bool
}

// New creates a Supervisor with the given configuration. The child process
// is not started until Start is called.
func New(cfg Config, logger *slog.Logger) *Supervisor {
	if logger == nil {
		logger = slog.Default()
	}
	return &Supervisor{
		cfg:    cfg,
		logger: logger.With("component", "supervisor"),
		state:  StateStopped,
	}
}

// --------------------------------------------------------------------------
// Public API
// --------------------------------------------------------------------------

// Start spawns the child process and blocks until it becomes healthy or the
// startup timeout elapses. On success it launches a background monitor
// goroutine that restarts the child on unexpected exit. The supplied context
// can be used to cancel the initial health-check wait.
func (s *Supervisor) Start(ctx context.Context) error {
	s.mu.Lock()
	if s.state != StateStopped {
		cur := s.state
		s.mu.Unlock()
		return fmt.Errorf("supervisor: cannot start in state %s", cur)
	}
	s.state = StateStarting
	s.mu.Unlock()

	// Spawn the child process.
	if err := s.spawn(); err != nil {
		s.mu.Lock()
		s.state = StateStopped
		s.mu.Unlock()
		return fmt.Errorf("supervisor: spawn: %w", err)
	}

	// Wait for the child to become healthy.
	if err := s.waitHealthy(ctx); err != nil {
		s.killProcess()
		s.mu.Lock()
		s.state = StateStopped
		s.mu.Unlock()
		return fmt.Errorf("supervisor: health check: %w", err)
	}

	s.mu.Lock()
	s.state = StateRunning
	s.healthy = true
	pid := s.processPID()
	s.mu.Unlock()

	s.logger.Info("child process started and healthy",
		"pid", pid,
		"binary", s.cfg.BinaryPath,
	)

	// Launch background monitor for crash detection / restart.
	monCtx, cancel := context.WithCancel(context.Background())
	s.mu.Lock()
	s.cancel = cancel
	s.done = make(chan struct{})
	s.mu.Unlock()

	go s.monitor(monCtx)

	return nil
}

// Stop performs a graceful shutdown of the child process. The sequence:
//  1. Close stdin pipe (Python detects EOF and shuts down).
//  2. Wait up to 10 seconds for the child to exit.
//  3. Send SIGTERM (Unix) or TerminateProcess (Windows).
//  4. Wait up to 5 more seconds.
//  5. Force kill (SIGKILL).
func (s *Supervisor) Stop() error {
	s.mu.Lock()
	if s.state == StateStopped || s.state == StateStopping {
		s.mu.Unlock()
		return nil
	}

	s.state = StateStopping
	cancel := s.cancel
	done := s.done
	s.mu.Unlock()

	// Stop the background monitor first so it doesn't try to restart.
	if cancel != nil {
		cancel()
	}
	if done != nil {
		<-done
	}

	// Run the multi-stage shutdown.
	err := s.gracefulShutdown()

	s.mu.Lock()
	s.state = StateStopped
	s.healthy = false
	s.mu.Unlock()

	return err
}

// GetState returns the current lifecycle state.
func (s *Supervisor) GetState() State {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.state
}

// IsHealthy returns true if the child is running and the last health check
// succeeded.
func (s *Supervisor) IsHealthy() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.healthy
}

// Restarts returns the cumulative number of times the child has been restarted
// due to unexpected exit. The counter resets to zero after a successful restart.
func (s *Supervisor) Restarts() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.restarts
}

// PID returns the OS process ID of the current child, or 0 if not running.
func (s *Supervisor) PID() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.processPID()
}

// processPID returns the PID with the mutex already held.
func (s *Supervisor) processPID() int {
	if s.cmd != nil && s.cmd.Process != nil {
		return s.cmd.Process.Pid
	}
	return 0
}

// --------------------------------------------------------------------------
// Process spawning
// --------------------------------------------------------------------------

// spawn creates and starts the child process, setting up the stdin pipe
// and stdout/stderr forwarding goroutines.
func (s *Supervisor) spawn() error {
	cmd := exec.Command(s.cfg.BinaryPath, s.cfg.Args...)

	// Build environment: configured vars + supervision markers.
	env := make([]string, 0, len(s.cfg.Env)+2)
	env = append(env, s.cfg.Env...)
	env = append(env, "PYTHONUNBUFFERED=1")
	env = append(env, "PLANE0_MANAGED=1")
	cmd.Env = env

	// Create the stdin pipe. The supervisor holds the write end; closing it
	// signals the Python process to shut down gracefully.
	stdinPipe, err := cmd.StdinPipe()
	if err != nil {
		return fmt.Errorf("create stdin pipe: %w", err)
	}

	// Create stdout and stderr pipes for log forwarding.
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return fmt.Errorf("create stdout pipe: %w", err)
	}
	stderr, err := cmd.StderrPipe()
	if err != nil {
		return fmt.Errorf("create stderr pipe: %w", err)
	}

	// Start the child process.
	if err := cmd.Start(); err != nil {
		return fmt.Errorf("start process: %w", err)
	}

	// Launch a single goroutine that calls cmd.Wait() exactly once.
	// All other code reads waitDone + waitErr to learn about process exit.
	waitDone := make(chan struct{})
	go func() {
		waitErr := cmd.Wait()
		s.mu.Lock()
		s.waitErr = waitErr
		s.mu.Unlock()
		close(waitDone)
	}()

	s.mu.Lock()
	s.cmd = cmd
	s.stdin = stdinPipe
	s.waitDone = waitDone
	s.waitErr = nil
	s.mu.Unlock()

	// Assign child to a Windows Job Object for guaranteed cleanup on parent
	// exit. This is a no-op on Unix platforms.
	if err := assignToJobObject(cmd.Process.Pid); err != nil {
		s.logger.Warn("failed to assign child to job object", "error", err)
	}

	// Forward child stdout and stderr to the logger.
	go s.forwardOutput(stdout, "stdout")
	go s.forwardOutput(stderr, "stderr")

	return nil
}

// forwardOutput reads lines from the child's output stream and logs each
// one with a [python] prefix.
func (s *Supervisor) forwardOutput(r io.Reader, stream string) {
	scanner := bufio.NewScanner(r)
	// Use a 256KB buffer to handle long Python log lines.
	buf := make([]byte, 0, 64*1024)
	scanner.Buffer(buf, 256*1024)

	for scanner.Scan() {
		s.logger.Info(scanner.Text(),
			"source", "python",
			"stream", stream,
		)
	}
	if err := scanner.Err(); err != nil {
		s.logger.Debug("output forwarding finished",
			"stream", stream,
			"error", err,
		)
	}
}

// --------------------------------------------------------------------------
// Health checking
// --------------------------------------------------------------------------

// waitHealthy performs a TCP dial loop against the configured health address.
// It returns nil as soon as a connection succeeds, or an error if the startup
// timeout elapses or the child exits prematurely.
func (s *Supervisor) waitHealthy(ctx context.Context) error {
	timeout := s.cfg.startupTimeout()
	interval := s.cfg.healthInterval()

	deadline := time.Now().Add(timeout)
	ticker := time.NewTicker(interval)
	defer ticker.Stop()

	for {
		// Try a TCP connection.
		if s.probeHealth() {
			return nil
		}

		// Check if the child exited before becoming healthy.
		s.mu.Lock()
		waitDone := s.waitDone
		s.mu.Unlock()

		select {
		case <-waitDone:
			s.mu.Lock()
			err := s.waitErr
			s.mu.Unlock()
			return fmt.Errorf("process exited before becoming healthy: %v", err)
		default:
		}

		// Check startup timeout.
		if time.Now().After(deadline) {
			return fmt.Errorf("startup timeout (%s) exceeded", timeout)
		}

		// Wait for the next tick or context cancellation.
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-ticker.C:
		}
	}
}

// probeHealth attempts a single TCP connection to the health address.
// Returns true if the connection succeeds (indicating the gRPC server is
// listening).
func (s *Supervisor) probeHealth() bool {
	conn, err := net.DialTimeout("tcp", s.cfg.HealthAddr, defaultHealthDialTimeout)
	if err != nil {
		return false
	}
	conn.Close()
	return true
}

// --------------------------------------------------------------------------
// Background monitor
// --------------------------------------------------------------------------

// monitor is the background goroutine that watches for child process exit.
// On unexpected exit it restarts the child with exponential backoff.
func (s *Supervisor) monitor(ctx context.Context) {
	defer func() {
		s.mu.Lock()
		done := s.done
		s.mu.Unlock()
		if done != nil {
			close(done)
		}
	}()

	for {
		s.mu.Lock()
		waitDone := s.waitDone
		s.mu.Unlock()

		select {
		case <-ctx.Done():
			// Supervisor is stopping; do not restart.
			return

		case <-waitDone:
			// Child has exited. Check if we are shutting down.
			s.mu.Lock()
			if s.state == StateStopping {
				s.mu.Unlock()
				return
			}

			waitErr := s.waitErr
			s.healthy = false
			s.restarts++
			attempts := s.restarts
			s.state = StateBackoff
			s.mu.Unlock()

			s.logger.Warn("child process exited unexpectedly",
				"error", waitErr,
				"restart_count", attempts,
			)

			// Compute backoff delay.
			delay := BackoffDelay(attempts)
			s.logger.Info("waiting before restart",
				"delay", delay,
				"attempt", attempts,
			)

			select {
			case <-ctx.Done():
				s.mu.Lock()
				s.state = StateStopped
				s.mu.Unlock()
				return
			case <-time.After(delay):
			}

			// Attempt restart.
			s.mu.Lock()
			s.state = StateStarting
			s.mu.Unlock()

			if err := s.spawn(); err != nil {
				s.logger.Error("failed to restart child", "error", err)
				// Loop back to try again after another backoff.
				continue
			}

			// Wait for the restarted child to become healthy.
			healthCtx, healthCancel := context.WithTimeout(ctx, s.cfg.startupTimeout())
			if err := s.waitHealthy(healthCtx); err != nil {
				healthCancel()
				s.logger.Error("restarted child failed health check", "error", err)
				s.killProcess()
				continue
			}
			healthCancel()

			s.mu.Lock()
			s.state = StateRunning
			s.healthy = true
			s.restarts = 0 // Reset on successful recovery.
			pid := s.processPID()
			s.mu.Unlock()

			s.logger.Info("child process restarted and healthy",
				"pid", pid,
			)
		}
	}
}

// --------------------------------------------------------------------------
// Graceful shutdown sequence
// --------------------------------------------------------------------------

// gracefulShutdown executes the multi-stage shutdown:
//  1. Close stdin pipe -> Python detects EOF -> graceful shutdown.
//  2. Wait shutdownGrace (10s) for process to exit.
//  3. Send SIGTERM (Unix) / TerminateProcess (Windows).
//  4. Wait shutdownTermWait (5s).
//  5. Force kill (SIGKILL).
func (s *Supervisor) gracefulShutdown() error {
	s.mu.Lock()
	cmd := s.cmd
	stdinPipe := s.stdin
	waitDone := s.waitDone
	s.mu.Unlock()

	if cmd == nil || cmd.Process == nil {
		return nil
	}

	// Step 1: Close stdin pipe to signal Python to shut down.
	if stdinPipe != nil {
		s.logger.Debug("closing child stdin pipe")
		stdinPipe.Close()
	}

	// Step 2: Wait for process to exit within the grace period.
	select {
	case <-waitDone:
		s.logger.Debug("child exited after stdin close")
		return nil
	case <-time.After(shutdownGrace):
		s.logger.Info("child did not exit within grace period, sending terminate signal")
	}

	// Step 3: Send SIGTERM / TerminateProcess.
	if err := terminateProcess(cmd.Process); err != nil {
		s.logger.Warn("terminate signal failed", "error", err)
	}

	// Step 4: Wait for process to respond to SIGTERM.
	select {
	case <-waitDone:
		s.logger.Debug("child exited after terminate signal")
		return nil
	case <-time.After(shutdownTermWait):
		s.logger.Warn("child did not exit after terminate, force killing")
	}

	// Step 5: Force kill.
	if err := cmd.Process.Kill(); err != nil {
		return fmt.Errorf("force kill: %w", err)
	}

	// Wait for the process to be reaped.
	<-waitDone
	return nil
}

// killProcess immediately kills the child process. Used when startup health
// checks fail and we need to clean up.
func (s *Supervisor) killProcess() {
	s.mu.Lock()
	stdinPipe := s.stdin
	cmd := s.cmd
	waitDone := s.waitDone
	s.mu.Unlock()

	// Close stdin first to let the child attempt a quick exit.
	if stdinPipe != nil {
		stdinPipe.Close()
	}

	if cmd != nil && cmd.Process != nil {
		_ = cmd.Process.Kill()
		// Wait for the Wait() goroutine to finish so we don't leak.
		if waitDone != nil {
			<-waitDone
		}
	}
}

// --------------------------------------------------------------------------
// Exponential backoff
// --------------------------------------------------------------------------

// BackoffDelay calculates the backoff duration for the given attempt number.
// Formula: min(initial * 2^(attempts-1), max) * (1 + rand(0, jitter))
//
// Examples (before jitter):
//
//	attempt 1 -> 2s
//	attempt 2 -> 4s
//	attempt 3 -> 8s
//	attempt 4 -> 16s
//	...
//	attempt 8+ -> 5min (capped)
func BackoffDelay(attempts int) time.Duration {
	if attempts < 1 {
		attempts = 1
	}
	base := float64(backoffInitial) * math.Pow(2, float64(attempts-1))
	capped := math.Min(base, float64(backoffMax))
	jittered := capped * (1.0 + rand.Float64()*backoffJitter)
	return time.Duration(jittered)
}
