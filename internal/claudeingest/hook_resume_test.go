package claudeingest

import (
	"context"
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// transcriptLine builds a JSONL event line for tests.
type transcriptLine struct {
	UUID        string `json:"uuid,omitempty"`
	ParentUUID  string `json:"parentUuid,omitempty"`
	IsSidechain bool   `json:"isSidechain,omitempty"`
	CWD         string `json:"cwd,omitempty"`
	Type        string `json:"type,omitempty"`
}

func writeTranscript(t *testing.T, path string, lines ...transcriptLine) {
	t.Helper()
	var buf strings.Builder
	for _, l := range lines {
		b, err := json.Marshal(l)
		if err != nil {
			t.Fatalf("marshal line: %v", err)
		}
		buf.Write(b)
		buf.WriteByte('\n')
	}
	if err := os.WriteFile(path, []byte(buf.String()), 0o600); err != nil {
		t.Fatalf("write transcript: %v", err)
	}
}

// resumeFixture sets up a hook test environment with consent + a runner
// hooked into a fake upload counter.
type resumeFixture struct {
	t            *testing.T
	home         string
	project      string
	transcript   string
	consent      Consent
	uploads      int
	uploadedRaws []json.RawMessage
	postBatches  []EventBatch
	runner       *HookRunner
}

func newResumeFixture(t *testing.T, opts ConsentOptions) *resumeFixture {
	t.Helper()
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("XDG_CONFIG_HOME", "")
	t.Setenv("APPDATA", filepath.Join(home, "AppData", "Roaming"))
	t.Setenv("USER", "tester")
	t.Setenv("USERNAME", "tester")

	project := filepath.Join(home, "work", "repo")
	if err := os.MkdirAll(project, 0o700); err != nil {
		t.Fatalf("mkdir project: %v", err)
	}
	subject, err := LocalSubject()
	if err != nil {
		t.Fatalf("LocalSubject: %v", err)
	}
	consent := NewConsentWithOptions(subject, []string{project}, time.Now().UTC(), opts)
	if err := SaveConsent(consent); err != nil {
		t.Fatalf("SaveConsent: %v", err)
	}

	transcript := filepath.Join(home, "session.jsonl")

	f := &resumeFixture{
		t:          t,
		home:       home,
		project:    project,
		transcript: transcript,
		consent:    consent,
	}
	httpClient := &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		f.uploads++
		body := decodeBatch(t, r)
		f.postBatches = append(f.postBatches, body)
		f.uploadedRaws = append(f.uploadedRaws, body.Lines...)
		return testResponse(http.StatusNoContent, ""), nil
	})}
	f.runner = NewHookRunner()
	f.runner.Control = &LocalControlClient{BaseURL: "http://local.control", Client: httpClient}
	return f
}

func decodeBatch(t *testing.T, r *http.Request) EventBatch {
	t.Helper()
	var b EventBatch
	if err := json.NewDecoder(r.Body).Decode(&b); err != nil {
		t.Fatalf("decode batch: %v", err)
	}
	return b
}

func (f *resumeFixture) hookInput() string {
	return `{"session_id":"s1","transcript_path":"` + f.transcript + `","cwd":"` + f.project + `","hook_event_name":"Stop"}`
}

func (f *resumeFixture) run() {
	f.t.Helper()
	if err := f.runner.Run(context.Background(), strings.NewReader(f.hookInput())); err != nil {
		f.t.Fatalf("Run: %v", err)
	}
}

func (f *resumeFixture) lastBatch() EventBatch {
	if len(f.postBatches) == 0 {
		f.t.Fatalf("no batches uploaded")
	}
	return f.postBatches[len(f.postBatches)-1]
}

func TestResumeFreshSessionUploadsAll(t *testing.T) {
	f := newResumeFixture(t, ConsentOptions{})
	writeTranscript(t, f.transcript,
		transcriptLine{UUID: "a", Type: "user"},
		transcriptLine{UUID: "b", ParentUUID: "a", Type: "assistant"},
	)
	f.run()
	if f.uploads != 1 {
		t.Fatalf("uploads: got %d want 1", f.uploads)
	}
	b := f.lastBatch()
	if b.AnchorUUIDBefore != "" {
		t.Errorf("AnchorUUIDBefore on fresh session should be empty, got %q", b.AnchorUUIDBefore)
	}
	if b.AnchorUUIDAfter != "b" {
		t.Errorf("AnchorUUIDAfter: got %q want %q", b.AnchorUUIDAfter, "b")
	}
	if len(b.Lines) != 2 {
		t.Errorf("Lines: got %d want 2", len(b.Lines))
	}
}

func TestResumeAppendOnlySendsNewEvents(t *testing.T) {
	f := newResumeFixture(t, ConsentOptions{})
	writeTranscript(t, f.transcript,
		transcriptLine{UUID: "a", Type: "user"},
	)
	f.run()
	// Append more events.
	writeTranscript(t, f.transcript,
		transcriptLine{UUID: "a", Type: "user"},
		transcriptLine{UUID: "b", ParentUUID: "a", Type: "assistant"},
		transcriptLine{UUID: "c", ParentUUID: "b", Type: "user"},
	)
	f.run()
	if f.uploads != 2 {
		t.Fatalf("uploads: got %d want 2", f.uploads)
	}
	b := f.lastBatch()
	if b.AnchorUUIDBefore != "a" {
		t.Errorf("AnchorUUIDBefore: got %q want %q", b.AnchorUUIDBefore, "a")
	}
	if b.AnchorUUIDAfter != "c" {
		t.Errorf("AnchorUUIDAfter: got %q want %q", b.AnchorUUIDAfter, "c")
	}
	if len(b.Lines) != 2 {
		t.Errorf("Lines: got %d want 2 (only the new ones)", len(b.Lines))
	}
}

func TestResumeRewindPastAnchorTriggersFullRestream(t *testing.T) {
	f := newResumeFixture(t, ConsentOptions{})
	writeTranscript(t, f.transcript,
		transcriptLine{UUID: "a"},
		transcriptLine{UUID: "b", ParentUUID: "a"},
		transcriptLine{UUID: "c", ParentUUID: "b"},
	)
	f.run()
	// Rewind: file truncated past the anchor (uuid c), now starts
	// from a different chain.
	writeTranscript(t, f.transcript,
		transcriptLine{UUID: "a"},
		transcriptLine{UUID: "x", ParentUUID: "a"}, // diverged
	)
	f.run()
	if f.uploads != 2 {
		t.Fatalf("uploads: got %d want 2", f.uploads)
	}
	b := f.lastBatch()
	if b.AnchorUUIDBefore != "" {
		t.Errorf("rewind should produce empty AnchorUUIDBefore (full re-stream), got %q", b.AnchorUUIDBefore)
	}
	if len(b.Lines) != 2 {
		t.Errorf("full re-stream should ship all 2 lines, got %d", len(b.Lines))
	}
}

func TestResumeCompactionRemovesAnchorTriggersFullRestream(t *testing.T) {
	f := newResumeFixture(t, ConsentOptions{})
	writeTranscript(t, f.transcript,
		transcriptLine{UUID: "a"},
		transcriptLine{UUID: "b", ParentUUID: "a"},
		transcriptLine{UUID: "c", ParentUUID: "b"},
	)
	f.run() // anchor → c

	// Compaction: middle was summarized away, anchor uuid no longer
	// present. New events with new uuids.
	writeTranscript(t, f.transcript,
		transcriptLine{UUID: "summary-1", Type: "compaction"},
		transcriptLine{UUID: "d", ParentUUID: "summary-1"},
	)
	f.run()
	if f.uploads != 2 {
		t.Fatalf("uploads: got %d want 2", f.uploads)
	}
	b := f.lastBatch()
	if b.AnchorUUIDBefore != "" {
		t.Errorf("compaction-removing-anchor should produce empty AnchorUUIDBefore, got %q", b.AnchorUUIDBefore)
	}
	if len(b.Lines) != 2 {
		t.Errorf("full re-stream length: got %d want 2", len(b.Lines))
	}
}

func TestResumeCompactionPreservesAnchorContinuesNormally(t *testing.T) {
	f := newResumeFixture(t, ConsentOptions{})
	writeTranscript(t, f.transcript,
		transcriptLine{UUID: "a"},
		transcriptLine{UUID: "b", ParentUUID: "a"},
		transcriptLine{UUID: "c", ParentUUID: "b"},
	)
	f.run()

	// Compaction kept c as the anchor and appended new events whose
	// chain roots at c. The middle was rewritten but c is still
	// present and consistent.
	writeTranscript(t, f.transcript,
		transcriptLine{UUID: "summary-1", Type: "compaction"},
		transcriptLine{UUID: "c", ParentUUID: "summary-1"},
		transcriptLine{UUID: "d", ParentUUID: "c"},
	)
	f.run()
	if f.uploads != 2 {
		t.Fatalf("uploads: got %d want 2", f.uploads)
	}
	b := f.lastBatch()
	// Anchor c was found; resume from after c. Tail = [d]. Chain check:
	// d.parentUUID == c ✓, fast path.
	if b.AnchorUUIDBefore != "c" {
		t.Errorf("anchor-preserved compaction should resume from c, got %q", b.AnchorUUIDBefore)
	}
	if b.AnchorUUIDAfter != "d" {
		t.Errorf("AnchorUUIDAfter: got %q want d", b.AnchorUUIDAfter)
	}
	if len(b.Lines) != 1 {
		t.Errorf("only post-c events should ship: got %d want 1", len(b.Lines))
	}
}

func TestResumeChainBreakTriggersFullRestream(t *testing.T) {
	f := newResumeFixture(t, ConsentOptions{})
	writeTranscript(t, f.transcript,
		transcriptLine{UUID: "a"},
		transcriptLine{UUID: "b", ParentUUID: "a"},
	)
	f.run() // anchor → b

	// File was rewritten such that b is still present but the next
	// event's parentUUID does NOT match b.
	writeTranscript(t, f.transcript,
		transcriptLine{UUID: "a"},
		transcriptLine{UUID: "b", ParentUUID: "a"},
		transcriptLine{UUID: "c", ParentUUID: "different-anchor"}, // chain break
	)
	f.run()
	b := f.lastBatch()
	if b.AnchorUUIDBefore != "" {
		t.Errorf("chain break should trigger full re-stream, got AnchorUUIDBefore=%q", b.AnchorUUIDBefore)
	}
	if len(b.Lines) != 3 {
		t.Errorf("full re-stream: got %d want 3", len(b.Lines))
	}
}

func TestResumeSidechainFilterDropsSidechainsButAdvancesAnchor(t *testing.T) {
	f := newResumeFixture(t, ConsentOptions{ExcludeSidechains: true})
	writeTranscript(t, f.transcript,
		transcriptLine{UUID: "a"},
		transcriptLine{UUID: "b", ParentUUID: "a", IsSidechain: true},
		transcriptLine{UUID: "c", ParentUUID: "b"},
	)
	f.run()
	if f.uploads != 1 {
		t.Fatalf("uploads: got %d want 1", f.uploads)
	}
	b := f.lastBatch()
	if len(b.Lines) != 2 {
		t.Errorf("Lines: got %d want 2 (sidechain dropped)", len(b.Lines))
	}
	if b.AnchorUUIDAfter != "c" {
		t.Errorf("anchor should advance past filtered events to %q, got %q", "c", b.AnchorUUIDAfter)
	}

	// Second run: nothing new in file, anchor at c → no upload.
	f.run()
	if f.uploads != 1 {
		t.Errorf("uploads after no-new-events second run: got %d want 1", f.uploads)
	}
}

func TestResumeAllSidechainsAdvancesAnchorWithoutUpload(t *testing.T) {
	f := newResumeFixture(t, ConsentOptions{ExcludeSidechains: true})
	writeTranscript(t, f.transcript,
		transcriptLine{UUID: "a", IsSidechain: true},
		transcriptLine{UUID: "b", ParentUUID: "a", IsSidechain: true},
	)
	f.run()
	if f.uploads != 0 {
		t.Errorf("all-sidechain: should not upload, got %d", f.uploads)
	}
	// Anchor should have advanced locally; on subsequent appends
	// the hook resumes from b.
	writeTranscript(t, f.transcript,
		transcriptLine{UUID: "a", IsSidechain: true},
		transcriptLine{UUID: "b", ParentUUID: "a", IsSidechain: true},
		transcriptLine{UUID: "c", ParentUUID: "b"}, // not sidechain
	)
	f.run()
	if f.uploads != 1 {
		t.Errorf("after non-sidechain append: got %d uploads want 1", f.uploads)
	}
	b := f.lastBatch()
	if len(b.Lines) != 1 || b.AnchorUUIDAfter != "c" {
		t.Errorf("resumed batch wrong: got %d lines anchorAfter=%q", len(b.Lines), b.AnchorUUIDAfter)
	}
}

func TestResumePerLineCWDOutsideAllowedDropsLine(t *testing.T) {
	f := newResumeFixture(t, ConsentOptions{})
	other := filepath.Join(f.home, "other-project")
	writeTranscript(t, f.transcript,
		transcriptLine{UUID: "a", CWD: f.project},
		transcriptLine{UUID: "b", ParentUUID: "a", CWD: other},
		transcriptLine{UUID: "c", ParentUUID: "b", CWD: f.project},
	)
	f.run()
	b := f.lastBatch()
	if len(b.Lines) != 2 {
		t.Errorf("per-event CWD filter: got %d lines want 2", len(b.Lines))
	}
}

func TestResumeExcludeGlobAtSessionLevelSkipsHook(t *testing.T) {
	f := newResumeFixture(t, ConsentOptions{
		ExcludeGlobs: []string{"**/repo/**"},
	})
	writeTranscript(t, f.transcript,
		transcriptLine{UUID: "a"},
	)
	f.run()
	if f.uploads != 0 {
		t.Errorf("exclude_glob match on session cwd should skip whole hook, got %d uploads", f.uploads)
	}
}

func TestResumeCredentialRedactorAppliesWhenEnabled(t *testing.T) {
	f := newResumeFixture(t, ConsentOptions{CredentialRedactor: true})
	// One event whose tool input contains an Anthropic-shaped key as
	// a whole string value. JSONL requires single-line records.
	rawWithSecret := `{"uuid":"a","toolInput":{"api_key":"sk-ant-` + strings.Repeat("X", 60) + `"}}`
	if err := os.WriteFile(f.transcript, []byte(rawWithSecret+"\n"), 0o600); err != nil {
		t.Fatalf("write: %v", err)
	}
	f.run()
	if f.uploads != 1 {
		t.Fatalf("uploads: got %d", f.uploads)
	}
	b := f.lastBatch()
	if !strings.Contains(string(b.Lines[0]), "[REDACTED:anthropic-key]") {
		t.Errorf("redacted marker missing: %s", string(b.Lines[0]))
	}
}

func TestResumeCredentialRedactorOffByDefault(t *testing.T) {
	f := newResumeFixture(t, ConsentOptions{}) // redactor off
	rawWithSecret := `{"uuid":"a","toolInput":{"api_key":"sk-ant-` + strings.Repeat("X", 60) + `"}}`
	if err := os.WriteFile(f.transcript, []byte(rawWithSecret+"\n"), 0o600); err != nil {
		t.Fatalf("write: %v", err)
	}
	f.run()
	b := f.lastBatch()
	if strings.Contains(string(b.Lines[0]), "[REDACTED") {
		t.Errorf("default-off redactor should NOT redact, got %s", string(b.Lines[0]))
	}
}

func TestResumeAdvancesAnchorOnlyOnAck(t *testing.T) {
	f := newResumeFixture(t, ConsentOptions{})
	// Switch transport to fail.
	failClient := &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		f.uploads++
		return testResponse(http.StatusBadGateway, "upstream down"), nil
	})}
	f.runner.Control = &LocalControlClient{BaseURL: "http://local.control", Client: failClient}
	writeTranscript(t, f.transcript,
		transcriptLine{UUID: "a"},
		transcriptLine{UUID: "b", ParentUUID: "a"},
	)
	f.run()
	// Recover transport; expect re-stream.
	f.runner.Control = &LocalControlClient{BaseURL: "http://local.control", Client: &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		f.uploads++
		batch := decodeBatch(t, r)
		f.postBatches = append(f.postBatches, batch)
		return testResponse(http.StatusNoContent, ""), nil
	})}}
	f.run()
	if f.uploads != 2 {
		t.Fatalf("uploads (1 fail + 1 success): got %d want 2", f.uploads)
	}
	b := f.lastBatch()
	if len(b.Lines) != 2 {
		t.Errorf("after failure, retry should re-stream all: got %d lines want 2", len(b.Lines))
	}
}
