package claudeingest

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestNormalizeFoldersAndAllowedPath(t *testing.T) {
	root := filepath.Join(t.TempDir(), "repo")
	sibling := root + "-other"

	folders, err := NormalizeFolders([]string{root, root})
	if err != nil {
		t.Fatalf("NormalizeFolders: %v", err)
	}
	if len(folders) != 1 {
		t.Fatalf("folders len: got %d, want 1", len(folders))
	}
	if !IsPathAllowed(filepath.Join(root, "subdir"), folders) {
		t.Fatal("expected child path to be allowed")
	}
	if IsPathAllowed(sibling, folders) {
		t.Fatal("sibling prefix should not be allowed")
	}
}

func TestSettingsInstallRemoveManagedHooks(t *testing.T) {
	path := filepath.Join(t.TempDir(), "settings.json")
	initial := `{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"echo keep"}]}]},"theme":"dark"}`
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(initial), 0o600); err != nil {
		t.Fatal(err)
	}

	cmd := ManagedHookCommand("/usr/local/bin/galois-edge")
	if err := InstallManagedHooks(path, cmd); err != nil {
		t.Fatalf("InstallManagedHooks: %v", err)
	}
	if err := InstallManagedHooks(path, cmd); err != nil {
		t.Fatalf("second InstallManagedHooks: %v", err)
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if count := strings.Count(string(data), ManagedHookMarker); count != 2 {
		t.Fatalf("managed marker count after idempotent install: got %d, want 2", count)
	}
	if !strings.Contains(string(data), "echo keep") {
		t.Fatal("existing hook was not preserved")
	}

	if err := RemoveManagedHooks(path); err != nil {
		t.Fatalf("RemoveManagedHooks: %v", err)
	}
	data, err = os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(data), ManagedHookMarker) {
		t.Fatal("managed hook marker still present after removal")
	}
	if !strings.Contains(string(data), "echo keep") {
		t.Fatal("unmanaged hook was removed")
	}
}

func TestReadTranscriptLinesUsesOffsets(t *testing.T) {
	path := filepath.Join(t.TempDir(), "session.jsonl")
	first := `{"type":"user","text":"hi"}` + "\n"
	second := `{"type":"assistant","text":"ok"}` + "\n"
	if err := os.WriteFile(path, []byte(first+second+"not-json\n"), 0o600); err != nil {
		t.Fatal(err)
	}

	lines, end, err := ReadTranscriptLines(path, 0)
	if err != nil {
		t.Fatalf("ReadTranscriptLines: %v", err)
	}
	if len(lines) != 2 {
		t.Fatalf("lines: got %d, want 2", len(lines))
	}
	if end != int64(len(first)+len(second)+len("not-json\n")) {
		t.Fatalf("end offset: got %d", end)
	}

	lines, _, err = ReadTranscriptLines(path, int64(len(first)))
	if err != nil {
		t.Fatalf("ReadTranscriptLines from offset: %v", err)
	}
	if len(lines) != 1 {
		t.Fatalf("lines from offset: got %d, want 1", len(lines))
	}
}

func TestCloudClientUsesExistingAPIKey(t *testing.T) {
	var gotAPIKey, gotMethod, gotPath string
	httpClient := &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		gotAPIKey = r.Header.Get("X-API-Key")
		gotMethod = r.Method
		gotPath = r.URL.Path
		return testResponse(http.StatusNoContent, ""), nil
	})}

	client := NewCloudClient("http://cloud.local", "glc_existing", httpClient)
	err := client.PutConsent(context.Background(), Consent{
		Version: ConsentVersion,
		Subject: Subject{
			Key: "subject",
		},
	})
	if err != nil {
		t.Fatalf("PutConsent: %v", err)
	}
	if gotAPIKey != "glc_existing" {
		t.Fatalf("X-API-Key: got %q", gotAPIKey)
	}
	if gotMethod != http.MethodPut || gotPath != "/api/v1/claude-ingest/consent" {
		t.Fatalf("request: got %s %s", gotMethod, gotPath)
	}
}

func TestControlServerForwardsConsentAndEvents(t *testing.T) {
	var consentCalls, eventCalls int
	httpClient := &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		switch r.URL.Path {
		case "/api/v1/claude-ingest/consent":
			if r.Method == http.MethodPut {
				consentCalls++
				return testResponse(http.StatusNoContent, ""), nil
			}
			if r.Method == http.MethodGet {
				return testJSONResponse(t, Consent{
					Version:        ConsentVersion,
					Enabled:        true,
					Subject:        Subject{Key: "subject"},
					AllowedFolders: []string{"/tmp/project"},
				}), nil
			}
		case "/api/v1/claude-ingest/events":
			eventCalls++
			return testResponse(http.StatusNoContent, ""), nil
		}
		return testResponse(http.StatusNotFound, ""), nil
	})}

	control := NewControlServer(ControlConfig{
		BackendURL: "http://cloud.local",
		AuthToken:  "glc_existing",
		HTTPClient: httpClient,
	})

	consent := Consent{
		Version:        ConsentVersion,
		Enabled:        true,
		Subject:        Subject{Key: "subject"},
		AllowedFolders: []string{"/tmp/project"},
	}
	rr := newTestRecorder()
	body := mustJSON(t, consent)
	req, _ := http.NewRequest(http.MethodPost, "/v1/claude/consent", strings.NewReader(body))
	control.handleConsent(rr, req)
	if rr.Code != http.StatusNoContent {
		t.Fatalf("consent status: got %d body %s", rr.Code, rr.BodyText())
	}

	batch := EventBatch{
		Version:        BatchVersion,
		Subject:        Subject{Key: "subject"},
		SessionID:      "s1",
		CWD:            "/tmp/project/sub",
		TranscriptPath: "/tmp/t.jsonl",
		Lines:          []json.RawMessage{json.RawMessage(`{"ok":true}`)},
	}
	rr = newTestRecorder()
	req, _ = http.NewRequest(http.MethodPost, "/v1/claude/events", strings.NewReader(mustJSON(t, batch)))
	control.handleEvents(rr, req)
	if rr.Code != http.StatusNoContent {
		t.Fatalf("events status: got %d body %s", rr.Code, rr.BodyText())
	}
	if consentCalls != 1 || eventCalls != 1 {
		t.Fatalf("cloud calls: consent=%d events=%d", consentCalls, eventCalls)
	}
}

func TestHookRunnerAdvancesOffsetOnlyAfterUpload(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USER", "tester")

	project := filepath.Join(home, "work", "repo")
	if err := os.MkdirAll(project, 0o700); err != nil {
		t.Fatal(err)
	}
	subject, err := LocalSubject()
	if err != nil {
		t.Fatal(err)
	}
	consent := NewConsent(subject, []string{project}, time.Now())
	if err := SaveConsent(consent); err != nil {
		t.Fatal(err)
	}

	transcript := filepath.Join(home, "session.jsonl")
	if err := os.WriteFile(transcript, []byte(`{"type":"user"}`+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}

	var uploads int
	httpClient := &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		uploads++
		return testResponse(http.StatusNoContent, ""), nil
	})}

	runner := NewHookRunner()
	runner.Control = &LocalControlClient{BaseURL: "http://local.control", Client: httpClient}
	input := `{"session_id":"s1","transcript_path":"` + transcript + `","cwd":"` + project + `","hook_event_name":"Stop"}`
	if err := runner.Run(context.Background(), strings.NewReader(input)); err != nil {
		t.Fatalf("Run: %v", err)
	}
	if uploads != 1 {
		t.Fatalf("uploads after first run: got %d, want 1", uploads)
	}
	if err := runner.Run(context.Background(), strings.NewReader(input)); err != nil {
		t.Fatalf("Run second: %v", err)
	}
	if uploads != 1 {
		t.Fatalf("uploads after second run: got %d, want still 1", uploads)
	}
}

func TestBackfillUploadsOnlyConsentedWorkspace(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USER", "tester")

	allowed := filepath.Join(home, "work", "repo")
	other := filepath.Join(home, "work", "other")
	for _, dir := range []string{allowed, other} {
		if err := os.MkdirAll(dir, 0o700); err != nil {
			t.Fatal(err)
		}
	}
	subject, err := LocalSubject()
	if err != nil {
		t.Fatal(err)
	}
	consent := NewConsent(subject, []string{allowed}, time.Now())
	if err := SaveConsent(consent); err != nil {
		t.Fatal(err)
	}

	root := filepath.Join(home, ".claude", "projects")
	allowedTranscript := filepath.Join(root, "allowed", "session-1.jsonl")
	otherTranscript := filepath.Join(root, "other", "session-2.jsonl")
	if err := os.MkdirAll(filepath.Dir(allowedTranscript), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Dir(otherTranscript), 0o700); err != nil {
		t.Fatal(err)
	}
	allowedLine := `{"sessionId":"session-1","cwd":"` + filepath.ToSlash(filepath.Join(allowed, "sub")) + `","type":"user"}`
	otherLine := `{"sessionId":"session-2","cwd":"` + filepath.ToSlash(other) + `","type":"user"}`
	if err := os.WriteFile(allowedTranscript, []byte(allowedLine+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(otherTranscript, []byte(otherLine+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}

	var uploads int
	httpClient := &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		uploads++
		return testResponse(http.StatusNoContent, ""), nil
	})}

	summary, err := Backfill(context.Background(), BackfillOptions{
		Consent: &consent,
		Control: &LocalControlClient{
			BaseURL: "http://local.control",
			Client:  httpClient,
		},
		RootDir: root,
	})
	if err != nil {
		t.Fatalf("Backfill: %v", err)
	}
	if summary.Scanned != 2 || summary.Matched != 1 || summary.Uploaded != 1 || summary.Skipped != 1 {
		t.Fatalf("summary: %+v", summary)
	}
	if uploads != 1 {
		t.Fatalf("uploads: got %d, want 1", uploads)
	}

	summary, err = Backfill(context.Background(), BackfillOptions{
		Consent: &consent,
		Control: &LocalControlClient{
			BaseURL: "http://local.control",
			Client:  httpClient,
		},
		RootDir: root,
	})
	if err != nil {
		t.Fatalf("Backfill second: %v", err)
	}
	if summary.Uploaded != 0 {
		t.Fatalf("second backfill should be deduped by offsets, summary: %+v", summary)
	}
}

func mustJSON(t *testing.T, v any) string {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) {
	return f(r)
}

func testResponse(status int, body string) *http.Response {
	return &http.Response{
		StatusCode: status,
		Header:     make(http.Header),
		Body:       io.NopCloser(strings.NewReader(body)),
	}
}

func testJSONResponse(t *testing.T, v any) *http.Response {
	t.Helper()
	return testResponse(http.StatusOK, mustJSON(t, v))
}

type testRecorder struct {
	Code   int
	header http.Header
	body   strings.Builder
}

func newTestRecorder() *testRecorder {
	return &testRecorder{Code: http.StatusOK, header: make(http.Header)}
}

func (r *testRecorder) Header() http.Header {
	return r.header
}

func (r *testRecorder) Write(b []byte) (int, error) {
	return r.body.Write(b)
}

func (r *testRecorder) WriteHeader(statusCode int) {
	r.Code = statusCode
}

func (r *testRecorder) BodyText() string {
	return r.body.String()
}
