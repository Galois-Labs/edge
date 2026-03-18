// Package registration implements a state-machine that keeps the edge daemon
// registered with the Galois cloud backend. It posts instrument inventories,
// sends periodic heartbeats, and gracefully unregisters on shutdown.
//
// State machine:
//
//	Disconnected --> Registering --> Connected <--> Backoff
//	     ^                              |
//	     |  (heartbeat 404)             |
//	     +------------------------------+
package registration

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"math"
	"math/rand"
	"net/http"
	"os"
	"sort"
	"sync"
	"time"
)

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

// State represents the current phase of the registration state machine.
type State int

const (
	// StateDisconnected is the initial state. No registration attempt has
	// succeeded yet, or the backend explicitly rejected the last heartbeat
	// with a 404 (edge forgotten).
	StateDisconnected State = iota

	// StateRegistering means a registration HTTP call is in flight.
	StateRegistering

	// StateConnected means the edge is registered and heartbeats are active.
	StateConnected

	// StateBackoff means consecutive failures exceeded the threshold and the
	// manager is waiting before retrying.
	StateBackoff
)

// String returns a human-readable label for the state.
func (s State) String() string {
	switch s {
	case StateDisconnected:
		return "Disconnected"
	case StateRegistering:
		return "Registering"
	case StateConnected:
		return "Connected"
	case StateBackoff:
		return "Backoff"
	default:
		return fmt.Sprintf("State(%d)", int(s))
	}
}

// ---------------------------------------------------------------------------
// InstrumentInfo — registration-layer instrument representation
// ---------------------------------------------------------------------------

// InstrumentInfo describes a single VISA instrument in terms the registration
// layer cares about. It is intentionally decoupled from protobuf types so that
// the registration package has no proto dependency.
type InstrumentInfo struct {
	ID           string `json:"id"`
	VisaAddress  string `json:"visa_address"`
	Name         string `json:"name"`
	Manufacturer string `json:"manufacturer"`
	Model        string `json:"model"`
	Status       string `json:"status"`
}

// instrumentHash returns a deterministic hash of the instrument list.
// Used to detect changes between heartbeat cycles.
func instrumentHash(instruments []InstrumentInfo) string {
	if len(instruments) == 0 {
		return ""
	}
	ids := make([]string, len(instruments))
	for i, inst := range instruments {
		ids[i] = inst.VisaAddress + "|" + inst.Name
	}
	sort.Strings(ids)
	h := sha256.Sum256([]byte(fmt.Sprintf("%v", ids)))
	return hex.EncodeToString(h[:8])
}

// ---------------------------------------------------------------------------
// InstrumentGetter — dependency injection point
// ---------------------------------------------------------------------------

// InstrumentGetter is the sole external dependency: something that can list
// the currently-visible instruments. The grpcclient package satisfies this
// interface.
type InstrumentGetter interface {
	GetInstruments(ctx context.Context) ([]InstrumentInfo, error)
}

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

// Config holds every tunable for a registration Manager.
type Config struct {
	// BackendURL is the base URL of the cloud backend (e.g.
	// "https://api.galois.dev").
	BackendURL string

	// EdgeName is the human-readable name for this edge daemon.
	EdgeName string

	// Hostname is the OS hostname.
	Hostname string

	// Token is a one-time registration token. It is consumed (cleared) after
	// the first successful registration.
	Token string

	// GRPCPort is the external gRPC port reported to the backend. Defaults
	// to 50051.
	GRPCPort int

	// WSPort is the external WebSocket port reported to the backend.
	// Defaults to 8765.
	WSPort int

	// Version is the daemon version string reported to the backend.
	Version string

	// OSInfo is an OS description string (e.g. "linux/amd64") reported to
	// the backend.
	OSInfo string

	// HeartbeatInterval is the time between heartbeats. Default 30s.
	HeartbeatInterval time.Duration

	// InitialBackoff is the first backoff duration. Default 2s.
	InitialBackoff time.Duration

	// MaxBackoff is the ceiling for exponential backoff. Default 300s.
	MaxBackoff time.Duration

	// FailureThreshold is the number of consecutive heartbeat failures that
	// trigger a transition to the Backoff state. Default 3.
	FailureThreshold int

	// IPFunc returns the current Tailscale/Headscale IP. When nil, the
	// empty string is sent.
	IPFunc func() string
}

// applyDefaults fills in zero-valued fields with sensible production defaults.
func (c *Config) applyDefaults() {
	if c.GRPCPort == 0 {
		c.GRPCPort = 50051
	}
	if c.WSPort == 0 {
		c.WSPort = 8765
	}
	if c.HeartbeatInterval == 0 {
		c.HeartbeatInterval = 30 * time.Second
	}
	if c.InitialBackoff == 0 {
		c.InitialBackoff = 2 * time.Second
	}
	if c.MaxBackoff == 0 {
		c.MaxBackoff = 300 * time.Second
	}
	if c.FailureThreshold == 0 {
		c.FailureThreshold = 3
	}
	if c.IPFunc == nil {
		c.IPFunc = func() string { return "" }
	}
	if c.Hostname == "" {
		c.Hostname, _ = os.Hostname()
	}
}

// ---------------------------------------------------------------------------
// Manager
// ---------------------------------------------------------------------------

// Manager drives the registration state machine as a background goroutine.
// Use NewManager to create one, then call Start to begin the loop.
type Manager struct {
	cfg    Config
	getter InstrumentGetter
	client *http.Client

	mu       sync.Mutex
	state    State
	edgeID   string // assigned by the backend on first registration
	attempts int    // consecutive failure counter

	// lastInstrumentHash tracks the last instrument set successfully
	// acked by the backend.  When the current set differs, the next
	// heartbeat includes the full instrument list ("full state on change").
	lastInstrumentHash string

	cancel context.CancelFunc
	done   chan struct{}
}

// NewManager creates a registration Manager. Call Start to begin the
// background loop. The InstrumentGetter is typically a grpcclient-based
// adapter.
func NewManager(cfg Config, getter InstrumentGetter) *Manager {
	cfg.applyDefaults()
	return &Manager{
		cfg:    cfg,
		getter: getter,
		client: &http.Client{Timeout: 15 * time.Second},
		state:  StateDisconnected,
		done:   make(chan struct{}),
	}
}

// State returns the current registration state (safe for concurrent use).
func (m *Manager) State() State {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.state
}

// EdgeID returns the backend-assigned edge identifier (empty until
// registration succeeds). Safe for concurrent use.
func (m *Manager) EdgeID() string {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.edgeID
}

// SetIPFunc replaces the function used to obtain the current Tailscale IP.
// This allows start.go to wire up the real tsnet IP source after tsnet starts,
// which happens after the initial RegisterOnce call. Safe for concurrent use.
func (m *Manager) SetIPFunc(fn func() string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.cfg.IPFunc = fn
}

func (m *Manager) setState(s State) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.state != s {
		log.Printf("[registration] state: %s -> %s", m.state, s)
	}
	m.state = s
}

// Start begins the background registration/heartbeat loop. It returns
// immediately. The loop runs until Stop is called or the parent context is
// cancelled.
func (m *Manager) Start(ctx context.Context) {
	ctx, m.cancel = context.WithCancel(ctx)
	go m.loop(ctx)
}

// Stop gracefully shuts down the manager. It sends a best-effort unregister
// request and then waits for the background loop to exit.
func (m *Manager) Stop() {
	// Best-effort deregistration.
	m.unregister()

	if m.cancel != nil {
		m.cancel()
	}
	<-m.done
}

// Done returns a channel that is closed when the background loop exits.
func (m *Manager) Done() <-chan struct{} {
	return m.done
}

// ---------------------------------------------------------------------------
// Main loop
// ---------------------------------------------------------------------------

func (m *Manager) loop(ctx context.Context) {
	defer close(m.done)

	// If already registered (e.g., via RegisterOnce), skip to connected state.
	if m.EdgeID() != "" {
		m.setState(StateConnected)
	}

	for {
		switch m.State() {
		case StateDisconnected, StateBackoff:
			// When entering from backoff, wait the computed duration first.
			if m.State() == StateBackoff {
				backoff := m.calcBackoff()
				log.Printf("[registration] backing off for %v (attempt %d)", backoff, m.attempts)
				select {
				case <-time.After(backoff):
				case <-ctx.Done():
					return
				}
			}

			m.setState(StateRegistering)
			if err := m.register(ctx); err != nil {
				log.Printf("[registration] register failed: %v", err)
				m.mu.Lock()
				m.attempts++
				m.mu.Unlock()
				m.setState(StateBackoff)
				continue
			}

			// Registration succeeded.
			m.mu.Lock()
			m.attempts = 0
			// Note: token is NOT cleared — it is needed for heartbeat
			// and unregister calls which also require API key auth.
			m.mu.Unlock()
			m.setState(StateConnected)

		case StateConnected:
			select {
			case <-time.After(m.cfg.HeartbeatInterval):
				if err := m.heartbeat(ctx); err != nil {
					log.Printf("[registration] heartbeat failed: %v", err)
				}
			case <-ctx.Done():
				return
			}

		case StateRegistering:
			// Should not linger here; reset to disconnected.
			m.setState(StateDisconnected)
		}

		// Check context at the top of every iteration so we never loop
		// indefinitely after cancellation.
		select {
		case <-ctx.Done():
			return
		default:
		}
	}
}

// ---------------------------------------------------------------------------
// Auth helper
// ---------------------------------------------------------------------------

// setAuthHeader adds the X-API-Key header to an outgoing request if a
// registration token is configured.
func (m *Manager) setAuthHeader(req *http.Request) {
	m.mu.Lock()
	token := m.cfg.Token
	m.mu.Unlock()
	if token != "" {
		req.Header.Set("X-API-Key", token)
	}
}

// ---------------------------------------------------------------------------
// HTTP payloads
// ---------------------------------------------------------------------------

type registerPayload struct {
	Name        string           `json:"name"`
	Hostname    string           `json:"hostname"`
	TailnetIP   string           `json:"tailnet_ip,omitempty"`
	GRPCPort    int              `json:"grpc_port"`
	WSPort      int              `json:"ws_port"`
	Version     string           `json:"version,omitempty"`
	OSInfo      string           `json:"os_info,omitempty"`
	Instruments []InstrumentInfo `json:"instruments"`
}

// registerResponse captures the relevant fields from the backend's JSON
// response to POST /api/v1/edges/register.
type registerResponse struct {
	ID           string `json:"id"`
	PreAuthKey   string `json:"pre_auth_key,omitempty"`
	HeadscaleURL string `json:"headscale_url,omitempty"`
}

// RegisterResult holds the outcome of a single registration call.
type RegisterResult struct {
	EdgeID       string
	PreAuthKey   string
	HeadscaleURL string
}

type heartbeatPayload struct {
	TailnetIP   string           `json:"tailnet_ip,omitempty"`
	Status      string           `json:"status"`
	Instruments []InstrumentInfo `json:"instruments,omitempty"`
}

// ---------------------------------------------------------------------------
// HTTP calls — register
// ---------------------------------------------------------------------------

// doRegister performs the HTTP POST to /api/v1/edges/register and returns
// the parsed response. It does NOT mutate Manager state — callers are
// responsible for updating edgeID etc.
func (m *Manager) doRegister(ctx context.Context) (*registerResponse, int, error) {
	instruments, err := m.getter.GetInstruments(ctx)
	if err != nil {
		return nil, 0, fmt.Errorf("get instruments: %w", err)
	}

	payload := registerPayload{
		Name:        m.cfg.EdgeName,
		Hostname:    m.cfg.Hostname,
		TailnetIP:   m.cfg.IPFunc(),
		GRPCPort:    m.cfg.GRPCPort,
		WSPort:      m.cfg.WSPort,
		Version:     m.cfg.Version,
		OSInfo:      m.cfg.OSInfo,
		Instruments: instruments,
	}

	body, err := json.Marshal(payload)
	if err != nil {
		return nil, 0, fmt.Errorf("marshal register payload: %w", err)
	}

	url := fmt.Sprintf("%s/api/v1/edges/register", m.cfg.BackendURL)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return nil, 0, fmt.Errorf("build register request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	m.setAuthHeader(req)

	resp, err := m.client.Do(req)
	if err != nil {
		return nil, 0, fmt.Errorf("POST %s: %w", url, err)
	}
	defer resp.Body.Close()

	// Drain the body so the connection can be reused.
	respBody, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))

	if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusCreated {
		return nil, resp.StatusCode, fmt.Errorf("POST %s: status %d: %s", url, resp.StatusCode, string(respBody))
	}

	var regResp registerResponse
	if err := json.Unmarshal(respBody, &regResp); err != nil {
		return nil, resp.StatusCode, fmt.Errorf("decode register response: %w", err)
	}

	return &regResp, resp.StatusCode, nil
}

// register performs a single registration call and updates Manager state.
// Used by the background loop.
func (m *Manager) register(ctx context.Context) error {
	regResp, _, err := m.doRegister(ctx)
	if err != nil {
		return err
	}

	m.mu.Lock()
	if regResp.ID != "" {
		m.edgeID = regResp.ID
	}
	m.mu.Unlock()

	log.Printf("[registration] registered as edge %s", regResp.ID)
	return nil
}

// RegisterOnce performs a single registration call and returns the full
// result including pre_auth_key and headscale_url. It sets the Manager's
// edgeID so that a subsequent Start() loop skips to heartbeats instead of
// re-registering. This is intended for use by start.go before tsnet startup.
func (m *Manager) RegisterOnce(ctx context.Context) (*RegisterResult, error) {
	regResp, _, err := m.doRegister(ctx)
	if err != nil {
		return nil, err
	}

	m.mu.Lock()
	if regResp.ID != "" {
		m.edgeID = regResp.ID
	}
	m.mu.Unlock()

	log.Printf("[registration] registered as edge %s (pre_auth_key=%t, headscale_url=%t)",
		regResp.ID, regResp.PreAuthKey != "", regResp.HeadscaleURL != "")

	return &RegisterResult{
		EdgeID:       regResp.ID,
		PreAuthKey:   regResp.PreAuthKey,
		HeadscaleURL: regResp.HeadscaleURL,
	}, nil
}

// ---------------------------------------------------------------------------
// HTTP calls — heartbeat
// ---------------------------------------------------------------------------

func (m *Manager) heartbeat(ctx context.Context) error {
	m.mu.Lock()
	edgeID := m.edgeID
	m.mu.Unlock()

	if edgeID == "" {
		// Cannot heartbeat without an edge ID; force re-registration.
		m.setState(StateDisconnected)
		return fmt.Errorf("no edge ID, forcing re-registration")
	}

	payload := heartbeatPayload{
		TailnetIP: m.cfg.IPFunc(),
		Status:    "online",
	}

	// Include instruments when the set has changed since last ack.
	// This ensures the cloud stays in sync without sending redundant
	// data on every heartbeat ("full state on change").
	instruments, err := m.getter.GetInstruments(ctx)
	if err == nil {
		hash := instrumentHash(instruments)
		m.mu.Lock()
		changed := hash != m.lastInstrumentHash
		m.mu.Unlock()
		if changed {
			payload.Instruments = instruments
		}
	}

	body, err := json.Marshal(payload)
	if err != nil {
		return fmt.Errorf("marshal heartbeat: %w", err)
	}

	url := fmt.Sprintf("%s/api/v1/edges/%s/heartbeat", m.cfg.BackendURL, edgeID)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return fmt.Errorf("build heartbeat request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	m.setAuthHeader(req)

	resp, err := m.client.Do(req)
	if err != nil {
		return m.handleHeartbeatFailure(fmt.Errorf("POST %s: %w", url, err))
	}
	defer resp.Body.Close()
	io.Copy(io.Discard, resp.Body) // drain for connection reuse

	switch resp.StatusCode {
	case http.StatusOK:
		// Success — update instrument hash and reset failure counter.
		if payload.Instruments != nil {
			m.mu.Lock()
			m.lastInstrumentHash = instrumentHash(payload.Instruments)
			m.mu.Unlock()
		}
		m.mu.Lock()
		m.attempts = 0
		m.mu.Unlock()
		return nil

	case http.StatusNotFound:
		// Backend forgot about us. Reset and re-register.
		log.Printf("[registration] heartbeat 404: backend lost our registration, re-registering")
		m.mu.Lock()
		m.attempts = 0
		m.edgeID = ""
		m.mu.Unlock()
		m.setState(StateDisconnected)
		return fmt.Errorf("heartbeat 404: backend lost registration")

	default:
		return m.handleHeartbeatFailure(fmt.Errorf("heartbeat status %d", resp.StatusCode))
	}
}

// handleHeartbeatFailure increments the failure counter and transitions to
// backoff if the threshold is reached.
func (m *Manager) handleHeartbeatFailure(err error) error {
	m.mu.Lock()
	m.attempts++
	exceeded := m.attempts >= m.cfg.FailureThreshold
	attempts := m.attempts
	m.mu.Unlock()

	if exceeded {
		log.Printf("[registration] %d consecutive heartbeat failures, entering backoff", attempts)
		m.setState(StateBackoff)
	}
	return err
}

// ---------------------------------------------------------------------------
// HTTP calls — unregister
// ---------------------------------------------------------------------------

func (m *Manager) unregister() {
	m.mu.Lock()
	edgeID := m.edgeID
	m.mu.Unlock()

	if edgeID == "" {
		return
	}

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	url := fmt.Sprintf("%s/api/v1/edges/%s/unregister", m.cfg.BackendURL, edgeID)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, nil)
	if err != nil {
		log.Printf("[registration] unregister request build error: %v", err)
		return
	}
	m.setAuthHeader(req)

	resp, err := m.client.Do(req)
	if err != nil {
		log.Printf("[registration] unregister failed: %v", err)
		return
	}
	resp.Body.Close()
	log.Printf("[registration] unregistered edge %s (status %d)", edgeID, resp.StatusCode)
}

// ---------------------------------------------------------------------------
// Backoff calculation
// ---------------------------------------------------------------------------

// calcBackoff computes an exponential backoff duration with 25% additive
// jitter:
//
//	base   = min(initialBackoff * 2^attempts, maxBackoff)
//	jitter = base * 0.25 * rand()
//	total  = base + jitter
func (m *Manager) calcBackoff() time.Duration {
	m.mu.Lock()
	attempts := m.attempts
	initial := m.cfg.InitialBackoff.Seconds()
	maximum := m.cfg.MaxBackoff.Seconds()
	m.mu.Unlock()

	base := math.Min(initial*math.Pow(2, float64(attempts)), maximum)
	jitter := base * 0.25 * rand.Float64()
	total := base + jitter

	return time.Duration(total * float64(time.Second))
}
