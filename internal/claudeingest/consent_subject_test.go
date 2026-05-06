package claudeingest

import (
	"crypto/sha256"
	"encoding/hex"
	"path/filepath"
	"strings"
	"testing"
)

func TestLocalSubjectDerivesFromInstallID(t *testing.T) {
	tmp := t.TempDir()
	t.Setenv("HOME", tmp)
	t.Setenv("XDG_CONFIG_HOME", "")
	t.Setenv("APPDATA", filepath.Join(tmp, "AppData", "Roaming"))
	t.Setenv("USER", "testuser")
	t.Setenv("USERNAME", "testuser")

	// First call seeds the install ID via Ensure.
	s1, err := LocalSubject()
	if err != nil {
		t.Fatalf("LocalSubject (first): %v", err)
	}
	if s1.OSUser != "testuser" {
		t.Errorf("OSUser: got %q want %q", s1.OSUser, "testuser")
	}
	if s1.Key == "" || len(s1.Key) != 64 {
		t.Errorf("Key not 64 hex chars: %q", s1.Key)
	}

	// Second call must produce the same key — install_id stable, user same.
	s2, err := LocalSubject()
	if err != nil {
		t.Fatalf("LocalSubject (second): %v", err)
	}
	if s2.Key != s1.Key {
		t.Errorf("Key not stable: %q vs %q", s1.Key, s2.Key)
	}

	// Hash must NOT contain hostname or home dir as inputs (v2 change).
	// We verify by recomputing manually and comparing.
	// Read the install ID we just persisted.
	idBytes, err := readInstallIDForTest(t)
	if err != nil {
		t.Fatalf("read install id: %v", err)
	}
	expectedSum := sha256.Sum256([]byte(strings.Join([]string{idBytes, "testuser"}, "\x00")))
	expectedKey := hex.EncodeToString(expectedSum[:])
	if s1.Key != expectedKey {
		t.Errorf("Key derivation mismatch: got %q want %q", s1.Key, expectedKey)
	}
}

func TestLocalSubjectDifferentUsersDifferentKeys(t *testing.T) {
	tmp := t.TempDir()
	t.Setenv("HOME", tmp)
	t.Setenv("XDG_CONFIG_HOME", "")
	t.Setenv("APPDATA", filepath.Join(tmp, "AppData", "Roaming"))

	t.Setenv("USER", "alice")
	t.Setenv("USERNAME", "alice")
	a, err := LocalSubject()
	if err != nil {
		t.Fatalf("LocalSubject(alice): %v", err)
	}

	t.Setenv("USER", "bob")
	t.Setenv("USERNAME", "bob")
	b, err := LocalSubject()
	if err != nil {
		t.Fatalf("LocalSubject(bob): %v", err)
	}

	if a.Key == b.Key {
		t.Errorf("alice and bob should have different subject keys, both got %q", a.Key)
	}
}
