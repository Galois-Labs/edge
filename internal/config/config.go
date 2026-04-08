// Package config provides configuration loading, validation, and persistence
// for the galois-edge daemon. Configuration is loaded from a KEY=VALUE file
// with environment variable overrides. Platform-specific default paths are
// used for config file discovery.
package config

import (
	"bufio"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
)

// Config holds all configuration for the galois-edge daemon.
// Field names are PascalCase; file and environment keys use UPPER_SNAKE_CASE.
type Config struct {
	// ---------- Edge identity ----------

	// EdgeName is the human-readable name for this edge node.
	// Defaults to the system hostname.
	EdgeName string

	// ---------- Python subprocess ----------

	// PythonBin is the path to the frozen Python engine binary.
	// Auto-detected if empty: looks for galois-engine next to the Go binary.
	PythonBin string

	// ---------- gRPC ports ----------

	// GRPCPort is the external gRPC port exposed via tsnet / TCP proxy.
	GRPCPort int

	// GRPCInternalPort is the port the Python gRPC server binds to on localhost.
	GRPCInternalPort int

	// GRPCMaxWorkers controls the Python gRPC thread pool size.
	GRPCMaxWorkers int

	// ---------- WebSocket ports ----------

	// WSPort is the external WebSocket port exposed via tsnet / TCP proxy.
	WSPort int

	// WSInternalPort is the port the Python WebSocket server binds to on localhost.
	WSInternalPort int

	// ---------- Backend registration ----------

	// BackendURL is the cloud backend URL for registration and heartbeat.
	BackendURL string

	// RelayURL is the WebSocket URL for the relay endpoint. When set, the
	// daemon maintains a persistent WebSocket connection to the backend so
	// that the backend can send instrument commands even when direct gRPC
	// dial fails. If empty but BackendURL is set, it is derived automatically
	// by replacing http(s):// with ws(s):// and appending /api/v1/relay/ws.
	RelayURL string

	// RegistrationToken is the one-time token for initial edge registration.
	RegistrationToken string

	// HeartbeatIntervalSec is how often (in seconds) to send heartbeats.
	HeartbeatIntervalSec int

	// ---------- Tailscale / Headscale ----------

	// TailscaleAuthKey is a Tailscale or Headscale pre-auth key.
	TailscaleAuthKey string

	// HeadscaleURL is the Headscale control server URL (optional).
	// When empty, standard Tailscale coordination is used.
	HeadscaleURL string

	// TsnetStateDir is the directory for tsnet persistent state.
	// Defaults to a platform-appropriate directory.
	TsnetStateDir string

	// ---------- Profile system ----------

	// ProfilesEnabled controls whether YAML instrument profiles are loaded.
	ProfilesEnabled bool

	// ProfileDir is the path to the directory containing YAML profiles.
	ProfileDir string

	// ---------- GPIB ----------

	// GPIBEnabled controls GPIB bus scanning. Values: "true", "false", "auto".
	// "auto" means true on Linux, false elsewhere.
	GPIBEnabled string

	// GPIBBoard is the linux-gpib board index.
	GPIBBoard int

	// GPIBScanOnInit controls whether GPIB bus is scanned at startup.
	GPIBScanOnInit bool

	// ---------- LAN instruments ----------

	// LANEnabled controls LAN instrument discovery.
	LANEnabled bool

	// LANMdnsEnabled controls mDNS/Zeroconf discovery.
	LANMdnsEnabled bool

	// LANInstruments is a list of static TCPIP VISA addresses.
	LANInstruments []string

	// ---------- Raw USB ----------

	// USBRawEnabled controls the raw USB transport for vendor-specific devices.
	USBRawEnabled bool

	// ---------- WebSocket streaming ----------

	// WSEnabled controls the WebSocket streaming server.
	WSEnabled bool

	// ---------- ZMQ streaming ----------

	// ZMQEnabled controls ZMQ PUB streaming on the edge.
	ZMQEnabled bool

	// ZMQPubPort is the ZMQ PUB port.
	ZMQPubPort int

	// ---------- Instrument rescan ----------

	// RescanIntervalSec is the interval (in seconds) for periodic instrument rediscovery.
	RescanIntervalSec int

	// ---------- PyVISA ----------

	// VisaBackend is the PyVISA backend selector (e.g. "@py" or empty for default).
	VisaBackend string

	// ---------- Backoff tuning ----------

	// ConnectionInitialBackoff is the initial backoff in seconds for registration retries.
	ConnectionInitialBackoff float64

	// ConnectionMaxBackoff is the maximum backoff in seconds.
	ConnectionMaxBackoff float64

	// ConnectionFailureThreshold is how many consecutive failures before entering backoff.
	ConnectionFailureThreshold int

	// ---------- Logging ----------

	// LogLevel controls the daemon log level (debug, info, warn, error).
	LogLevel string

	// ---------- Passthrough ----------

	// Extra holds config.env keys that are not recognized by the Go
	// supervisor. They are passed through as environment variables to
	// the Python child process (e.g. DEMO_MODE, MODBUS_INSTRUMENTS).
	Extra map[string]string
}

// --------------------------------------------------------------------------
// Field mapping: single source of truth for KEY <-> struct field binding.
// --------------------------------------------------------------------------

type fieldEntry struct {
	key   string // UPPER_SNAKE_CASE (file/env key)
	field string // PascalCase (Config struct field)
}

var fieldMapping = []fieldEntry{
	// Edge identity
	{"EDGE_NAME", "EdgeName"},

	// Python subprocess
	{"PYTHON_BIN", "PythonBin"},

	// gRPC ports
	{"GRPC_PORT", "GRPCPort"},
	{"GRPC_INTERNAL_PORT", "GRPCInternalPort"},
	{"GRPC_MAX_WORKERS", "GRPCMaxWorkers"},

	// WebSocket ports
	{"WS_PORT", "WSPort"},
	{"WS_INTERNAL_PORT", "WSInternalPort"},

	// Backend registration
	{"BACKEND_URL", "BackendURL"},
	{"RELAY_URL", "RelayURL"},
	{"REGISTRATION_TOKEN", "RegistrationToken"},
	{"HEARTBEAT_INTERVAL_SEC", "HeartbeatIntervalSec"},

	// Tailscale / Headscale
	{"TAILSCALE_AUTH_KEY", "TailscaleAuthKey"},
	{"HEADSCALE_URL", "HeadscaleURL"},
	{"TSNET_STATE_DIR", "TsnetStateDir"},

	// Profile system
	{"PROFILES_ENABLED", "ProfilesEnabled"},
	{"PROFILE_DIR", "ProfileDir"},

	// GPIB
	{"GPIB_ENABLED", "GPIBEnabled"},
	{"GPIB_BOARD", "GPIBBoard"},
	{"GPIB_SCAN_ON_INIT", "GPIBScanOnInit"},

	// LAN
	{"LAN_ENABLED", "LANEnabled"},
	{"LAN_MDNS_ENABLED", "LANMdnsEnabled"},
	{"LAN_INSTRUMENTS", "LANInstruments"},

	// Raw USB
	{"USB_RAW_ENABLED", "USBRawEnabled"},

	// WebSocket streaming
	{"WS_ENABLED", "WSEnabled"},

	// ZMQ
	{"ZMQ_ENABLED", "ZMQEnabled"},
	{"ZMQ_PUB_PORT", "ZMQPubPort"},

	// Rescan
	{"RESCAN_INTERVAL_SEC", "RescanIntervalSec"},

	// PyVISA
	{"VISA_BACKEND", "VisaBackend"},

	// Backoff
	{"CONNECTION_INITIAL_BACKOFF", "ConnectionInitialBackoff"},
	{"CONNECTION_MAX_BACKOFF", "ConnectionMaxBackoff"},
	{"CONNECTION_FAILURE_THRESHOLD", "ConnectionFailureThreshold"},

	// Logging
	{"LOG_LEVEL", "LogLevel"},
}

// --------------------------------------------------------------------------
// Constructor
// --------------------------------------------------------------------------

// New returns a Config populated with sensible default values.
// The EdgeName defaults to the system hostname.
func New() *Config {
	hostname, _ := os.Hostname()

	gpibDefault := "auto"
	if runtime.GOOS == "linux" {
		gpibDefault = "true"
	} else {
		gpibDefault = "false"
	}

	return &Config{
		EdgeName: hostname,

		PythonBin: "", // auto-detected at startup

		GRPCPort:         50051,
		GRPCInternalPort: 50052,
		GRPCMaxWorkers:   10,

		WSPort:         8765,
		WSInternalPort: 8766,

		BackendURL:           "",
		RegistrationToken:    "",
		HeartbeatIntervalSec: 30,

		TailscaleAuthKey: "",
		HeadscaleURL:     "",
		TsnetStateDir:    "",

		ProfilesEnabled: true,
		ProfileDir:      "",

		GPIBEnabled:    gpibDefault,
		GPIBBoard:      0,
		GPIBScanOnInit: true,

		LANEnabled:     true,
		LANMdnsEnabled: true,
		LANInstruments: []string{},

		USBRawEnabled: true,

		WSEnabled: true,

		ZMQEnabled: false,
		ZMQPubPort: 5556,

		RescanIntervalSec: 60,

		VisaBackend: "",

		ConnectionInitialBackoff:   2.0,
		ConnectionMaxBackoff:       300.0,
		ConnectionFailureThreshold: 3,

		LogLevel: "info",
	}
}

// --------------------------------------------------------------------------
// Platform-specific paths
// --------------------------------------------------------------------------

// SystemConfigDir returns the platform-specific system-wide config directory.
//
//	Linux/Darwin: /etc/galois-edge
//	Windows:      C:\ProgramData\galois-edge
func SystemConfigDir() string {
	if runtime.GOOS == "windows" {
		pd := os.Getenv("ProgramData")
		if pd == "" {
			pd = `C:\ProgramData`
		}
		return filepath.Join(pd, "galois-edge")
	}
	return "/etc/galois-edge"
}

// UserConfigDir returns the platform-specific per-user config directory.
//
//	Linux/Darwin: ~/.config/galois-edge
//	Windows:      %APPDATA%\galois-edge
func UserConfigDir() string {
	if runtime.GOOS == "windows" {
		appdata := os.Getenv("APPDATA")
		if appdata == "" {
			home, _ := os.UserHomeDir()
			appdata = filepath.Join(home, "AppData", "Roaming")
		}
		return filepath.Join(appdata, "galois-edge")
	}
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".config", "galois-edge")
}

// FindConfigFile searches for config.env in the standard locations:
//  1. System config dir
//  2. User config dir
//
// It returns the first path that exists, or an empty string if none is found.
func FindConfigFile() string {
	candidates := []string{
		filepath.Join(SystemConfigDir(), "config.env"),
		filepath.Join(UserConfigDir(), "config.env"),
	}
	for _, p := range candidates {
		if _, err := os.Stat(p); err == nil {
			return p
		}
	}
	return ""
}

// --------------------------------------------------------------------------
// Loading
// --------------------------------------------------------------------------

// LoadFromFile reads a KEY=VALUE config file and returns a Config with
// defaults overridden by the file's values.
func LoadFromFile(path string) (*Config, error) {
	kvs, err := parseFile(path)
	if err != nil {
		return nil, err
	}
	cfg := New()
	if err := applyMap(cfg, kvs); err != nil {
		return nil, fmt.Errorf("config %s: %w", path, err)
	}
	collectExtra(cfg, kvs)
	return cfg, nil
}

// LoadWithEnvOverrides loads a config file and then applies any matching
// environment variables on top. Environment variables take precedence.
func LoadWithEnvOverrides(path string) (*Config, error) {
	cfg, err := LoadFromFile(path)
	if err != nil {
		return nil, err
	}
	applyEnvOverrides(cfg)
	return cfg, nil
}

// LoadFromEnv creates a Config from defaults and environment variables only,
// with no config file.
func LoadFromEnv() *Config {
	cfg := New()
	applyEnvOverrides(cfg)
	return cfg
}

// Load performs the full config resolution: CLI-specified path (if any),
// then auto-discovered config file, then env overrides. This implements
// the search order defined in SPEC.md 4.1.
func Load(cliPath string) (*Config, error) {
	path := cliPath
	if path == "" {
		path = FindConfigFile()
	}

	var cfg *Config
	if path != "" {
		var err error
		cfg, err = LoadFromFile(path)
		if err != nil {
			return nil, fmt.Errorf("load config: %w", err)
		}
	} else {
		cfg = New()
	}

	applyEnvOverrides(cfg)
	return cfg, nil
}

// applyEnvOverrides reads all known config keys from the environment
// and applies them to cfg.
func applyEnvOverrides(cfg *Config) {
	overrides := make(map[string]string)
	for _, m := range fieldMapping {
		if v, ok := os.LookupEnv(m.key); ok {
			overrides[m.key] = v
		}
	}
	if len(overrides) > 0 {
		// Errors from env overrides are best-effort — we log nothing here
		// because config is imported standalone without a logger. The caller
		// can use Validate() to catch issues.
		_ = applyMap(cfg, overrides)
	}
}

// --------------------------------------------------------------------------
// Validation
// --------------------------------------------------------------------------

// ValidationError collects one or more field-level validation failures.
type ValidationError struct {
	Failures []string
}

func (e *ValidationError) Error() string {
	return fmt.Sprintf("config validation failed: %s", strings.Join(e.Failures, "; "))
}

// Validate checks the Config for required fields and value constraints.
// It returns a *ValidationError if any issues are found, or nil if valid.
func (c *Config) Validate() error {
	var failures []string

	fail := func(msg string) {
		failures = append(failures, msg)
	}

	// Port range checks.
	checkPort := func(name string, port int) {
		if port < 1 || port > 65535 {
			fail(fmt.Sprintf("%s must be 1-65535, got %d", name, port))
		}
	}

	checkPort("GRPC_PORT", c.GRPCPort)
	checkPort("GRPC_INTERNAL_PORT", c.GRPCInternalPort)
	checkPort("WS_PORT", c.WSPort)
	checkPort("WS_INTERNAL_PORT", c.WSInternalPort)

	if c.ZMQEnabled {
		checkPort("ZMQ_PUB_PORT", c.ZMQPubPort)
	}

	// External and internal ports must not collide.
	if c.GRPCPort == c.GRPCInternalPort {
		fail(fmt.Sprintf("GRPC_PORT (%d) and GRPC_INTERNAL_PORT (%d) must differ",
			c.GRPCPort, c.GRPCInternalPort))
	}
	if c.WSPort == c.WSInternalPort {
		fail(fmt.Sprintf("WS_PORT (%d) and WS_INTERNAL_PORT (%d) must differ",
			c.WSPort, c.WSInternalPort))
	}

	// GPIB_ENABLED must be one of the accepted values.
	switch strings.ToLower(c.GPIBEnabled) {
	case "true", "false", "auto":
		// ok
	default:
		fail(fmt.Sprintf("GPIB_ENABLED must be true/false/auto, got %q", c.GPIBEnabled))
	}

	// Log level validation.
	switch strings.ToLower(c.LogLevel) {
	case "debug", "info", "warn", "error":
		// ok
	default:
		fail(fmt.Sprintf("LOG_LEVEL must be debug/info/warn/error, got %q", c.LogLevel))
	}

	// Positive intervals.
	if c.HeartbeatIntervalSec < 1 {
		fail(fmt.Sprintf("HEARTBEAT_INTERVAL_SEC must be >= 1, got %d", c.HeartbeatIntervalSec))
	}
	if c.RescanIntervalSec < 1 {
		fail(fmt.Sprintf("RESCAN_INTERVAL_SEC must be >= 1, got %d", c.RescanIntervalSec))
	}
	if c.GRPCMaxWorkers < 1 {
		fail(fmt.Sprintf("GRPC_MAX_WORKERS must be >= 1, got %d", c.GRPCMaxWorkers))
	}

	// Backoff sanity.
	if c.ConnectionInitialBackoff <= 0 {
		fail("CONNECTION_INITIAL_BACKOFF must be > 0")
	}
	if c.ConnectionMaxBackoff < c.ConnectionInitialBackoff {
		fail("CONNECTION_MAX_BACKOFF must be >= CONNECTION_INITIAL_BACKOFF")
	}
	if c.ConnectionFailureThreshold < 1 {
		fail(fmt.Sprintf("CONNECTION_FAILURE_THRESHOLD must be >= 1, got %d",
			c.ConnectionFailureThreshold))
	}

	if len(failures) > 0 {
		return &ValidationError{Failures: failures}
	}
	return nil
}

// IsValidationError returns true if the given error is a *ValidationError.
func IsValidationError(err error) bool {
	var ve *ValidationError
	return errors.As(err, &ve)
}

// --------------------------------------------------------------------------
// Persistence (Save / Get / Set)
// --------------------------------------------------------------------------

// Save writes the current Config to a KEY=VALUE file grouped by category.
// Directories are created as needed.
func (c *Config) Save(path string) error {
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return fmt.Errorf("create config dir: %w", err)
	}

	var b strings.Builder

	section := func(comment string, pairs []kv) {
		b.WriteString("# " + comment + "\n")
		for _, p := range pairs {
			b.WriteString(p.k + "=" + p.v + "\n")
		}
		b.WriteString("\n")
	}

	section("Edge identity", []kv{
		{"EDGE_NAME", c.EdgeName},
	})

	section("Python subprocess", []kv{
		{"PYTHON_BIN", c.PythonBin},
	})

	section("gRPC ports", []kv{
		{"GRPC_PORT", itoa(c.GRPCPort)},
		{"GRPC_INTERNAL_PORT", itoa(c.GRPCInternalPort)},
		{"GRPC_MAX_WORKERS", itoa(c.GRPCMaxWorkers)},
	})

	section("WebSocket ports", []kv{
		{"WS_PORT", itoa(c.WSPort)},
		{"WS_INTERNAL_PORT", itoa(c.WSInternalPort)},
	})

	section("Backend registration", []kv{
		{"BACKEND_URL", c.BackendURL},
		{"RELAY_URL", c.RelayURL},
		{"REGISTRATION_TOKEN", c.RegistrationToken},
		{"HEARTBEAT_INTERVAL_SEC", itoa(c.HeartbeatIntervalSec)},
	})

	section("Tailscale / Headscale", []kv{
		{"TAILSCALE_AUTH_KEY", c.TailscaleAuthKey},
		{"HEADSCALE_URL", c.HeadscaleURL},
		{"TSNET_STATE_DIR", c.TsnetStateDir},
	})

	section("Profile system", []kv{
		{"PROFILES_ENABLED", btoa(c.ProfilesEnabled)},
		{"PROFILE_DIR", c.ProfileDir},
	})

	section("GPIB settings", []kv{
		{"GPIB_ENABLED", c.GPIBEnabled},
		{"GPIB_BOARD", itoa(c.GPIBBoard)},
		{"GPIB_SCAN_ON_INIT", btoa(c.GPIBScanOnInit)},
	})

	section("LAN instrument settings", []kv{
		{"LAN_ENABLED", btoa(c.LANEnabled)},
		{"LAN_MDNS_ENABLED", btoa(c.LANMdnsEnabled)},
		{"LAN_INSTRUMENTS", strings.Join(c.LANInstruments, ",")},
	})

	section("Raw USB transport", []kv{
		{"USB_RAW_ENABLED", btoa(c.USBRawEnabled)},
	})

	section("WebSocket streaming", []kv{
		{"WS_ENABLED", btoa(c.WSEnabled)},
	})

	section("ZMQ streaming", []kv{
		{"ZMQ_ENABLED", btoa(c.ZMQEnabled)},
		{"ZMQ_PUB_PORT", itoa(c.ZMQPubPort)},
	})

	section("Instrument rescan", []kv{
		{"RESCAN_INTERVAL_SEC", itoa(c.RescanIntervalSec)},
	})

	section("PyVISA settings", []kv{
		{"VISA_BACKEND", c.VisaBackend},
	})

	section("Connection backoff", []kv{
		{"CONNECTION_INITIAL_BACKOFF", ftoa(c.ConnectionInitialBackoff)},
		{"CONNECTION_MAX_BACKOFF", ftoa(c.ConnectionMaxBackoff)},
		{"CONNECTION_FAILURE_THRESHOLD", itoa(c.ConnectionFailureThreshold)},
	})

	section("Logging", []kv{
		{"LOG_LEVEL", c.LogLevel},
	})

	return os.WriteFile(path, []byte(b.String()), 0o644)
}

// kv is a trivial key-value pair used by Save.
type kv struct {
	k, v string
}

// --------------------------------------------------------------------------
// CLI helpers: EnvKeys, GetValue, SetValue
// --------------------------------------------------------------------------

// EnvKeys returns all known configuration keys in the canonical order
// defined by fieldMapping.
func EnvKeys() []string {
	keys := make([]string, len(fieldMapping))
	for i, m := range fieldMapping {
		keys[i] = m.key
	}
	return keys
}

// GetValue returns the string representation of a Config field identified by
// its UPPER_SNAKE_CASE key. The second return value is false if the key is
// not recognized.
func GetValue(cfg *Config, envKey string) (string, bool) {
	for _, m := range fieldMapping {
		if m.key == envKey {
			return getFieldStr(cfg, m.field), true
		}
	}
	return "", false
}

// SetValue sets a Config field identified by its UPPER_SNAKE_CASE key.
func SetValue(cfg *Config, envKey, value string) error {
	for _, m := range fieldMapping {
		if m.key == envKey {
			return setField(cfg, m.field, value)
		}
	}
	return fmt.Errorf("unknown config key %q", envKey)
}

// --------------------------------------------------------------------------
// Raw file helpers (for CLI configure commands)
// --------------------------------------------------------------------------

// ParseFile reads a KEY=VALUE config file and returns the raw map.
// Useful for read-modify-write workflows without going through the struct.
func ParseFile(path string) (map[string]string, error) {
	return parseFile(path)
}

// WriteFileMap writes a set of KEY=VALUE pairs to a config file.
// The file is overwritten. Keys not in fieldMapping are preserved for
// forward compatibility.
func WriteFileMap(path string, kvs map[string]string) error {
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return fmt.Errorf("create config dir: %w", err)
	}

	var b strings.Builder

	// Write known keys in canonical order first.
	known := make(map[string]bool, len(fieldMapping))
	for _, m := range fieldMapping {
		known[m.key] = true
		if v, ok := kvs[m.key]; ok {
			b.WriteString(m.key + "=" + v + "\n")
		}
	}

	// Write any extra keys the caller wants to persist.
	for k, v := range kvs {
		if !known[k] {
			b.WriteString(k + "=" + v + "\n")
		}
	}

	return os.WriteFile(path, []byte(b.String()), 0o644)
}

// --------------------------------------------------------------------------
// File parsing
// --------------------------------------------------------------------------

// parseFile reads a KEY=VALUE file. Blank lines, lines starting with #, and
// the optional "export " prefix are handled. Values may be quoted with single
// or double quotes. Unquoted values support inline comments (# after a space).
func parseFile(path string) (map[string]string, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()

	kvs := make(map[string]string)
	scanner := bufio.NewScanner(f)
	lineNo := 0

	for scanner.Scan() {
		lineNo++
		line := strings.TrimSpace(scanner.Text())

		// Skip blank lines and comments.
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}

		// Strip optional "export " prefix.
		line = strings.TrimPrefix(line, "export ")
		line = strings.TrimSpace(line)

		// Split on first '='.
		eqIdx := strings.IndexByte(line, '=')
		if eqIdx < 0 {
			continue // skip malformed lines silently
		}

		key := strings.TrimSpace(line[:eqIdx])
		val := strings.TrimSpace(line[eqIdx+1:])

		// Unquote and strip inline comments.
		val = unquote(val)

		kvs[key] = val
	}

	if err := scanner.Err(); err != nil {
		return nil, fmt.Errorf("read %s: %w", path, err)
	}
	return kvs, nil
}

// unquote removes surrounding single or double quotes. For unquoted values,
// inline comments (text after " #") are stripped.
func unquote(s string) string {
	if len(s) >= 2 {
		if (s[0] == '"' && s[len(s)-1] == '"') ||
			(s[0] == '\'' && s[len(s)-1] == '\'') {
			return s[1 : len(s)-1]
		}
	}
	// Strip inline comment for unquoted values.
	if idx := strings.Index(s, " #"); idx >= 0 {
		s = strings.TrimSpace(s[:idx])
	}
	return s
}

// --------------------------------------------------------------------------
// Map application
// --------------------------------------------------------------------------

// applyMap walks fieldMapping and sets any Config fields whose key appears
// in kvs.
func applyMap(cfg *Config, kvs map[string]string) error {
	for _, m := range fieldMapping {
		v, ok := kvs[m.key]
		if !ok {
			continue
		}
		if err := setField(cfg, m.field, v); err != nil {
			return fmt.Errorf("field %s (%s): %w", m.field, m.key, err)
		}
	}
	return nil
}

// collectExtra stores any config file keys that are not in fieldMapping
// into cfg.Extra so they can be passed through to the Python child.
func collectExtra(cfg *Config, kvs map[string]string) {
	known := make(map[string]bool, len(fieldMapping))
	for _, m := range fieldMapping {
		known[m.key] = true
	}
	for k, v := range kvs {
		if !known[k] {
			if cfg.Extra == nil {
				cfg.Extra = make(map[string]string)
			}
			cfg.Extra[k] = v
		}
	}
}

// --------------------------------------------------------------------------
// Field setters / getters by name
// --------------------------------------------------------------------------

// setField sets a single Config struct field by its PascalCase name from a
// string value. It handles type conversion for int, float64, bool, []string,
// and string fields.
func setField(cfg *Config, name, val string) error {
	switch name {
	// --- strings ---
	case "EdgeName":
		cfg.EdgeName = val
	case "PythonBin":
		cfg.PythonBin = val
	case "BackendURL":
		cfg.BackendURL = val
	case "RelayURL":
		cfg.RelayURL = val
	case "RegistrationToken":
		cfg.RegistrationToken = val
	case "TailscaleAuthKey":
		cfg.TailscaleAuthKey = val
	case "HeadscaleURL":
		cfg.HeadscaleURL = val
	case "TsnetStateDir":
		cfg.TsnetStateDir = val
	case "ProfileDir":
		cfg.ProfileDir = val
	case "GPIBEnabled":
		cfg.GPIBEnabled = val
	case "VisaBackend":
		cfg.VisaBackend = val
	case "LogLevel":
		cfg.LogLevel = val

	// --- ints ---
	case "GRPCPort":
		return setInt(&cfg.GRPCPort, val)
	case "GRPCInternalPort":
		return setInt(&cfg.GRPCInternalPort, val)
	case "GRPCMaxWorkers":
		return setInt(&cfg.GRPCMaxWorkers, val)
	case "WSPort":
		return setInt(&cfg.WSPort, val)
	case "WSInternalPort":
		return setInt(&cfg.WSInternalPort, val)
	case "HeartbeatIntervalSec":
		return setInt(&cfg.HeartbeatIntervalSec, val)
	case "GPIBBoard":
		return setInt(&cfg.GPIBBoard, val)
	case "ZMQPubPort":
		return setInt(&cfg.ZMQPubPort, val)
	case "RescanIntervalSec":
		return setInt(&cfg.RescanIntervalSec, val)
	case "ConnectionFailureThreshold":
		return setInt(&cfg.ConnectionFailureThreshold, val)

	// --- floats ---
	case "ConnectionInitialBackoff":
		return setFloat(&cfg.ConnectionInitialBackoff, val)
	case "ConnectionMaxBackoff":
		return setFloat(&cfg.ConnectionMaxBackoff, val)

	// --- bools ---
	case "ProfilesEnabled":
		return setBool(&cfg.ProfilesEnabled, val)
	case "GPIBScanOnInit":
		return setBool(&cfg.GPIBScanOnInit, val)
	case "LANEnabled":
		return setBool(&cfg.LANEnabled, val)
	case "LANMdnsEnabled":
		return setBool(&cfg.LANMdnsEnabled, val)
	case "USBRawEnabled":
		return setBool(&cfg.USBRawEnabled, val)
	case "WSEnabled":
		return setBool(&cfg.WSEnabled, val)
	case "ZMQEnabled":
		return setBool(&cfg.ZMQEnabled, val)

	// --- []string ---
	case "LANInstruments":
		cfg.LANInstruments = parseCSV(val)

	default:
		return fmt.Errorf("unknown field %q", name)
	}
	return nil
}

// getFieldStr returns the string representation of a Config field.
func getFieldStr(cfg *Config, name string) string {
	switch name {
	case "EdgeName":
		return cfg.EdgeName
	case "PythonBin":
		return cfg.PythonBin
	case "GRPCPort":
		return itoa(cfg.GRPCPort)
	case "GRPCInternalPort":
		return itoa(cfg.GRPCInternalPort)
	case "GRPCMaxWorkers":
		return itoa(cfg.GRPCMaxWorkers)
	case "WSPort":
		return itoa(cfg.WSPort)
	case "WSInternalPort":
		return itoa(cfg.WSInternalPort)
	case "BackendURL":
		return cfg.BackendURL
	case "RelayURL":
		return cfg.RelayURL
	case "RegistrationToken":
		return cfg.RegistrationToken
	case "HeartbeatIntervalSec":
		return itoa(cfg.HeartbeatIntervalSec)
	case "TailscaleAuthKey":
		return cfg.TailscaleAuthKey
	case "HeadscaleURL":
		return cfg.HeadscaleURL
	case "TsnetStateDir":
		return cfg.TsnetStateDir
	case "ProfilesEnabled":
		return btoa(cfg.ProfilesEnabled)
	case "ProfileDir":
		return cfg.ProfileDir
	case "GPIBEnabled":
		return cfg.GPIBEnabled
	case "GPIBBoard":
		return itoa(cfg.GPIBBoard)
	case "GPIBScanOnInit":
		return btoa(cfg.GPIBScanOnInit)
	case "LANEnabled":
		return btoa(cfg.LANEnabled)
	case "LANMdnsEnabled":
		return btoa(cfg.LANMdnsEnabled)
	case "LANInstruments":
		return strings.Join(cfg.LANInstruments, ",")
	case "USBRawEnabled":
		return btoa(cfg.USBRawEnabled)
	case "WSEnabled":
		return btoa(cfg.WSEnabled)
	case "ZMQEnabled":
		return btoa(cfg.ZMQEnabled)
	case "ZMQPubPort":
		return itoa(cfg.ZMQPubPort)
	case "RescanIntervalSec":
		return itoa(cfg.RescanIntervalSec)
	case "VisaBackend":
		return cfg.VisaBackend
	case "ConnectionInitialBackoff":
		return ftoa(cfg.ConnectionInitialBackoff)
	case "ConnectionMaxBackoff":
		return ftoa(cfg.ConnectionMaxBackoff)
	case "ConnectionFailureThreshold":
		return itoa(cfg.ConnectionFailureThreshold)
	case "LogLevel":
		return cfg.LogLevel
	default:
		return ""
	}
}

// --------------------------------------------------------------------------
// Type conversion helpers
// --------------------------------------------------------------------------

func setInt(dst *int, s string) error {
	n, err := strconv.Atoi(s)
	if err != nil {
		return fmt.Errorf("parse int: %w", err)
	}
	*dst = n
	return nil
}

func setFloat(dst *float64, s string) error {
	f, err := strconv.ParseFloat(s, 64)
	if err != nil {
		return fmt.Errorf("parse float: %w", err)
	}
	*dst = f
	return nil
}

func setBool(dst *bool, s string) error {
	switch strings.ToLower(s) {
	case "true", "1", "yes":
		*dst = true
	case "false", "0", "no":
		*dst = false
	default:
		return fmt.Errorf("parse bool: unrecognized value %q", s)
	}
	return nil
}

func parseCSV(s string) []string {
	if s == "" {
		return []string{}
	}
	parts := strings.Split(s, ",")
	out := make([]string, 0, len(parts))
	for _, p := range parts {
		p = strings.TrimSpace(p)
		if p != "" {
			out = append(out, p)
		}
	}
	return out
}

func itoa(n int) string     { return strconv.Itoa(n) }
func ftoa(f float64) string { return strconv.FormatFloat(f, 'f', -1, 64) }

func btoa(b bool) string {
	if b {
		return "true"
	}
	return "false"
}
