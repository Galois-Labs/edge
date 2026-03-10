//go:build darwin

// Package service provides platform-specific service lifecycle management.
// On macOS (darwin), this is a development-only stub. Production deployments
// target Linux (systemd) and Windows (SCM). macOS is used for local
// development where the daemon is run directly from the terminal.
package service

import "fmt"

// ServiceName is the identifier used across all platforms.
const ServiceName = "galois-edge"

// InstallService is a no-op on macOS. Use launchd directly if needed for
// development.
func InstallService(exePath, configPath, user string) error {
	return fmt.Errorf("service install not supported on macOS (development only); run the daemon directly or use launchd")
}

// UninstallService is a no-op on macOS.
func UninstallService() error {
	return fmt.Errorf("service uninstall not supported on macOS (development only)")
}

// StartService is a no-op on macOS.
func StartService() error {
	return fmt.Errorf("service start not supported on macOS; run the daemon directly")
}

// StopService is a no-op on macOS.
func StopService() error {
	return fmt.Errorf("service stop not supported on macOS; send SIGTERM to the process")
}

// ServiceStatus always returns "unknown" on macOS.
func ServiceStatus() (string, error) {
	return "unknown", fmt.Errorf("service status not supported on macOS")
}

// RunAsService is not applicable on macOS.
func RunAsService(_ func() error, _ func()) error {
	return fmt.Errorf("RunAsService not supported on macOS")
}

// IsWindowsService always returns false on macOS.
func IsWindowsService() bool { return false }
