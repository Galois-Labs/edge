// Package installid manages the per-machine install identifier used to
// derive stable subject keys for Claude Code ingestion (and any other
// future per-edge identity needs that should outlive hostname changes).
//
// The ID is a UUIDv4 generated once at first daemon registration. It is
// persisted under the daemon's system config dir
// (config.SystemConfigDir() + "/install-id"). The system file is created
// world-readable so user-context processes (CLI, Claude Code hooks) can
// read it without needing to query the daemon.
//
// If the system file does not exist at read time — for example, when the
// CLI is invoked before galois-edge setup has ever run, or when the user
// lacks permission to read the system file — the package falls back to a
// per-user file under config.UserConfigDir() + "/install-id". The
// fallback is documented and stable per user, but loses the per-machine
// invariant. Cloud subject keys derived from a fallback ID won't match
// keys derived from the system ID; this is an acceptable degradation
// because the cloud treats subject keys as opaque, and consent records
// are tied to whichever key was used at consent time.
package installid

import (
	"crypto/rand"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/galois-labs/edge/internal/config"
)

// fileName is the basename for both the system and per-user install ID
// files. Kept in one place so a future rename only touches this constant.
const fileName = "install-id"

// SystemPath returns the absolute path to the system install ID file.
func SystemPath() string {
	return filepath.Join(config.SystemConfigDir(), fileName)
}

// UserPath returns the absolute path to the per-user fallback install ID
// file.
func UserPath() string {
	return filepath.Join(config.UserConfigDir(), fileName)
}

// Read returns the install ID, preferring the system file and falling
// back to the per-user file. If neither exists, Read returns
// os.ErrNotExist.
func Read() (string, error) {
	if id, err := readFile(SystemPath()); err == nil {
		return id, nil
	} else if !errors.Is(err, os.ErrNotExist) {
		// Permission errors are common when a user-context process
		// cannot read the system file; fall through to user fallback.
		// Other unexpected errors are reported.
		if !os.IsPermission(err) {
			return "", err
		}
	}
	if id, err := readFile(UserPath()); err == nil {
		return id, nil
	} else if !errors.Is(err, os.ErrNotExist) {
		return "", err
	}
	return "", os.ErrNotExist
}

// Ensure returns the install ID, generating and persisting one if
// neither the system nor user file exists. The system file is preferred
// when the caller has permission; otherwise the per-user file is used.
//
// Callers running as the daemon (during setup or first start) should be
// able to write the system file. CLI/hook callers running as a regular
// user typically can't write /etc and will create the per-user file
// instead. Either way Ensure is idempotent.
func Ensure() (string, error) {
	if id, err := Read(); err == nil {
		return id, nil
	} else if !errors.Is(err, os.ErrNotExist) {
		return "", err
	}

	id, err := newUUIDv4()
	if err != nil {
		return "", err
	}

	if err := writeFile(SystemPath(), id, 0o755, 0o644); err == nil {
		return id, nil
	}
	// System write failed (perms). Fall back to user.
	if err := writeFile(UserPath(), id, 0o700, 0o600); err != nil {
		return "", fmt.Errorf("persist install id: %w", err)
	}
	return id, nil
}

// readFile reads and validates an install ID from the given path.
func readFile(path string) (string, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}
	id := strings.TrimSpace(string(b))
	if !looksLikeUUID(id) {
		return "", fmt.Errorf("install id at %s does not look like a UUID", path)
	}
	return id, nil
}

// writeFile creates parent dir and writes id with platform-appropriate
// modes. On Windows the mode arguments are advisory; the file inherits
// the parent ACL.
func writeFile(path, id string, dirMode, fileMode os.FileMode) error {
	if err := os.MkdirAll(filepath.Dir(path), dirMode); err != nil {
		return err
	}
	return os.WriteFile(path, []byte(id+"\n"), fileMode)
}

// newUUIDv4 generates a v4 UUID using crypto/rand and the standard
// 8-4-4-4-12 hex layout with version (0x40) and variant (0x80) bits set.
// We avoid the google/uuid dependency to keep the import graph minimal.
func newUUIDv4() (string, error) {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", err
	}
	b[6] = (b[6] & 0x0f) | 0x40 // version 4
	b[8] = (b[8] & 0x3f) | 0x80 // variant RFC 4122
	return fmt.Sprintf(
		"%08x-%04x-%04x-%04x-%012x",
		b[0:4], b[4:6], b[6:8], b[8:10], b[10:16],
	), nil
}

// looksLikeUUID is a cheap shape check: 36 chars, hex+hyphens in 8-4-4-4-12.
// Strict RFC validation is not needed — we just guard against truncated or
// junk reads, not maliciously crafted IDs (which would only hurt the
// attacker's own subject_key).
func looksLikeUUID(s string) bool {
	if len(s) != 36 {
		return false
	}
	parts := strings.Split(s, "-")
	if len(parts) != 5 {
		return false
	}
	wants := []int{8, 4, 4, 4, 12}
	for i, p := range parts {
		if len(p) != wants[i] {
			return false
		}
		for _, c := range p {
			if !(c >= '0' && c <= '9' || c >= 'a' && c <= 'f' || c >= 'A' && c <= 'F') {
				return false
			}
		}
	}
	return true
}
