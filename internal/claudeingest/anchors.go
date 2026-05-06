package claudeingest

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sync"
	"time"
)

// Anchor records the resume position for one Claude Code session as seen
// by this client.
//
// The authoritative resume signal is LastAckedUUID — the per-event uuid
// of the most recent line the cloud has acknowledged. The hook locates
// that uuid in the transcript and resumes from the next line, validating
// the parentUuid chain. UUID-based resume is robust across rewinds,
// compaction, and in-place rewrites where pure offset+hash schemes fail
// silently.
//
// LastAckedOffset is a best-effort optimization: when the file appears
// untouched at startup of a hook fire, the hook may seek directly to
// this offset to avoid a linear UUID scan over a large transcript. If
// the optimization disagrees with the UUID scan, the UUID scan wins.
//
// UploadedEventCount is informational, used by `claude status` and
// telemetry; it is never used as a dedup key.
type Anchor struct {
	LastAckedUUID      string    `json:"last_acked_uuid"`
	LastAckedOffset    int64     `json:"last_acked_offset"`
	UploadedEventCount int       `json:"uploaded_event_count"`
	UpdatedAt          time.Time `json:"updated_at"`
}

// AnchorKey returns the stable storage key for a session/transcript
// pair. Session ID alone is not enough because Claude Code can write the
// same session_id under different transcript paths in pathological
// cases; including the path makes the key truly unambiguous.
func AnchorKey(sessionID, transcriptPath string) string {
	sum := sha256.Sum256([]byte(sessionID + "\x00" + transcriptPath))
	return hex.EncodeToString(sum[:])
}

// AnchorsPath returns the local anchor store path under the user's
// claude-ingest state directory.
func AnchorsPath() (string, error) {
	dir, err := UserStateDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(dir, "anchors.json"), nil
}

// AnchorStore persists per-session resume anchors to a single JSON file.
// Reads are unsynchronized; writes hold a process-level mutex AND an OS
// file lock so concurrent hook invocations do not clobber each other's
// anchor advances. On lock acquisition failure a Set returns an error;
// callers treat that as "do not advance" and rely on the next hook fire
// to retry — by then the cloud has already deduped the prior upload
// idempotently by uuid.
type AnchorStore struct {
	path string
	mu   sync.Mutex
	data map[string]Anchor
}

// NewAnchorStore opens or initializes an anchor store at path.
func NewAnchorStore(path string) (*AnchorStore, error) {
	s := &AnchorStore{path: path, data: map[string]Anchor{}}
	b, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return s, nil
		}
		return nil, err
	}
	if len(b) == 0 {
		return s, nil
	}
	if err := json.Unmarshal(b, &s.data); err != nil {
		// Tolerate corrupt store: log via error, treat as empty so
		// the hook re-streams cleanly. Any prior anchors are lost
		// but the cloud's uuid dedup absorbs the redundant upload.
		return s, fmt.Errorf("anchor store at %s is corrupt: %w", path, err)
	}
	return s, nil
}

// Get returns the anchor for key. Zero Anchor means no prior anchor.
func (s *AnchorStore) Get(key string) Anchor {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.data[key]
}

// Set updates key and atomically rewrites the underlying file under an
// OS-level file lock so concurrent hook processes serialize their
// read-modify-write cycles.
func (s *AnchorStore) Set(key string, anchor Anchor) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if anchor.UpdatedAt.IsZero() {
		anchor.UpdatedAt = time.Now().UTC()
	}

	if err := os.MkdirAll(filepath.Dir(s.path), 0o700); err != nil {
		return err
	}

	// Acquire OS-level lock on a sidecar lockfile. Locking the JSON
	// file directly would race with the rename-over-tempfile pattern
	// that re-creates the inode each write. The sidecar's inode is
	// stable.
	lockPath := s.path + ".lock"
	release, err := acquireFileLock(lockPath)
	if err != nil {
		return fmt.Errorf("acquire anchor lock: %w", err)
	}
	defer release()

	// Re-read on disk under the lock so we merge with whatever
	// concurrent writers have already committed since we opened.
	if b, err := os.ReadFile(s.path); err == nil && len(b) > 0 {
		merged := map[string]Anchor{}
		if err := json.Unmarshal(b, &merged); err == nil {
			for k, v := range merged {
				if _, ours := s.data[k]; !ours {
					s.data[k] = v
				}
			}
		}
	}
	s.data[key] = anchor

	return s.writeAtomicLocked()
}

// writeAtomicLocked renders s.data to disk via tmpfile+rename. The
// caller must hold both the in-process mutex and the OS file lock.
func (s *AnchorStore) writeAtomicLocked() error {
	tmp, err := os.CreateTemp(filepath.Dir(s.path), ".anchors-*.json")
	if err != nil {
		return err
	}
	tmpPath := tmp.Name()
	defer func() {
		_ = os.Remove(tmpPath) // best-effort cleanup if rename failed
	}()

	enc := json.NewEncoder(tmp)
	enc.SetIndent("", "  ")
	if err := enc.Encode(s.data); err != nil {
		_ = tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		_ = tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	// Restrictive mode on the final file.
	if err := os.Chmod(tmpPath, 0o600); err != nil {
		return err
	}
	return os.Rename(tmpPath, s.path)
}

// All returns a copy of every anchor in the store. Used by status and
// tests; not on the hot path.
func (s *AnchorStore) All() map[string]Anchor {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make(map[string]Anchor, len(s.data))
	for k, v := range s.data {
		out[k] = v
	}
	return out
}

// Reset removes the anchor for key and persists. Used when a session is
// purged from local state (e.g., after `claude disable --purge-local`).
func (s *AnchorStore) Reset(key string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.data[key]; !ok {
		return nil
	}
	delete(s.data, key)
	lockPath := s.path + ".lock"
	release, err := acquireFileLock(lockPath)
	if err != nil {
		return err
	}
	defer release()
	return s.writeAtomicLocked()
}

// PurgeAll deletes every anchor. Used by `claude disable --purge-local`.
func (s *AnchorStore) PurgeAll() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.data = map[string]Anchor{}
	if _, err := os.Stat(s.path); err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		return err
	}
	return os.Remove(s.path)
}

// ----------------------------------------------------------------------
// Compatibility shim: legacy OffsetStore-style API, kept only so the
// existing offsets.go callers (hook.go, backfill.go) compile during the
// staged migration. The hook resume rewrite (#27) and backfill rewrite
// (#31) replace these calls with proper anchor semantics.

// OffsetKey is the legacy alias for AnchorKey.
//
// Deprecated: use AnchorKey directly.
func OffsetKey(sessionID, transcriptPath string) string {
	return AnchorKey(sessionID, transcriptPath)
}

// OffsetStore is a thin wrapper that exposes the legacy int64-only
// Get/Set surface on top of AnchorStore. Will be removed once the hook
// and backfill rewrites complete.
//
// Deprecated: use AnchorStore.
type OffsetStore struct {
	inner *AnchorStore
}

// NewOffsetStore opens an AnchorStore and returns the legacy adapter.
//
// Deprecated: use NewAnchorStore.
func NewOffsetStore(path string) (*OffsetStore, error) {
	inner, err := NewAnchorStore(path)
	if err != nil {
		return nil, err
	}
	return &OffsetStore{inner: inner}, nil
}

// Get returns the legacy int64 offset.
//
// Deprecated: use AnchorStore.Get and inspect the Anchor record.
func (s *OffsetStore) Get(key string) int64 {
	return s.inner.Get(key).LastAckedOffset
}

// Set persists the legacy int64 offset, leaving the UUID anchor blank.
//
// Deprecated: use AnchorStore.Set with a full Anchor.
func (s *OffsetStore) Set(key string, offset int64) error {
	prev := s.inner.Get(key)
	prev.LastAckedOffset = offset
	prev.UpdatedAt = time.Now().UTC()
	return s.inner.Set(key, prev)
}

// readJSONFile is a small helper used by tests.
func readJSONFile(path string, dst any) error {
	b, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	if len(b) == 0 {
		return io.EOF
	}
	return json.Unmarshal(b, dst)
}
