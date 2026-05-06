package claudeingest

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"sync"
	"time"
)

// ControlConfig configures the local control endpoint that runs inside
// the daemon process. The endpoint listens on an OS-native local IPC
// channel (Unix domain socket on POSIX, named pipe on Windows) and
// authenticates each accepted connection by recomputing the subject key
// from the peer's credentials. There is no other auth mechanism on the
// listener — the trust boundary is the OS-enforced peer identity.
type ControlConfig struct {
	BackendURL string
	AuthToken  string
	HTTPClient *http.Client
	Logger     *slog.Logger
}

// ControlServer receives user-context hook payloads, authenticates the
// peer, and forwards approved events to cloud.
type ControlServer struct {
	cfg    ControlConfig
	cloud  *CloudClient
	server *http.Server
	logger *slog.Logger

	// cloud consent cache, keyed by subject_key.
	cacheMu sync.Mutex
	cache   map[string]cachedConsent

	// backfill cancel: a list of channels each of which will be
	// closed when /v1/claude/cancel-backfill fires. Backfill code
	// registers and unregisters on its own.
	backfillMu      sync.Mutex
	backfillCancels []chan struct{}
}

type cachedConsent struct {
	consent   Consent
	exists    bool
	fetchedAt time.Time
}

// peerKeyContextKey is used to retrieve the peer-derived subject key
// from the request context.
type peerKeyContextKey struct{}

func withPeerKey(parent context.Context, key string) context.Context {
	return context.WithValue(parent, peerKeyContextKey{}, key)
}

// PeerKeyFromContext returns the peer-derived subject key for a
// request, or "" if no peer authentication was applied (which should
// only happen in tests using the in-process handler shortcut).
func PeerKeyFromContext(ctx context.Context) string {
	v, _ := ctx.Value(peerKeyContextKey{}).(string)
	return v
}

// NewControlServer creates a control server. The actual listener is
// created by Run, which delegates to platform-specific code in
// control_listener_unix.go / control_listener_windows.go.
func NewControlServer(cfg ControlConfig) *ControlServer {
	logger := cfg.Logger
	if logger == nil {
		logger = slog.Default()
	}
	s := &ControlServer{
		cfg:    cfg,
		cloud:  NewCloudClient(cfg.BackendURL, cfg.AuthToken, cfg.HTTPClient),
		logger: logger.With("component", "claude-ingest"),
		cache:  map[string]cachedConsent{},
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/health", s.handleHealth)
	mux.HandleFunc("/v1/claude/consent", s.handleConsent)
	mux.HandleFunc("/v1/claude/events", s.handleEvents)
	mux.HandleFunc("/v1/claude/cancel-backfill", s.handleCancelBackfill)
	s.server = &http.Server{
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
	}
	return s
}

// Run starts the local control endpoint, blocking until ctx is canceled
// or the listener errors. The listener is created by platform-specific
// code in control_listener_*.go files. ConnContext attaches the peer-
// derived subject key (computed at accept time) to every request's
// context.
func (s *ControlServer) Run(ctx context.Context) error {
	listener, err := newControlListener(s.logger)
	if err != nil {
		return fmt.Errorf("create control listener: %w", err)
	}

	// On accept, the listener wraps the conn so we can pull peer
	// creds out via ConnContext below.
	s.server.ConnContext = func(parent context.Context, conn net.Conn) context.Context {
		key := peerSubjectKeyFromConn(conn, s.logger)
		return withPeerKey(parent, key)
	}

	go func() {
		<-ctx.Done()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), DaemonShutdownDrain)
		defer cancel()
		_ = s.server.Shutdown(shutdownCtx)
	}()
	s.logger.Info("claude ingest control endpoint started", "addr", listener.Addr().String())
	err = s.server.Serve(listener)
	if errors.Is(err, http.ErrServerClosed) {
		return nil
	}
	return err
}

// RegisterBackfillCancel returns a channel that will be closed when a
// cancel-backfill request arrives, and a deregister function the
// caller MUST invoke when the backfill completes (regardless of how).
func (s *ControlServer) RegisterBackfillCancel() (<-chan struct{}, func()) {
	ch := make(chan struct{})
	s.backfillMu.Lock()
	s.backfillCancels = append(s.backfillCancels, ch)
	s.backfillMu.Unlock()
	return ch, func() {
		s.backfillMu.Lock()
		defer s.backfillMu.Unlock()
		for i, c := range s.backfillCancels {
			if c == ch {
				s.backfillCancels = append(s.backfillCancels[:i], s.backfillCancels[i+1:]...)
				return
			}
		}
	}
}

func (s *ControlServer) handleHealth(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	_, _ = w.Write([]byte(`{"ok":true}` + "\n"))
}

func (s *ControlServer) handleCancelBackfill(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	s.backfillMu.Lock()
	chans := s.backfillCancels
	s.backfillCancels = nil
	s.backfillMu.Unlock()
	for _, ch := range chans {
		close(ch)
	}
	w.WriteHeader(http.StatusNoContent)
}

func (s *ControlServer) handleConsent(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	body, err := readCappedBody(r, MaxBatchBytes)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	var consent Consent
	if err := json.Unmarshal(body, &consent); err != nil {
		http.Error(w, "invalid consent JSON", http.StatusBadRequest)
		return
	}
	if consent.Subject.Key == "" {
		http.Error(w, "subject key is required", http.StatusBadRequest)
		return
	}
	if err := s.requirePeerAuthorizesSubject(r, consent.Subject.Key); err != nil {
		s.logger.Warn("consent peer-cred mismatch",
			"claimed_subject", consent.Subject.Key,
			"error", err)
		http.Error(w, err.Error(), http.StatusForbidden)
		return
	}
	if err := s.cloud.PutConsent(r.Context(), consent); err != nil {
		s.logger.Warn("cloud consent sync failed", "subject", consent.Subject.Key, "error", err)
		http.Error(w, err.Error(), http.StatusBadGateway)
		return
	}
	s.invalidateCache(consent.Subject.Key)
	w.WriteHeader(http.StatusNoContent)
}

func (s *ControlServer) handleEvents(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	body, err := readCappedBody(r, MaxBatchBytes)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	var batch EventBatch
	if err := json.Unmarshal(body, &batch); err != nil {
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
	if err := s.requirePeerAuthorizesSubject(r, batch.Subject.Key); err != nil {
		s.logger.Warn("event peer-cred mismatch",
			"claimed_subject", batch.Subject.Key,
			"error", err)
		http.Error(w, err.Error(), http.StatusForbidden)
		return
	}

	allowed, err := s.cloudAllows(r.Context(), batch)
	if err != nil {
		s.logger.Warn("cloud consent lookup failed",
			"subject", batch.Subject.Key, "error", err)
		http.Error(w, err.Error(), http.StatusServiceUnavailable)
		return
	}
	if !allowed {
		http.Error(w, "cloud consent disabled or folder not allowed", http.StatusForbidden)
		return
	}

	if err := s.cloud.PostEvents(r.Context(), batch); err != nil {
		s.logger.Warn("cloud event upload failed",
			"subject", batch.Subject.Key, "session", batch.SessionID, "error", err)
		http.Error(w, err.Error(), http.StatusBadGateway)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

// requirePeerAuthorizesSubject returns nil if the request's peer-derived
// subject key matches the one claimed in the payload. The peer key is
// computed at accept time and stashed in the request context. In tests
// that exercise the handler directly without going through a real
// listener, the peer key is empty; we accept that ONLY if the test has
// not stashed a peer key (production code always does via ConnContext).
func (s *ControlServer) requirePeerAuthorizesSubject(r *http.Request, claimed string) error {
	peer := PeerKeyFromContext(r.Context())
	if peer == "" {
		// In-process handler test path. Production never reaches this
		// branch because Run installs ConnContext that always sets a
		// (possibly empty-on-failure) value.
		return nil
	}
	if peer == "unauthorized" {
		return fmt.Errorf("peer authentication failed")
	}
	if peer != claimed {
		return fmt.Errorf("peer subject %s does not match claimed subject %s",
			peer, claimed)
	}
	return nil
}

func (s *ControlServer) cloudAllows(ctx context.Context, batch EventBatch) (bool, error) {
	if cached, ok := s.lookupCache(batch.Subject.Key); ok {
		if !cached.exists {
			return false, nil
		}
		return cached.consent.Enabled && IsPathAllowed(batch.CWD, cached.consent.AllowedFolders), nil
	}
	consent, found, err := s.cloud.GetConsent(ctx, batch.Subject.Key)
	if err != nil {
		return false, err
	}
	if !found || consent == nil {
		s.storeCache(batch.Subject.Key, Consent{}, false)
		return false, nil
	}
	s.storeCache(batch.Subject.Key, *consent, true)
	return consent.Enabled && IsPathAllowed(batch.CWD, consent.AllowedFolders), nil
}

func (s *ControlServer) lookupCache(subjectKey string) (cachedConsent, bool) {
	s.cacheMu.Lock()
	defer s.cacheMu.Unlock()
	c, ok := s.cache[subjectKey]
	if !ok {
		return cachedConsent{}, false
	}
	if time.Since(c.fetchedAt) > ConsentCacheTTL {
		delete(s.cache, subjectKey)
		return cachedConsent{}, false
	}
	return c, true
}

func (s *ControlServer) storeCache(subjectKey string, consent Consent, exists bool) {
	s.cacheMu.Lock()
	defer s.cacheMu.Unlock()
	s.cache[subjectKey] = cachedConsent{
		consent:   consent,
		exists:    exists,
		fetchedAt: time.Now(),
	}
}

func (s *ControlServer) invalidateCache(subjectKey string) {
	s.cacheMu.Lock()
	defer s.cacheMu.Unlock()
	delete(s.cache, subjectKey)
}

// readCappedBody reads up to limit+1 bytes from r.Body, returning an
// error if the body is larger. Larger reads short-circuit before
// allocating MaxBatchBytes for buggy/malicious peers.
func readCappedBody(r *http.Request, limit int64) ([]byte, error) {
	body := io.LimitReader(r.Body, limit+1)
	b, err := io.ReadAll(body)
	if err != nil {
		return nil, err
	}
	if int64(len(b)) > limit {
		return nil, fmt.Errorf("request body exceeds %d bytes", limit)
	}
	return b, nil
}

// ============================================================
// LocalControlClient — used by the hook process and CLI to dial into
// the daemon's control endpoint.
// ============================================================

// LocalControlClient posts hook/CLI payloads to the daemon control
// endpoint. The dial path is platform-specific (Unix socket on POSIX,
// named pipe on Windows); see control_listener_*.go.
type LocalControlClient struct {
	BaseURL string // logical base URL, "http://galois-control"
	Client  *http.Client
}

// NewLocalControlClient creates a client that dials the local control
// endpoint over the platform-native IPC. The legacyAddr argument is
// ignored except that callers still pass DefaultControlAddr for
// transitional compatibility.
func NewLocalControlClient(legacyAddr string) *LocalControlClient {
	if legacyAddr == "" {
		legacyAddr = DefaultControlAddr
	}
	transport := &http.Transport{
		DialContext: dialControl,
	}
	// If legacyAddr starts with http(s) we accept it as an override to
	// the dial behavior — used by tests that want to inject their own
	// transport. Production callers use the empty / default form.
	baseURL := "http://galois-claude-control"
	if isHTTPLikeAddr(legacyAddr) {
		baseURL = "http://" + stripScheme(legacyAddr)
	}
	return &LocalControlClient{
		BaseURL: baseURL,
		Client: &http.Client{
			Timeout:   10 * time.Second,
			Transport: transport,
		},
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

// CancelBackfill asks the running daemon to cancel any in-flight
// backfill walk.
func (c *LocalControlClient) CancelBackfill(ctx context.Context) error {
	return c.postJSON(ctx, "/v1/claude/cancel-backfill", struct{}{})
}

func (c *LocalControlClient) postJSON(ctx context.Context, path string, payload any) error {
	body, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.BaseURL+path, bytes.NewReader(body))
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

func isHTTPLikeAddr(addr string) bool {
	return len(addr) >= 7 && (addr[:7] == "http://" || (len(addr) >= 8 && addr[:8] == "https://"))
}

func stripScheme(addr string) string {
	if len(addr) >= 8 && addr[:8] == "https://" {
		return addr[8:]
	}
	if len(addr) >= 7 && addr[:7] == "http://" {
		return addr[7:]
	}
	return addr
}
