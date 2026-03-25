//go:build windows

package tray

import (
	"context"
	"fmt"
	"net"
	"sync/atomic"
	"time"

	"github.com/galois-labs/edge/internal/grpcclient"
	edgepb "github.com/galois-labs/edge/proto/gen/go/edge/v1"
)

// ServiceState represents the daemon health.
type ServiceState int

const (
	StateUnknown  ServiceState = iota
	StateOnline                // gRPC reachable, healthy
	StateDegraded              // gRPC reachable but issues
	StateOffline               // Unreachable
)

func (s ServiceState) String() string {
	switch s {
	case StateOnline:
		return "ONLINE"
	case StateDegraded:
		return "DEGRADED"
	case StateOffline:
		return "OFFLINE"
	default:
		return "UNKNOWN"
	}
}

// StatusSnapshot is produced by the poller each tick.
type StatusSnapshot struct {
	State           ServiceState
	InstrumentCount int32
	Instruments     []*edgepb.Instrument
	Version         string
	Hostname        string
	UptimeSeconds   int64
	Error           error
}

// Poller polls the daemon gRPC endpoint at a fixed interval.
type Poller struct {
	target   string
	interval time.Duration
	latest   atomic.Value // stores StatusSnapshot
	updateCh chan struct{}
	cancel   context.CancelFunc
}

// NewPoller creates a poller targeting the given gRPC address (e.g. "127.0.0.1:50052").
func NewPoller(target string, interval time.Duration) *Poller {
	p := &Poller{
		target:   target,
		interval: interval,
		updateCh: make(chan struct{}, 1),
	}
	p.latest.Store(StatusSnapshot{State: StateUnknown})
	return p
}

// Start launches the polling goroutine. It stops when ctx is cancelled.
func (p *Poller) Start(ctx context.Context) {
	ctx, p.cancel = context.WithCancel(ctx)
	go p.run(ctx)
}

// Stop cancels the polling goroutine.
func (p *Poller) Stop() {
	if p.cancel != nil {
		p.cancel()
	}
}

// Latest returns the most recent status snapshot (thread-safe).
func (p *Poller) Latest() StatusSnapshot {
	return p.latest.Load().(StatusSnapshot)
}

// Updates returns a channel that receives a signal when new data is available.
// The channel is buffered (size 1) so sends never block.
func (p *Poller) Updates() <-chan struct{} {
	return p.updateCh
}

func (p *Poller) run(ctx context.Context) {
	// Poll immediately on start, then every interval.
	p.poll(ctx)

	ticker := time.NewTicker(p.interval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			p.poll(ctx)
		}
	}
}

func (p *Poller) poll(ctx context.Context) {
	snap := p.doPoll(ctx)
	p.latest.Store(snap)

	// Non-blocking send to notify listeners.
	select {
	case p.updateCh <- struct{}{}:
	default:
	}
}

func (p *Poller) doPoll(ctx context.Context) StatusSnapshot {
	// Step 1: TCP probe (mirrors status.go line 38).
	conn, err := net.DialTimeout("tcp", p.target, 2*time.Second)
	if err != nil {
		return StatusSnapshot{
			State: StateOffline,
			Error: fmt.Errorf("tcp probe: %w", err),
		}
	}
	conn.Close()

	// Step 2: gRPC connect + query.
	gc, err := grpcclient.New(p.target)
	if err != nil {
		return StatusSnapshot{
			State: StateOffline,
			Error: fmt.Errorf("grpc connect: %w", err),
		}
	}
	defer gc.Close()

	callCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()

	// Get status.
	status, statusErr := gc.GetStatus(callCtx)

	// Get instruments.
	instruments, instrErr := gc.GetInstruments(callCtx)

	// Build snapshot.
	if statusErr != nil && instrErr != nil {
		return StatusSnapshot{
			State: StateDegraded,
			Error: fmt.Errorf("status: %v; instruments: %v", statusErr, instrErr),
		}
	}

	snap := StatusSnapshot{State: StateOnline}

	if status != nil {
		snap.Version = status.GetVersion()
		snap.Hostname = status.GetHostname()
		snap.UptimeSeconds = status.GetUptimeSeconds()
		snap.InstrumentCount = status.GetInstrumentCount()
	}

	if instruments != nil {
		snap.Instruments = instruments
		snap.InstrumentCount = int32(len(instruments))
	}

	if statusErr != nil || instrErr != nil {
		snap.State = StateDegraded
		if statusErr != nil {
			snap.Error = statusErr
		} else {
			snap.Error = instrErr
		}
	}

	return snap
}
