package claudeingest

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"sync"
)

// OffsetStore persists the last cloud-acknowledged byte offset for each
// transcript. It is intentionally a small JSON file: hook invocations are
// short-lived and per-user, so a database would add avoidable install friction.
type OffsetStore struct {
	path string
	mu   sync.Mutex
	data map[string]int64
}

// NewOffsetStore opens or initializes an offset store at path.
func NewOffsetStore(path string) (*OffsetStore, error) {
	s := &OffsetStore{
		path: path,
		data: map[string]int64{},
	}
	if b, err := os.ReadFile(path); err == nil && len(b) > 0 {
		if err := json.Unmarshal(b, &s.data); err != nil {
			return nil, err
		}
	} else if err != nil && !os.IsNotExist(err) {
		return nil, err
	}
	return s, nil
}

// Key returns the stable offset key for a session/transcript pair.
func OffsetKey(sessionID, transcriptPath string) string {
	sum := sha256.Sum256([]byte(sessionID + "\x00" + transcriptPath))
	return hex.EncodeToString(sum[:])
}

// Get returns the last acked offset for key.
func (s *OffsetStore) Get(key string) int64 {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.data[key]
}

// Set updates key and flushes the store to disk.
func (s *OffsetStore) Set(key string, offset int64) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.data[key] = offset
	return s.saveLocked()
}

func (s *OffsetStore) saveLocked() error {
	if err := os.MkdirAll(filepath.Dir(s.path), 0o700); err != nil {
		return err
	}
	b, err := json.MarshalIndent(s.data, "", "  ")
	if err != nil {
		return err
	}
	b = append(b, '\n')
	return os.WriteFile(s.path, b, 0o600)
}
