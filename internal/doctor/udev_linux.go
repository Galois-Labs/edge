//go:build linux

package doctor

import (
	"fmt"
	"os"
	"strings"
)

const udevRulesPath = "/etc/udev/rules.d/99-galois-edge.rules"

// expectedVendorIDs are the seven vendor-ID substrings that must all appear in
// the udev rules file for it to be considered complete.
var expectedVendorIDs = []string{"0957", "0699", "0aad", "3923", "1ab1", "f4ec", "fe"}

// checkUdevRulesInstalled verifies that the galois-edge udev rules file exists
// and contains all expected vendor ID strings.
func checkUdevRulesInstalled() CheckResult {
	_, err := os.Stat(udevRulesPath)
	if err != nil {
		return CheckResult{
			Name:    "udev_rules_installed",
			Status:  "fail",
			Message: fmt.Sprintf("udev rules file not found: %s (run the installer to create it)", udevRulesPath),
		}
	}

	data, err := os.ReadFile(udevRulesPath)
	if err != nil {
		return CheckResult{
			Name:    "udev_rules_installed",
			Status:  "warn",
			Message: fmt.Sprintf("udev rules file exists but cannot be read: %v", err),
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
			Message: fmt.Sprintf("udev rules file exists but is missing vendor IDs: %s (re-run installer to update)", strings.Join(missing, ", ")),
		}
	}

	return CheckResult{
		Name:    "udev_rules_installed",
		Status:  "pass",
		Message: fmt.Sprintf("udev rules file OK: %s (all vendor IDs present)", udevRulesPath),
	}
}
