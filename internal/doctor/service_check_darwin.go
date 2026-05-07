//go:build darwin

package doctor

// checkServiceUnitInstalled always passes on macOS, which is used as a
// development platform only and has no galois-edge service install path.
func checkServiceUnitInstalled() CheckResult {
	return CheckResult{
		Name:    "service_unit_installed",
		Status:  "pass",
		Message: "service install not applicable on macOS (development platform)",
	}
}
