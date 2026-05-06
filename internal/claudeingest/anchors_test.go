package claudeingest

import (
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"
)

func newTestAnchorStore(t *testing.T) (*AnchorStore, string) {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "anchors.json")
	s, err := NewAnchorStore(path)
	if err != nil {
		t.Fatalf("NewAnchorStore: %v", err)
	}
	return s, path
}

func TestAnchorStoreEmptyOnNewFile(t *testing.T) {
	s, _ := newTestAnchorStore(t)
	a := s.Get("missing")
	if a.LastAckedUUID != "" || a.LastAckedOffset != 0 || a.UploadedEventCount != 0 {
		t.Errorf("missing key should be zero Anchor, got %+v", a)
	}
}

func TestAnchorStoreRoundTrip(t *testing.T) {
	s, path := newTestAnchorStore(t)
	now := time.Now().UTC().Truncate(time.Second)
	a := Anchor{
		LastAckedUUID:      "uuid-1",
		LastAckedOffset:    1234,
		UploadedEventCount: 5,
		UpdatedAt:          now,
	}
	if err := s.Set("k", a); err != nil {
		t.Fatalf("Set: %v", err)
	}
	got := s.Get("k")
	if got.LastAckedUUID != "uuid-1" || got.LastAckedOffset != 1234 || got.UploadedEventCount != 5 {
		t.Errorf("Get after Set: got %+v want %+v", got, a)
	}

	// New AnchorStore reading the same file sees the same record.
	s2, err := NewAnchorStore(path)
	if err != nil {
		t.Fatalf("reopen: %v", err)
	}
	got2 := s2.Get("k")
	if got2.LastAckedUUID != "uuid-1" {
		t.Errorf("after reopen: got %+v", got2)
	}
}

func TestAnchorStoreSetFillsUpdatedAt(t *testing.T) {
	s, _ := newTestAnchorStore(t)
	if err := s.Set("k", Anchor{LastAckedUUID: "u"}); err != nil {
		t.Fatalf("Set: %v", err)
	}
	a := s.Get("k")
	if a.UpdatedAt.IsZero() {
		t.Errorf("UpdatedAt should be auto-filled when zero")
	}
}

func TestAnchorStoreReset(t *testing.T) {
	s, _ := newTestAnchorStore(t)
	_ = s.Set("k1", Anchor{LastAckedUUID: "u1"})
	_ = s.Set("k2", Anchor{LastAckedUUID: "u2"})
	if err := s.Reset("k1"); err != nil {
		t.Fatalf("Reset: %v", err)
	}
	if got := s.Get("k1"); got.LastAckedUUID != "" {
		t.Errorf("after Reset, k1 should be zero, got %+v", got)
	}
	if got := s.Get("k2"); got.LastAckedUUID != "u2" {
		t.Errorf("k2 should survive: %+v", got)
	}
}

func TestAnchorStorePurgeAll(t *testing.T) {
	s, path := newTestAnchorStore(t)
	_ = s.Set("k1", Anchor{LastAckedUUID: "u1"})
	if err := s.PurgeAll(); err != nil {
		t.Fatalf("PurgeAll: %v", err)
	}
	if got := s.Get("k1"); got.LastAckedUUID != "" {
		t.Errorf("after PurgeAll, k1 should be zero")
	}
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Errorf("after PurgeAll, file should not exist: stat err = %v", err)
	}
}

func TestAnchorStoreCorruptFileTolerated(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "anchors.json")
	if err := os.WriteFile(path, []byte("{not json"), 0o600); err != nil {
		t.Fatalf("seed corrupt: %v", err)
	}
	s, err := NewAnchorStore(path)
	if err == nil {
		t.Errorf("expected non-nil err on corrupt file (best-effort report)")
	}
	if s == nil {
		t.Fatalf("store should still be usable for fresh writes")
	}
	if err := s.Set("k", Anchor{LastAckedUUID: "u"}); err != nil {
		t.Errorf("Set after corrupt: %v", err)
	}
	if got := s.Get("k").LastAckedUUID; got != "u" {
		t.Errorf("Get after corrupt+Set: got %q want %q", got, "u")
	}
}

func TestAnchorStoreConcurrentSetsDoNotClobber(t *testing.T) {
	s, _ := newTestAnchorStore(t)
	var wg sync.WaitGroup
	keys := []string{"k1", "k2", "k3", "k4", "k5", "k6", "k7", "k8"}
	for _, k := range keys {
		wg.Add(1)
		go func(key string) {
			defer wg.Done()
			_ = s.Set(key, Anchor{
				LastAckedUUID:      "u-" + key,
				UploadedEventCount: 1,
			})
		}(k)
	}
	wg.Wait()
	for _, k := range keys {
		if got := s.Get(k).LastAckedUUID; got != "u-"+k {
			t.Errorf("concurrent Set lost: %q got %q", k, got)
		}
	}
}

func TestAnchorStoreCrossProcessMergeOnSet(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "anchors.json")

	a, err := NewAnchorStore(path)
	if err != nil {
		t.Fatalf("a: %v", err)
	}
	b, err := NewAnchorStore(path)
	if err != nil {
		t.Fatalf("b: %v", err)
	}

	// b commits k1 first.
	if err := b.Set("k1", Anchor{LastAckedUUID: "from-b"}); err != nil {
		t.Fatalf("b set: %v", err)
	}
	// a, which doesn't yet know about k1, commits k2. Merge under
	// the lock should preserve k1 from disk.
	if err := a.Set("k2", Anchor{LastAckedUUID: "from-a"}); err != nil {
		t.Fatalf("a set: %v", err)
	}

	final, err := NewAnchorStore(path)
	if err != nil {
		t.Fatalf("reopen: %v", err)
	}
	if final.Get("k1").LastAckedUUID != "from-b" {
		t.Errorf("k1 lost across processes: %+v", final.Get("k1"))
	}
	if final.Get("k2").LastAckedUUID != "from-a" {
		t.Errorf("k2 lost across processes: %+v", final.Get("k2"))
	}
}

func TestAnchorKeyIsStable(t *testing.T) {
	a := AnchorKey("session-1", "/path/a.jsonl")
	b := AnchorKey("session-1", "/path/a.jsonl")
	if a != b {
		t.Errorf("AnchorKey not deterministic: %q vs %q", a, b)
	}
	c := AnchorKey("session-1", "/path/b.jsonl")
	if a == c {
		t.Errorf("AnchorKey collision across paths: %q", a)
	}
	d := AnchorKey("session-2", "/path/a.jsonl")
	if a == d {
		t.Errorf("AnchorKey collision across sessions: %q", a)
	}
}
