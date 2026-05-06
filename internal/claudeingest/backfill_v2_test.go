package claudeingest

import (
	"context"
	"net/http"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestBackfillSkipsTranscriptsWithoutCWD(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("XDG_CONFIG_HOME", "")
	t.Setenv("APPDATA", filepath.Join(home, "AppData", "Roaming"))
	t.Setenv("USER", "tester")

	allowed := filepath.Join(home, "work", "repo")
	if err := os.MkdirAll(allowed, 0o700); err != nil {
		t.Fatal(err)
	}
	subject, err := LocalSubject()
	if err != nil {
		t.Fatal(err)
	}
	consent := NewConsent(subject, []string{allowed}, time.Now())

	root := filepath.Join(home, ".claude", "projects")
	tx := filepath.Join(root, "no-cwd", "session.jsonl")
	if err := os.MkdirAll(filepath.Dir(tx), 0o700); err != nil {
		t.Fatal(err)
	}
	// No cwd field at all.
	if err := os.WriteFile(tx, []byte(`{"uuid":"a","type":"user"}`+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}

	httpClient := &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		return testResponse(http.StatusNoContent, ""), nil
	})}
	summary, err := Backfill(context.Background(), BackfillOptions{
		Consent: &consent,
		Control: &LocalControlClient{BaseURL: "http://local", Client: httpClient},
		RootDir: root,
	})
	if err != nil {
		t.Fatalf("Backfill: %v", err)
	}
	if summary.Uploaded != 0 {
		t.Errorf("uploaded: got %d want 0", summary.Uploaded)
	}
	if summary.SkipsBy[SkipNoCWD] != 1 {
		t.Errorf("expected 1 no-cwd skip, summary=%+v", summary)
	}
}

func TestBackfillEncodedNameCollisionNoLongerLeaks(t *testing.T) {
	// Reviewer S2: a transcript whose encoded directory name happens
	// to start with the consented folder's encoding (e.g.,
	// /Users/alex/work/galois consents but
	// /Users/alex/work/galois-customer-x leaks). v2 has dropped the
	// fallback, so transcripts whose only signal is the encoded dir
	// name are skipped with SkipNoCWD instead of forged-as-allowed.
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("XDG_CONFIG_HOME", "")
	t.Setenv("APPDATA", filepath.Join(home, "AppData", "Roaming"))
	t.Setenv("USER", "tester")

	allowed := filepath.Join(home, "work", "galois")
	if err := os.MkdirAll(allowed, 0o700); err != nil {
		t.Fatal(err)
	}
	subject, err := LocalSubject()
	if err != nil {
		t.Fatal(err)
	}
	consent := NewConsent(subject, []string{allowed}, time.Now())

	root := filepath.Join(home, ".claude", "projects")
	// Encoded dir of /Users/alex/work/galois-customer-x — would have
	// matched the v1 prefix-fallback against /Users/alex/work/galois.
	encodedDir := filepath.Join(root, "-Users-tester-work-galois-customer-x")
	tx := filepath.Join(encodedDir, "session.jsonl")
	if err := os.MkdirAll(encodedDir, 0o700); err != nil {
		t.Fatal(err)
	}
	// No cwd field — v1 would have fallen back; v2 must skip.
	if err := os.WriteFile(tx, []byte(`{"uuid":"a","type":"user"}`+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}

	httpClient := &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		return testResponse(http.StatusNoContent, ""), nil
	})}
	summary, err := Backfill(context.Background(), BackfillOptions{
		Consent: &consent,
		Control: &LocalControlClient{BaseURL: "http://local", Client: httpClient},
		RootDir: root,
	})
	if err != nil {
		t.Fatalf("Backfill: %v", err)
	}
	if summary.Uploaded != 0 {
		t.Errorf("collision case uploaded %d events; v2 must skip", summary.Uploaded)
	}
	if summary.SkipsBy[SkipNoCWD] == 0 {
		t.Errorf("expected SkipNoCWD count, summary=%+v", summary)
	}
}

func TestBackfillExcludeGlobAppliesAtBackfillTime(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("XDG_CONFIG_HOME", "")
	t.Setenv("APPDATA", filepath.Join(home, "AppData", "Roaming"))
	t.Setenv("USER", "tester")

	allowed := filepath.Join(home, "work", "repo")
	if err := os.MkdirAll(allowed, 0o700); err != nil {
		t.Fatal(err)
	}
	subject, err := LocalSubject()
	if err != nil {
		t.Fatal(err)
	}
	consent := NewConsentWithOptions(
		subject,
		[]string{allowed},
		time.Now(),
		ConsentOptions{ExcludeGlobs: []string{"**/secrets/**"}},
	)

	root := filepath.Join(home, ".claude", "projects")
	excludedTranscript := filepath.Join(root, "x", "session.jsonl")
	if err := os.MkdirAll(filepath.Dir(excludedTranscript), 0o700); err != nil {
		t.Fatal(err)
	}
	excludedCWD := filepath.Join(allowed, "secrets", "deep")
	line := `{"uuid":"a","sessionId":"s","cwd":"` + filepath.ToSlash(excludedCWD) + `","type":"user"}`
	if err := os.WriteFile(excludedTranscript, []byte(line+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}

	httpClient := &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		return testResponse(http.StatusNoContent, ""), nil
	})}
	summary, err := Backfill(context.Background(), BackfillOptions{
		Consent: &consent,
		Control: &LocalControlClient{BaseURL: "http://local", Client: httpClient},
		RootDir: root,
	})
	if err != nil {
		t.Fatalf("Backfill: %v", err)
	}
	if summary.Uploaded != 0 {
		t.Errorf("exclude-glob match should not upload, got %d", summary.Uploaded)
	}
	if summary.SkipsBy[SkipExcludeGlob] != 1 {
		t.Errorf("expected SkipExcludeGlob, summary=%+v", summary)
	}
}

func TestBackfillCancellationStopsWalk(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("XDG_CONFIG_HOME", "")
	t.Setenv("APPDATA", filepath.Join(home, "AppData", "Roaming"))
	t.Setenv("USER", "tester")

	allowed := filepath.Join(home, "work", "repo")
	if err := os.MkdirAll(allowed, 0o700); err != nil {
		t.Fatal(err)
	}
	subject, err := LocalSubject()
	if err != nil {
		t.Fatal(err)
	}
	consent := NewConsent(subject, []string{allowed}, time.Now())

	root := filepath.Join(home, ".claude", "projects")
	if err := os.MkdirAll(filepath.Join(root, "x"), 0o700); err != nil {
		t.Fatal(err)
	}
	// Make a few transcripts so the walk has something to do.
	for i := 0; i < 5; i++ {
		path := filepath.Join(root, "x", "session-"+string(rune('a'+i))+".jsonl")
		line := `{"uuid":"u-` + string(rune('a'+i)) + `","sessionId":"s","cwd":"` + filepath.ToSlash(allowed) + `","type":"user"}`
		if err := os.WriteFile(path, []byte(line+"\n"), 0o600); err != nil {
			t.Fatal(err)
		}
	}

	cancel := make(chan struct{})
	close(cancel) // pre-cancelled

	httpClient := &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		return testResponse(http.StatusNoContent, ""), nil
	})}
	summary, err := Backfill(context.Background(), BackfillOptions{
		Consent: &consent,
		Control: &LocalControlClient{BaseURL: "http://local", Client: httpClient},
		RootDir: root,
		Cancel:  cancel,
	})
	if err != nil {
		t.Fatalf("Backfill: %v", err)
	}
	if !summary.Cancelled {
		t.Errorf("expected Cancelled=true, got summary=%+v", summary)
	}
	if summary.Uploaded != 0 {
		t.Errorf("pre-cancelled walk should not upload, got %d", summary.Uploaded)
	}
}
