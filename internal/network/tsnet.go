// Package network manages the embedded Tailscale networking stack for the
// galois-edge daemon. It wraps tsnet.Server to join a tailnet (Tailscale or
// Headscale) and expose listeners on the assigned Tailscale IP addresses.
package network

import (
	"context"
	"fmt"
	"log"
	"net"
	"net/http"
	"net/netip"
	"os"
	"sync"

	"tailscale.com/tsnet"
)

// Config holds the parameters needed to join a tailnet.
type Config struct {
	Hostname   string // Node name on the tailnet.
	AuthKey    string // Pre-auth key (Tailscale or Headscale). Empty = interactive login.
	ControlURL string // Headscale control URL. Empty = default Tailscale control plane.
	StateDir   string // Persistent state directory. Created with 0700 if absent.
}

// Server wraps a tsnet.Server with a simplified start/listen/stop interface.
type Server struct {
	cfg     Config
	mu      sync.Mutex
	srv     *tsnet.Server
	started bool
}

// NewServer returns a Server configured with the given Config.
// The underlying tsnet.Server is not created until Start is called.
func NewServer(cfg Config) *Server {
	return &Server{cfg: cfg}
}

// Start initializes the tsnet.Server and joins the tailnet. The provided
// context governs the startup phase; once Start returns nil the server is
// running independently of that context. Call Stop to shut down.
func (s *Server) Start(ctx context.Context) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	if s.started {
		return fmt.Errorf("tsnet: server already started")
	}

	// Ensure the state directory exists.
	if s.cfg.StateDir != "" {
		if err := os.MkdirAll(s.cfg.StateDir, 0700); err != nil {
			return fmt.Errorf("tsnet: create state dir %q: %w", s.cfg.StateDir, err)
		}
	}

	ts := &tsnet.Server{
		Hostname:  s.cfg.Hostname,
		Dir:       s.cfg.StateDir,
		AuthKey:   s.cfg.AuthKey,
		Ephemeral: false,
	}

	// Point at a Headscale control server when configured.
	if s.cfg.ControlURL != "" {
		ts.ControlURL = s.cfg.ControlURL
	}

	if err := ts.Start(); err != nil {
		return fmt.Errorf("tsnet: start server: %w", err)
	}

	s.srv = ts
	s.started = true
	log.Printf("[tsnet] joined tailnet as %q", s.cfg.Hostname)
	return nil
}

// TailscaleIPs returns the Tailscale IP addresses assigned to this node.
// Both IPv4 (100.x.y.z) and IPv6 (fd7a:...) addresses may be returned.
// Start must be called first.
func (s *Server) TailscaleIPs() ([]netip.Addr, error) {
	s.mu.Lock()
	srv := s.srv
	s.mu.Unlock()

	if srv == nil {
		return nil, fmt.Errorf("tsnet: server not started")
	}

	lc, err := srv.LocalClient()
	if err != nil {
		return nil, fmt.Errorf("tsnet: local client: %w", err)
	}

	status, err := lc.Status(context.Background())
	if err != nil {
		return nil, fmt.Errorf("tsnet: get status: %w", err)
	}

	return status.TailscaleIPs, nil
}

// IPv4 returns the first IPv4 address assigned on the tailnet, or an
// empty string if none is available.
func (s *Server) IPv4() string {
	addrs, err := s.TailscaleIPs()
	if err != nil {
		return ""
	}
	for _, a := range addrs {
		if a.Is4() {
			return a.String()
		}
	}
	return ""
}

// Listen creates a net.Listener bound to the tailnet interface. The
// network parameter should be "tcp" and addr should include the port
// (e.g. ":50051"). Start must be called first.
func (s *Server) Listen(network, addr string) (net.Listener, error) {
	s.mu.Lock()
	srv := s.srv
	s.mu.Unlock()

	if srv == nil {
		return nil, fmt.Errorf("tsnet: server not started")
	}

	ln, err := srv.Listen(network, addr)
	if err != nil {
		return nil, fmt.Errorf("tsnet: listen %s %s: %w", network, addr, err)
	}

	log.Printf("[tsnet] listening on %s %s", network, addr)
	return ln, nil
}

// DialContext connects to an address over the tailnet. Start must be called
// first; this wrapper keeps callers from depending directly on tailscale.com.
func (s *Server) DialContext(ctx context.Context, network, addr string) (net.Conn, error) {
	s.mu.Lock()
	srv := s.srv
	s.mu.Unlock()

	if srv == nil {
		return nil, fmt.Errorf("tsnet: server not started")
	}
	return srv.Dial(ctx, network, addr)
}

// HTTPClient returns an HTTP client whose transport dials over the tailnet.
// Start must be called first.
func (s *Server) HTTPClient() (*http.Client, error) {
	s.mu.Lock()
	srv := s.srv
	s.mu.Unlock()

	if srv == nil {
		return nil, fmt.Errorf("tsnet: server not started")
	}
	return srv.HTTPClient(), nil
}

// Stop gracefully shuts down the tsnet server and releases resources.
// It is safe to call Stop on a server that was never started or has
// already been stopped.
func (s *Server) Stop() error {
	s.mu.Lock()
	defer s.mu.Unlock()

	if s.srv == nil {
		return nil
	}

	err := s.srv.Close()
	s.srv = nil
	s.started = false

	if err != nil {
		return fmt.Errorf("tsnet: close: %w", err)
	}

	log.Printf("[tsnet] server stopped")
	return nil
}
