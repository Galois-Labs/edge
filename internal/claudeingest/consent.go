package claudeingest

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"os/user"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
)

// UserStateDir returns the per-user state directory for Claude ingestion.
func UserStateDir() (string, error) {
	if runtime.GOOS == "windows" {
		base := os.Getenv("APPDATA")
		if base == "" {
			home, err := os.UserHomeDir()
			if err != nil {
				return "", err
			}
			base = filepath.Join(home, "AppData", "Roaming")
		}
		return filepath.Join(base, "galois-edge", "claude-ingest"), nil
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(home, ".config", "galois-edge", "claude-ingest"), nil
}

// ConsentPath returns the local consent JSON path.
func ConsentPath() (string, error) {
	dir, err := UserStateDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(dir, "consent.json"), nil
}

// OffsetsPath returns the local transcript offset JSON path.
func OffsetsPath() (string, error) {
	dir, err := UserStateDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(dir, "offsets.json"), nil
}

// LocalSubject returns the stable local consent subject for the current user.
func LocalSubject() (Subject, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return Subject{}, err
	}
	hostname, _ := os.Hostname()

	osUser := os.Getenv("USER")
	if osUser == "" {
		osUser = os.Getenv("USERNAME")
	}
	if osUser == "" {
		if u, err := user.Current(); err == nil {
			osUser = u.Username
		}
	}
	if osUser == "" {
		osUser = "unknown"
	}

	keyInput := strings.Join([]string{osUser, hostname, home}, "\x00")
	sum := sha256.Sum256([]byte(keyInput))
	return Subject{
		Key:      hex.EncodeToString(sum[:]),
		OSUser:   osUser,
		Hostname: hostname,
		HomeDir:  home,
	}, nil
}

// NormalizeFolders converts user-provided folder arguments into absolute,
// deduplicated paths. Paths do not need to exist; this allows enabling before a
// project checkout is created, but still stores a deterministic absolute scope.
func NormalizeFolders(folders []string) ([]string, error) {
	if len(folders) == 0 {
		return nil, fmt.Errorf("at least one folder is required")
	}

	seen := make(map[string]bool, len(folders))
	out := make([]string, 0, len(folders))
	for _, f := range folders {
		f = strings.TrimSpace(f)
		if f == "" {
			continue
		}
		if strings.HasPrefix(f, "~") {
			home, err := os.UserHomeDir()
			if err != nil {
				return nil, err
			}
			if f == "~" {
				f = home
			} else if strings.HasPrefix(f, "~/") || strings.HasPrefix(f, `~\`) {
				f = filepath.Join(home, f[2:])
			}
		}
		abs, err := filepath.Abs(f)
		if err != nil {
			return nil, err
		}
		abs = filepath.Clean(abs)
		key := normalizePathForCompare(abs)
		if !seen[key] {
			seen[key] = true
			out = append(out, abs)
		}
	}
	if len(out) == 0 {
		return nil, fmt.Errorf("at least one folder is required")
	}
	sort.Strings(out)
	return out, nil
}

// IsPathAllowed reports whether cwd is exactly an allowed folder or beneath
// one. It never treats sibling prefixes as matches.
func IsPathAllowed(cwd string, allowed []string) bool {
	if cwd == "" {
		return false
	}
	abs, err := filepath.Abs(cwd)
	if err != nil {
		return false
	}
	cwdKey := normalizePathForCompare(filepath.Clean(abs))
	for _, folder := range allowed {
		folderAbs, err := filepath.Abs(folder)
		if err != nil {
			continue
		}
		folderKey := normalizePathForCompare(filepath.Clean(folderAbs))
		if cwdKey == folderKey {
			return true
		}
		prefix := folderKey
		if !strings.HasSuffix(prefix, string(filepath.Separator)) {
			prefix += string(filepath.Separator)
		}
		if strings.HasPrefix(cwdKey, prefix) {
			return true
		}
	}
	return false
}

func normalizePathForCompare(path string) string {
	if runtime.GOOS == "windows" {
		return strings.ToLower(path)
	}
	return path
}

// LoadConsent reads local consent. os.ErrNotExist means not configured.
func LoadConsent() (*Consent, error) {
	path, err := ConsentPath()
	if err != nil {
		return nil, err
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var consent Consent
	if err := json.Unmarshal(data, &consent); err != nil {
		return nil, err
	}
	return &consent, nil
}

// SaveConsent writes local consent with user-only permissions.
func SaveConsent(consent Consent) error {
	path, err := ConsentPath()
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	data, err := json.MarshalIndent(consent, "", "  ")
	if err != nil {
		return err
	}
	data = append(data, '\n')
	return os.WriteFile(path, data, 0o600)
}
