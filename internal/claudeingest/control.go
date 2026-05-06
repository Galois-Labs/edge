package claudeingest

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"strings"
	"sync"
	"time"
)

// ControlConfig configures the localhost daemon control endpoint.
type ControlConfig struct {
	Addr       string
	BackendURL string
	AuthToken  string
	HTTPClient *http.Client
	Logger     *slog.Logger
}

// ControlServer receives user-context hook payloads and forwards them to cloud.
type ControlServer struct {
	cfg    ControlConfig
	cloud  *CloudClient
	server *http.Server
	logger *slog.Logger

	mu      sync.Mutex
	consent map[string]Consent
}

// NewControlServer creates a localhost-only Claude ingestion control server.
func NewControlServer(cfg ControlConfig) *ControlServer {
	if cfg.Addr == "" {
		cfg.Addr = DefaultControlAddr
	}
	logger := cfg.Logger
	if logger == nil {
		logger = slog.Default()
	}
	s := &ControlServer{
		cfg:     cfg,
		cloud:   NewCloudClient(cfg.BackendURL, cfg.AuthToken, cfg.HTTPClient),
		logger:  logger.With("component", "claude-ingest"),
		consent: map[string]Consent{},
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/health", s.handleHealth)
	mux.HandleFunc("/v1/claude/consent", s.handleConsent)
	mux.HandleFunc("/v1/claude/events", s.handleEvents)
	s.server = &http.Server{
		Addr:              cfg.Addr,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
	}
	return s
}

// Run starts the control server and blocks until ctx is canceled or the server
// stops. Only loopback addresses are allowed.
func (s *ControlServer) Run(ctx context.Context) error {
	host, _, err := net.SplitHostPort(s.cfg.Addr)
	if err != nil {
		return err
	}
	if ip := net.ParseIP(host); ip == nil || !ip.IsLoopback() {
		return fmt.Errorf("claude ingest control address must be loopback, got %s", s.cfg.Addr)
	}

	ln, err := net.Listen("tcp", s.cfg.Addr)
	if err != nil {
		return err
	}
	go func() {
		<-ctx.Done()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = s.server.Shutdown(shutdownCtx)
	}()
	s.logger.Info("claude ingest control endpoint started", "addr", s.cfg.Addr)
	err = s.server.Serve(ln)
	if err == http.ErrServerClosed {
		return nil
	}
	return err
}

func (s *ControlServer) handleHealth(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	_, _ = w.Write([]byte(`{"ok":true}` + "\n"))
}

func (s *ControlServer) handleConsent(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var consent Consent
	if err := json.NewDecoder(r.Body).Decode(&consent); err != nil {
		http.Error(w, "invalid consent JSON", http.StatusBadRequest)
		return
	}
	if consent.Subject.Key == "" {
		http.Error(w, "subject key is required", http.StatusBadRequest)
		return
	}
	if err := s.cloud.PutConsent(r.Context(), consent); err != nil {
		s.logger.Warn("cloud consent sync failed", "subject", consent.Subject.Key, "error", err)
		http.Error(w, err.Error(), http.StatusBadGateway)
		return
	}
	s.cacheConsent(consent)
	w.WriteHeader(http.StatusNoContent)
}

func (s *ControlServer) handleEvents(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var batch EventBatch
	if err := json.NewDecoder(r.Body).Decode(&batch); err != nil {
		http.Error(w, "invalid event JSON", http.StatusBadRequest)
		return
	}
	if batch.Subject.Key == "" || batch.SessionID == "" || batch.TranscriptPath == "" {
		http.Error(w, "subject, session_id, and transcript_path are required", http.StatusBadRequest)
		return
	}
	if len(batch.Lines) == 0 {
		w.WriteHeader(http.StatusNoContent)
		return
	}

	allowed, err := s.cloudAllows(r.Context(), batch)
	if err != nil {
		s.logger.Warn("cloud consent lookup failed", "subject", batch.Subject.Key, "error", err)
		http.Error(w, err.Error(), http.StatusServiceUnavailable)
		return
	}
	if !allowed {
		http.Error(w, "cloud consent disabled or folder not allowed", http.StatusForbidden)
		return
	}

	if err := s.cloud.PostEvents(r.Context(), batch); err != nil {
		s.logger.Warn("cloud event upload failed", "subject", batch.Subject.Key, "session", batch.SessionID, "error", err)
		http.Error(w, err.Error(), http.StatusBadGateway)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

func (s *ControlServer) cloudAllows(ctx context.Context, batch EventBatch) (bool, error) {
	consent, found, err := s.cloud.GetConsent(ctx, batch.Subject.Key)
	if err != nil {
		return false, err
	}
	if !found || consent == nil {
		return false, nil
	}
	s.cacheConsent(*consent)
	return consent.Enabled && IsPathAllowed(batch.CWD, consent.AllowedFolders), nil
}

func (s *ControlServer) cacheConsent(consent Consent) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.consent[consent.Subject.Key] = consent
}

func (s *ControlServer) cachedConsent(subjectKey string) (Consent, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	consent, ok := s.consent[subjectKey]
	return consent, ok
}

// LocalControlClient posts hook/CLI payloads to the daemon control endpoint.
type LocalControlClient struct {
	BaseURL string
	Client  *http.Client
}

// NewLocalControlClient creates a client for the default local control endpoint.
func NewLocalControlClient(addr string) *LocalControlClient {
	if addr == "" {
		addr = DefaultControlAddr
	}
	if !strings.HasPrefix(addr, "http://") && !strings.HasPrefix(addr, "https://") {
		addr = "http://" + addr
	}
	return &LocalControlClient{
		BaseURL: strings.TrimRight(addr, "/"),
		Client:  &http.Client{Timeout: 10 * time.Second},
	}
}

// PostConsent asks the running daemon to sync consent to cloud.
func (c *LocalControlClient) PostConsent(ctx context.Context, consent Consent) error {
	return c.postJSON(ctx, "/v1/claude/consent", consent)
}

// PostEvents asks the running daemon to upload a transcript batch.
func (c *LocalControlClient) PostEvents(ctx context.Context, batch EventBatch) error {
	return c.postJSON(ctx, "/v1/claude/events", batch)
}

func (c *LocalControlClient) postJSON(ctx context.Context, path string, payload any) error {
	body, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.BaseURL+path, strings.NewReader(string(body)))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := c.Client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("local control %s: status %d", path, resp.StatusCode)
	}
	return nil
}
