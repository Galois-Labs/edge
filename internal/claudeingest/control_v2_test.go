package claudeingest

import (
	"context"
	"encoding/json"
	"net/http"
	"strings"
	"testing"
	"time"
)

func TestControlServerCacheTTLExpires(t *testing.T) {
	var consentLookups int
	httpClient := &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		switch {
		case r.Method == http.MethodGet && strings.Contains(r.URL.Path, "/claude-ingest/consent"):
			consentLookups++
			return testJSONResponse(t, Consent{
				Version:        ConsentVersion,
				Enabled:        true,
				Subject:        Subject{Key: "subject-x"},
				AllowedFolders: []string{"/tmp"},
			}), nil
		case r.Method == http.MethodPost && strings.Contains(r.URL.Path, "/claude-ingest/events"):
			return testResponse(http.StatusNoContent, ""), nil
		}
		return testResponse(http.StatusNotFound, ""), nil
	})}

	server := NewControlServer(ControlConfig{
		BackendURL: "http://cloud.local",
		AuthToken:  "tok",
		HTTPClient: httpClient,
	})

	batch := EventBatch{
		Version:        BatchVersion,
		Subject:        Subject{Key: "subject-x"},
		SessionID:      "s",
		CWD:            "/tmp/sub",
		TranscriptPath: "/t.jsonl",
		Lines:          []json.RawMessage{json.RawMessage(`{"uuid":"a"}`)},
	}

	// First call: cache miss, GET /consent fires.
	rr := newTestRecorder()
	body := mustJSON(t, batch)
	req, _ := http.NewRequest(http.MethodPost, "/v1/claude/events", strings.NewReader(body))
	server.handleEvents(rr, req)
	if rr.Code != http.StatusNoContent {
		t.Fatalf("first call: %d %s", rr.Code, rr.BodyText())
	}
	if consentLookups != 1 {
		t.Fatalf("first call should have caused 1 consent lookup, got %d", consentLookups)
	}

	// Second call within TTL: cache hit, no extra GET.
	rr = newTestRecorder()
	req, _ = http.NewRequest(http.MethodPost, "/v1/claude/events", strings.NewReader(mustJSON(t, batch)))
	server.handleEvents(rr, req)
	if consentLookups != 1 {
		t.Errorf("within-TTL call should have hit cache, lookups=%d", consentLookups)
	}

	// Force-expire cache by reaching into store.
	server.cacheMu.Lock()
	for k, v := range server.cache {
		v.fetchedAt = time.Now().Add(-2 * ConsentCacheTTL)
		server.cache[k] = v
	}
	server.cacheMu.Unlock()

	// Third call after expiry: cache miss again.
	rr = newTestRecorder()
	req, _ = http.NewRequest(http.MethodPost, "/v1/claude/events", strings.NewReader(mustJSON(t, batch)))
	server.handleEvents(rr, req)
	if consentLookups != 2 {
		t.Errorf("after TTL expiry, lookups=%d want 2", consentLookups)
	}
}

func TestControlServerCacheInvalidatedOnConsentPut(t *testing.T) {
	var consentGets, consentPuts int
	httpClient := &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		switch {
		case r.Method == http.MethodGet:
			consentGets++
			return testJSONResponse(t, Consent{
				Version: ConsentVersion, Enabled: true,
				Subject:        Subject{Key: "s"},
				AllowedFolders: []string{"/tmp"},
			}), nil
		case r.Method == http.MethodPut:
			consentPuts++
			return testResponse(http.StatusNoContent, ""), nil
		case r.Method == http.MethodPost:
			return testResponse(http.StatusNoContent, ""), nil
		}
		return testResponse(http.StatusNotFound, ""), nil
	})}
	server := NewControlServer(ControlConfig{
		BackendURL: "http://cloud.local", AuthToken: "tok", HTTPClient: httpClient,
	})

	batch := EventBatch{
		Version: BatchVersion, Subject: Subject{Key: "s"},
		SessionID: "s1", CWD: "/tmp/x", TranscriptPath: "/t.jsonl",
		Lines: []json.RawMessage{json.RawMessage(`{"uuid":"a"}`)},
	}

	// Seed cache.
	rr := newTestRecorder()
	req, _ := http.NewRequest(http.MethodPost, "/v1/claude/events", strings.NewReader(mustJSON(t, batch)))
	server.handleEvents(rr, req)
	if consentGets != 1 {
		t.Fatalf("seed: gets=%d", consentGets)
	}

	// PUT /consent — must invalidate cache.
	consent := Consent{Version: ConsentVersion, Subject: Subject{Key: "s"}, Enabled: true, AllowedFolders: []string{"/tmp"}}
	rr = newTestRecorder()
	req, _ = http.NewRequest(http.MethodPost, "/v1/claude/consent", strings.NewReader(mustJSON(t, consent)))
	server.handleConsent(rr, req)
	if rr.Code != http.StatusNoContent {
		t.Fatalf("consent put: %d %s", rr.Code, rr.BodyText())
	}
	if consentPuts != 1 {
		t.Fatalf("consent puts: got %d want 1", consentPuts)
	}

	// Next event call should re-fetch consent (cache invalidated).
	rr = newTestRecorder()
	req, _ = http.NewRequest(http.MethodPost, "/v1/claude/events", strings.NewReader(mustJSON(t, batch)))
	server.handleEvents(rr, req)
	if consentGets != 2 {
		t.Errorf("after consent PUT, cache should be invalidated; gets=%d want 2", consentGets)
	}
}

func TestControlServerBatchSizeCap(t *testing.T) {
	httpClient := &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		return testResponse(http.StatusNoContent, ""), nil
	})}
	server := NewControlServer(ControlConfig{
		BackendURL: "http://cloud.local", AuthToken: "tok", HTTPClient: httpClient,
	})

	// Construct a body slightly over MaxBatchBytes.
	huge := strings.Repeat("a", int(MaxBatchBytes)+1024)
	rr := newTestRecorder()
	req, _ := http.NewRequest(http.MethodPost, "/v1/claude/events", strings.NewReader(huge))
	server.handleEvents(rr, req)
	if rr.Code != http.StatusBadRequest {
		t.Errorf("oversized body: got %d body=%s want 400", rr.Code, rr.BodyText())
	}
}

func TestControlServerCancelBackfill(t *testing.T) {
	server := NewControlServer(ControlConfig{
		BackendURL: "http://cloud.local", AuthToken: "tok",
	})
	ch1, deregister1 := server.RegisterBackfillCancel()
	ch2, deregister2 := server.RegisterBackfillCancel()

	rr := newTestRecorder()
	req, _ := http.NewRequest(http.MethodPost, "/v1/claude/cancel-backfill", strings.NewReader(""))
	server.handleCancelBackfill(rr, req)
	if rr.Code != http.StatusNoContent {
		t.Fatalf("cancel: %d", rr.Code)
	}

	// Both channels should be closed.
	for i, ch := range []<-chan struct{}{ch1, ch2} {
		select {
		case _, ok := <-ch:
			if ok {
				t.Errorf("ch %d: received value, want closed", i)
			}
		default:
			t.Errorf("ch %d: not closed", i)
		}
	}
	deregister1()
	deregister2()
}

func TestPeerKeyMismatchIsRejected(t *testing.T) {
	server := NewControlServer(ControlConfig{
		BackendURL: "http://cloud.local", AuthToken: "tok",
		HTTPClient: &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
			return testResponse(http.StatusNoContent, ""), nil
		})},
	})

	// Inject a peer key in the request context that disagrees with
	// the claimed subject.
	batch := EventBatch{
		Version: BatchVersion, Subject: Subject{Key: "claimed-key"},
		SessionID: "s1", CWD: "/tmp/x", TranscriptPath: "/t.jsonl",
		Lines: []json.RawMessage{json.RawMessage(`{"uuid":"a"}`)},
	}
	rr := newTestRecorder()
	req, _ := http.NewRequest(http.MethodPost, "/v1/claude/events", strings.NewReader(mustJSON(t, batch)))
	req = req.WithContext(withPeerKey(context.Background(), "real-peer-key"))
	server.handleEvents(rr, req)
	if rr.Code != http.StatusForbidden {
		t.Errorf("mismatched peer key: got %d want 403, body=%s", rr.Code, rr.BodyText())
	}
}

func TestPeerKeyMatchPasses(t *testing.T) {
	httpClient := &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		switch r.Method {
		case http.MethodGet:
			return testJSONResponse(t, Consent{
				Version: ConsentVersion, Enabled: true,
				Subject: Subject{Key: "k"}, AllowedFolders: []string{"/tmp"},
			}), nil
		default:
			return testResponse(http.StatusNoContent, ""), nil
		}
	})}
	server := NewControlServer(ControlConfig{
		BackendURL: "http://cloud.local", AuthToken: "tok", HTTPClient: httpClient,
	})
	batch := EventBatch{
		Version: BatchVersion, Subject: Subject{Key: "k"},
		SessionID: "s1", CWD: "/tmp/x", TranscriptPath: "/t.jsonl",
		Lines: []json.RawMessage{json.RawMessage(`{"uuid":"a"}`)},
	}
	rr := newTestRecorder()
	req, _ := http.NewRequest(http.MethodPost, "/v1/claude/events", strings.NewReader(mustJSON(t, batch)))
	req = req.WithContext(withPeerKey(context.Background(), "k"))
	server.handleEvents(rr, req)
	if rr.Code != http.StatusNoContent {
		t.Errorf("matched peer key: got %d body=%s want 204", rr.Code, rr.BodyText())
	}
}

func TestPeerKeyUnauthorizedSentinelRejected(t *testing.T) {
	server := NewControlServer(ControlConfig{
		BackendURL: "http://cloud.local", AuthToken: "tok",
	})
	batch := EventBatch{
		Version: BatchVersion, Subject: Subject{Key: "k"},
		SessionID: "s1", CWD: "/tmp/x", TranscriptPath: "/t.jsonl",
		Lines: []json.RawMessage{json.RawMessage(`{"uuid":"a"}`)},
	}
	rr := newTestRecorder()
	req, _ := http.NewRequest(http.MethodPost, "/v1/claude/events", strings.NewReader(mustJSON(t, batch)))
	req = req.WithContext(withPeerKey(context.Background(), "unauthorized"))
	server.handleEvents(rr, req)
	if rr.Code != http.StatusForbidden {
		t.Errorf("unauthorized sentinel: got %d want 403", rr.Code)
	}
}
