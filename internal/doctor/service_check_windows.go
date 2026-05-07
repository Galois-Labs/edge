//go:build windows

package doctor

import (
	"fmt"

	"golang.org/x/sys/windows/svc/mgr"
)

// checkServiceUnitInstalled verifies that the galois-edge Windows service is
// registered with the SCM and configured for automatic start.
func checkServiceUnitInstalled() CheckResult {
	m, err := mgr.Connect()
	if err != nil {
		return CheckResult{
			Name:    "service_unit_installed",
			Status:  "warn",
			Message: fmt.Sprintf("cannot connect to Windows SCM: %v", err),
		}
	}
	defer m.Disconnect()

	s, err := m.OpenService("galois-edge")
	if err != nil {
		return CheckResult{
			Name:    "service_unit_installed",
			Status:  "fail",
			Message: "galois-edge Windows service is not registered (run 'galois-edge install' as Administrator)",
		}
	}
	defer s.Close()

	cfg, err := s.Config()
	if err != nil {
		return CheckResult{
			Name:    "service_unit_installed",
			Status:  "warn",
			Message: fmt.Sprintf("galois-edge service is registered but config could not be read: %v", err),
		}
	}

	switch cfg.StartType {
	case mgr.StartAutomatic:
		return CheckResult{
			Name:    "service_unit_installed",
			Status:  "pass",
			Message: "galois-edge Windows service is registered and set to automatic start",
		}
	case mgr.StartManual:
		return CheckResult{
			Name:    "service_unit_installed",
			Status:  "warn",
			Message: "galois-edge Windows service is registered but set to manual start (change to Automatic in services.msc)",
		}
	case mgr.StartDisabled:
		return CheckResult{
			Name:    "service_unit_installed",
			Status:  "warn",
			Message: "galois-edge Windows service is registered but disabled (enable it in services.msc)",
		}
	default:
		return CheckResult{
			Name:    "service_unit_installed",
			Status:  "warn",
			Message: fmt.Sprintf("galois-edge Windows service has unexpected start type: %d", cfg.StartType),
		}
	}
}
