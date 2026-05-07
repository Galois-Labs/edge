//go:build !linux

package doctor

// checkUdevRulesInstalled is a no-op pass on non-Linux platforms where udev
// is not used.
func checkUdevRulesInstalled() CheckResult {
	return CheckResult{
		Name:    "udev_rules_installed",
		Status:  "pass",
		Message: "udev rules not applicable on this platform (Linux only)",
	}
}
