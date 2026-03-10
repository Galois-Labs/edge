//go:build !linux && !windows && !darwin

// Package service provides platform-specific service lifecycle management.
// This file is a stub for unsupported platforms (FreeBSD, OpenBSD, etc.).
// All operations return an error directing the user to a supported OS.
package service

import (
	"fmt"
	"runtime"
)

// ServiceName is the identifier used across all platforms.
const ServiceName = "galois-edge"

func unsupported(op string) error {
	return fmt.Errorf("%s: platform %s/%s is not supported; use Linux (systemd) or Windows (SCM)",
		op, runtime.GOOS, runtime.GOARCH)
}

// InstallService returns an error on unsupported platforms.
func InstallService(exePath, configPath, user string) error {
	return unsupported("install")
}

// UninstallService returns an error on unsupported platforms.
func UninstallService() error {
	return unsupported("uninstall")
}

// StartService returns an error on unsupported platforms.
func StartService() error {
	return unsupported("start")
}

// StopService returns an error on unsupported platforms.
func StopService() error {
	return unsupported("stop")
}

// ServiceStatus returns an error on unsupported platforms.
func ServiceStatus() (string, error) {
	return "", unsupported("status")
}

// RunAsService returns an error on unsupported platforms.
func RunAsService(_ func() error, _ func()) error {
	return unsupported("run-as-service")
}

// IsWindowsService always returns false on unsupported platforms.
func IsWindowsService() bool { return false }
