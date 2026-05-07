//go:build linux

package doctor

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestCheckUdevRulesInstalled_Pass(t *testing.T) {
	// Write a rules file that contains all seven vendor IDs.
	dir := t.TempDir()
	path := filepath.Join(dir, "99-galois-edge.rules")
	content := buildCompleteRulesContent()
	if err := os.WriteFile(path, []byte(content), 0644); err != nil {
		t.Fatal(err)
	}

	result := checkUdevRulesInstalledAt(path)
	if result.Status != "pass" {
		t.Errorf("status = %q, want pass; message: %s", result.Status, result.Message)
	}
}

func TestCheckUdevRulesInstalled_Warn_MissingVendorID(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "99-galois-edge.rules")
	// Build content that omits "f4ec".
	content := buildCompleteRulesContent()
	content = strings.ReplaceAll(content, "f4ec", "XXXX")
	if err := os.WriteFile(path, []byte(content), 0644); err != nil {
		t.Fatal(err)
	}

	result := checkUdevRulesInstalledAt(path)
	if result.Status != "warn" {
		t.Errorf("status = %q, want warn; message: %s", result.Status, result.Message)
	}
	if !strings.Contains(result.Message, "f4ec") {
		t.Errorf("message %q should mention missing vendor ID 'f4ec'", result.Message)
	}
}

func TestCheckUdevRulesInstalled_Fail_Missing(t *testing.T) {
	result := checkUdevRulesInstalledAt("/nonexistent/99-galois-edge.rules")
	if result.Status != "fail" {
		t.Errorf("status = %q, want fail", result.Status)
	}
}

// buildCompleteRulesContent returns a fake udev rules string that includes
// all seven expected vendor IDs.
func buildCompleteRulesContent() string {
	return `# Galois Edge udev rules
SUBSYSTEM=="usb", ATTR{idVendor}=="0957", MODE="0666", GROUP="plugdev"
SUBSYSTEM=="usb", ATTR{idVendor}=="0699", MODE="0666", GROUP="plugdev"
SUBSYSTEM=="usb", ATTR{idVendor}=="0aad", MODE="0666", GROUP="plugdev"
SUBSYSTEM=="usb", ATTR{idVendor}=="3923", MODE="0666", GROUP="plugdev"
SUBSYSTEM=="usb", ATTR{idVendor}=="1ab1", MODE="0666", GROUP="plugdev"
SUBSYSTEM=="usb", ATTR{idVendor}=="f4ec", MODE="0666", GROUP="plugdev"
SUBSYSTEM=="usb_device", ENV{DEVTYPE}=="usb_interface", ENV{MODALIAS}=="usb:v*fe*", MODE="0666", GROUP="plugdev"
`
}

// checkUdevRulesInstalledAt is a testable variant that checks an arbitrary path.
func checkUdevRulesInstalledAt(path string) CheckResult {
	_, err := os.Stat(path)
	if err != nil {
		return CheckResult{
			Name:    "udev_rules_installed",
			Status:  "fail",
			Message: "udev rules file not found: " + path + " (run the installer to create it)",
		}
	}

	data, err := os.ReadFile(path)
	if err != nil {
		return CheckResult{
			Name:    "udev_rules_installed",
			Status:  "warn",
			Message: "udev rules file exists but cannot be read: " + err.Error(),
		}
	}

	contents := string(data)
	var missing []string
	for _, vid := range expectedVendorIDs {
		if !strings.Contains(contents, vid) {
			missing = append(missing, vid)
		}
	}

	if len(missing) > 0 {
		return CheckResult{
			Name:    "udev_rules_installed",
			Status:  "warn",
			Message: "udev rules file exists but is missing vendor IDs: " + strings.Join(missing, ", ") + " (re-run installer to update)",
		}
	}

	return CheckResult{
		Name:    "udev_rules_installed",
		Status:  "pass",
		Message: "udev rules file OK: " + path + " (all vendor IDs present)",
	}
}
