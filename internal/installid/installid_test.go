package installid

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestNewUUIDv4Shape(t *testing.T) {
	for i := 0; i < 32; i++ {
		id, err := newUUIDv4()
		if err != nil {
			t.Fatalf("newUUIDv4: %v", err)
		}
		if !looksLikeUUID(id) {
			t.Errorf("not UUID-shaped: %q", id)
		}
		// version + variant bits
		parts := strings.Split(id, "-")
		if !strings.HasPrefix(parts[2], "4") {
			t.Errorf("version nibble not 4: %q", id)
		}
		v := parts[3][0]
		if v != '8' && v != '9' && v != 'a' && v != 'b' {
			t.Errorf("variant nibble not RFC-4122: %q", id)
		}
	}
}

func TestLooksLikeUUID(t *testing.T) {
	cases := []struct {
		in   string
		want bool
	}{
		{"00000000-0000-4000-8000-000000000000", true},
		{"abcdef01-2345-4abc-9def-0123456789ab", true},
		{"too-short", false},
		{"00000000_0000_4000_8000_000000000000", false},
		{"gggggggg-0000-4000-8000-000000000000", false},
		{"00000000-0000-4000-8000-00000000000Z", false},
		{"", false},
	}
	for _, c := range cases {
		if got := looksLikeUUID(c.in); got != c.want {
			t.Errorf("looksLikeUUID(%q): got %v want %v", c.in, got, c.want)
		}
	}
}

func TestEnsureUserFallback(t *testing.T) {
	// Force HOME so config.UserConfigDir() points into a temp dir.
	tmp := t.TempDir()
	t.Setenv("HOME", tmp)
	t.Setenv("XDG_CONFIG_HOME", "")
	t.Setenv("APPDATA", filepath.Join(tmp, "AppData", "Roaming"))

	// Make the system path unwritable by giving it a non-existent
	// parent that we cannot create. We can't easily simulate /etc on a
	// dev box, so we write our own probe: the user path round-trips.
	// SystemPath() is /etc/galois-edge/install-id which the test
	// process likely cannot write — that's the point of the fallback.
	id, err := Ensure()
	if err != nil {
		t.Fatalf("Ensure: %v", err)
	}
	if !looksLikeUUID(id) {
		t.Fatalf("not UUID-shaped: %q", id)
	}

	// Read should return the same value next time, from whichever
	// path Ensure wrote.
	id2, err := Read()
	if err != nil {
		t.Fatalf("Read: %v", err)
	}
	if id != id2 {
		t.Errorf("Read != Ensure: %q vs %q", id2, id)
	}
}

func TestReadNotFound(t *testing.T) {
	tmp := t.TempDir()
	t.Setenv("HOME", tmp)
	t.Setenv("XDG_CONFIG_HOME", "")
	t.Setenv("APPDATA", filepath.Join(tmp, "AppData", "Roaming"))

	// Override SystemPath via private write of a stub: easiest is to
	// just confirm Read returns ErrNotExist when no file exists in
	// either location (we know /etc/galois-edge/install-id likely
	// doesn't exist on a dev box without the daemon installed).
	if _, err := os.Stat(SystemPath()); err == nil {
		t.Skip("system install id present; cannot test not-found path")
	}
	_, err := Read()
	if !errors.Is(err, os.ErrNotExist) {
		t.Errorf("Read with no files: got %v want os.ErrNotExist", err)
	}
}
