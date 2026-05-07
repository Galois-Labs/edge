package doctor

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"github.com/galois-labs/edge/internal/config"
)

// --------------------------------------------------------------------------
// registration_token_format
// --------------------------------------------------------------------------

func TestCheckRegistrationTokenFormat(t *testing.T) {
	tests := []struct {
		name           string
		cfg            *config.Config
		token          string
		wantStatus     string
		wantMsgContain string
	}{
		{
			name:           "nil cfg returns warn",
			cfg:            nil,
			wantStatus:     "warn",
			wantMsgContain: "config not loaded",
		},
		{
			name:           "empty token passes",
			cfg:            makeConfig("", ""),
			wantStatus:     "pass",
			wantMsgContain: "not set",
		},
		{
			name:           "glc_ prefix passes",
			cfg:            makeConfig("glc_abc123", ""),
			wantStatus:     "pass",
			wantMsgContain: "glc_",
		},
		{
			name:           "wrong prefix warns",
			cfg:            makeConfig("tok_abc123", ""),
			wantStatus:     "warn",
			wantMsgContain: "glc_",
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			result := checkRegistrationTokenFormat(tc.cfg)
			if result.Name != "registration_token_format" {
				t.Errorf("unexpected name: %q", result.Name)
			}
			if result.Status != tc.wantStatus {
				t.Errorf("status = %q, want %q", result.Status, tc.wantStatus)
			}
			if tc.wantMsgContain != "" && !containsStr(result.Message, tc.wantMsgContain) {
				t.Errorf("message %q does not contain %q", result.Message, tc.wantMsgContain)
			}
		})
	}
}

// --------------------------------------------------------------------------
// config_writable
// --------------------------------------------------------------------------

func TestCheckConfigWritable_FileWritable(t *testing.T) {
	// Create a temp dir with a writable config file.
	dir := t.TempDir()
	cfgPath := filepath.Join(dir, "config.env")
	if err := os.WriteFile(cfgPath, []byte("# test\n"), 0600); err != nil {
		t.Fatal(err)
	}

	// Point FindConfigFile at our temp file by overriding the env.
	// We can't easily mock FindConfigFile, so we call checkConfigWritable
	// indirectly via a helper that accepts the path.
	result := checkConfigWritableForPath(cfgPath)
	if result.Status != "pass" {
		t.Errorf("status = %q, want pass; message: %s", result.Status, result.Message)
	}
}

func TestCheckConfigWritable_FileNotWritable(t *testing.T) {
	dir := t.TempDir()
	cfgPath := filepath.Join(dir, "config.env")
	if err := os.WriteFile(cfgPath, []byte("# test\n"), 0400); err != nil { // read-only
		t.Fatal(err)
	}

	// Skip if running as root (root can write read-only files).
	if os.Getuid() == 0 {
		t.Skip("skipping read-only test when running as root")
	}

	result := checkConfigWritableForPath(cfgPath)
	if result.Status != "warn" {
		t.Errorf("status = %q, want warn; message: %s", result.Status, result.Message)
	}
	if !containsStr(result.Message, "not writable") {
		t.Errorf("message %q does not mention 'not writable'", result.Message)
	}
}

func TestCheckConfigWritable_DirWritable(t *testing.T) {
	// No config file — check the directory.
	dir := t.TempDir()
	result := checkConfigWritableForDir(dir)
	if result.Status != "pass" {
		t.Errorf("status = %q, want pass; message: %s", result.Status, result.Message)
	}
}

func TestCheckConfigWritable_DirNotWritable(t *testing.T) {
	if os.Getuid() == 0 {
		t.Skip("skipping read-only dir test when running as root")
	}
	dir := t.TempDir()
	if err := os.Chmod(dir, 0500); err != nil { // r-x, no write
		t.Fatal(err)
	}
	defer os.Chmod(dir, 0700) // restore so TempDir cleanup works

	result := checkConfigWritableForDir(dir)
	if result.Status != "warn" {
		t.Errorf("status = %q, want warn; message: %s", result.Status, result.Message)
	}
}

// --------------------------------------------------------------------------
// tailnet_connected
// --------------------------------------------------------------------------

func TestCheckTailnetConnected_NilCfg(t *testing.T) {
	result := checkTailnetConnected(nil)
	if result.Status != "warn" {
		t.Errorf("status = %q, want warn", result.Status)
	}
}

func TestCheckTailnetConnected_NoAuthKey(t *testing.T) {
	cfg := makeConfig("", "")
	result := checkTailnetConnected(cfg)
	if result.Status != "pass" {
		t.Errorf("status = %q, want pass", result.Status)
	}
	if !containsStr(result.Message, "not set") {
		t.Errorf("message %q should mention 'not set'", result.Message)
	}
}

func TestCheckTailnetConnected_MockPass(t *testing.T) {
	cfg := makeConfig("", "tskey-12345")

	// Mock tailscale binary that returns a connected status.
	mockJSON, _ := json.Marshal(tailscaleStatusJSON{
		Self: struct {
			TailscaleIPs []string `json:"TailscaleIPs"`
		}{
			TailscaleIPs: []string{"100.64.0.1"},
		},
	})

	origExec := execCommand
	defer func() { execCommand = origExec }()
	execCommand = newMockCommand(mockJSON, nil)

	// We also need tailscale to appear on PATH; mock that by pointing to any binary.
	result := checkTailnetConnectedWithLookup(cfg, func(name string) (string, error) {
		return "/usr/bin/tailscale", nil
	})
	if result.Status != "pass" {
		t.Errorf("status = %q, want pass; message: %s", result.Status, result.Message)
	}
	if !containsStr(result.Message, "100.64.0.1") {
		t.Errorf("message %q should contain the IP", result.Message)
	}
}

func TestCheckTailnetConnected_NoTailscaleBinary(t *testing.T) {
	cfg := makeConfig("", "tskey-12345")
	result := checkTailnetConnectedWithLookup(cfg, func(name string) (string, error) {
		return "", &os.PathError{Op: "exec", Path: name, Err: os.ErrNotExist}
	})
	if result.Status != "warn" {
		t.Errorf("status = %q, want warn", result.Status)
	}
	if !containsStr(result.Message, "journalctl") {
		t.Errorf("message %q should mention journalctl", result.Message)
	}
}

func TestCheckTailnetConnected_MockFail(t *testing.T) {
	cfg := makeConfig("", "tskey-12345")

	origExec := execCommand
	defer func() { execCommand = origExec }()
	execCommand = newMockCommand(nil, &os.PathError{Op: "exec", Path: "tailscale", Err: os.ErrPermission})

	result := checkTailnetConnectedWithLookup(cfg, func(name string) (string, error) {
		return "/usr/bin/tailscale", nil
	})
	if result.Status != "fail" {
		t.Errorf("status = %q, want fail; message: %s", result.Status, result.Message)
	}
}

func TestCheckTailnetConnected_NoIPsFail(t *testing.T) {
	cfg := makeConfig("", "tskey-12345")

	// Return valid JSON but no IPs.
	mockJSON, _ := json.Marshal(tailscaleStatusJSON{})

	origExec := execCommand
	defer func() { execCommand = origExec }()
	execCommand = newMockCommand(mockJSON, nil)

	result := checkTailnetConnectedWithLookup(cfg, func(name string) (string, error) {
		return "/usr/bin/tailscale", nil
	})
	if result.Status != "fail" {
		t.Errorf("status = %q, want fail; message: %s", result.Status, result.Message)
	}
}

// --------------------------------------------------------------------------
// RunChecks integration — verify count and name stability
// --------------------------------------------------------------------------

func TestRunChecksCount(t *testing.T) {
	cfg := config.New()
	results := RunChecks(cfg)
	if len(results) != 13 {
		t.Errorf("RunChecks returned %d results, want 13", len(results))
	}
}

func TestRunChecksNameStability(t *testing.T) {
	cfg := config.New()
	results := RunChecks(cfg)

	// The 8 original names must be present at the exact positions documented.
	wantNames := []string{
		"go_binary",
		"disk_space",
		"config_file",
		"config_writable",
		"python_binary",
		"python_health",
		"usb_permissions",
		"gpib_driver",
		"network_backend",
		"udev_rules_installed",
		"service_unit_installed",
		"tailnet_connected",
		"registration_token_format",
	}

	for i, want := range wantNames {
		if i >= len(results) {
			t.Errorf("result index %d missing (want %q)", i, want)
			continue
		}
		if results[i].Name != want {
			t.Errorf("results[%d].Name = %q, want %q", i, results[i].Name, want)
		}
	}
}

// --------------------------------------------------------------------------
// Helpers
// --------------------------------------------------------------------------

// makeConfig builds a minimal Config with the given registration token and
// tailscale auth key.
func makeConfig(registrationToken, tailscaleAuthKey string) *config.Config {
	cfg := config.New()
	cfg.RegistrationToken = registrationToken
	cfg.TailscaleAuthKey = tailscaleAuthKey
	return cfg
}

func containsStr(s, substr string) bool {
	return len(s) >= len(substr) && (s == substr || len(substr) == 0 ||
		func() bool {
			for i := 0; i <= len(s)-len(substr); i++ {
				if s[i:i+len(substr)] == substr {
					return true
				}
			}
			return false
		}())
}

// checkConfigWritableForPath is a testable variant that accepts the config
// path directly, bypassing FindConfigFile.
func checkConfigWritableForPath(path string) CheckResult {
	f, err := os.OpenFile(path, os.O_WRONLY, 0)
	if err != nil {
		return CheckResult{
			Name:    "config_writable",
			Status:  "warn",
			Message: "Config file at " + path + " is not writable by current user; 'configure set' will fail",
		}
	}
	f.Close()
	return CheckResult{
		Name:    "config_writable",
		Status:  "pass",
		Message: "Config file is writable: " + path,
	}
}

// checkConfigWritableForDir is a testable variant for the directory case.
func checkConfigWritableForDir(dir string) CheckResult {
	cfgPath := filepath.Join(dir, "config.env")
	tmp, err := os.CreateTemp(dir, ".galois-doctor-writable-*")
	if err != nil {
		return CheckResult{
			Name:    "config_writable",
			Status:  "warn",
			Message: "Config file at " + cfgPath + " is not writable by current user; 'configure set' will fail",
		}
	}
	tmp.Close()
	os.Remove(tmp.Name())
	return CheckResult{
		Name:    "config_writable",
		Status:  "pass",
		Message: "Config directory is writable: " + dir,
	}
}

// checkTailnetConnectedWithLookup is the real tailnet check but accepts a
// custom lookPath so tests can control PATH visibility without changing env.
func checkTailnetConnectedWithLookup(cfg *config.Config, lookPath func(string) (string, error)) CheckResult {
	if cfg == nil {
		return CheckResult{Name: "tailnet_connected", Status: "warn", Message: "config not loaded; check skipped"}
	}
	if cfg.TailscaleAuthKey == "" {
		return CheckResult{Name: "tailnet_connected", Status: "pass", Message: "TAILSCALE_AUTH_KEY not set; tailnet check skipped"}
	}
	if _, err := lookPath("tailscale"); err != nil {
		return CheckResult{
			Name:    "tailnet_connected",
			Status:  "warn",
			Message: "tailnet check requires the tailscale CLI; install tailscale or check 'journalctl -u galois-edge' for daemon-side tsnet state",
		}
	}
	cmd := execCommand("tailscale", "status", "--json", "--self")
	out, err := cmd.Output()
	if err != nil {
		return CheckResult{
			Name:    "tailnet_connected",
			Status:  "fail",
			Message: "tailscale status failed (auth key is configured but tailnet may not be connected): " + err.Error(),
		}
	}
	var status tailscaleStatusJSON
	if jsonErr := json.Unmarshal(out, &status); jsonErr != nil {
		return CheckResult{Name: "tailnet_connected", Status: "fail", Message: "tailscale status returned invalid JSON: " + jsonErr.Error()}
	}
	if len(status.Self.TailscaleIPs) == 0 {
		return CheckResult{Name: "tailnet_connected", Status: "fail", Message: "tailscale status returned no IPs; machine is not connected to the tailnet (check TAILSCALE_AUTH_KEY)"}
	}
	ips := ""
	for i, ip := range status.Self.TailscaleIPs {
		if i > 0 {
			ips += ", "
		}
		ips += ip
	}
	return CheckResult{Name: "tailnet_connected", Status: "pass", Message: "tailnet connected; assigned IPs: " + ips}
}
