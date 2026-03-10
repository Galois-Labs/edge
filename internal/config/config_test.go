package config

import (
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// writeTempConfig writes a KEY=VALUE config file into t.TempDir and returns
// its path.
func writeTempConfig(t *testing.T, content string) string {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "config.env")
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatalf("write temp config: %v", err)
	}
	return path
}

// setEnvForTest sets an environment variable and registers cleanup to restore
// the previous value.
func setEnvForTest(t *testing.T, key, value string) {
	t.Helper()
	prev, hadPrev := os.LookupEnv(key)
	os.Setenv(key, value)
	t.Cleanup(func() {
		if hadPrev {
			os.Setenv(key, prev)
		} else {
			os.Unsetenv(key)
		}
	})
}

// unsetEnvForTest unsets an environment variable and registers cleanup to
// restore the previous value.
func unsetEnvForTest(t *testing.T, key string) {
	t.Helper()
	prev, hadPrev := os.LookupEnv(key)
	os.Unsetenv(key)
	t.Cleanup(func() {
		if hadPrev {
			os.Setenv(key, prev)
		} else {
			os.Unsetenv(key)
		}
	})
}

// ---------------------------------------------------------------------------
// New() defaults
// ---------------------------------------------------------------------------

func TestNew_Defaults(t *testing.T) {
	cfg := New()

	hostname, _ := os.Hostname()
	if cfg.EdgeName != hostname {
		t.Errorf("EdgeName: got %q, want %q", cfg.EdgeName, hostname)
	}

	if cfg.GRPCPort != 50051 {
		t.Errorf("GRPCPort: got %d, want 50051", cfg.GRPCPort)
	}
	if cfg.GRPCInternalPort != 50052 {
		t.Errorf("GRPCInternalPort: got %d, want 50052", cfg.GRPCInternalPort)
	}
	if cfg.GRPCMaxWorkers != 10 {
		t.Errorf("GRPCMaxWorkers: got %d, want 10", cfg.GRPCMaxWorkers)
	}
	if cfg.WSPort != 8765 {
		t.Errorf("WSPort: got %d, want 8765", cfg.WSPort)
	}
	if cfg.WSInternalPort != 8766 {
		t.Errorf("WSInternalPort: got %d, want 8766", cfg.WSInternalPort)
	}
	if cfg.HeartbeatIntervalSec != 30 {
		t.Errorf("HeartbeatIntervalSec: got %d, want 30", cfg.HeartbeatIntervalSec)
	}
	if !cfg.ProfilesEnabled {
		t.Error("ProfilesEnabled: got false, want true")
	}
	if !cfg.LANEnabled {
		t.Error("LANEnabled: got false, want true")
	}
	if !cfg.USBRawEnabled {
		t.Error("USBRawEnabled: got false, want true")
	}
	if !cfg.WSEnabled {
		t.Error("WSEnabled: got false, want true")
	}
	if cfg.ZMQEnabled {
		t.Error("ZMQEnabled: got true, want false")
	}
	if cfg.ZMQPubPort != 5556 {
		t.Errorf("ZMQPubPort: got %d, want 5556", cfg.ZMQPubPort)
	}
	if cfg.RescanIntervalSec != 60 {
		t.Errorf("RescanIntervalSec: got %d, want 60", cfg.RescanIntervalSec)
	}
	if cfg.ConnectionInitialBackoff != 2.0 {
		t.Errorf("ConnectionInitialBackoff: got %f, want 2.0", cfg.ConnectionInitialBackoff)
	}
	if cfg.ConnectionMaxBackoff != 300.0 {
		t.Errorf("ConnectionMaxBackoff: got %f, want 300.0", cfg.ConnectionMaxBackoff)
	}
	if cfg.ConnectionFailureThreshold != 3 {
		t.Errorf("ConnectionFailureThreshold: got %d, want 3", cfg.ConnectionFailureThreshold)
	}
	if cfg.LogLevel != "info" {
		t.Errorf("LogLevel: got %q, want %q", cfg.LogLevel, "info")
	}

	// GPIB should be platform-specific.
	if runtime.GOOS == "linux" {
		if cfg.GPIBEnabled != "true" {
			t.Errorf("GPIBEnabled (linux): got %q, want %q", cfg.GPIBEnabled, "true")
		}
	} else {
		if cfg.GPIBEnabled != "false" {
			t.Errorf("GPIBEnabled (non-linux): got %q, want %q", cfg.GPIBEnabled, "false")
		}
	}
}

// ---------------------------------------------------------------------------
// LoadFromFile — config file parsing
// ---------------------------------------------------------------------------

func TestLoadFromFile_Basic(t *testing.T) {
	content := `# Test config
EDGE_NAME=test-edge
GRPC_PORT=9001
GRPC_INTERNAL_PORT=9002
WS_PORT=9003
WS_INTERNAL_PORT=9004
LOG_LEVEL=debug
PROFILES_ENABLED=false
LAN_INSTRUMENTS=TCPIP::192.168.1.1,TCPIP::192.168.1.2
CONNECTION_INITIAL_BACKOFF=5.5
`
	path := writeTempConfig(t, content)

	cfg, err := LoadFromFile(path)
	if err != nil {
		t.Fatalf("LoadFromFile: %v", err)
	}

	if cfg.EdgeName != "test-edge" {
		t.Errorf("EdgeName: got %q, want %q", cfg.EdgeName, "test-edge")
	}
	if cfg.GRPCPort != 9001 {
		t.Errorf("GRPCPort: got %d, want 9001", cfg.GRPCPort)
	}
	if cfg.GRPCInternalPort != 9002 {
		t.Errorf("GRPCInternalPort: got %d, want 9002", cfg.GRPCInternalPort)
	}
	if cfg.WSPort != 9003 {
		t.Errorf("WSPort: got %d, want 9003", cfg.WSPort)
	}
	if cfg.WSInternalPort != 9004 {
		t.Errorf("WSInternalPort: got %d, want 9004", cfg.WSInternalPort)
	}
	if cfg.LogLevel != "debug" {
		t.Errorf("LogLevel: got %q, want %q", cfg.LogLevel, "debug")
	}
	if cfg.ProfilesEnabled {
		t.Error("ProfilesEnabled: got true, want false")
	}
	if len(cfg.LANInstruments) != 2 {
		t.Fatalf("LANInstruments: got %d items, want 2", len(cfg.LANInstruments))
	}
	if cfg.LANInstruments[0] != "TCPIP::192.168.1.1" || cfg.LANInstruments[1] != "TCPIP::192.168.1.2" {
		t.Errorf("LANInstruments: got %v", cfg.LANInstruments)
	}
	if cfg.ConnectionInitialBackoff != 5.5 {
		t.Errorf("ConnectionInitialBackoff: got %f, want 5.5", cfg.ConnectionInitialBackoff)
	}
}

func TestLoadFromFile_QuotedValues(t *testing.T) {
	content := `EDGE_NAME="my edge"
BACKEND_URL='https://api.example.com'
LOG_LEVEL=warn # inline comment
`
	path := writeTempConfig(t, content)

	cfg, err := LoadFromFile(path)
	if err != nil {
		t.Fatalf("LoadFromFile: %v", err)
	}

	if cfg.EdgeName != "my edge" {
		t.Errorf("EdgeName: got %q, want %q", cfg.EdgeName, "my edge")
	}
	if cfg.BackendURL != "https://api.example.com" {
		t.Errorf("BackendURL: got %q, want %q", cfg.BackendURL, "https://api.example.com")
	}
	if cfg.LogLevel != "warn" {
		t.Errorf("LogLevel: got %q, want %q", cfg.LogLevel, "warn")
	}
}

func TestLoadFromFile_ExportPrefix(t *testing.T) {
	content := `export EDGE_NAME=exported-edge
export GRPC_PORT=12345
`
	path := writeTempConfig(t, content)

	cfg, err := LoadFromFile(path)
	if err != nil {
		t.Fatalf("LoadFromFile: %v", err)
	}

	if cfg.EdgeName != "exported-edge" {
		t.Errorf("EdgeName: got %q, want %q", cfg.EdgeName, "exported-edge")
	}
	if cfg.GRPCPort != 12345 {
		t.Errorf("GRPCPort: got %d, want 12345", cfg.GRPCPort)
	}
}

func TestLoadFromFile_MissingFile(t *testing.T) {
	_, err := LoadFromFile("/nonexistent/config.env")
	if err == nil {
		t.Fatal("expected error for missing file")
	}
}

func TestLoadFromFile_InvalidInt(t *testing.T) {
	content := `GRPC_PORT=not_a_number`
	path := writeTempConfig(t, content)

	_, err := LoadFromFile(path)
	if err == nil {
		t.Fatal("expected error for invalid int")
	}
}

// ---------------------------------------------------------------------------
// Environment variable overrides
// ---------------------------------------------------------------------------

func TestLoadFromEnv_Overrides(t *testing.T) {
	setEnvForTest(t, "EDGE_NAME", "env-edge")
	setEnvForTest(t, "GRPC_PORT", "7777")
	setEnvForTest(t, "LOG_LEVEL", "error")
	setEnvForTest(t, "ZMQ_ENABLED", "true")

	cfg := LoadFromEnv()

	if cfg.EdgeName != "env-edge" {
		t.Errorf("EdgeName: got %q, want %q", cfg.EdgeName, "env-edge")
	}
	if cfg.GRPCPort != 7777 {
		t.Errorf("GRPCPort: got %d, want 7777", cfg.GRPCPort)
	}
	if cfg.LogLevel != "error" {
		t.Errorf("LogLevel: got %q, want %q", cfg.LogLevel, "error")
	}
	if !cfg.ZMQEnabled {
		t.Error("ZMQEnabled: got false, want true")
	}
}

func TestLoadWithEnvOverrides_FileAndEnv(t *testing.T) {
	content := `EDGE_NAME=file-edge
GRPC_PORT=9999
`
	path := writeTempConfig(t, content)

	// Env overrides file.
	setEnvForTest(t, "EDGE_NAME", "env-wins")

	cfg, err := LoadWithEnvOverrides(path)
	if err != nil {
		t.Fatalf("LoadWithEnvOverrides: %v", err)
	}

	if cfg.EdgeName != "env-wins" {
		t.Errorf("EdgeName: got %q, want %q (env should override file)", cfg.EdgeName, "env-wins")
	}
	if cfg.GRPCPort != 9999 {
		t.Errorf("GRPCPort: got %d, want 9999 (from file)", cfg.GRPCPort)
	}
}

// ---------------------------------------------------------------------------
// Load() — full resolution
// ---------------------------------------------------------------------------

func TestLoad_NoFileNoEnv(t *testing.T) {
	// Clear any env vars that might be set.
	for _, m := range fieldMapping {
		unsetEnvForTest(t, m.key)
	}

	cfg, err := Load("")
	if err != nil {
		t.Fatalf("Load: %v", err)
	}

	// Should have defaults.
	if cfg.GRPCPort != 50051 {
		t.Errorf("GRPCPort: got %d, want 50051", cfg.GRPCPort)
	}
}

func TestLoad_WithCLIPath(t *testing.T) {
	content := `GRPC_PORT=11111`
	path := writeTempConfig(t, content)

	cfg, err := Load(path)
	if err != nil {
		t.Fatalf("Load: %v", err)
	}

	if cfg.GRPCPort != 11111 {
		t.Errorf("GRPCPort: got %d, want 11111", cfg.GRPCPort)
	}
}

// ---------------------------------------------------------------------------
// Validation
// ---------------------------------------------------------------------------

func TestValidate_DefaultsPass(t *testing.T) {
	cfg := New()
	if err := cfg.Validate(); err != nil {
		t.Fatalf("Validate defaults: %v", err)
	}
}

func TestValidate_PortRange(t *testing.T) {
	cfg := New()
	cfg.GRPCPort = 0 // invalid
	err := cfg.Validate()
	if err == nil {
		t.Fatal("expected validation error for port 0")
	}
	if !IsValidationError(err) {
		t.Fatalf("expected *ValidationError, got %T", err)
	}

	cfg = New()
	cfg.WSPort = 70000 // invalid
	err = cfg.Validate()
	if err == nil {
		t.Fatal("expected validation error for port 70000")
	}
}

func TestValidate_PortCollision(t *testing.T) {
	cfg := New()
	cfg.GRPCPort = 50051
	cfg.GRPCInternalPort = 50051 // collision
	err := cfg.Validate()
	if err == nil {
		t.Fatal("expected validation error for port collision")
	}
}

func TestValidate_BadGPIBEnabled(t *testing.T) {
	cfg := New()
	cfg.GPIBEnabled = "maybe"
	err := cfg.Validate()
	if err == nil {
		t.Fatal("expected validation error for GPIBEnabled=maybe")
	}
}

func TestValidate_BadLogLevel(t *testing.T) {
	cfg := New()
	cfg.LogLevel = "verbose"
	err := cfg.Validate()
	if err == nil {
		t.Fatal("expected validation error for LogLevel=verbose")
	}
}

func TestValidate_BackoffConstraints(t *testing.T) {
	cfg := New()
	cfg.ConnectionInitialBackoff = 0 // invalid
	err := cfg.Validate()
	if err == nil {
		t.Fatal("expected validation error for zero initial backoff")
	}

	cfg = New()
	cfg.ConnectionMaxBackoff = 1.0
	cfg.ConnectionInitialBackoff = 5.0 // max < initial
	err = cfg.Validate()
	if err == nil {
		t.Fatal("expected validation error for max < initial backoff")
	}
}

func TestValidate_ZeroIntervals(t *testing.T) {
	cfg := New()
	cfg.HeartbeatIntervalSec = 0
	err := cfg.Validate()
	if err == nil {
		t.Fatal("expected validation error for zero heartbeat interval")
	}
}

func TestValidate_MultipleFailures(t *testing.T) {
	cfg := New()
	cfg.GRPCPort = 0
	cfg.LogLevel = "invalid"
	cfg.HeartbeatIntervalSec = -1
	err := cfg.Validate()
	if err == nil {
		t.Fatal("expected validation error")
	}
	ve, ok := err.(*ValidationError)
	if !ok {
		t.Fatalf("expected *ValidationError, got %T", err)
	}
	if len(ve.Failures) < 3 {
		t.Errorf("expected at least 3 failures, got %d: %v", len(ve.Failures), ve.Failures)
	}
}

func TestValidate_ZMQPortOnlyWhenEnabled(t *testing.T) {
	cfg := New()
	cfg.ZMQEnabled = false
	cfg.ZMQPubPort = 0 // invalid port, but ZMQ is disabled
	err := cfg.Validate()
	if err != nil {
		t.Fatalf("should pass when ZMQ disabled: %v", err)
	}

	cfg.ZMQEnabled = true
	cfg.ZMQPubPort = 0
	err = cfg.Validate()
	if err == nil {
		t.Fatal("expected validation error for zero ZMQ port when enabled")
	}
}

// ---------------------------------------------------------------------------
// Platform-specific paths
// ---------------------------------------------------------------------------

func TestSystemConfigDir(t *testing.T) {
	dir := SystemConfigDir()
	if runtime.GOOS == "windows" {
		if !filepath.IsAbs(dir) {
			t.Errorf("SystemConfigDir should be absolute: %q", dir)
		}
	} else {
		if dir != "/etc/galois-edge" {
			t.Errorf("SystemConfigDir: got %q, want /etc/galois-edge", dir)
		}
	}
}

func TestUserConfigDir(t *testing.T) {
	dir := UserConfigDir()
	if !filepath.IsAbs(dir) {
		t.Errorf("UserConfigDir should be absolute: %q", dir)
	}
	if runtime.GOOS != "windows" {
		if !filepath.IsAbs(dir) || dir == "" {
			t.Errorf("UserConfigDir: got %q, expected non-empty absolute path", dir)
		}
	}
}

// ---------------------------------------------------------------------------
// GetValue / SetValue
// ---------------------------------------------------------------------------

func TestGetValue(t *testing.T) {
	cfg := New()
	cfg.GRPCPort = 12345

	val, ok := GetValue(cfg, "GRPC_PORT")
	if !ok {
		t.Fatal("GetValue should recognize GRPC_PORT")
	}
	if val != "12345" {
		t.Errorf("GetValue GRPC_PORT: got %q, want %q", val, "12345")
	}

	val, ok = GetValue(cfg, "LOG_LEVEL")
	if !ok || val != "info" {
		t.Errorf("GetValue LOG_LEVEL: got (%q, %v)", val, ok)
	}

	_, ok = GetValue(cfg, "UNKNOWN_KEY")
	if ok {
		t.Error("GetValue should return false for unknown key")
	}
}

func TestSetValue(t *testing.T) {
	cfg := New()

	if err := SetValue(cfg, "GRPC_PORT", "54321"); err != nil {
		t.Fatalf("SetValue: %v", err)
	}
	if cfg.GRPCPort != 54321 {
		t.Errorf("GRPCPort: got %d, want 54321", cfg.GRPCPort)
	}

	if err := SetValue(cfg, "PROFILES_ENABLED", "false"); err != nil {
		t.Fatalf("SetValue: %v", err)
	}
	if cfg.ProfilesEnabled {
		t.Error("ProfilesEnabled should be false")
	}

	if err := SetValue(cfg, "LAN_INSTRUMENTS", "TCPIP::1.1.1.1,TCPIP::2.2.2.2"); err != nil {
		t.Fatalf("SetValue: %v", err)
	}
	if len(cfg.LANInstruments) != 2 {
		t.Errorf("LANInstruments: got %d items, want 2", len(cfg.LANInstruments))
	}

	err := SetValue(cfg, "NONEXISTENT", "val")
	if err == nil {
		t.Error("SetValue should return error for unknown key")
	}
}

// ---------------------------------------------------------------------------
// Save + round-trip
// ---------------------------------------------------------------------------

func TestSaveAndReload(t *testing.T) {
	cfg := New()
	cfg.EdgeName = "round-trip-test"
	cfg.GRPCPort = 33333
	cfg.LANInstruments = []string{"TCPIP::10.0.0.1", "TCPIP::10.0.0.2"}
	cfg.ConnectionInitialBackoff = 3.14

	dir := t.TempDir()
	path := filepath.Join(dir, "saved.env")

	if err := cfg.Save(path); err != nil {
		t.Fatalf("Save: %v", err)
	}

	loaded, err := LoadFromFile(path)
	if err != nil {
		t.Fatalf("LoadFromFile after save: %v", err)
	}

	if loaded.EdgeName != "round-trip-test" {
		t.Errorf("EdgeName: got %q, want %q", loaded.EdgeName, "round-trip-test")
	}
	if loaded.GRPCPort != 33333 {
		t.Errorf("GRPCPort: got %d, want 33333", loaded.GRPCPort)
	}
	if len(loaded.LANInstruments) != 2 {
		t.Fatalf("LANInstruments: got %d, want 2", len(loaded.LANInstruments))
	}
	if loaded.ConnectionInitialBackoff != 3.14 {
		t.Errorf("ConnectionInitialBackoff: got %f, want 3.14", loaded.ConnectionInitialBackoff)
	}
}

// ---------------------------------------------------------------------------
// EnvKeys
// ---------------------------------------------------------------------------

func TestEnvKeys(t *testing.T) {
	keys := EnvKeys()
	if len(keys) != len(fieldMapping) {
		t.Errorf("EnvKeys: got %d, want %d", len(keys), len(fieldMapping))
	}

	// Spot check a few.
	found := make(map[string]bool, len(keys))
	for _, k := range keys {
		found[k] = true
	}
	for _, want := range []string{"EDGE_NAME", "GRPC_PORT", "LOG_LEVEL"} {
		if !found[want] {
			t.Errorf("EnvKeys missing %q", want)
		}
	}
}

// ---------------------------------------------------------------------------
// ParseFile / WriteFileMap raw helpers
// ---------------------------------------------------------------------------

func TestParseFileAndWriteFileMap(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "raw.env")

	kvs := map[string]string{
		"EDGE_NAME":  "raw-test",
		"GRPC_PORT":  "11111",
		"CUSTOM_KEY": "custom_value",
	}

	if err := WriteFileMap(path, kvs); err != nil {
		t.Fatalf("WriteFileMap: %v", err)
	}

	got, err := ParseFile(path)
	if err != nil {
		t.Fatalf("ParseFile: %v", err)
	}

	for k, want := range kvs {
		if got[k] != want {
			t.Errorf("%s: got %q, want %q", k, got[k], want)
		}
	}
}
