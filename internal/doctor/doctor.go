// Package doctor runs system-level health checks for the galois-edge daemon.
// It verifies infrastructure prerequisites (disk space, Python binary, config,
// GPIB drivers, USB permissions, network connectivity) and reports results in
// a structured format suitable for both human display and JSON output.
package doctor

import (
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"os"
	"os/exec"
	"os/user"
	"path/filepath"
	"runtime"
	"strings"
	"time"

	"github.com/galois-labs/edge/internal/config"
)

// execCommand is a variable so tests can replace it with a mock.
var execCommand = exec.Command

// --------------------------------------------------------------------------
// CheckResult
// --------------------------------------------------------------------------

// CheckResult represents the outcome of a single diagnostic check.
type CheckResult struct {
	Name    string `json:"name"`
	Status  string `json:"status"`  // "pass", "warn", "fail"
	Message string `json:"message"`
}

// --------------------------------------------------------------------------
// Public API
// --------------------------------------------------------------------------

// RunChecks executes all system-level health checks and returns the results.
// The cfg parameter supplies the Python binary path, gRPC address, and backend
// URL used by individual checks. If cfg is nil, doctor still runs infrastructure
// checks; provisioning checks that require cfg fields guard internally.
func RunChecks(cfg *config.Config) []CheckResult {
	// Determine effective config for checks that need it (python_binary,
	// python_health, gpib_driver, network_backend). Checks that need cfg for
	// provisioning fields guard with "if cfg == nil" internally.
	var effectiveCfg *config.Config
	if cfg != nil {
		effectiveCfg = cfg
	} else {
		effectiveCfg = config.New()
	}

	pythonBin := effectiveCfg.PythonBin
	grpcAddr := fmt.Sprintf("127.0.0.1:%d", effectiveCfg.GRPCInternalPort)

	results := []CheckResult{
		// 1. go_binary
		checkGoBinary(),
		// 2. disk_space
		checkDiskSpace(),
		// 3. config_file
		checkConfigFile(),
		// 4. config_writable (NEW)
		checkConfigWritable(),
		// 5. python_binary
		checkPythonBinary(pythonBin),
		// 6. python_health
		checkPythonHealth(grpcAddr),
		// 7. usb_permissions
		checkUSBPermissions(),
		// 8. gpib_driver
		checkGPIBDriver(effectiveCfg.GPIBEnabled),
		// 9. network_backend
		checkNetworkBackend(effectiveCfg.BackendURL),
		// 10. udev_rules_installed (NEW)
		checkUdevRulesInstalled(),
		// 11. service_unit_installed (NEW)
		checkServiceUnitInstalled(),
		// 12. tailnet_connected (NEW)
		checkTailnetConnected(cfg),
		// 13. registration_token_format (NEW)
		checkRegistrationTokenFormat(cfg),
	}
	return results
}

// FormatText returns a human-readable multi-line summary of check results.
func FormatText(results []CheckResult) string {
	var b strings.Builder
	for _, r := range results {
		icon := statusIcon(r.Status)
		fmt.Fprintf(&b, "  %s %s: %s\n", icon, r.Name, r.Message)
	}
	return b.String()
}

// FormatJSON returns the check results as an indented JSON string.
func FormatJSON(results []CheckResult) (string, error) {
	data, err := json.MarshalIndent(results, "", "  ")
	if err != nil {
		return "", fmt.Errorf("marshal check results: %w", err)
	}
	return string(data), nil
}

// HasFailures returns true if any check has status "fail".
func HasFailures(results []CheckResult) bool {
	for _, r := range results {
		if r.Status == "fail" {
			return true
		}
	}
	return false
}

// statusIcon returns a bracketed text indicator for terminal output.
func statusIcon(status string) string {
	switch status {
	case "pass":
		return "[PASS]"
	case "warn":
		return "[WARN]"
	case "fail":
		return "[FAIL]"
	default:
		return "[????]"
	}
}

// --------------------------------------------------------------------------
// Individual checks
// --------------------------------------------------------------------------

// checkGoBinary confirms the Go binary is running. If we can execute this
// code, the binary is functional.
func checkGoBinary() CheckResult {
	exe, _ := os.Executable()
	return CheckResult{
		Name:    "go_binary",
		Status:  "pass",
		Message: fmt.Sprintf("Go binary is running (%s/%s) at %s", runtime.GOOS, runtime.GOARCH, exe),
	}
}

// checkDiskSpace verifies at least 100 MB of free disk in the config
// directory (or a reasonable fallback).
func checkDiskSpace() CheckResult {
	dir := config.SystemConfigDir()
	if _, err := os.Stat(dir); err != nil {
		dir = os.TempDir()
	}

	free, err := freeDiskBytes(dir)
	if err != nil {
		return CheckResult{
			Name:    "disk_space",
			Status:  "warn",
			Message: fmt.Sprintf("Could not check disk space for %s: %v", dir, err),
		}
	}

	const minFreeBytes = 100 * 1024 * 1024 // 100 MB per SPEC.md 4.8
	freeMB := free / (1024 * 1024)

	if free < minFreeBytes {
		return CheckResult{
			Name:    "disk_space",
			Status:  "fail",
			Message: fmt.Sprintf("Low disk space on %s: %d MB free (minimum 100 MB)", dir, freeMB),
		}
	}

	return CheckResult{
		Name:    "disk_space",
		Status:  "pass",
		Message: fmt.Sprintf("Disk space OK: %d MB free on %s", freeMB, dir),
	}
}

// checkConfigFile verifies that a config file exists and is readable.
func checkConfigFile() CheckResult {
	path := config.FindConfigFile()
	if path == "" {
		return CheckResult{
			Name:    "config_file",
			Status:  "warn",
			Message: "No config file found (using defaults); expected in " + config.SystemConfigDir() + " or " + config.UserConfigDir(),
		}
	}

	info, err := os.Stat(path)
	if err != nil {
		return CheckResult{
			Name:    "config_file",
			Status:  "fail",
			Message: fmt.Sprintf("Cannot stat config file: %v", err),
		}
	}

	if info.Size() == 0 {
		return CheckResult{
			Name:    "config_file",
			Status:  "warn",
			Message: fmt.Sprintf("Config file is empty: %s", path),
		}
	}

	f, err := os.Open(path)
	if err != nil {
		return CheckResult{
			Name:    "config_file",
			Status:  "fail",
			Message: fmt.Sprintf("Config file is not readable: %v", err),
		}
	}
	f.Close()

	return CheckResult{
		Name:    "config_file",
		Status:  "pass",
		Message: fmt.Sprintf("Config file OK: %s (%d bytes)", path, info.Size()),
	}
}

// checkPythonBinary verifies that the Python engine binary exists and can
// execute.
func checkPythonBinary(pythonBin string) CheckResult {
	if pythonBin == "" {
		// Try to auto-detect next to the Go binary.
		if exe, err := os.Executable(); err == nil {
			candidate := filepath.Join(filepath.Dir(exe), "galois-engine")
			if _, err := os.Stat(candidate); err == nil {
				pythonBin = candidate
			}
		}
	}

	if pythonBin == "" {
		return CheckResult{
			Name:    "python_binary",
			Status:  "warn",
			Message: "Python binary path not configured (set PYTHON_BIN in config)",
		}
	}

	info, err := os.Stat(pythonBin)
	if err != nil {
		return CheckResult{
			Name:    "python_binary",
			Status:  "fail",
			Message: fmt.Sprintf("Python binary not found: %s", pythonBin),
		}
	}

	// On Unix, check executable permission.
	if runtime.GOOS != "windows" {
		if info.Mode()&0o111 == 0 {
			return CheckResult{
				Name:    "python_binary",
				Status:  "fail",
				Message: fmt.Sprintf("Python binary is not executable: %s", pythonBin),
			}
		}
	}

	// Verify the binary can actually run.
	cmd := exec.Command(pythonBin, "--version")
	if _, err := cmd.CombinedOutput(); err != nil {
		return CheckResult{
			Name:    "python_binary",
			Status:  "warn",
			Message: fmt.Sprintf("Python binary exists but --version failed: %v", err),
		}
	}

	return CheckResult{
		Name:    "python_binary",
		Status:  "pass",
		Message: fmt.Sprintf("Python binary OK: %s", pythonBin),
	}
}

// checkPythonHealth attempts a TCP dial to the Python gRPC server.
func checkPythonHealth(grpcAddr string) CheckResult {
	if grpcAddr == "" {
		return CheckResult{
			Name:    "python_health",
			Status:  "warn",
			Message: "gRPC address not configured",
		}
	}

	conn, err := net.DialTimeout("tcp", grpcAddr, 3*time.Second)
	if err != nil {
		return CheckResult{
			Name:    "python_health",
			Status:  "fail",
			Message: fmt.Sprintf("Cannot reach Python gRPC at %s: %v", grpcAddr, err),
		}
	}
	conn.Close()

	return CheckResult{
		Name:    "python_health",
		Status:  "pass",
		Message: fmt.Sprintf("Python gRPC reachable at %s", grpcAddr),
	}
}

// checkUSBPermissions verifies the current user is in the plugdev group
// on Linux. On other platforms this is a no-op pass.
func checkUSBPermissions() CheckResult {
	if runtime.GOOS != "linux" {
		return CheckResult{
			Name:    "usb_permissions",
			Status:  "pass",
			Message: fmt.Sprintf("USB permission check not required on %s", runtime.GOOS),
		}
	}

	u, err := user.Current()
	if err != nil {
		return CheckResult{
			Name:    "usb_permissions",
			Status:  "warn",
			Message: fmt.Sprintf("Could not determine current user: %v", err),
		}
	}

	groups, err := u.GroupIds()
	if err != nil {
		return CheckResult{
			Name:    "usb_permissions",
			Status:  "warn",
			Message: fmt.Sprintf("Could not list user groups: %v", err),
		}
	}

	// Look up the plugdev group.
	plugdev, err := user.LookupGroup("plugdev")
	if err != nil {
		return CheckResult{
			Name:    "usb_permissions",
			Status:  "warn",
			Message: "plugdev group does not exist on this system",
		}
	}

	for _, gid := range groups {
		if gid == plugdev.Gid {
			return CheckResult{
				Name:    "usb_permissions",
				Status:  "pass",
				Message: fmt.Sprintf("User %s is in plugdev group", u.Username),
			}
		}
	}

	return CheckResult{
		Name:    "usb_permissions",
		Status:  "warn",
		Message: fmt.Sprintf("User %s is NOT in plugdev group (USB instruments may not be accessible)", u.Username),
	}
}

// checkGPIBDriver checks whether the GPIB driver is available. On Linux,
// this looks for gpib_config. On other platforms, GPIB is typically
// handled through NI-VISA.
func checkGPIBDriver(gpibEnabled string) CheckResult {
	enabled := strings.ToLower(gpibEnabled)
	if enabled == "false" {
		return CheckResult{
			Name:    "gpib_driver",
			Status:  "pass",
			Message: "GPIB disabled in config, skipping driver check",
		}
	}

	if runtime.GOOS == "linux" {
		_, err := exec.LookPath("gpib_config")
		if err != nil {
			status := "warn"
			if enabled == "true" {
				status = "fail"
			}
			return CheckResult{
				Name:    "gpib_driver",
				Status:  status,
				Message: "gpib_config not found in PATH (linux-gpib may not be installed)",
			}
		}
		return CheckResult{
			Name:    "gpib_driver",
			Status:  "pass",
			Message: "linux-gpib driver found (gpib_config present)",
		}
	}

	// On Windows/macOS, GPIB is handled by NI-VISA or not applicable.
	return CheckResult{
		Name:    "gpib_driver",
		Status:  "pass",
		Message: fmt.Sprintf("GPIB on %s uses vendor drivers (NI-VISA); no additional check needed", runtime.GOOS),
	}
}

// checkNetworkBackend verifies that the cloud backend is reachable via
// an HTTP HEAD request. If no backend URL is configured, this is a
// warning (standalone mode).
func checkNetworkBackend(backendURL string) CheckResult {
	if backendURL == "" {
		return CheckResult{
			Name:    "network_backend",
			Status:  "warn",
			Message: "No BACKEND_URL configured (running in standalone mode)",
		}
	}

	client := &http.Client{Timeout: 5 * time.Second}
	resp, err := client.Head(backendURL)
	if err != nil {
		return CheckResult{
			Name:    "network_backend",
			Status:  "fail",
			Message: fmt.Sprintf("Cannot reach backend at %s: %v", backendURL, err),
		}
	}
	resp.Body.Close()

	return CheckResult{
		Name:    "network_backend",
		Status:  "pass",
		Message: fmt.Sprintf("Backend reachable at %s (HTTP %d)", backendURL, resp.StatusCode),
	}
}

// checkConfigWritable verifies that the config file (or its parent directory
// if no file exists yet) is writable by the current process.
func checkConfigWritable() CheckResult {
	path := config.FindConfigFile()
	if path != "" {
		// File exists — try to open it for writing.
		f, err := os.OpenFile(path, os.O_WRONLY, 0)
		if err != nil {
			return CheckResult{
				Name:    "config_writable",
				Status:  "warn",
				Message: fmt.Sprintf("Config file at %s is not writable by current user; 'configure set' will fail", path),
			}
		}
		f.Close()
		return CheckResult{
			Name:    "config_writable",
			Status:  "pass",
			Message: fmt.Sprintf("Config file is writable: %s", path),
		}
	}

	// No config file — check whether the default directory is writable.
	dir := config.SystemConfigDir()
	if _, err := os.Stat(dir); err != nil {
		expectedPath := filepath.Join(dir, "config.env")
		return CheckResult{
			Name:    "config_writable",
			Status:  "warn",
			Message: fmt.Sprintf("Config directory %s does not exist; expected config at %s", dir, expectedPath),
		}
	}

	tmp, err := os.CreateTemp(dir, ".galois-doctor-writable-*")
	if err != nil {
		return CheckResult{
			Name:    "config_writable",
			Status:  "warn",
			Message: fmt.Sprintf("Config file at %s is not writable by current user; 'configure set' will fail", filepath.Join(dir, "config.env")),
		}
	}
	tmp.Close()
	os.Remove(tmp.Name())

	return CheckResult{
		Name:    "config_writable",
		Status:  "pass",
		Message: fmt.Sprintf("Config directory is writable: %s", dir),
	}
}

// tailscaleStatusJSON is the subset of tailscale status --json output we need.
type tailscaleStatusJSON struct {
	Self struct {
		TailscaleIPs []string `json:"TailscaleIPs"`
	} `json:"Self"`
}

// checkTailnetConnected uses the tailscale CLI as a side channel to determine
// whether the machine has at least one tailnet IP. If TAILSCALE_AUTH_KEY is
// not configured, the check is skipped (pass).
func checkTailnetConnected(cfg *config.Config) CheckResult {
	if cfg == nil {
		return CheckResult{
			Name:    "tailnet_connected",
			Status:  "warn",
			Message: "config not loaded; check skipped",
		}
	}

	if cfg.TailscaleAuthKey == "" {
		return CheckResult{
			Name:    "tailnet_connected",
			Status:  "pass",
			Message: "TAILSCALE_AUTH_KEY not set; tailnet check skipped",
		}
	}

	// Auth key is set — try to inspect tailnet state via the tailscale CLI.
	_, err := exec.LookPath("tailscale")
	if err != nil {
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
			Message: fmt.Sprintf("tailscale status failed (auth key is configured but tailnet may not be connected): %v", err),
		}
	}

	var status tailscaleStatusJSON
	if jsonErr := json.Unmarshal(out, &status); jsonErr != nil {
		return CheckResult{
			Name:    "tailnet_connected",
			Status:  "fail",
			Message: fmt.Sprintf("tailscale status returned invalid JSON: %v", jsonErr),
		}
	}

	if len(status.Self.TailscaleIPs) == 0 {
		return CheckResult{
			Name:    "tailnet_connected",
			Status:  "fail",
			Message: "tailscale status returned no IPs; machine is not connected to the tailnet (check TAILSCALE_AUTH_KEY)",
		}
	}

	return CheckResult{
		Name:    "tailnet_connected",
		Status:  "pass",
		Message: fmt.Sprintf("tailnet connected; assigned IPs: %s", strings.Join(status.Self.TailscaleIPs, ", ")),
	}
}

// checkRegistrationTokenFormat verifies that REGISTRATION_TOKEN, if set,
// starts with the expected "glc_" prefix. This is advisory only — never fail.
func checkRegistrationTokenFormat(cfg *config.Config) CheckResult {
	if cfg == nil {
		return CheckResult{
			Name:    "registration_token_format",
			Status:  "warn",
			Message: "config not loaded; check skipped",
		}
	}

	if cfg.RegistrationToken == "" {
		return CheckResult{
			Name:    "registration_token_format",
			Status:  "pass",
			Message: "REGISTRATION_TOKEN not set; skipping format check",
		}
	}

	if strings.HasPrefix(cfg.RegistrationToken, "glc_") {
		return CheckResult{
			Name:    "registration_token_format",
			Status:  "pass",
			Message: "REGISTRATION_TOKEN format OK (starts with 'glc_')",
		}
	}

	return CheckResult{
		Name:    "registration_token_format",
		Status:  "warn",
		Message: "REGISTRATION_TOKEN does not start with 'glc_'; check for copy-paste errors",
	}
}
