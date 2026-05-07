//go:build linux

package doctor

import (
	"fmt"
	"os"
	"os/exec"
	"strings"
)

const systemdUnitPath = "/etc/systemd/system/galois-edge.service"

// checkServiceUnitInstalled verifies that the galois-edge systemd service unit
// is installed and enabled.
func checkServiceUnitInstalled() CheckResult {
	_, err := os.Stat(systemdUnitPath)
	if err != nil {
		return CheckResult{
			Name:    "service_unit_installed",
			Status:  "fail",
			Message: fmt.Sprintf("systemd unit file not found: %s (run 'galois-edge install' to create it)", systemdUnitPath),
		}
	}

	out, err := exec.Command("systemctl", "is-enabled", "galois-edge").Output()
	state := strings.TrimSpace(string(out))

	if err != nil {
		// systemctl is-enabled exits non-zero for disabled/masked units too;
		// we still want to report the state if we got output.
		if state == "" {
			state = "unknown"
		}
	}

	switch state {
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
			Message: fmt.Sprintf("systemd unit galois-edge.service is installed but %s (run 'systemctl enable galois-edge' to enable autostart)", state),
		}
	default:
		return CheckResult{
			Name:    "service_unit_installed",
			Status:  "warn",
			Message: fmt.Sprintf("systemd unit galois-edge.service is installed but is-enabled returned: %q", state),
		}
	}
}
