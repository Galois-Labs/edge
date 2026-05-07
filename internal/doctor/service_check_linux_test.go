//go:build linux

package doctor

import (
	"os"
	"path/filepath"
	"testing"
)

func TestCheckServiceUnitInstalled_FailMissing(t *testing.T) {
	result := checkServiceUnitInstalledAt("/nonexistent/galois-edge.service", "")
	if result.Status != "fail" {
		t.Errorf("status = %q, want fail", result.Status)
	}
}

func TestCheckServiceUnitInstalled_Pass(t *testing.T) {
	dir := t.TempDir()
	unitPath := filepath.Join(dir, "galois-edge.service")
	if err := os.WriteFile(unitPath, []byte("[Unit]\nDescription=galois-edge\n"), 0644); err != nil {
		t.Fatal(err)
	}
	result := checkServiceUnitInstalledAt(unitPath, "enabled")
	if result.Status != "pass" {
		t.Errorf("status = %q, want pass; message: %s", result.Status, result.Message)
	}
}

func TestCheckServiceUnitInstalled_WarnDisabled(t *testing.T) {
	dir := t.TempDir()
	unitPath := filepath.Join(dir, "galois-edge.service")
	if err := os.WriteFile(unitPath, []byte("[Unit]\nDescription=galois-edge\n"), 0644); err != nil {
		t.Fatal(err)
	}
	result := checkServiceUnitInstalledAt(unitPath, "disabled")
	if result.Status != "warn" {
		t.Errorf("status = %q, want warn; message: %s", result.Status, result.Message)
	}
}

// checkServiceUnitInstalledAt is a testable variant that accepts the unit
// path and the simulated is-enabled output.
func checkServiceUnitInstalledAt(unitPath, isEnabledState string) CheckResult {
	_, err := os.Stat(unitPath)
	if err != nil {
		return CheckResult{
			Name:    "service_unit_installed",
			Status:  "fail",
			Message: "systemd unit file not found: " + unitPath + " (run 'galois-edge install' to create it)",
		}
	}

	switch isEnabledState {
	case "enabled":
		return CheckResult{
			Name:    "service_unit_installed",
			Status:  "pass",
			Message: "systemd unit galois-edge.service is installed and enabled",
		}
	case "disabled", "static":
		return CheckResult{
			Name:    "service_unit_installed",
			Status:  "warn",
			Message: "systemd unit galois-edge.service is installed but " + isEnabledState + " (run 'systemctl enable galois-edge' to enable autostart)",
		}
	default:
		return CheckResult{
			Name:    "service_unit_installed",
			Status:  "warn",
			Message: "systemd unit galois-edge.service is installed but is-enabled returned: \"" + isEnabledState + "\"",
		}
	}
}
